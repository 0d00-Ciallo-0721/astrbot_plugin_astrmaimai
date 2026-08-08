from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone() is not None


def _status_counts(db: sqlite3.Connection, table: str, *, kind: str = "") -> dict[str, int]:
    if not _table_exists(db, table):
        return {}
    if kind:
        rows = db.execute(
            f"SELECT COALESCE(status, ''), COUNT(*) FROM {table} WHERE kind = ? GROUP BY status",
            (kind,),
        ).fetchall()
    else:
        rows = db.execute(
            f"SELECT COALESCE(status, ''), COUNT(*) FROM {table} GROUP BY status"
        ).fetchall()
    return {str(status or "unknown"): int(count or 0) for status, count in rows}


def audit_plugin_db(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "checkpoints": {},
        "runs": {},
        "evidence_backlog": [],
    }
    if not path.is_file():
        return report
    now = time.time()
    with sqlite3.connect(path) as db:
        report["integrity"] = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        if _table_exists(db, "learning_pipeline_checkpoint"):
            rows = db.execute(
                """
                SELECT pipeline, last_status, failure_count, retry_at
                FROM learning_pipeline_checkpoint
                """
            ).fetchall()
            by_pipeline: dict[str, dict[str, Any]] = {}
            for pipeline, status, failure_count, retry_at in rows:
                item = by_pipeline.setdefault(
                    str(pipeline),
                    {"count": 0, "status_counts": {}, "quarantined": 0, "failures": 0},
                )
                item["count"] += 1
                clean_status = str(status or "unknown")
                item["status_counts"][clean_status] = item["status_counts"].get(clean_status, 0) + 1
                item["quarantined"] += int(float(retry_at or 0.0) > now)
                item["failures"] += int(failure_count or 0)
            report["checkpoints"] = by_pipeline
            if _table_exists(db, "messagelog"):
                report["evidence_backlog"] = [
                    {
                        "pipeline": str(row[0]),
                        "chat_id": str(row[1]),
                        "pending_messages": int(row[2] or 0),
                        "last_status": str(row[3] or ""),
                        "retry_at": float(row[4] or 0.0),
                    }
                    for row in db.execute(
                        """
                        SELECT c.pipeline, c.chat_id, COUNT(m.id), c.last_status, c.retry_at
                        FROM learning_pipeline_checkpoint AS c
                        LEFT JOIN messagelog AS m
                          ON m.group_id = c.chat_id AND m.id > c.cursor_log_id
                        GROUP BY c.pipeline, c.chat_id
                        ORDER BY COUNT(m.id) DESC, c.pipeline, c.chat_id
                        LIMIT 100
                        """
                    ).fetchall()
                ]
        if _table_exists(db, "learning_mining_run"):
            run_rows = db.execute(
                "SELECT pipeline, status, COUNT(*) FROM learning_mining_run GROUP BY pipeline, status"
            ).fetchall()
            run_counts: dict[str, Counter[str]] = {}
            for pipeline, status, count in run_rows:
                run_counts.setdefault(str(pipeline), Counter())[str(status or "unknown")] += int(count or 0)
            report["runs"] = {
                pipeline: dict(counts) for pipeline, counts in run_counts.items()
            }
    return report


def audit_memory_db(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "expression_status": {},
        "jargon_status": {},
        "duplicate_dedup_keys": [],
        "legacy_jargon_keys": [],
    }
    if not path.is_file():
        return report
    with sqlite3.connect(path) as db:
        report["integrity"] = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        report["expression_status"] = _status_counts(
            db, "canonical_memories", kind="expression_pattern"
        )
        report["jargon_status"] = _status_counts(db, "canonical_memories", kind="jargon")
        if _table_exists(db, "canonical_memories"):
            report["duplicate_dedup_keys"] = [
                {"kind": str(row[0]), "dedup_key": str(row[1]), "count": int(row[2])}
                for row in db.execute(
                    """
                    SELECT kind, dedup_key, COUNT(*)
                    FROM canonical_memories
                    WHERE dedup_key != '' AND kind IN ('expression_pattern', 'jargon')
                    GROUP BY kind, dedup_key
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, kind, dedup_key
                    LIMIT 200
                    """
                ).fetchall()
            ]
            report["legacy_jargon_keys"] = [
                {"id": str(row[0]), "dedup_key": str(row[1]), "status": str(row[2])}
                for row in db.execute(
                    """
                    SELECT id, dedup_key, status
                    FROM canonical_memories
                    WHERE kind = 'jargon'
                      AND (dedup_key LIKE 'jargon:ff:%' OR dedup_key LIKE 'jargon:group-%')
                    ORDER BY update_time DESC
                    LIMIT 200
                    """
                ).fetchall()
            ]
    return report


def build_report(*, plugin_db: Path | None, memory_db: Path | None) -> dict[str, Any]:
    return {
        "generated_at": time.time(),
        "mode": "read_only",
        "plugin_db": audit_plugin_db(plugin_db) if plugin_db else {},
        "memory_db": audit_memory_db(memory_db) if memory_db else {},
    }


def _backup_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    shutil.copystat(source, destination)


def _iter_chunks(values: list[str], size: int = 500):
    safe_size = max(1, int(size or 500))
    for start in range(0, len(values), safe_size):
        yield values[start : start + safe_size]


def clean_memory_db(
    path: Path,
    *,
    backup_dir: Path,
    delete_rejected_expression: bool = False,
    delete_rejected_jargon: bool = False,
    delete_stale_jargon: bool = False,
) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    selected: list[tuple[str, str]] = []
    if delete_rejected_expression:
        selected.append(("expression_pattern", "rejected"))
    if delete_rejected_jargon:
        selected.append(("jargon", "rejected"))
    if delete_stale_jargon:
        selected.append(("jargon", "stale"))
    if not selected:
        raise ValueError("no cleanup action selected")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{path.stem}.learning-cleanup-{stamp}{path.suffix}"
    _backup_sqlite(path, backup_path)

    deleted: dict[str, int] = {}
    with sqlite3.connect(path) as db:
        if not _table_exists(db, "canonical_memories"):
            raise RuntimeError("canonical_memories table is missing")
        db.execute("BEGIN IMMEDIATE")
        try:
            for kind, status in selected:
                ids = [
                    str(row[0])
                    for row in db.execute(
                        "SELECT id FROM canonical_memories WHERE kind = ? AND status = ?",
                        (kind, status),
                    ).fetchall()
                ]
                if ids:
                    has_fts = _table_exists(db, "canonical_fts")
                    has_aliases = _table_exists(db, "memory_dedup_aliases")
                    for chunk in _iter_chunks(ids):
                        placeholders = ",".join("?" for _ in chunk)
                        if has_fts:
                            db.execute(
                                f"DELETE FROM canonical_fts WHERE memory_id IN ({placeholders})",
                                chunk,
                            )
                        if has_aliases:
                            db.execute(
                                f"DELETE FROM memory_dedup_aliases WHERE canonical_memory_id IN ({placeholders})",
                                chunk,
                            )
                        db.execute(
                            f"DELETE FROM canonical_memories WHERE id IN ({placeholders})",
                            chunk,
                        )
                deleted[f"{kind}:{status}"] = len(ids)
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {"backup": str(backup_path), "deleted": deleted}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit AstrMai learning checkpoints and canonical learning data."
    )
    parser.add_argument("--plugin-db", type=Path, help="path to astrmai.db")
    parser.add_argument("--memory-db", type=Path, help="path to memory_v2.db")
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    parser.add_argument("--apply", action="store_true", help="enable explicitly selected cleanup")
    parser.add_argument("--backup-dir", type=Path, help="required with --apply")
    parser.add_argument("--delete-rejected-expression", action="store_true")
    parser.add_argument("--delete-rejected-jargon", action="store_true")
    parser.add_argument("--delete-stale-jargon", action="store_true")
    args = parser.parse_args()
    if not args.plugin_db and not args.memory_db:
        parser.error("at least one of --plugin-db or --memory-db is required")

    report = build_report(plugin_db=args.plugin_db, memory_db=args.memory_db)
    if args.apply:
        if not args.memory_db or not args.backup_dir:
            parser.error("--apply requires --memory-db and --backup-dir")
        report["mode"] = "apply"
        report["cleanup"] = clean_memory_db(
            args.memory_db,
            backup_dir=args.backup_dir,
            delete_rejected_expression=args.delete_rejected_expression,
            delete_rejected_jargon=args.delete_rejected_jargon,
            delete_stale_jargon=args.delete_stale_jargon,
        )
        report["memory_db_after"] = audit_memory_db(args.memory_db)

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
