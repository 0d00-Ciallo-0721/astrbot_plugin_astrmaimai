"""Clone-only production snapshot migration runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Direct execution places ``tests/manual`` first on sys.path.  Add the
# repository root so the runner works both as a script and as a module.
REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_DIR = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(MANUAL_DIR) not in sys.path:
    sys.path.insert(0, str(MANUAL_DIR))

from astrmai.infrastructure.persistence.architecture_migration_audit import (
    LATEST_ARCHITECTURE_SCHEMA_VERSION,
)
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.memory.services.v2_store import MemoryV2Store

from production_snapshot_archive import (
    ArchiveApplyError,
    CLONE_MARKER,
    apply_archive_plan,
    build_archive_plan,
    rollback_archive_plan,
)


LATEST_MAIN_SCHEMA = LATEST_ARCHITECTURE_SCHEMA_VERSION
REQUIRED_V2_TABLES = {
    "memory_projection_outbox",
    "vector_index_delete_repairs",
    "vector_resource_descriptors",
    "memory_consistency_repairs",
}
BACKUP_RETENTION_POLICY = {
    "automatic_cleanup": False,
    "policy": "retain_until_explicit_prune",
    "reason": "preserve an auditable rollback point for every clone migration run",
}


def _assert_clone(root: Path) -> Path:
    resolved = root.resolve()
    normalized = str(resolved).lower().replace("/", "\\")
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError(f"clone must be inside the system temporary directory: {resolved}") from exc
    if resolved == temp_root or "prod_snapshot" in normalized or ".git" in resolved.parts:
        raise ValueError(f"refusing production-looking path: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"clone directory does not exist: {resolved}")
    if not (resolved / CLONE_MARKER).is_file():
        raise ValueError(f"clone marker is required: {resolved / CLONE_MARKER}")
    return resolved


def _file_manifest(root: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        rows.append({"path": str(path.relative_to(root)), "size": path.stat().st_size, "sha256": digest.hexdigest()})
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_run_manifest(
    path: Path,
    *,
    run_id: str,
    phase: str,
    files: list[dict[str, Any]],
    migration_status: str,
    rollback_status: str,
    error_type: str = "",
    error: str = "",
    rollback_error: str = "",
    failed_stage: str = "",
    archive_partial_result: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "phase": phase,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "migration_status": migration_status,
        "rollback_status": rollback_status,
        "files": files,
    }
    if error_type:
        payload["error_type"] = str(error_type)[:120]
    if error:
        payload["error"] = str(error)[:2000]
    if rollback_error:
        payload["rollback_error"] = str(rollback_error)[:2000]
    if failed_stage:
        payload["failed_stage"] = str(failed_stage)[:120]
    if archive_partial_result is not None:
        payload["archive_partial_result"] = archive_partial_result
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


def _annotate_failure(
    error: BaseException,
    *,
    run_id: str | None,
    rollback_status: str,
    failed_stage: str,
    failed_manifest: Path | None,
    error_type: str | None = None,
    error_text: str | None = None,
    rollback_error: str | None = None,
    archive_partial_result: dict[str, Any] | None = None,
) -> BaseException:
    """Attach machine-readable run context without replacing the original exception."""
    setattr(error, "run_id", run_id)
    setattr(error, "rollback_status", rollback_status)
    setattr(error, "failed_stage", failed_stage)
    setattr(
        error,
        "failed_manifest",
        str(failed_manifest) if failed_manifest and failed_manifest.is_file() else None,
    )
    setattr(error, "error_type", str(error_type or type(error).__name__)[:120])
    setattr(
        error,
        "error",
        str(error_text if error_text is not None else error)[:2000],
    )
    setattr(
        error,
        "rollback_error",
        str(rollback_error)[:2000] if rollback_error is not None else None,
    )
    setattr(error, "archive_partial_result", archive_partial_result)
    return error


def _main_db_summary(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(str(path))
    try:
        before = int(db.execute("PRAGMA user_version").fetchone()[0])
        _run_migrations(db)
        db.commit()
        after = int(db.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in db.execute("PRAGMA foreign_key_check").fetchall()]
        tables = sorted(
            str(row[0])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
        return {
            "before_user_version": before,
            "user_version": after,
            "integrity_check": integrity,
            "foreign_key_check": foreign_keys,
            "tables": tables,
        }
    finally:
        db.close()


async def _initialize_v2(db_path: Path, data_path: Path) -> dict[str, Any]:
    store = MemoryV2Store(str(db_path), data_path=data_path)
    await store.initialize()
    db = sqlite3.connect(str(db_path))
    try:
        version_row = db.execute("SELECT value FROM memory_v2_meta WHERE key='schema_version'").fetchone()
        tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        active = int(db.execute("SELECT COUNT(*) FROM canonical_memories WHERE status='active'").fetchone()[0])
        fts = int(db.execute("SELECT COUNT(*) FROM canonical_fts").fetchone()[0])
        return {"schema_version": int(version_row[0]) if version_row else None, "tables": sorted(REQUIRED_V2_TABLES & tables), "required_tables_present": REQUIRED_V2_TABLES <= tables, "active": active, "fts": fts}
    finally:
        db.close()


def migrate_clone(root: Path, *, archive_legacy: bool = True) -> dict[str, Any]:
    root = _assert_clone(root)
    archive = root / "archive" / "pre_migration"
    archive.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:16]
    manifest_paths = {
        "before": archive / f"migration_{run_id}_before_manifest.json",
        "post_archive": archive / f"migration_{run_id}_post_archive_manifest.json",
        "after": archive / f"migration_{run_id}_after_manifest.json",
        "failed": archive / f"migration_{run_id}_failed_manifest.json",
    }
    before_manifest: list[dict[str, Any]] = []
    archive_plan: dict[str, Any] = {"root": str(root), "archive": str(archive), "items": []}
    archive_result = {**archive_plan, "moved": []}
    main_db = root / "astrmai.db"
    v2_db = root / "memory" / "memory_v2.db"
    v2_existed = v2_db.exists()
    backups_ready = False
    backup = archive / f"astrmai.db.before_migration.{run_id}"
    v2_backup = archive / f"memory_v2.db.before_migration.{run_id}"
    try:
        before_manifest = _file_manifest(root)
        archive_plan = build_archive_plan(root)
        archive_result = {**archive_plan, "moved": []}
        _write_run_manifest(
            manifest_paths["before"],
            run_id=run_id,
            phase="before",
            files=before_manifest,
            migration_status="started",
            rollback_status="backup_pending",
        )
        if not main_db.is_file():
            raise ValueError(f"missing clone main database: {main_db}")
        # Every invocation snapshots the state it is about to mutate.  Never
        # reuse a prior run's backup: a later failure must restore this run's
        # starting point, not an older pre-migration database.
        shutil.copy2(main_db, backup)
        if v2_existed:
            shutil.copy2(v2_db, v2_backup)
        if _file_sha256(main_db) != _file_sha256(backup):
            raise OSError(f"main database backup hash mismatch: {backup}")
        if v2_existed and _file_sha256(v2_db) != _file_sha256(v2_backup):
            raise OSError(f"memory v2 database backup hash mismatch: {v2_backup}")
        backups_ready = True
        _write_run_manifest(
            manifest_paths["before"],
            run_id=run_id,
            phase="before",
            files=before_manifest,
            migration_status="started",
            rollback_status="available_from_run_backup",
        )
    except Exception as initialization_exc:
        try:
            _write_run_manifest(
                manifest_paths["failed"],
                run_id=run_id,
                phase="failed",
                files=_file_manifest(root),
                migration_status="failed",
                rollback_status=(
                    "available_from_run_backup" if backups_ready else "not_started"
                ),
                error_type=type(initialization_exc).__name__,
                error=str(initialization_exc),
                failed_stage="initialization",
            )
        except Exception:
            pass
        _annotate_failure(
            initialization_exc,
            run_id=run_id,
            rollback_status="available_from_run_backup" if backups_ready else "not_started",
            failed_stage="initialization",
            failed_manifest=manifest_paths["failed"],
        )
        raise
    try:
        if archive_legacy:
            archive_result = apply_archive_plan(archive_plan)
        post_archive = _file_manifest(root)
        _write_run_manifest(
            manifest_paths["post_archive"],
            run_id=run_id,
            phase="post_archive",
            files=post_archive,
            migration_status="in_progress",
            rollback_status="available_from_run_backup",
        )
        main_summary = _main_db_summary(main_db)
        v2_summary = asyncio.run(_initialize_v2(v2_db, root / "memory"))
        after_manifest = _file_manifest(root)
        _write_run_manifest(
            manifest_paths["after"],
            run_id=run_id,
            phase="after",
            files=after_manifest,
            migration_status="completed",
            rollback_status="available_from_run_backup",
        )
    except Exception as migration_exc:
        if isinstance(migration_exc, ArchiveApplyError):
            try:
                _write_run_manifest(
                    manifest_paths["failed"],
                    run_id=run_id,
                    phase="failed",
                    files=_file_manifest(root),
                    migration_status="failed",
                    rollback_status="rollback_failed",
                    error_type=type(migration_exc.original_error).__name__,
                    error=str(migration_exc.original_error),
                    rollback_error=str(migration_exc.rollback_error),
                    failed_stage="archive_rollback",
                    archive_partial_result=migration_exc.partial_result,
                )
            except Exception:
                pass
            failure = RuntimeError(
                f"archive rollback failed after partial apply: {migration_exc}"
            )
            _annotate_failure(
                failure,
                run_id=run_id,
                rollback_status="rollback_failed",
                failed_stage="archive_rollback",
                failed_manifest=manifest_paths["failed"],
                error_type=type(migration_exc.original_error).__name__,
                error_text=str(migration_exc.original_error),
                rollback_error=str(migration_exc.rollback_error),
                archive_partial_result=migration_exc.partial_result,
            )
            raise failure from migration_exc
        rollback_status = "rolled_back"
        try:
            shutil.copy2(backup, main_db)
            if v2_backup.exists():
                shutil.copy2(v2_backup, v2_db)
            elif not v2_existed and v2_db.exists():
                v2_db.unlink()
            rollback_archive_plan(archive_result)
        except Exception as rollback_exc:
            rollback_status = "rollback_failed"
            try:
                _write_run_manifest(
                    manifest_paths["failed"],
                    run_id=run_id,
                    phase="failed",
                    files=_file_manifest(root),
                    migration_status="failed",
                    rollback_status=rollback_status,
                    error_type=type(migration_exc).__name__,
                    error=str(migration_exc),
                    rollback_error=str(rollback_exc),
                    failed_stage="rollback",
                )
            except Exception:
                pass
            failure = RuntimeError(
                f"clone migration failed ({type(migration_exc).__name__}: {migration_exc}) "
                f"and rollback failed ({type(rollback_exc).__name__}: {rollback_exc})"
            )
            _annotate_failure(
                failure,
                run_id=run_id,
                rollback_status=rollback_status,
                failed_stage="rollback",
                failed_manifest=manifest_paths["failed"],
                error_type=type(migration_exc).__name__,
                error_text=str(migration_exc),
                rollback_error=str(rollback_exc),
            )
            raise failure from rollback_exc
        try:
            _write_run_manifest(
                manifest_paths["failed"],
                run_id=run_id,
                phase="failed",
                files=_file_manifest(root),
                migration_status="failed",
                rollback_status=rollback_status,
                error_type=type(migration_exc).__name__,
                error=str(migration_exc),
                failed_stage="migration",
            )
        except Exception:
            pass
        _annotate_failure(
            migration_exc,
            run_id=run_id,
            rollback_status=rollback_status,
            failed_stage="migration",
            failed_manifest=manifest_paths["failed"],
        )
        raise
    return {
        "root": str(root),
        "canonical_root": str(root),
        "migration_status": "completed",
        "rollback_status": "available_from_run_backup",
        "run_id": run_id,
        "backup_retention": dict(BACKUP_RETENTION_POLICY),
        "main": main_summary,
        "memory_v2": v2_summary,
        "archive": archive_result,
        "backup_files": {
            "main": str(backup),
            "memory_v2": str(v2_backup) if v2_existed else None,
        },
        "manifests": {
            "run_id": run_id,
            "before": str(manifest_paths["before"]),
            "post_archive": str(manifest_paths["post_archive"]),
            "after": str(manifest_paths["after"]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("clone_root", type=Path)
    parser.add_argument("--no-archive", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate_clone(args.clone_root, archive_legacy=not args.no_archive)
    except Exception as exc:
        rollback_status = getattr(exc, "rollback_status", "not_available")
        failed_stage = getattr(exc, "failed_stage", "pre_report")
        run_id = getattr(exc, "run_id", None)
        failed_manifest = getattr(exc, "failed_manifest", None)
        error_type = str(getattr(exc, "error_type", type(exc).__name__))[:120]
        error_text = str(getattr(exc, "error", str(exc)))[:2000]
        rollback_error = getattr(exc, "rollback_error", None)
        if rollback_error is not None:
            rollback_error = str(rollback_error)[:2000]
        archive_partial_result = getattr(exc, "archive_partial_result", None)
        payload = {
            "canonical_root": str(args.clone_root.resolve()),
            "migration_status": "failed",
            "rollback_status": rollback_status,
            "failed_stage": failed_stage,
            "run_id": run_id,
            "failed_manifest": failed_manifest,
            "error_type": error_type,
            "error": error_text,
            "rollback_error": rollback_error,
        }
        if archive_partial_result is not None:
            payload["archive_partial_result"] = archive_partial_result
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
