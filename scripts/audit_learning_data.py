from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote


_BASELINE_QUERY_VERSION = "learning-baseline-v1"
_MAX_TRACE_LINE_BYTES = 2_000_000
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.:-]+")
_SENSITIVE_VALUE = re.compile(r"(?i)(api[_ -]?key|token|cookie|secret|password|authorization)\s*[:=]\s*[^,;\s]+")
_PORTABLE_ID_KEYS = {
    "id", "*_id", "chat_id", "group_id", "sender_id", "platform_id", "target_id",
    "candidate_id", "memory_id", "source_row_id", "source_row_ids", "source_message_id",
    "source_message_ids", "event_id", "platform_message_id", "dedup_key", "turn_id", "trace_id",
}


def canonical_json_bytes(value: object) -> bytes:
    """Return the portable JSON representation used by baseline hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def redact_diagnostic(value: object) -> str:
    text = str(value)
    return _SENSITIVE_VALUE.sub(lambda match: f"{match.group(1)}=[redacted]", text)[:240]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_file_state(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    sidecars = []
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecars.append({
                "name": sidecar.name,
                "size_bytes": sidecar.stat().st_size,
                "sha256": sha256_file(sidecar),
            })
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "mtime_ns": path.stat().st_mtime_ns if path.is_file() else 0,
        "sha256": sha256_file(path) if path.is_file() else None,
        "sidecars": sidecars,
    }
    return result


def inspect_trace_source(path: Path, logical_name: str, summary: dict[str, Any], *, before: dict[str, Any] | None = None, after: dict[str, Any] | None = None) -> dict[str, Any]:
    state = after or snapshot_file_state(path)
    before = before or state
    suffix = path.suffix.lower()
    fmt = "json" if suffix == ".json" else "jsonl"
    if summary.get("format"):
        fmt = str(summary["format"])
    return {
        "logical_name": logical_name,
        "basename": path.name,
        "format": fmt,
        "sha256": state["sha256"],
        "size_bytes": state["size_bytes"],
        "mtime_ns": state["mtime_ns"],
        "sidecars_before": before["sidecars"],
        "sidecars_after": state["sidecars"],
        "invalid_records": int(summary.get("invalid_count", 0)),
        "oversized_records": int(summary.get("oversized_count", 0)),
        "duplicate_conflicts": int(summary.get("conflict_count", 0)),
        "status": (str(summary.get("status")) if state["exists"] and summary.get("status") else ("observed" if state["exists"] else "blocked")),
        "unchanged": before == state,
    }


def open_sqlite_read_only(path: Path) -> sqlite3.Connection:
    """Open a SQLite file without allowing writes or journal sidecars."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    # quote() keeps Windows drive colons and separators in a valid SQLite URI.
    uri = f"file:{quote(resolved.as_posix(), safe='/:%')}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5.0)
    db.execute("PRAGMA query_only=ON")
    return db


def _schema_fingerprint(db: sqlite3.Connection) -> str:
    rows = db.execute(
        """
        SELECT name, type, sql FROM sqlite_master
        WHERE type IN ('table', 'index', 'view', 'trigger')
        ORDER BY type, name
        """
    ).fetchall()
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def inspect_sqlite_source(path: Path, logical_name: str) -> dict[str, Any]:
    before = snapshot_file_state(path)
    result: dict[str, Any] = {
        "logical_name": logical_name,
        "basename": Path(path).name,
        "format": "sqlite",
        "sha256": before["sha256"],
        "size_bytes": before["size_bytes"],
        "mtime_ns": before["mtime_ns"],
        "sidecars_before": before["sidecars"],
        "warnings": [],
        "status": "observed",
    }
    try:
        with open_sqlite_read_only(path) as db:
            result["sqlite_user_version"] = int(db.execute("PRAGMA user_version").fetchone()[0])
            result["sqlite_page_count"] = int(db.execute("PRAGMA page_count").fetchone()[0])
            result["sqlite_page_size"] = int(db.execute("PRAGMA page_size").fetchone()[0])
            result["integrity_check"] = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            fk_rows = db.execute("PRAGMA foreign_key_check").fetchall()
            result["foreign_key_violation_count"] = len(fk_rows)
            result["schema_fingerprint"] = _schema_fingerprint(db)
    except (OSError, sqlite3.Error) as exc:
        result["status"] = "blocked"
        result["warnings"].append(type(exc).__name__)
    after = snapshot_file_state(path)
    result["sidecars_after"] = after["sidecars"]
    result["unchanged"] = before == after
    if not result["unchanged"]:
        result["status"] = "blocked"
        result["warnings"].append("source_changed")
    if result.get("integrity_check") != "ok" or result.get("foreign_key_violation_count", 0):
        result["status"] = "blocked"
    return result


def iter_trace_records(path: Path, *, max_line_bytes: int = _MAX_TRACE_LINE_BYTES):
    """Stream trace records and retain malformed/duplicate diagnostics."""
    path = Path(path)
    if not path.is_file():
        yield {"line_number": 0, "format": "missing", "record": None, "issue_code": "missing_source", "byte_length": 0}
        return
    if path.stat().st_size <= 16 * 1024 * 1024:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                prefix = handle.read(1)
                handle.seek(0)
                if prefix == "[":
                    try:
                        values = json.load(handle)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        yield {"line_number": 1, "format": "json", "record": None, "issue_code": "invalid_json", "byte_length": path.stat().st_size}
                        return
                    if not isinstance(values, list):
                        yield {"line_number": 1, "format": "json", "record": None, "issue_code": "invalid_record", "byte_length": path.stat().st_size}
                        return
                    seen: dict[str, str] = {}
                    for line_number, value in enumerate(values, 1):
                        if not isinstance(value, dict):
                            yield {"line_number": line_number, "format": "json", "record": None, "issue_code": "invalid_record", "byte_length": 0}
                            continue
                        identity = str(value.get("turn_id") or value.get("trace_id") or "")
                        digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
                        issue = ""
                        if identity and identity in seen:
                            issue = "duplicate_same" if seen[identity] == digest else "duplicate_conflict"
                        elif identity:
                            seen[identity] = digest
                        yield {"line_number": line_number, "format": "json", "record": value, "issue_code": issue, "byte_length": len(canonical_json_bytes(value))}
                    return
        except OSError:
            pass
    seen: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace", newline=None) as handle:
        for line_number, raw in enumerate(handle, 1):
            byte_length = len(raw.encode("utf-8", errors="replace"))
            if not raw.strip():
                yield {"line_number": line_number, "format": "jsonl", "record": None, "issue_code": "empty_line", "byte_length": byte_length}
                continue
            if byte_length > max_line_bytes:
                yield {"line_number": line_number, "format": "jsonl", "record": None, "issue_code": "oversized_line", "byte_length": byte_length}
                continue
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                yield {"line_number": line_number, "format": "jsonl", "record": None, "issue_code": "invalid_json", "byte_length": byte_length}
                continue
            if not isinstance(value, dict):
                yield {"line_number": line_number, "format": "jsonl", "record": None, "issue_code": "invalid_record", "byte_length": byte_length}
                continue
            identity = str(value.get("turn_id") or value.get("trace_id") or "")
            digest = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
            issue = ""
            if identity and identity in seen:
                issue = "duplicate_same" if seen[identity] == digest else "duplicate_conflict"
            elif identity:
                seen[identity] = digest
            yield {"line_number": line_number, "format": "jsonl", "record": value, "issue_code": issue, "byte_length": byte_length}


def _metric_envelope(*, status: str, source: str, query_id: str, window: tuple[float | None, float | None],
                     numerator: int | None = None, denominator: int | None = None,
                     sample_count: int = 0, unknown_count: int = 0, excluded_count: int = 0,
                     warnings: list[str] | None = None, value: float | int | None = None,
                     metric_name: str | None = None, unit: str = "count",
                     dimensions: dict[str, Any] | None = None) -> dict[str, Any]:
    if numerator is not None and denominator not in (None, 0) and value is None:
        value = numerator / denominator
    return {
        "metric_name": metric_name or query_id,
        "status": status,
        "source": source,
        "query_id": query_id,
        "window": {"kind": "event_window", "start": window[0], "end": window[1], "interval": "[start,end)"},
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "sample_count": max(0, int(sample_count)),
        "unknown_count": max(0, int(unknown_count)),
        "excluded_count": max(0, int(excluded_count)),
        "unit": unit,
        "dimensions": dict(dimensions or {}),
        "warnings": list(warnings or []),
    }


def _column_names(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, table):
        return set()
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _missing_columns(db: sqlite3.Connection, table: str, required: tuple[str, ...]) -> list[str]:
    present = _column_names(db, table)
    return [name for name in required if name not in present]


def _hashed_id(value: object, namespace: str) -> str:
    return hashlib.sha256(f"{namespace}:{value}".encode("utf-8")).hexdigest()


def build_plugin_baseline(db: sqlite3.Connection, *, window: tuple[float | None, float | None], query_catalog: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"messages": {}, "checkpoint_backlog": [], "checkpoint_snapshot": {}, "runs": {}, "candidates": [], "candidate_summary": {}, "schema": {}}
    tables = [str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
    result["schema"] = {"tables": tables, "missing_required": [name for name in ("messagelog", "learning_pipeline_checkpoint", "learning_mining_run") if name not in tables]}
    if _table_exists(db, "messagelog"):
        columns = _column_names(db, "messagelog")
        total = int(db.execute("SELECT COUNT(*) FROM messagelog").fetchone()[0])
        if "timestamp" in columns and window[0] is not None and window[1] is not None:
            count = int(db.execute("SELECT COUNT(*) FROM messagelog WHERE timestamp >= ? AND timestamp < ?", window).fetchone()[0])
        else:
            count = None
        required_message = ("id", "group_id", "timestamp")
        missing = [name for name in required_message if name not in columns]
        result["messages"] = {
            "total": _metric_envelope(status="observed", source="database", query_id="plugin.message.total.v1", window=window, numerator=total, denominator=total, sample_count=total, value=total),
            "window": _metric_envelope(status="partial" if missing else ("observed" if count is not None else "unavailable"), source="database", query_id="plugin.message.window.v1", window=window, numerator=count, denominator=count, sample_count=count or 0, value=count, warnings=["missing_columns:" + ",".join(missing)] if missing else []),
            "structure": _metric_envelope(status="unavailable", source="database", query_id="plugin.message.structure.v1", window=window, warnings=["message_structure_aggregation_not_available"]),
            "sender_scope": _metric_envelope(status="unavailable", source="database", query_id="plugin.message.sender_scope.v1", window=window, warnings=["sender_scope_aggregation_not_available"]),
            "columns": sorted(columns),
        }
    else:
        result["messages"] = {"total": _metric_envelope(status="blocked", source="database", query_id="plugin.message.total.v1", window=window, warnings=["missing_table"]), "window": _metric_envelope(status="blocked", source="database", query_id="plugin.message.window.v1", window=window, warnings=["missing_table"]), "structure": _metric_envelope(status="unavailable", source="database", query_id="plugin.message.structure.v1", window=window, warnings=["missing_table"]), "sender_scope": _metric_envelope(status="unavailable", source="database", query_id="plugin.message.sender_scope.v1", window=window, warnings=["missing_table"])}
    if _table_exists(db, "learning_pipeline_checkpoint"):
        checkpoint_count = int(db.execute("SELECT COUNT(*) FROM learning_pipeline_checkpoint").fetchone()[0])
        result["checkpoint_snapshot"] = _metric_envelope(status="observed", source="database", query_id="plugin.checkpoint.snapshot.v1", window=window, numerator=checkpoint_count, denominator=checkpoint_count, sample_count=checkpoint_count, value=checkpoint_count)
    else:
        result["checkpoint_snapshot"] = _metric_envelope(status="unavailable", source="database", query_id="plugin.checkpoint.snapshot.v1", window=window, warnings=["missing_table:learning_pipeline_checkpoint"])
    if _table_exists(db, "learning_pipeline_checkpoint") and _table_exists(db, "messagelog"):
        cp_columns = _column_names(db, "learning_pipeline_checkpoint")
        required_cp = ("pipeline", "chat_id", "cursor_log_id", "last_status", "retry_at")
        missing_cp = [name for name in required_cp if name not in cp_columns]
        result["schema"]["learning_pipeline_checkpoint"] = {"columns": sorted(cp_columns), "missing_columns": missing_cp, "status": "blocked" if missing_cp else "observed"}
        if not missing_cp and {"id", "group_id"}.issubset(_column_names(db, "messagelog")):
            select = list(required_cp)
            rows = db.execute(f"SELECT {', '.join(select)} FROM learning_pipeline_checkpoint").fetchall()
            for pipeline, chat_id, cursor, status, retry_at in rows:
                pending = int(db.execute("SELECT COUNT(*) FROM messagelog WHERE group_id = ? AND id > ?", (chat_id, cursor)).fetchone()[0])
                max_id = db.execute("SELECT MAX(id) FROM messagelog WHERE group_id = ?", (chat_id,)).fetchone()[0]
                result["checkpoint_backlog"].append({"pipeline": str(pipeline), "chat_id": str(chat_id), "cursor_log_id": int(cursor or 0), "pending_count": pending, "id_span": max(0, int(max_id or 0) - int(cursor or 0)), "last_status": str(status or "unknown"), "retry_at": float(retry_at or 0), "columns": sorted(cp_columns)})
            result["checkpoint_backlog"].sort(key=lambda row: (-row["pending_count"], row["pipeline"], row["chat_id"]))
        elif not missing_cp:
            result["schema"]["messagelog"] = {"columns": sorted(_column_names(db, "messagelog")), "missing_columns": ["id", "group_id"], "status": "blocked"}
    if _table_exists(db, "learning_mining_run"):
        columns = _column_names(db, "learning_mining_run")
        required_run = ("status", "candidate_count", "saved_count", "deduplicated_count")
        missing_run = [name for name in required_run if name not in columns]
        result["schema"]["learning_mining_run"] = {"columns": sorted(columns), "missing_columns": missing_run, "status": "blocked" if missing_run else "observed"}
        status_rows = db.execute("SELECT status, COUNT(*) FROM learning_mining_run GROUP BY status").fetchall() if "status" in columns else []
        known = {"completed", "failed", "quarantined", "waiting", "skipped", "retry_wait", "blocked"}
        status_counts = {str(status or "unknown"): int(count) for status, count in status_rows}
        unknown = sum(count for status, count in status_counts.items() if status not in known)
        invalid_details = 0
        if "details_json" in columns:
            for (payload,) in db.execute("SELECT details_json FROM learning_mining_run").fetchall():
                if payload:
                    try:
                        json.loads(payload)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        invalid_details += 1
        result["runs"] = {"status_counts": status_counts, "unknown_status_count": unknown, "details_json_invalid": invalid_details, "columns": sorted(columns)}
        result["runs"]["funnel"] = _metric_envelope(status="partial", source="database", query_id="plugin.learning_run.funnel.v1", window=window, warnings=["candidate_identity_unavailable"])
        result["runs"]["diagnostics_completeness"] = _metric_envelope(status="partial" if invalid_details else ("observed" if "details_json" in columns else "unavailable"), source="database", query_id="plugin.learning_run.diagnostics_completeness.v1", window=window, numerator=max(0, len(status_rows) - invalid_details) if "details_json" in columns else None, denominator=len(status_rows) if "details_json" in columns else None, warnings=["invalid_details_json"] if invalid_details else ([] if "details_json" in columns else ["missing_column:details_json"]))
        for field in ("candidate_count", "saved_count", "deduplicated_count"):
            result["runs"][field] = int(db.execute(f"SELECT COALESCE(SUM({field}), 0) FROM learning_mining_run").fetchone()[0]) if field in columns else None
        result["candidate_summary"] = {
            "candidate_count": result["runs"].get("candidate_count"),
            "saved_count": result["runs"].get("saved_count"),
            "deduplicated_count": result["runs"].get("deduplicated_count"),
            "identity_available": False,
            "status": "partial" if any(result["runs"].get(field) is None for field in ("candidate_count", "saved_count", "deduplicated_count")) else "observed",
            "warnings": ["run-level counts have no candidate identity"] if not result["candidates"] else [],
        }
    else:
        result["candidate_summary"] = {"candidate_count": None, "saved_count": None, "deduplicated_count": None, "identity_available": False, "status": "unavailable", "warnings": ["missing_table:learning_mining_run"]}
    return result


def build_memory_baseline(db: sqlite3.Connection, *, window: tuple[float | None, float | None], query_catalog: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"canonical": [], "expression_status": {}, "jargon_status": {}, "repair_status": {}, "retrieval_join": {"status": "unavailable", "unknown_count": 0}, "provenance": {}, "dedup": {}, "schema": {}}
    if _table_exists(db, "canonical_memories"):
        columns = _column_names(db, "canonical_memories")
        required = ("id", "kind", "status", "dedup_key")
        missing = [name for name in required if name not in columns]
        result["schema"]["canonical_memories"] = {"columns": sorted(columns), "missing_columns": missing, "status": "blocked" if missing else "observed"}
        if not missing:
            result["canonical"] = [{"id": str(row[0]), "kind": str(row[1]), "status": str(row[2]), "dedup_key": str(row[3] or "")} for row in db.execute("SELECT id, kind, status, dedup_key FROM canonical_memories").fetchall()]
            result["expression_status"] = _status_counts(db, "canonical_memories", kind="expression_pattern")
            result["jargon_status"] = _status_counts(db, "canonical_memories", kind="jargon")
            result["provenance"] = _metric_envelope(status="unavailable", source="database", query_id="memory.learning_canonical.provenance.v1", window=window, warnings=["provenance_columns_not_available"])
            duplicate_count = int(db.execute("SELECT COUNT(*) FROM (SELECT dedup_key FROM canonical_memories WHERE dedup_key != '' GROUP BY dedup_key HAVING COUNT(*) > 1)").fetchone()[0])
            result["dedup"] = _metric_envelope(status="observed", source="database", query_id="memory.learning_canonical.dedup.v1", window=window, numerator=duplicate_count, denominator=len(result["canonical"]), sample_count=len(result["canonical"]), value=duplicate_count)
    else:
        result["schema"]["canonical_memories"] = {"columns": [], "missing_columns": ["table"], "status": "unavailable"}
        result["provenance"] = _metric_envelope(status="unavailable", source="database", query_id="memory.learning_canonical.provenance.v1", window=window, warnings=["missing_table:canonical_memories"])
        result["dedup"] = _metric_envelope(status="unavailable", source="database", query_id="memory.learning_canonical.dedup.v1", window=window, warnings=["missing_table:canonical_memories"])
    if _table_exists(db, "memory_consistency_repairs"):
        result["repair_status"] = _status_counts(db, "memory_consistency_repairs")
    if _table_exists(db, "memoryretrievaltrace") and _table_exists(db, "canonical_memories") and not result["schema"].get("canonical_memories", {}).get("missing_columns"):
        columns = _column_names(db, "memoryretrievaltrace")
        if "selected_memory_ids" in columns:
            selected_rows = db.execute("SELECT selected_memory_ids FROM memoryretrievaltrace").fetchall()
            canonical_ids = {str(row[0]) for row in db.execute("SELECT id FROM canonical_memories").fetchall()}
            selected_count = 0
            unknown_count = 0
            joined_count = 0
            for (payload,) in selected_rows:
                if not payload:
                    unknown_count += 1
                    continue
                try:
                    values = json.loads(payload) if isinstance(payload, str) else payload
                    values = values if isinstance(values, list) else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    unknown_count += 1
                    continue
                selected_count += 1
                if any(str(item) in canonical_ids for item in values):
                    joined_count += 1
            result["retrieval_join"] = {"status": "observed", "selected_count": selected_count, "joined_count": joined_count, "unknown_count": unknown_count}
    elif not _table_exists(db, "memoryretrievaltrace"):
        result["retrieval_join"] = {"status": "unavailable", "selected_count": None, "joined_count": None, "unknown_count": 0, "warnings": ["missing_table:memoryretrievaltrace"]}
    return result


def _trace_summary(path: Path, window: tuple[float | None, float | None]) -> dict[str, Any]:
    record_count = conflict_count = invalid_count = oversized_count = 0
    selected_count = unknown_outcome_count = 0
    unknown_time_count = unknown_identity_count = 0
    selected: list[dict[str, Any]] = []
    unique: dict[str, tuple[str, dict[str, Any]]] = {}
    anonymous: list[dict[str, Any]] = []

    def record_time(record: dict[str, Any]) -> float | None:
        value = next((record.get(key) for key in ("timestamp", "created_at", "started_at", "time", "created_at_utc") if record.get(key) is not None), None)
        if value is None:
            return None
        try:
            if isinstance(value, str):
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    for item in iter_trace_records(path):
        issue = item.get("issue_code")
        if issue == "oversized_line":
            oversized_count += 1
        if issue in {"invalid_json", "invalid_record", "oversized_line"}:
            invalid_count += 1
        record = item.get("record")
        if not isinstance(record, dict):
            continue
        turn_identity = str(record.get("turn_id") or record.get("trace_id") or "")
        call_identity = str(record.get("call_id") or "")
        identity = f"{turn_identity}:{call_identity}" if call_identity else turn_identity
        if not identity:
            unknown_identity_count += 1
        timestamp = record_time(record)
        if timestamp is None:
            unknown_time_count += 1
            continue
        if window[0] is not None and (timestamp < window[0] or timestamp >= window[1]):
            continue
        digest = hashlib.sha256(canonical_json_bytes(record)).hexdigest()
        if identity:
            previous = unique.get(identity)
            if previous is None:
                unique[identity] = (digest, record)
            elif previous[0] != digest:
                conflict_count += int(previous[0] != "__conflict__")
                unique[identity] = ("__conflict__", {})
        else:
            anonymous.append(record)
    records = [record for digest, record in unique.values() if digest != "__conflict__"] + anonymous
    record_count = len(records)
    for record in records:
        if record.get("candidate_id"):
            selected_count += 1
            selected.append({"candidate_id": str(record.get("candidate_id")), "final_answer": record.get("final_answer")})
            if record.get("final_answer") in (None, ""):
                unknown_outcome_count += 1
    status = "partial" if (conflict_count or invalid_count or oversized_count or unknown_time_count or unknown_identity_count) else "observed"
    formats = {str(item.get("format")) for item in iter_trace_records(path) if item.get("format")}
    unknown_total = unknown_time_count + unknown_identity_count + invalid_count + conflict_count
    return {"format": sorted(formats)[0] if len(formats) == 1 else ("mixed" if formats else "unknown"), "record_count": record_count, "conflict_count": conflict_count, "invalid_count": invalid_count,
            "oversized_count": oversized_count, "unknown_time_count": unknown_time_count,
            "unknown_identity_count": unknown_identity_count, "selected_count": selected_count,
            "unknown_outcome_count": unknown_outcome_count, "selected": selected, "status": status,
            "metrics": {
                "outcome": _metric_envelope(status=status, source="trace", query_id="trace.turn.outcome.v1", window=window, numerator=None, denominator=None, sample_count=record_count, unknown_count=unknown_total, warnings=["trace_outcome_fields_incomplete"] if unknown_total else ["accepted_reply_outcome_not_available"]),
                "learning_workload": _metric_envelope(status=status, source="trace", query_id="trace.call.learning_workload.v1", window=window, numerator=record_count if not unknown_total else None, denominator=record_count if not unknown_total else None, sample_count=record_count, unknown_count=unknown_total, warnings=["workload_dimensions_not_available"]),
                "parse_health": _metric_envelope(status=status, source="trace", query_id="trace.parse_health.v1", window=window, numerator=max(0, record_count - invalid_count - conflict_count), denominator=record_count + invalid_count + conflict_count, sample_count=record_count, unknown_count=unknown_total),
            }}


def build_learning_funnel(plugin_report: dict[str, Any], memory_report: dict[str, Any], trace_report: dict[str, Any]) -> dict[str, Any]:
    saved = {str(row.get("candidate_id")) for row in plugin_report.get("candidates", []) if row.get("saved") and row.get("candidate_id")}
    canonical = {str(row.get("candidate_id") or row.get("id")) for row in memory_report.get("canonical", []) if row.get("candidate_id") or row.get("id")}
    selected = trace_report.get("selected", [])

    def edge(name: str, source_count: int | None, joined_count: int | None, unknown_count: int = 0,
             *, status: str = "observed", join_keys: list[str] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
        return {"from_layer": name.split("->", 1)[0], "to_layer": name.split("->", 1)[1],
                "source_count": source_count, "joined_count": joined_count, "unknown_count": unknown_count,
                "conflict_count": 0, "join_keys": list(join_keys or []), "status": status, "warnings": list(warnings or [])}

    candidate_summary = plugin_report.get("candidate_summary", {})
    candidate_count = candidate_summary.get("candidate_count")
    saved_count = candidate_summary.get("saved_count")
    message_total = ((plugin_report.get("messages") or {}).get("total") or {}).get("numerator")
    selected_count = len(selected) if isinstance(selected, list) else None
    retrieval = memory_report.get("retrieval_join", {})
    explicit_mapping = plugin_report.get("candidate_to_memory")
    if explicit_mapping is None:
        explicit_mapping = memory_report.get("candidate_to_memory")
    mapping_available = isinstance(explicit_mapping, dict)
    if mapping_available:
        mapping = {str(key): str(value) for key, value in explicit_mapping.items() if value not in (None, "")}
        mapped_saved = {mapping[candidate_id] for candidate_id in saved if candidate_id in mapping}
        saved_source_count = saved_count if saved_count is not None else (len(saved) if saved else None)
        saved_joined_count = len(mapped_saved & canonical)
        saved_unknown_count = max(0, int(saved_source_count or 0) - len([candidate_id for candidate_id in saved if candidate_id in mapping]))
        saved_status = "observed" if saved_source_count is not None and saved_unknown_count == 0 else "partial"
        saved_warnings = [] if saved_status == "observed" else ["candidate_to_memory_mapping_incomplete"]
    else:
        saved_source_count = saved_count if saved_count is not None else (len(saved) if saved else None)
        saved_joined_count = None
        saved_unknown_count = int(saved_source_count or 0)
        saved_status = "partial"
        saved_warnings = ["stable_candidate_to_memory_mapping_unavailable"]
    edges = [
        edge("messages->candidates", message_total, None, unknown_count=message_total or 0, status="partial" if message_total is not None else "unavailable", warnings=["candidate_identity_unavailable"]),
        edge("candidates->saved", candidate_count, None, unknown_count=candidate_count or 0, status="partial" if candidate_count is not None else "unavailable", join_keys=["candidate_id"], warnings=["candidate_identity_unavailable"]),
        edge("saved->canonical", saved_source_count, saved_joined_count, unknown_count=saved_unknown_count, status=saved_status, join_keys=["candidate_id", "memory_id"], warnings=saved_warnings),
        edge("canonical->review", len(canonical) if canonical else None, None, unknown_count=len(canonical) if canonical else 0, status="unavailable", join_keys=["memory_id"], warnings=["review_decision_not_available"]),
        edge("review->index", None, None, unknown_count=0, status="unavailable", join_keys=["memory_id", "generation"], warnings=["physical_index_identity_not_available"]),
        edge("index->retrieval", retrieval.get("selected_count"), retrieval.get("joined_count"), unknown_count=int(retrieval.get("unknown_count") or 0), status=str(retrieval.get("status") or "unavailable"), join_keys=["memory_id"], warnings=list(retrieval.get("warnings") or [])),
        edge("retrieval->reply", selected_count, None, unknown_count=selected_count or 0, status="unavailable", join_keys=["turn_id"], warnings=["accepted_for_prompt_and_reply_outcome_not_available"]),
    ]
    edge_map = {f"{item['from_layer']}_to_{item['to_layer']}": item for item in edges}
    # Keep the pre-baseline key for existing audit consumers; it has the same unknown semantics.
    edge_map["selected_to_reply"] = edge("selected->reply", selected_count, None, unknown_count=sum(1 for row in selected if row.get("final_answer") in (None, "")) if isinstance(selected, list) else 0, status="unavailable", join_keys=["turn_id"], warnings=["reply_outcome_not_available"])
    return {"edges": edge_map}


def scan_artifact_for_sensitive_values(path: Path) -> list[dict[str, str]]:
    """Scan portable artifacts for raw identifiers, credentials, URLs, or message bodies."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    findings: list[dict[str, str]] = []
    patterns = {
        "credential": r"(?i)[\"']?(?:api[_ -]?key|token|cookie|secret|password|authorization|bearer|set-cookie)[\"']?\s*[:=]",
        "url_query": r"https?://[^\s\"']+\?[^\s\"']+",
        "platform_id": r"(?i)(?:user|sender|group|platform|qq|chat)_?id\"?\s*[:=]\s*\"?\d{5,}",
        "absolute_path": r"(?i)(?:[A-Z]:[\\/]|/(?:home|Users|AstrBot)/)",
        "raw_message": r"(?i)\"(?:message|prompt|response|content)\"\s*:\s*\"[^\"]{80,}\"",
    }
    for kind, pattern in patterns.items():
        if re.search(pattern, text):
            findings.append({"kind": kind, "path": Path(path).name})
    return findings


def build_gold_sampling_frame(rows: list[dict[str, Any]], *, seed: int, query_version: str) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        source_rows = row.get("source_row_ids", row.get("source_row_id"))
        if not isinstance(source_rows, list):
            source_rows = [source_rows] if source_rows is not None else []
        identity = row.get("candidate_id") or ":".join(str(item) for item in sorted(source_rows)) or "unknown"
        sample_id = hashlib.sha256(canonical_json_bytes({"identity": identity, "seed": seed, "query_version": query_version})).hexdigest()
        output.append({"sample_id": sample_id, "candidate_id_hash": _hashed_id(row.get("candidate_id"), "candidate") if row.get("candidate_id") is not None else None,
                      "source_row_id_hashes": [_hashed_id(item, "source_row") for item in sorted(source_rows)],
                      "scope_id_hash": _hashed_id(row.get("scope_id", "unknown"), "scope"),
                      "candidate_family": row.get("candidate_family", "unknown")})
    return sorted(output, key=lambda row: row["sample_id"])


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
