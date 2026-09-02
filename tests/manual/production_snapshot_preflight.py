"""Read-only compatibility preflight for an AstrMai production data snapshot.

The command never migrates, rewrites, deletes, or moves files.  It is intended
to be run against a copied snapshot before a release candidate is started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import shutil
import tempfile
import time
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrmai.memory.services.vector_migration_decision import decide_vector_migration


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_report(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return report
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    db: sqlite3.Connection | None = None
    try:
        # query_only prevents accidental writes.  Always inspect a temporary
        # copy so source-side locks/WAL metadata cannot affect the audit.
        temp_dir = tempfile.TemporaryDirectory()
        copied = Path(temp_dir.name) / path.name
        shutil.copy2(path, copied)
        db = sqlite3.connect(str(copied))
        with db:
            db.execute("PRAGMA query_only=ON")
            try:
                report["integrity_check"] = db.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.Error as exc:
                report["integrity_check"] = None
                report["integrity_check_error"] = f"{type(exc).__name__}: {exc}"
            try:
                report["foreign_key_check"] = [
                    list(row) for row in db.execute("PRAGMA foreign_key_check").fetchall()
                ]
            except sqlite3.Error as exc:
                report["foreign_key_check"] = None
                report["foreign_key_check_error"] = f"{type(exc).__name__}: {exc}"
            report["user_version"] = int(db.execute("PRAGMA user_version").fetchone()[0])
            tables = [
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            report["tables"] = sorted(tables)
            report["row_counts"] = {}
            report["table_errors"] = {}
            for table in tables:
                safe_table = '"' + table.replace('"', '""') + '"'
                try:
                    report["row_counts"][table] = int(
                        db.execute(f"SELECT COUNT(*) FROM {safe_table}").fetchone()[0]
                    )
                except sqlite3.Error as exc:
                    report["row_counts"][table] = None
                    report["table_errors"][table] = f"{type(exc).__name__}: {exc}"
            if "canonical_memories" in tables:
                report["canonical_status"] = {
                    str(status): int(count)
                    for status, count in db.execute(
                        "SELECT status, COUNT(*) FROM canonical_memories GROUP BY status"
                    ).fetchall()
                }
                if "canonical_fts" in tables:
                    report["canonical_projection"] = {
                        "active": int(
                            db.execute(
                                "SELECT COUNT(*) FROM canonical_memories WHERE status = 'active'"
                            ).fetchone()[0]
                        ),
                        "fts": int(db.execute("SELECT COUNT(*) FROM canonical_fts").fetchone()[0]),
                    }
            if "documents" in tables:
                report["documents_projection"] = {
                    "canonical": int(
                        db.execute(
                            "SELECT COUNT(*) FROM documents "
                            "WHERE json_extract(metadata, '$.canonical_id') IS NOT NULL"
                        ).fetchone()[0]
                    ),
                    "legacy": int(
                        db.execute(
                            "SELECT COUNT(*) FROM documents "
                            "WHERE json_extract(metadata, '$.canonical_id') IS NULL"
                        ).fetchone()[0]
                    ),
                }
    except (OSError, sqlite3.Error) as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if db is not None:
            db.close()
        if temp_dir is not None:
            temp_dir.cleanup()
    return report


def _faiss_report(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {"path": str(path), "exists": path.is_file()}
    if not path.is_file():
        return report
    try:
        import faiss  # type: ignore

        index = faiss.read_index(str(path))
        report.update(
            {
                "readable": True,
                "type": type(index).__name__,
                "dimension": int(getattr(index, "d")),
                "ntotal": int(getattr(index, "ntotal")),
                "metric": int(getattr(index, "metric_type", -1)),
            }
        )
        if hasattr(index, "id_map"):
            report["id_count"] = int(index.id_map.size())
    except Exception as exc:
        report["readable"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def _dialogue_report(root: Path) -> dict[str, Any]:
    path = root / "cache" / "dialogue_store_state.json"
    result: dict[str, Any] = {"path": str(path.relative_to(root)), "exists": path.is_file()}
    if not path.is_file():
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pending = payload.get("pending_direct") or {}
        entries = [item for values in pending.values() if isinstance(values, list) for item in values if isinstance(item, dict)]
        now = time.time()
        ttl = 1200.0
        result.update(
            {
                "schema_version": payload.get("schema_version"),
                "writer_generation": payload.get("writer_generation"),
                "pending_direct_total": len(entries),
                "pending_direct_active": sum(1 for item in entries if str(item.get("status") or "pending") == "pending"),
                "pending_direct_expired": sum(1 for item in entries if now - float(item.get("created_at") or now) > ttl),
            }
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_report(
    root: Path,
    *,
    apply_to_clone: bool = False,
    expected_model: str | None = None,
    expected_provider_source: str | None = None,
    expected_api_base_fingerprint: str | None = None,
    expected_dimension: int | None = None,
    provider_available: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    files = sorted(path for path in root.rglob("*") if path.is_file()) if root.is_dir() else []
    report: dict[str, Any] = {
        "root": str(root),
        "mode": "apply_to_clone_requested" if apply_to_clone else "read_only",
        "file_count": len(files),
        "total_bytes": sum(path.stat().st_size for path in files),
        "files": [
            {
                "path": str(path.relative_to(root)),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ],
        "sqlite": [],
        "faiss": [],
        "manifest": None,
        "unknown_indexes": [],
        "cache": [],
        "blocking_reasons": [],
        "warnings": [],
    }

    for path in files:
        if path.suffix.lower() == ".db":
            report["sqlite"].append(_sqlite_report(path))
        elif path.suffix.lower() == ".index":
            report["faiss"].append(_faiss_report(path))
    manifest_path = root / "memory" / "vector_index_manifest.json"
    if manifest_path.is_file():
        try:
            report["manifest"] = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["blocking_reasons"].append(f"manifest_unreadable:{type(exc).__name__}")

    active_name = str((report["manifest"] or {}).get("file_name") or "").strip()
    for path in sorted((root / "memory").glob("*.index")):
        if path.name != active_name:
            report["unknown_indexes"].append(str(path.relative_to(root)))
            report["blocking_reasons"].append(f"unknown_index:{path.name}")
    for path in files:
        if "cache" in path.parts:
            item: dict[str, Any] = {
                "path": str(path.relative_to(root)),
                "size": path.stat().st_size,
            }
            try:
                if path.suffix.lower() == ".json":
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        item["schema_version"] = payload.get(
                            "schema_version",
                            payload.get("version", payload.get("trace_schema_version")),
                        )
                elif path.suffix.lower() == ".jsonl":
                    with path.open("r", encoding="utf-8") as handle:
                        for line in handle:
                            if line.strip():
                                payload = json.loads(line)
                                if isinstance(payload, dict):
                                    item["schema_version"] = payload.get(
                                        "schema_version", payload.get("trace_schema_version")
                                    )
                                break
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                item["parse_error"] = f"{type(exc).__name__}: {exc}"
            report["cache"].append(item)

    for item in report["sqlite"]:
        if item.get("integrity_check") not in (None, "ok"):
            report["blocking_reasons"].append(f"sqlite_integrity:{item['path']}")
        if item.get("path", "").endswith("astrmai.db") and "backups" not in item.get("path", ""):
            if int(item.get("user_version", 0) or 0) < 128:
                report["blocking_reasons"].append("main_schema_requires_migration:95_to_128")
    if report.get("manifest"):
        manifest = report["manifest"]
        if not manifest.get("api_base_fingerprint"):
            report["blocking_reasons"].append("embedding_identity_unknown:empty_api_base_fingerprint")
        if manifest.get("dimension") in (None, ""):
            report["blocking_reasons"].append("manifest_dimension_unknown")
    report["risk_level"] = "blocked" if report["blocking_reasons"] else "review_required"
    live_main = root / "astrmai.db"
    live_v2 = root / "memory" / "memory_v2.db"
    main_report = next((item for item in report["sqlite"] if Path(str(item.get("path", ""))).resolve() == live_main), None)
    v2_report = next((item for item in report["sqlite"] if Path(str(item.get("path", ""))).resolve() == live_v2), None)
    report["dialogue"] = _dialogue_report(root)
    report["preflight_status"] = "blocked" if report["blocking_reasons"] else "ready_for_review"
    report["database_summary"] = {
        "main": main_report,
        "memory_v2": v2_report,
    }
    report["vector_summary"] = {
        "indexes": report["faiss"],
        "manifest": report["manifest"],
        "unknown_indexes": report["unknown_indexes"],
        "identity_status": "unknown" if any("embedding_identity_unknown" in item for item in report["blocking_reasons"]) else "measured",
    }
    active_index = next((item for item in report["faiss"] if item.get("path", "").endswith(active_name)), {})
    manifest = report["manifest"] or {}
    configured_models = manifest.get("embedding_models")
    configured_model = configured_models[0] if isinstance(configured_models, list) and configured_models else manifest.get("embedding_model")
    decision = decide_vector_migration(
        manifest,
        expected_model=str(expected_model if expected_model is not None else configured_model or ""),
        expected_provider_source=str(expected_provider_source if expected_provider_source is not None else manifest.get("provider_source_id") or ""),
        expected_api_base_fingerprint=str(expected_api_base_fingerprint if expected_api_base_fingerprint is not None else manifest.get("api_base_fingerprint") or ""),
        expected_dimension=expected_dimension if expected_dimension is not None else (int(manifest["dimension"]) if str(manifest.get("dimension") or "").isdigit() else None),
        physical_readable=bool(active_index.get("readable", False)),
        physical_dimension=int(active_index["dimension"]) if active_index.get("dimension") is not None else None,
        provider_available=provider_available,
    )
    report["vector_summary"]["migration_decision"] = {"action": decision.action, "reasons": list(decision.reasons)}
    report["cache_summary"] = {
        "files": report["cache"],
        "dialogue": report["dialogue"],
        "sensitive_or_probe_files": [item["path"] for item in report["cache"] if str(item["path"]).endswith(("gemini_probe.py", ".key", ".pem"))],
    }
    report["migration_plan"] = {
        "main_database": "v95_to_v128" if any("main_schema_requires_migration" in item for item in report["blocking_reasons"]) else "no_op",
        "memory_v2": "initialize_or_verify_schema_v2",
        "vector_identity": "rebuild_new_generation_or_lexical_fallback" if report["vector_summary"]["identity_status"] == "unknown" else "verify_and_decide",
    }
    report["rollback_requirements"] = {
        "preserve_source_snapshot": True,
        "preserve_legacy_indexes": True,
        "preserve_pre_migration_databases": True,
        "rollback_pair": "old_code+v95_data or new_code+v128_data",
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="snapshot or clone root directory")
    parser.add_argument("--apply-to-clone", action="store_true", help="mark clone-only mode; still no production mutation")
    parser.add_argument("--output", type=Path, help="optional JSON output path")
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-provider-source")
    parser.add_argument("--expected-api-base-fingerprint")
    parser.add_argument("--expected-dimension", type=int)
    parser.add_argument("--provider-unavailable", action="store_true")
    args = parser.parse_args()
    report = build_report(
        args.root,
        apply_to_clone=args.apply_to_clone,
        expected_model=args.expected_model,
        expected_provider_source=args.expected_provider_source,
        expected_api_base_fingerprint=args.expected_api_base_fingerprint,
        expected_dimension=args.expected_dimension,
        provider_available=not args.provider_unavailable,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
