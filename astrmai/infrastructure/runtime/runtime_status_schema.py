"""Unified, read-only runtime diagnostics schema.

The schema is deliberately an adapter layer.  Existing component status
payloads remain source-of-truth and are kept intact; this module only maps
observed values into stable, JSON-safe fields.  Missing measurements remain
``None`` rather than being represented as zero.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

RUNTIME_STATUS_SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    """Return an explicit UTC ISO-8601 timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def json_safe(value: Any) -> Any:
    """Convert arbitrary status/config values into JSON-safe primitives."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    if dataclasses.is_dataclass(value):
        return json_safe(dataclasses.asdict(value))
    if hasattr(value, "as_dict") and callable(value.as_dict):
        try:
            return json_safe(value.as_dict())
        except Exception:
            return None
    return str(value)


def normalize_api_base(value: Any) -> str | None:
    """Normalize an API base while removing credentials and query material."""

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme or not parsed.netloc:
            return re.sub(r"[?#].*$", "", re.sub(r"(?<=://)[^/@]+@", "", raw)).rstrip("/") or None
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{hostname.lower()}{port}"
        path = (parsed.path or "").rstrip("/")
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
    except Exception:
        sanitized = re.sub(r"(?<=://)[^/@]+@", "", raw)
        return re.sub(r"[?#].*$", "", sanitized).rstrip("/") or None


def _lookup(config: Any, *paths: str) -> Any:
    for path in paths:
        current = config
        try:
            for part in path.split("."):
                if isinstance(current, Mapping):
                    current = current.get(part)
                else:
                    current = getattr(current, part)
            if current is not None:
                return current
        except Exception:
            continue
    return None


def effective_config_fingerprint(config: Any, infrastructure_settings: Any = None) -> str:
    """Return a stable hash of non-secret effective runtime settings."""

    settings = infrastructure_settings
    gateway = getattr(settings, "gateway", None) if settings is not None else None
    def _normalized_models(*paths: str) -> list[str]:
        values = _lookup(config, *paths)
        if values is None:
            values = []
        if not isinstance(values, (list, tuple, set, frozenset)):
            values = [values]
        return sorted({str(item).strip() for item in values if str(item).strip()})

    model_pools = {
        "fallback_models": _normalized_models("provider.fallback_models", "fallback_models"),
        "agent_models": _normalized_models("provider.agent_models", "agent_models"),
        "task_models": _normalized_models("provider.task_models", "task_models"),
        "vision_models": _normalized_models("provider.vision_models", "vision_models"),
        "embedding_models": _normalized_models("provider.embedding_models", "embedding_models"),
    }
    model_id = _lookup(config, "model", "model_id", "provider.model", "provider.model_id")
    if model_id is None:
        model_id = _lookup(gateway, "model_id") if gateway is not None else None
    provider_source = _lookup(
        config,
        "provider.source",
        "provider.provider_source",
        "provider.provider_source_id",
        "provider_source",
    )
    if provider_source is None:
        source_ids = {
            str(item).split("/", 1)[0]
            for models in model_pools.values()
            for item in models
            if "/" in str(item)
        }
        provider_source = sorted(source_ids)
    values = {
        "model_id": model_id,
        "provider_source": provider_source,
        "model_pools": model_pools,
        "api_base": normalize_api_base(_lookup(config, "provider.api_base", "api_base", "provider.base_url", "base_url")),
        "api_timeout": _lookup(gateway, "api_timeout") if gateway is not None else _lookup(config, "infra.api_timeout", "api_timeout"),
        "model_request_timeout": _lookup(config, "timing.model_request_timeout_sec", "model_request_timeout_sec"),
        "llm_retries": _lookup(gateway, "llm_retries") if gateway is not None else _lookup(config, "infra.llm_retries", "llm_retries"),
        "backoff_factor": _lookup(gateway, "backoff_factor") if gateway is not None else _lookup(config, "infra.backoff_factor", "backoff_factor"),
        "max_concurrent_llm_calls": _lookup(gateway, "max_concurrent_llm_calls") if gateway is not None else _lookup(config, "infra.max_concurrent_llm_calls", "max_concurrent_llm_calls"),
        "background_budget": _lookup(config, "infra.background_task_concurrency", "background_task_concurrency"),
        "background_queue_limit": _lookup(config, "infra.background_task_queue_limit", "background_task_queue_limit"),
        "background_wait_timeout": _lookup(config, "infra.background_task_wait_timeout_sec", "background_task_wait_timeout_sec"),
        "background_execution_timeout": _lookup(config, "infra.background_task_execution_timeout_sec", "background_task_execution_timeout_sec"),
        "attention_queue_timeout": _lookup(config, "timing.attention_background_slot_wait_timeout_sec", "attention_queue_timeout_sec"),
        "embedding_timeout": _lookup(config, "timing.embedding_timeout_sec", "embedding_timeout_sec"),
    }
    canonical = json.dumps(json_safe(values), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for source, target in (("p50", "p50_ms"), ("p95", "p95_ms"), ("p99", "p99_ms"), ("max", "max_ms"), ("count", "sample_size")):
        if value.get(source) is not None:
            result[target] = value.get(source)
        elif value.get(target) is not None:
            result[target] = value.get(target)
    return result or None


def _status_value(status: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if status.get(key) is not None:
            return status[key]
    return None


def _prefer(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def _numeric(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 2)


def aggregate_turn_telemetry(traces: Any) -> dict[str, Any]:
    """Aggregate observed Turn traces without double-counting retries.

    A trace is deduplicated by its stable turn/trace identity and each logical
    LLM call contributes at most one latency sample.  Per-attempt data is used
    only for retry/fallback counters.
    """
    if not isinstance(traces, (list, tuple)):
        return {"records": [], "sample_size": 0}
    records: list[dict[str, Any]] = []
    seen_turns: set[str] = set()
    for raw in traces:
        if not isinstance(raw, Mapping):
            continue
        identity = str(raw.get("turn_id") or raw.get("trace_id") or "")
        if identity and identity in seen_turns:
            continue
        if identity:
            seen_turns.add(identity)
        started = _numeric(raw.get("started_at"))
        elapsed = _numeric(raw.get("turn_total_elapsed_ms", raw.get("total_elapsed_ms")))
        finished = _numeric(raw.get("finished_at", raw.get("trace_finalized_at")))
        if finished is None and started is not None and elapsed is not None:
            finished = started + elapsed / 1000.0
        reply_stats = raw.get("reply_stats") if isinstance(raw.get("reply_stats"), Mapping) else {}
        tool_summary = raw.get("tool_ledger_summary") if isinstance(raw.get("tool_ledger_summary"), Mapping) else {}
        records.append(
            {
                "turn_id": raw.get("turn_id"),
                "trace_id": raw.get("trace_id"),
                "chat_id": raw.get("chat_id"),
                "thread_id": raw.get("thread_id"),
                "generation": raw.get("generation"),
                "started_at": started,
                "finished_at": finished,
                "total_elapsed_ms": elapsed,
                "terminal_status": raw.get("status"),
                "terminal_reason": raw.get("terminal_reason") or raw.get("reason"),
                "reply_sent": raw.get("reply_sent") if raw.get("reply_sent") is not None else reply_stats.get("reply_sent"),
                "fallback_sent": raw.get("fallback_sent") if raw.get("fallback_sent") is not None else reply_stats.get("fallback_sent"),
                "tool_action_count": raw.get("tool_action_count") if raw.get("tool_action_count") is not None else tool_summary.get("tool_call_count"),
                "deferred_replayed": raw.get("deferred_replayed") if raw.get("deferred_replayed") is not None else raw.get("background_task_ledger", {}).get("deferred_replayed") if isinstance(raw.get("background_task_ledger"), Mapping) else None,
            }
        )
    elapsed_values = [item["total_elapsed_ms"] for item in records if item.get("total_elapsed_ms") is not None]
    latest = records[-1] if records else None
    return {
        "records": records[-64:],
        "latest": latest,
        "sample_size": len(elapsed_values),
        "elapsed_ms_p50": _percentile(elapsed_values, 0.50),
        "elapsed_ms_p95": _percentile(elapsed_values, 0.95),
        "elapsed_ms_p99": _percentile(elapsed_values, 0.99),
        "total_elapsed_ms": elapsed_values[0] if len(elapsed_values) == 1 else None,
    }


def aggregate_provider_telemetry(traces: Any) -> dict[str, Any]:
    """Aggregate provider observations from logical call ledgers."""
    calls: list[Mapping[str, Any]] = []
    seen_calls: set[tuple[str, str]] = set()
    for trace in traces if isinstance(traces, (list, tuple)) else ():
        if not isinstance(trace, Mapping):
            continue
        turn_id = str(trace.get("turn_id") or trace.get("trace_id") or "")
        raw_calls = trace.get("llm_call_ledger")
        if not isinstance(raw_calls, (list, tuple)):
            continue
        for call in raw_calls:
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("call_id") or "")
            key = (turn_id, call_id) if call_id else (turn_id, str(id(call)))
            if key in seen_calls:
                continue
            seen_calls.add(key)
            calls.append(call)
    latencies: list[float] = []
    backoff_samples: list[float] = []
    success = errors = timeouts = retries = fallbacks = empty = 0
    for call in calls:
        status = str(call.get("status") or "").lower()
        error_kind = str(call.get("error_kind") or "").lower()
        if status in {"success", "completed", "ok"}:
            success += 1
        elif "timeout" in status or "timeout" in error_kind:
            timeouts += 1
        elif status not in {"pending", ""}:
            errors += 1
        elapsed = _numeric(call.get("elapsed_ms"))
        if elapsed is not None and status not in {"pending", ""}:
            latencies.append(elapsed)
        attempts = call.get("model_attempts")
        if isinstance(attempts, (list, tuple)):
            for attempt in attempts:
                if not isinstance(attempt, Mapping):
                    continue
                if _numeric(attempt.get("retry_index")) and float(attempt.get("retry_index") or 0) > 0:
                    retries += 1
                if bool(attempt.get("fallback")):
                    fallbacks += 1
                attempt_status = str(attempt.get("status") or "").lower()
                attempt_error = str(attempt.get("error_kind") or "").lower()
                if "empty" in attempt_status or "empty" in attempt_error:
                    empty += 1
        if "empty" in status or "empty" in error_kind:
            empty += 1
    for trace in traces if isinstance(traces, (list, tuple)) else ():
        if not isinstance(trace, Mapping):
            continue
        stages = trace.get("stage_ledger")
        if not isinstance(stages, (list, tuple)):
            continue
        for stage in stages:
            if not isinstance(stage, Mapping) or str(stage.get("stage") or "") != "gateway.retry_backoff":
                continue
            if str(stage.get("status") or "").lower() == "pending":
                continue
            elapsed = _numeric(stage.get("elapsed_ms"))
            if elapsed is not None:
                backoff_samples.append(elapsed)
    return {
        "provider_request_count": len(calls) or None,
        "provider_success_count": success or None,
        "provider_error_count": errors or None,
        "provider_timeout_count": timeouts or None,
        "provider_latency_p50_ms": _percentile(latencies, 0.50),
        "provider_latency_p95_ms": _percentile(latencies, 0.95),
        "provider_latency_p99_ms": _percentile(latencies, 0.99),
        "provider_latency_max_ms": max(latencies) if latencies else None,
        "retry_count": retries or None,
        "retry_backoff_ms": _percentile(backoff_samples, 0.95),
        "fallback_count": fallbacks or None,
        "empty_response_count": empty or None,
        "measurement_scope": "runtime_turn_traces",
    }


def aggregate_stage_waits(traces: Any) -> dict[str, Any]:
    """Map stage ledger samples to queue/lock wait metrics."""
    buckets: dict[str, list[float]] = {
        "gateway_semaphore_wait_ms": [],
        "lane_lock_wait_ms": [],
        "sys2_lock_wait_ms": [],
        "executor_lock_wait_ms": [],
        "attention_queue_wait_ms": [],
    }
    stage_names = {
        "gateway_semaphore_wait_ms": {"gateway.semaphore_wait", "gateway.tool_semaphore_wait", "gateway.background_semaphore_wait", "gateway.compaction_semaphore_wait"},
        "lane_lock_wait_ms": {"lane.lock_wait", "gateway.lane_lock_wait"},
        "sys2_lock_wait_ms": {"system2.chat_lock_wait", "system2.chat_lock_resolve"},
        "executor_lock_wait_ms": {"executor.chat_lock_wait"},
        "attention_queue_wait_ms": {"attention.queue_wait", "attention.judge_queue_wait"},
    }
    for trace in traces if isinstance(traces, (list, tuple)) else ():
        if not isinstance(trace, Mapping):
            continue
        stages = trace.get("stage_ledger")
        if not isinstance(stages, (list, tuple)):
            continue
        for stage in stages:
            if not isinstance(stage, Mapping):
                continue
            name = str(stage.get("stage") or "")
            if str(stage.get("status") or "").lower() == "pending":
                continue
            elapsed = _numeric(stage.get("elapsed_ms"))
            if elapsed is None:
                continue
            for bucket, names in stage_names.items():
                if name in names:
                    buckets[bucket].append(elapsed)
                    break
    return {name: _percentile(values, 0.95) for name, values in buckets.items()}


def _sum_mapping(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    values = []
    for item in value.values():
        try:
            values.append(max(0, int(item or 0)))
        except (TypeError, ValueError):
            continue
    return sum(values) if values else None


def build_runtime_status_schema(
    *,
    runtime_status: Mapping[str, Any] | None = None,
    config: Any = None,
    infrastructure_settings: Any = None,
    attention: Mapping[str, Any] | None = None,
    gateway: Mapping[str, Any] | None = None,
    background: Mapping[str, Any] | None = None,
    memory: Mapping[str, Any] | None = None,
    shutdown: Mapping[str, Any] | None = None,
    turns: Mapping[str, Any] | None = None,
    stages: Mapping[str, Any] | None = None,
    generated_at: str | None = None,
    traces: Any = None,
) -> dict[str, Any]:
    """Build the versioned envelope without mutating any source status."""

    validation_errors: list[str] = []

    def _mapping(name: str, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            validation_errors.append(f"{name}:expected_mapping")
            return {}
        return dict(value)

    runtime = _mapping("runtime_status", runtime_status)
    attention = _mapping("attention", attention)
    gateway = _mapping("gateway", gateway)
    background = _mapping("background", background)
    memory = _mapping("memory", memory)
    shutdown = _mapping("shutdown", shutdown)
    turns_input = _mapping("turns", turns)
    stages_input = _mapping("stages", stages)
    telemetry_turns = aggregate_turn_telemetry(traces)
    telemetry_provider = aggregate_provider_telemetry(traces)
    telemetry_waits = aggregate_stage_waits(traces)

    lifecycle = "unknown"
    if runtime.get("shutdown_final_status") == "degraded":
        lifecycle = "degraded"
    elif runtime.get("is_running") and runtime.get("accepting_events"):
        lifecycle = "ready"
    elif runtime.get("boot_phase"):
        candidate = str(runtime.get("boot_phase"))
        allowed = {
            "created", "initializing", "ready", "running", "degraded", "blocked",
            "retry_wait", "shutdown_requested", "draining", "shutdown_complete", "failed",
        }
        if candidate in allowed:
            lifecycle = candidate
        else:
            validation_errors.append("runtime_status.boot_phase:invalid_state")

    background_queue_wait_by_kind = background.get("queue_wait_ms_by_kind")
    background_queue_wait = background.get("queue_wait_ms")
    queue_wait = {
        "attention_queue_wait_ms": _prefer(_status_value(attention, "queue_wait_ms", "judge_queue_wait_ms"), telemetry_waits["attention_queue_wait_ms"]),
        "background_budget_queue_wait_ms": background_queue_wait,
        "background_budget_queue_wait_ms_by_kind": background_queue_wait_by_kind,
        "gateway_semaphore_wait_ms": _prefer(_status_value(gateway, "semaphore_wait_ms"), telemetry_waits["gateway_semaphore_wait_ms"]),
        "lane_lock_wait_ms": _prefer(_status_value(gateway, "lane_lock_wait_ms"), telemetry_waits["lane_lock_wait_ms"]),
        "sys2_lock_wait_ms": _prefer(_status_value(gateway, "sys2_lock_wait_ms"), telemetry_waits["sys2_lock_wait_ms"]),
        "executor_lock_wait_ms": _prefer(_status_value(gateway, "executor_lock_wait_ms"), telemetry_waits["executor_lock_wait_ms"]),
    }
    provider = {
        "measurement_scope": gateway.get("measurement_scope") if any(key in gateway for key in ("provider_request_count", "request_count", "provider_latency_p95_ms")) else telemetry_provider.get("measurement_scope"),
        "provider_request_count": _prefer(_status_value(gateway, "provider_request_count", "request_count"), telemetry_provider["provider_request_count"]),
        "provider_success_count": _prefer(_status_value(gateway, "provider_success_count", "success_count"), telemetry_provider["provider_success_count"]),
        "provider_error_count": _prefer(_status_value(gateway, "provider_error_count", "error_count"), telemetry_provider["provider_error_count"]),
        "provider_timeout_count": _prefer(_status_value(gateway, "provider_timeout_count", "timeout_count"), telemetry_provider["provider_timeout_count"]),
        "provider_latency_p50_ms": _prefer(_status_value(gateway, "provider_latency_p50_ms"), telemetry_provider["provider_latency_p50_ms"]),
        "provider_latency_p95_ms": _prefer(_status_value(gateway, "provider_latency_p95_ms"), telemetry_provider["provider_latency_p95_ms"]),
        "provider_latency_p99_ms": _prefer(_status_value(gateway, "provider_latency_p99_ms"), telemetry_provider["provider_latency_p99_ms"]),
        "provider_latency_max_ms": _prefer(_status_value(gateway, "provider_latency_max_ms"), telemetry_provider["provider_latency_max_ms"]),
        "retry_count": _prefer(_status_value(gateway, "retry_count"), telemetry_provider["retry_count"]),
        "retry_backoff_ms": _prefer(_status_value(gateway, "retry_backoff_ms"), telemetry_provider["retry_backoff_ms"]),
        "fallback_count": _prefer(_status_value(gateway, "fallback_count"), telemetry_provider["fallback_count"]),
        "empty_response_count": _prefer(_status_value(gateway, "empty_response_count"), telemetry_provider["empty_response_count"]),
    }
    background_schema = {
        "active": _status_value(background, "active"),
        "queued": _status_value(background, "queued"),
        "deferred": _status_value(background, "deferred", "deferred_tasks"),
        "physical_owner_count": _status_value(background, "physical_owner_count", "physical"),
        "queue_timeout_count": _prefer(_status_value(background, "timed_out", "queue_timeout_count"), _sum_mapping(background.get("timed_out_by_kind"))),
        "queue_full_count": _prefer(_status_value(background, "rejected", "queue_full_count"), _sum_mapping(background.get("rejected_by_kind"))),
        "cancelled_count": _prefer(_status_value(background, "cancelled", "cancelled_count"), _sum_mapping(background.get("cancelled_by_kind"))),
        "retry_wait_count": _status_value(background, "retry_wait_count"),
        "late_completed_count": _prefer(_status_value(background, "late_completed", "late_completed_count"), _sum_mapping(background.get("late_completed_by_kind"))),
        "shutdown_rejected_count": _prefer(_status_value(background, "shutdown_rejected", "shutdown_rejected_count"), _sum_mapping(background.get("shutdown_rejected_by_kind"))),
        "active_by_kind": background.get("active_by_kind"),
        "queued_by_kind": background.get("queued_by_kind"),
        "deferred_by_kind": background.get("deferred_by_kind"),
        "failed_by_kind": background.get("failed_by_kind"),
        "cancelled_by_kind": background.get("cancelled_by_kind"),
        "late_completed_by_kind": background.get("late_completed_by_kind"),
        "queue_wait_ms_by_kind": background_queue_wait_by_kind,
    }
    turns_schema = {
        "active": _status_value(runtime, "active_turn_task_count") if turns is None else _status_value(turns_input, "active"),
        "turn_id": _status_value(turns_input, "turn_id") or (telemetry_turns.get("latest") or {}).get("turn_id"),
        "trace_id": _status_value(turns_input, "trace_id") or (telemetry_turns.get("latest") or {}).get("trace_id"),
        "started_at": _status_value(turns_input, "started_at") or (telemetry_turns.get("latest") or {}).get("started_at"),
        "finished_at": _status_value(turns_input, "finished_at") or (telemetry_turns.get("latest") or {}).get("finished_at"),
        "total_elapsed_ms": _status_value(turns_input, "total_elapsed_ms") if _status_value(turns_input, "total_elapsed_ms") is not None else telemetry_turns.get("total_elapsed_ms"),
        "total_elapsed_p50_ms": _status_value(turns_input, "elapsed_ms_p50") if _status_value(turns_input, "elapsed_ms_p50") is not None else telemetry_turns.get("elapsed_ms_p50"),
        "total_elapsed_p95_ms": _status_value(turns_input, "elapsed_ms_p95") if _status_value(turns_input, "elapsed_ms_p95") is not None else telemetry_turns.get("elapsed_ms_p95"),
        "total_elapsed_p99_ms": _status_value(turns_input, "elapsed_ms_p99") if _status_value(turns_input, "elapsed_ms_p99") is not None else telemetry_turns.get("elapsed_ms_p99"),
        "terminal_reason": _status_value(turns_input, "terminal_reason") or (telemetry_turns.get("latest") or {}).get("terminal_reason"),
        "reply_sent": _status_value(turns_input, "reply_sent") if _status_value(turns_input, "reply_sent") is not None else (telemetry_turns.get("latest") or {}).get("reply_sent"),
        "fallback_sent": _status_value(turns_input, "fallback_sent") if _status_value(turns_input, "fallback_sent") is not None else (telemetry_turns.get("latest") or {}).get("fallback_sent"),
        "tool_action_count": _status_value(turns_input, "tool_action_count") if _status_value(turns_input, "tool_action_count") is not None else (telemetry_turns.get("latest") or {}).get("tool_action_count"),
        "deferred_replayed": _status_value(turns_input, "deferred_replayed") if _status_value(turns_input, "deferred_replayed") is not None else (telemetry_turns.get("latest") or {}).get("deferred_replayed"),
        "terminal_status": _status_value(turns_input, "terminal_status") or (telemetry_turns.get("latest") or {}).get("terminal_status"),
        "sample_size": _status_value(turns_input, "sample_size") if _status_value(turns_input, "sample_size") is not None else telemetry_turns.get("sample_size"),
        "measurement_scope": "runtime_turn_traces" if telemetry_turns.get("sample_size") else None,
        "records": telemetry_turns.get("records") or None,
    }
    resource_state = _status_value(memory, "resource_state", "vector_resource_state")
    if resource_state is None and isinstance(memory.get("resources"), list):
        states = {str(item.get("state")) for item in memory["resources"] if isinstance(item, Mapping) and item.get("state")}
        resource_state = next(iter(states)) if len(states) == 1 else "mixed" if states else None
    memory_schema = {
        "resource_state": resource_state,
        "lifecycle_status": _status_value(memory, "resource_lifecycle_status") or resource_state,
        "health_status": _status_value(memory, "health_status", "diagnostics_status", "circuit_state"),
        "generation": _status_value(memory, "generation", "runtime_generation"),
        "index_dimension": memory.get("index_dimension"),
        "diagnostics": memory,
    }
    shutdown_schema = {
        "status": _status_value(shutdown, "status", "shutdown_final_status") or runtime.get("shutdown_final_status"),
        "pending": _status_value(shutdown, "remaining", "pending", "has_pending"),
        "generation": _status_value(shutdown, "shutdown_generation") or runtime.get("shutdown_generation"),
        "pending_by_kind": shutdown.get("remaining_by_kind"),
    }
    return json_safe({
        "runtime_status_schema_version": RUNTIME_STATUS_SCHEMA_VERSION,
        "effective_config_fingerprint": effective_config_fingerprint(config, infrastructure_settings),
        "generated_at": generated_at or utc_now_iso(),
        "runtime_generation": runtime.get("runtime_generation"),
        "lifecycle_status": lifecycle,
        "turns": turns_schema,
        "stages": stages_input,
        "queues": queue_wait,
        "provider": provider,
        "background": background_schema,
        "memory": memory_schema,
        "shutdown": shutdown_schema,
        "validation_errors": validation_errors or None,
    })


__all__ = [
    "RUNTIME_STATUS_SCHEMA_VERSION",
    "aggregate_provider_telemetry",
    "aggregate_stage_waits",
    "aggregate_turn_telemetry",
    "build_runtime_status_schema",
    "effective_config_fingerprint",
    "json_safe",
    "normalize_api_base",
    "utc_now_iso",
]
