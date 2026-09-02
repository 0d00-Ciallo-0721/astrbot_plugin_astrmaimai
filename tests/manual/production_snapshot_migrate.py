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

from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.memory.services.v2_store import MemoryV2Store

from production_snapshot_archive import apply_archive_plan, build_archive_plan, rollback_archive_plan, CLONE_MARKER


LATEST_MAIN_SCHEMA = 128
REQUIRED_V2_TABLES = {
    "memory_projection_outbox",
    "vector_index_delete_repairs",
    "vector_resource_descriptors",
    "memory_consistency_repairs",
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


def _main_db_summary(path: Path) -> dict[str, Any]:
    db = sqlite3.connect(str(path))
    try:
        before = int(db.execute("PRAGMA user_version").fetchone()[0])
        _run_migrations(db)
        db.commit()
        after = int(db.execute("PRAGMA user_version").fetchone()[0])
        integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [list(row) for row in db.execute("PRAGMA foreign_key_check").fetchall()]
        return {"before_user_version": before, "user_version": after, "integrity_check": integrity, "foreign_key_check": foreign_keys}
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
    before_manifest = _file_manifest(root)
    archive_plan = build_archive_plan(root)
    archive = root / "archive" / "pre_migration"
    archive.mkdir(parents=True, exist_ok=True)
    before_manifest_path = archive / "migration_before_manifest.json"
    before_manifest_path.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "files": before_manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    main_db = root / "astrmai.db"
    if not main_db.is_file():
        raise ValueError(f"missing clone main database: {main_db}")
    backup = archive / "astrmai.db.before_migration"
    if not backup.exists():
        shutil.copy2(main_db, backup)
    v2_db = root / "memory" / "memory_v2.db"
    v2_existed = v2_db.exists()
    v2_backup = archive / "memory_v2.db.before_migration"
    if v2_existed and not v2_backup.exists():
        shutil.copy2(v2_db, v2_backup)
    archive_result = {**archive_plan, "moved": []}
    try:
        if archive_legacy:
            archive_result = apply_archive_plan(archive_plan)
        post_archive = _file_manifest(root)
        (archive / "migration_post_archive_manifest.json").write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "files": post_archive}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        main_summary = _main_db_summary(main_db)
        v2_summary = asyncio.run(_initialize_v2(v2_db, root / "memory"))
        after_manifest = _file_manifest(root)
        after_manifest_path = archive / "migration_after_manifest.json"
        after_manifest_path.write_text(json.dumps({"created_at": datetime.now(timezone.utc).isoformat(), "files": after_manifest}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        shutil.copy2(backup, main_db)
        if v2_backup.exists():
            shutil.copy2(v2_backup, v2_db)
        elif not v2_existed and v2_db.exists():
            v2_db.unlink()
        try:
            rollback_archive_plan(archive_result)
        except Exception as rollback_exc:
            raise RuntimeError(f"clone migration failed and rollback failed: {rollback_exc}") from rollback_exc
        raise
    return {
        "root": str(root),
        "canonical_root": str(root),
        "migration_status": "completed",
        "rollback_status": "available_from_archive",
        "main": main_summary,
        "memory_v2": v2_summary,
        "archive": archive_result,
        "manifests": {
            "before": str(before_manifest_path),
            "post_archive": str(archive / "migration_post_archive_manifest.json"),
            "after": str(archive / "migration_after_manifest.json"),
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
        print(
            json.dumps(
                {
                    "canonical_root": str(args.clone_root.resolve()),
                    "migration_status": "failed",
                    "rollback_status": "rolled_back_or_clone_invalid",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
