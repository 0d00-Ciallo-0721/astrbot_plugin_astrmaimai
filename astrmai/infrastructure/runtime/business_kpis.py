"""Read-only business KPI aggregation with explicit unknown semantics.

The module intentionally performs no writes and does not assume that every
database has the newest schema.  Database observations are preferred; trace
fallbacks are marked as derived so callers cannot mistake them for persisted
business facts.
"""

from __future__ import annotations

import math
import re
import sqlite3
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


_SENSITIVE = re.compile(r"(?i)(api[_ -]?key|token|cookie|secret|password)\s*[:=]\s*[^,;\s]+")
_ERROR_LIMIT = 240


def _safe_error_summary(value: Any, limit: int = _ERROR_LIMIT) -> str | None:
    if value is None:
        return None
    text = _SENSITIVE.sub(r"\1=[redacted]", str(value)).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _metric(
    value: float | int | None,
    numerator: int | None,
    denominator: int | None,
    *,
    source: str,
    reason: str | None = None,
    unknown_count: int = 0,
    status: str | None = None,
    window_start: float | None = None,
    window_end: float | None = None,
    sample_count: int | None = None,
    excluded_count: int = 0,
    warnings: Iterable[str] = (),
) -> dict[str, Any]:
    known = value is not None
    metric_status = status or ("observed" if source == "database" and known else "derived" if source == "trace" and known else "unavailable")
    return {
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "source": source,
        "status": metric_status,
        "known": known,
        "reason": reason if reason else (None if known else "no_observations"),
        "unknown_count": max(0, int(unknown_count)),
        "window_start": window_start,
        "window_end": window_end,
        "sample_count": sample_count if sample_count is not None else (denominator or 0),
        "excluded_count": max(0, int(excluded_count)),
        "warnings": list(dict.fromkeys(str(item) for item in warnings if item)),
    }


def _rate(numerator: int, denominator: int, *, source: str, unknown_count: int = 0, reason: str | None = None, **kwargs: Any) -> dict[str, Any]:
    if denominator <= 0:
        return _metric(None, numerator, denominator, source=source, reason=reason or "no_observations", unknown_count=unknown_count, **kwargs)
    return _metric(round(numerator / denominator, 4), numerator, denominator, source=source, reason=reason, unknown_count=unknown_count, **kwargs)


def _percentile(values: Iterable[float], ratio: float) -> float | None:
    ordered = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) >= 0)
    if not ordered:
        return None
    return round(ordered[min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))], 2)


def _median(values: Iterable[float]) -> float | None:
    ordered = sorted(float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v)))
    if not ordered:
        return None
    middle = len(ordered) // 2
    value = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return round(value, 4)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def iter_jsonl_traces(path: str | Path, *, max_line_bytes: int = 2_000_000):
    """Yield valid JSON objects one line at a time without loading the file."""
    with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            if len(raw.encode("utf-8", errors="replace")) > max_line_bytes:
                continue
            try:
                value = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(value, Mapping):
                yield value


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    return sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)


def _trace_items(traces: Any, start_ts: float | None, end_ts: float | None) -> list[Mapping[str, Any]]:
    if not isinstance(traces, Iterable) or isinstance(traces, (str, bytes, Mapping)):
        return []
    result: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for item in traces:
        if not isinstance(item, Mapping):
            continue
        identity = str(item.get("turn_id") or item.get("trace_id") or "")
        if identity and identity in seen:
            continue
        if identity:
            seen.add(identity)
        timestamp = item.get("started_at", item.get("timestamp"))
        try:
            numeric = float(timestamp) if timestamp is not None else None
        except (TypeError, ValueError):
            numeric = None
        if (start_ts is not None or end_ts is not None) and numeric is None:
            continue
        if start_ts is not None and numeric is not None and numeric < start_ts:
            continue
        if end_ts is not None and numeric is not None and numeric >= end_ts:
            continue
        # Keep only fields needed by the KPI reducer; never retain full message
        # bodies or provider responses from a large JSONL input.
        result.append({
            "turn_id": item.get("turn_id"),
            "trace_id": item.get("trace_id"),
            "timestamp": timestamp,
            "reply_sent": item.get("reply_sent"),
            "stage_ledger": item.get("stage_ledger"),
            "llm_call_ledger": item.get("llm_call_ledger"),
        })
    return result


def _trace_metric_values(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    queue_timeout = provider_total = provider_errors = provider_cancelled = provider_abandoned = 0
    provider_latencies: list[float] = []
    replies = reply_success = 0
    seen_calls: set[tuple[str, str]] = set()
    for trace in items:
        reply = trace.get("reply_sent")
        if reply is not None:
            replies += 1
            reply_success += int(bool(reply))
        for stage in trace.get("stage_ledger", ()) if isinstance(trace.get("stage_ledger"), (list, tuple)) else ():
            if not isinstance(stage, Mapping):
                continue
            status = str(stage.get("status") or "").lower()
            kind = str(stage.get("kind") or stage.get("reason") or stage.get("stage") or "").lower()
            if "queue" in status or "queue" in kind:
                queue_timeout += int("timeout" in status or "timeout" in kind)
        calls = trace.get("llm_call_ledger", ()) if isinstance(trace.get("llm_call_ledger"), (list, tuple)) else ()
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            status = str(call.get("status") or "").lower()
            error_kind = str(call.get("error_kind") or call.get("failure_kind") or "").lower()
            if status in {"pending", ""}:
                continue
            call_id = str(call.get("call_id") or "")
            key = (str(trace.get("turn_id") or trace.get("trace_id") or ""), call_id)
            if call_id and key in seen_calls:
                continue
            if call_id:
                seen_calls.add(key)
            provider_total += 1
            if "cancel" in status or "cancel" in error_kind:
                provider_cancelled += 1
            elif "abandon" in status or "abandon" in error_kind:
                provider_abandoned += 1
            elif "timeout" in status or "timeout" in error_kind:
                provider_errors += 1
            elif status not in {"success", "ok", "completed", "succeeded"}:
                provider_errors += 1
            try:
                elapsed = float(call.get("elapsed_ms"))
                if math.isfinite(elapsed) and elapsed >= 0:
                    provider_latencies.append(elapsed)
            except (TypeError, ValueError):
                pass
    source = "trace" if items else "unknown"
    return {
        "reply_success_rate": _rate(reply_success, replies, source=source),
        "queue_timeout_rate": _rate(queue_timeout, len(items), source=source),
        "provider_error_rate": _rate(provider_errors, provider_total, source=source),
        "provider_cancel_rate": _rate(provider_cancelled, provider_total, source=source),
        "provider_abandoned_rate": _rate(provider_abandoned, provider_total, source=source),
        "provider_latency_p50": _metric(_percentile(provider_latencies, 0.50), len(provider_latencies), provider_total, source=source),
        "provider_latency_p95": _metric(_percentile(provider_latencies, 0.95), len(provider_latencies), provider_total, source=source),
        "provider_latency_p99": _metric(_percentile(provider_latencies, 0.99), len(provider_latencies), provider_total, source=source),
    }


def aggregate_business_kpis(
    db_path: str | Path | None = None,
    *,
    traces: Any = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, Any]:
    """Return safe KPI envelopes for a database and optional turn traces."""

    if db_path is not None and Path(db_path).suffix.lower() in {".jsonl", ".ndjson"} and traces is None:
        traces = iter_jsonl_traces(db_path)
        db_path = None
    if isinstance(traces, (str, Path)):
        traces = iter_jsonl_traces(traces)
    items = _trace_items(traces, start_ts, end_ts)
    trace_metrics = _trace_metric_values(items)
    result: dict[str, Any] = {"window": {"start_ts": start_ts, "end_ts": end_ts}, "metrics": {}}
    metrics = result["metrics"]
    for name, value in trace_metrics.items():
        value["window_start"] = start_ts
        value["window_end"] = end_ts
        value["sample_count"] = value.get("denominator") or 0
        metrics[name] = value

    if db_path is None or not Path(db_path).exists():
        db_source = "unknown"
        metrics.setdefault("outcome_completeness", _metric(None, 0, 0, source=db_source, reason="database_unavailable"))
        for name in ("memory_injection_rate", "memory_confidence_median", "learning_saved_rate", "learning_timeout_rate", "proactive_delivery_rate", "proactive_engagement_rate", "terminal_settlement_coverage"):
            metrics.setdefault(name, _metric(None, 0, 0, source=db_source, reason="database_unavailable"))
        result["source"] = "trace" if items else "unknown"
        return result

    with _connect_readonly(db_path) as conn:
        # Persisted message outcomes are authoritative when non-empty values exist.
        message_columns = _table_columns_for(conn, "messagelog") if _table_exists(conn, "messagelog") else set()
        if "outcome" in message_columns:
            rows = conn.execute("SELECT outcome FROM messagelog").fetchall()
            values = [str(row[0] or "").strip().lower() for row in rows]
            known = [value for value in values if value]
            success_values = {"success", "sent", "replied", "reply_sent", "completed", "succeeded"}
            success = sum(value in success_values for value in known)
            incomplete = len(values) - len(known)
            metrics["outcome_completeness"] = _rate(len(known), len(values), source="database", unknown_count=incomplete, reason="outcome_missing" if not known else None, status="partial" if incomplete else None)
            if known:
                metrics["reply_success_rate"] = _rate(success, len(known), source="database", unknown_count=incomplete, status="partial" if incomplete else None)
            else:
                metrics["reply_success_rate"] = _metric(None, 0, 0, source="database", reason="outcome_missing", unknown_count=len(values))
        else:
            metrics["outcome_completeness"] = _metric(None, 0, 0, source="unknown", reason="missing_table_or_column")

        if _table_exists(conn, "memoryretrievaltrace"):
            columns = _table_columns_for(conn, "memoryretrievaltrace")
            if {"selected_memory_ids", "confidence"}.issubset(columns):
                rows = conn.execute("SELECT selected_memory_ids, confidence FROM memoryretrievaltrace").fetchall()
                selected = 0
                confidence: list[float] = []
                for selected_ids, value in rows:
                    try:
                        selected += int(bool(selected_ids and str(selected_ids).strip() not in {"[]", "{}", "null"}))
                    except Exception:
                        pass
                    try:
                        number = float(value)
                        if math.isfinite(number):
                            confidence.append(number)
                    except (TypeError, ValueError):
                        pass
                metrics["memory_injection_rate"] = _rate(selected, len(rows), source="database")
                metrics["memory_confidence_median"] = _metric(_median(confidence), len(confidence), len(rows), source="database", unknown_count=len(rows) - len(confidence))
            else:
                metrics["memory_injection_rate"] = _metric(None, 0, 0, source="unknown", reason="missing_column")
                metrics["memory_confidence_median"] = _metric(None, 0, 0, source="unknown", reason="missing_column")
        else:
            metrics["memory_injection_rate"] = _metric(None, 0, 0, source="unknown", reason="missing_table")
            metrics["memory_confidence_median"] = _metric(None, 0, 0, source="unknown", reason="missing_table")

        if _table_exists(conn, "learning_mining_run"):
            rows = conn.execute("SELECT status FROM learning_mining_run").fetchall()
            statuses = [str(row[0] or "").lower() for row in rows]
            known_values = {"saved", "success", "succeeded", "completed", "timeout", "timed_out", "failed", "skipped", "blocked"}
            known_statuses = [status for status in statuses if status in known_values]
            saved = sum(status in {"saved", "success", "succeeded", "completed"} for status in known_statuses)
            timed_out = sum(status in {"timeout", "timed_out"} for status in known_statuses)
            unknown_count = len(statuses) - len(known_statuses)
            metrics["learning_saved_rate"] = _rate(saved, len(known_statuses), source="database", unknown_count=unknown_count)
            metrics["learning_timeout_rate"] = _rate(timed_out, len(known_statuses), source="database", unknown_count=unknown_count)
        else:
            metrics["learning_saved_rate"] = _metric(None, 0, 0, source="unknown", reason="missing_table")
            metrics["learning_timeout_rate"] = _metric(None, 0, 0, source="unknown", reason="missing_table")

        if _table_exists(conn, "proactive_scenario_delivery"):
            rows = conn.execute("SELECT status FROM proactive_scenario_delivery").fetchall()
            statuses = [str(row[0] or "").lower() for row in rows]
            known_values = {
                "generated", "filtered", "admitted", "claimed", "sending", "sent",
                "delivery_unknown", "delivered", "delivery_confirmed", "delivery_failed",
                "engaged", "responded", "replied", "skipped", "failed", "blocked",
                "queued", "retry_wait", "superseded", "cancelled", "expired",
            }
            known_statuses = [status for status in statuses if status in known_values]
            unknown_count = len(statuses) - len(known_statuses)
            # ``sent`` is only a local send commit.  A platform acknowledgement
            # is required before counting delivery; without one, expose a
            # partial/unknown metric instead of treating send as delivery.
            ack_statuses = {"delivered", "delivery_confirmed", "engaged", "responded", "replied"}
            ack_count = sum(status in ack_statuses for status in known_statuses)
            send_statuses = {"sent", "delivery_unknown", "delivered", "delivery_confirmed", "delivery_failed", "engaged", "responded", "replied"}
            send_attempts = sum(status in send_statuses for status in known_statuses)
            if ack_count or any(status in {"delivery_failed", "delivery_unknown"} for status in known_statuses):
                metrics["proactive_delivery_rate"] = _rate(
                    ack_count,
                    send_attempts,
                    source="database",
                    unknown_count=unknown_count + sum(status == "sent" for status in known_statuses),
                    status="partial" if any(status == "sent" for status in known_statuses) else None,
                    reason="acknowledgement_missing" if any(status == "sent" for status in known_statuses) else None,
                    warnings=("acknowledgement_missing",) if any(status == "sent" for status in known_statuses) else (),
                )
            else:
                metrics["proactive_delivery_rate"] = _metric(
                    None,
                    0,
                    send_attempts,
                    source="database",
                    reason="acknowledgement_missing",
                    unknown_count=unknown_count + send_attempts,
                    status="partial" if send_attempts else None,
                    warnings=("acknowledgement_missing",) if send_attempts else (),
                )
            metrics["proactive_engagement_rate"] = _metric(
                None,
                0,
                0,
                source="database",
                reason="engagement_not_recorded",
                unknown_count=len(statuses),
            )
        else:
            metrics["proactive_delivery_rate"] = _metric(None, 0, 0, source="unknown", reason="missing_table")
            metrics["proactive_engagement_rate"] = _metric(None, 0, 0, source="unknown", reason="missing_table")

        if _table_exists(conn, "background_task_ledger"):
            rows = conn.execute("SELECT status FROM background_task_ledger").fetchall()
            statuses = [str(row[0] or "").lower() for row in rows]
            terminal = {"replayed", "failed", "skipped", "exhausted", "completed", "cancelled", "blocked", "stale"}
            settled = sum(status in terminal for status in statuses)
            metrics["terminal_settlement_coverage"] = _rate(settled, len(statuses), source="database")
        else:
            metrics["terminal_settlement_coverage"] = _metric(None, 0, 0, source="unknown", reason="missing_table")

    for name, metric in metrics.items():
        if metric.get("source") == "trace" and db_path:
            metric.setdefault("warnings", []).append("database_unavailable_or_incomplete")
    result["source"] = "database" if db_path else ("trace" if items else "unknown")
    return result


def _table_columns_for(conn: sqlite3.Connection, table: str) -> set[str]:
    return _table_columns(conn, table)


build_business_kpis = aggregate_business_kpis

__all__ = ["aggregate_business_kpis", "build_business_kpis"]
