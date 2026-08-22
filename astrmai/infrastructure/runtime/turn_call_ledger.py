from __future__ import annotations

import asyncio
import copy
import hashlib
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


CALL_LEDGER_KEY = "astrmai_llm_call_ledger"
CONTEXT_BLOCK_STATS_KEY = "astrmai_context_block_stats"
REPLY_STATS_KEY = "astrmai_reply_stats"
STAGE_LEDGER_KEY = "astrmai_stage_ledger"
BACKGROUND_TASK_LEDGER_KEY = "astrmai_background_task_ledger"
VISION_OBSERVATION_KEY = "astrmai_vision_observation"
TELEMETRY_CONTEXT_KEY = "astrmai_turn_telemetry"
TRACE_SCHEMA_VERSION = 2
INSTRUMENTATION_VERSION = "2026.08.01-v3"
_MAX_ENTRIES = 128
_MAX_BLOCKS = 64
_CURRENT_TELEMETRY: ContextVar["TurnTelemetryContext | None"] = ContextVar(
    "astrmai_turn_telemetry",
    default=None,
)


@dataclass(slots=True)
class TurnTelemetryContext:
    turn_id: str
    trace_id: str
    chat_id: str = ""
    thread_id: str = ""
    mode: str = ""
    generation: int = 0
    message_id_hash: str = ""
    started_at: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    deadline_monotonic: float = 0.0
    total_budget_sec: float = 0.0
    main_reply_reserve_sec: float = 0.0
    finalized_at: float = 0.0
    reply_completed_at: float = 0.0
    trace_finalized_at: float = 0.0
    sequence: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    context_blocks: list[dict[str, Any]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
    background_tasks: list[dict[str, Any]] = field(default_factory=list)
    vision_observation: dict[str, Any] = field(default_factory=dict)
    reply_stats: dict[str, Any] = field(default_factory=dict)

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def bind_identity(self, identity: Any) -> None:
        if identity is None:
            return
        self.chat_id = str(getattr(identity, "chat_id", self.chat_id) or self.chat_id)
        self.thread_id = str(getattr(identity, "thread_id", self.thread_id) or self.thread_id)
        self.mode = str(getattr(identity, "mode", self.mode) or self.mode)
        self.generation = int(getattr(identity, "generation", self.generation) or self.generation)
        message_ids = tuple(getattr(identity, "input_message_ids", ()) or ())
        if message_ids:
            self.message_id_hash = _text_hash(message_ids[0])


def _safe_len(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    try:
        return len(value)
    except Exception:
        return len(str(value))


def _text_hash(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def _event_extra(event: Any, key: str, default: Any = None) -> Any:
    if event is None or not hasattr(event, "get_extra"):
        return default
    try:
        return event.get_extra(key, default)
    except Exception:
        return default


def ensure_turn_telemetry(event: Any) -> TurnTelemetryContext:
    existing = _event_extra(event, TELEMETRY_CONTEXT_KEY)
    if isinstance(existing, TurnTelemetryContext):
        existing.bind_identity(_event_extra(event, "astrmai_turn_identity"))
        return existing

    trace_id = str(_event_extra(event, "astrmai_trace_id", "") or "").strip()
    if not trace_id:
        trace_id = uuid.uuid4().hex[:12]
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("astrmai_trace_id", trace_id)
    identity = _event_extra(event, "astrmai_turn_identity")
    context = TurnTelemetryContext(
        turn_id=trace_id,
        trace_id=trace_id,
        chat_id=str(getattr(identity, "chat_id", "") or _event_extra(event, "chat_id", "") or ""),
        thread_id=str(getattr(identity, "thread_id", "") or _event_extra(event, "astrmai_turn_thread_id", "") or ""),
        mode=str(getattr(identity, "mode", "") or _event_extra(event, "astrmai_turn_mode", "") or ""),
        generation=int(getattr(identity, "generation", 0) or _event_extra(event, "astrmai_turn_generation", 0) or 0),
        started_at=float(
            getattr(identity, "created_at", 0.0)
            or _event_extra(event, "astrmai_turn_created_at", 0.0)
            or time.time()
        ),
    )
    context.bind_identity(identity)
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra(TELEMETRY_CONTEXT_KEY, context)
        event.set_extra(CALL_LEDGER_KEY, context.calls)
        event.set_extra(CONTEXT_BLOCK_STATS_KEY, context.context_blocks)
        event.set_extra(STAGE_LEDGER_KEY, context.stages)
        event.set_extra(BACKGROUND_TASK_LEDGER_KEY, context.background_tasks)
        event.set_extra(VISION_OBSERVATION_KEY, context.vision_observation)
        event.set_extra(REPLY_STATS_KEY, context.reply_stats)
    return context


def bind_turn_telemetry_identity(event: Any) -> TurnTelemetryContext:
    context = ensure_turn_telemetry(event)
    context.bind_identity(_event_extra(event, "astrmai_turn_identity"))
    return context


def configure_turn_budget(
    event: Any,
    *,
    total_budget_sec: float,
    main_reply_reserve_sec: float,
) -> TurnTelemetryContext:
    context = ensure_turn_telemetry(event)
    total = max(1.0, float(total_budget_sec or 0.0))
    reserve = max(0.0, min(float(main_reply_reserve_sec or 0.0), total))
    elapsed_wall = max(0.0, time.time() - float(context.started_at or time.time()))
    remaining = max(0.0, total - elapsed_wall)
    context.total_budget_sec = total
    context.main_reply_reserve_sec = reserve
    context.deadline_monotonic = time.monotonic() + remaining
    return context


def remaining_turn_budget(
    event: Any = None,
    *,
    reserve_for_reply: bool = False,
) -> float | None:
    context = current_turn_telemetry(event)
    if context is None or context.deadline_monotonic <= 0.0:
        return None
    reserve = context.main_reply_reserve_sec if reserve_for_reply else 0.0
    return max(0.0, context.deadline_monotonic - time.monotonic() - reserve)


def clamp_timeout_to_turn_budget(
    event: Any,
    requested_timeout_sec: float,
    *,
    reserve_for_reply: bool = False,
) -> float:
    requested = max(0.0, float(requested_timeout_sec or 0.0))
    remaining = remaining_turn_budget(event, reserve_for_reply=reserve_for_reply)
    if remaining is None:
        return requested
    return max(0.0, min(requested, remaining))


def finalize_turn_telemetry(event: Any = None, *, outcome: str = "") -> dict[str, int]:
    context = current_turn_telemetry(event)
    if context is None:
        return {"calls": 0, "stages": 0}
    now = time.time()
    finalized_calls = 0
    finalized_stages = 0
    for entry in context.calls:
        if str(entry.get("status", "")) != "pending":
            continue
        started_at = float(entry.get("started_at", now) or now)
        entry.update(
            {
                "status": "abandoned",
                "finished_at": now,
                "elapsed_ms": round(max(0.0, now - started_at) * 1000, 1),
                "error_kind": "turn_finalized",
                "error_hash": _text_hash(outcome),
            }
        )
        finalized_calls += 1
    for entry in context.stages:
        if str(entry.get("status", "")) != "pending":
            continue
        started_at = float(entry.get("started_at", now) or now)
        entry.update(
            {
                "status": "abandoned",
                "finished_at": now,
                "elapsed_ms": round(max(0.0, now - started_at) * 1000, 1),
                "reason": "turn_finalized",
            }
        )
        finalized_stages += 1
    context.finalized_at = now
    context.trace_finalized_at = now
    return {"calls": finalized_calls, "stages": finalized_stages}


def current_turn_telemetry(event: Any = None) -> TurnTelemetryContext | None:
    if event is not None:
        existing = _event_extra(event, TELEMETRY_CONTEXT_KEY)
        if isinstance(existing, TurnTelemetryContext):
            return existing
        if hasattr(event, "set_extra") and hasattr(event, "get_extra"):
            return ensure_turn_telemetry(event)
    return _CURRENT_TELEMETRY.get()


@contextmanager
def turn_telemetry_scope(event: Any):
    context = ensure_turn_telemetry(event)
    token = _CURRENT_TELEMETRY.set(context)
    try:
        yield context
    finally:
        _CURRENT_TELEMETRY.reset(token)


def detach_turn_telemetry() -> None:
    """常驻后台任务入口调用：斩断随 asyncio.create_task 复制继承的 turn telemetry。

    懒启动的常驻 worker 若在某个 turn 的处理上下文（main.py turn_telemetry_scope）中
    创建，将永久携带该轮 deadline，导致 worker 内所有 event=None 的网关调用在原轮
    预算耗尽后集体秒败（turn_deadline_exhausted）并把账记进陈旧 turn。
    contextvar 是 task 私有的，置空不影响创建方。
    """
    _CURRENT_TELEMETRY.set(None)


def rebind_turn_telemetry(event: Any) -> TurnTelemetryContext:
    """长驻 worker 每处理一个新批次时调用：把 telemetry contextvar 重绑到批次锚点事件。

    使排水循环中晚到批次的网关调用按自己的 turn 预算钳制、账本落回正确的 turn，
    而不是沿用 worker 创建时刻旧 turn 的 deadline 与 ledger。
    """
    context = ensure_turn_telemetry(event)
    _CURRENT_TELEMETRY.set(context)
    return context


def _timing_coverage_summary(context: TurnTelemetryContext, captured_at: float) -> dict[str, Any]:
    turn_start = float(context.started_at or captured_at)
    turn_end = max(turn_start, float(captured_at or turn_start))
    intervals: list[tuple[float, float]] = []
    for entries in (context.calls, context.stages, context.background_tasks):
        for entry in entries:
            try:
                started_at = max(turn_start, float(entry.get("started_at", 0.0) or 0.0))
                finished_at = min(turn_end, float(entry.get("finished_at", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
            if finished_at > started_at:
                intervals.append((started_at, finished_at))
    intervals.sort()
    merged: list[list[float]] = []
    for started_at, finished_at in intervals:
        if not merged or started_at > merged[-1][1]:
            merged.append([started_at, finished_at])
        else:
            merged[-1][1] = max(merged[-1][1], finished_at)
    instrumented_sec = sum(end - start for start, end in merged)
    total_sec = max(0.0, turn_end - turn_start)
    gaps: list[float] = []
    cursor = turn_start
    for started_at, finished_at in merged:
        gaps.append(max(0.0, started_at - cursor))
        cursor = max(cursor, finished_at)
    gaps.append(max(0.0, turn_end - cursor))
    return {
        "instrumented_ms": round(instrumented_sec * 1000.0, 1),
        "unattributed_ms": round(max(0.0, total_sec - instrumented_sec) * 1000.0, 1),
        "coverage_ratio": round(instrumented_sec / total_sec, 4) if total_sec else 0.0,
        "first_observed_delay_ms": round((merged[0][0] - turn_start) * 1000.0, 1) if merged else round(total_sec * 1000.0, 1),
        "post_last_observed_delay_ms": round((turn_end - merged[-1][1]) * 1000.0, 1) if merged else round(total_sec * 1000.0, 1),
        "max_unattributed_gap_ms": round(max(gaps, default=0.0) * 1000.0, 1),
        "interval_count": len(merged),
    }


def turn_telemetry_snapshot(
    event: Any = None,
    *,
    captured_at: float | None = None,
) -> dict[str, Any]:
    context = current_turn_telemetry(event)
    if context is None:
        return {}
    snapshot_captured_at = (
        time.time()
        if captured_at is None
        else max(float(context.started_at or 0.0), float(captured_at))
    )
    if captured_at is not None and context.total_budget_sec > 0.0:
        remaining = max(
            0.0,
            context.total_budget_sec
            - max(0.0, snapshot_captured_at - context.started_at),
        )
    else:
        remaining = remaining_turn_budget(event)
    reply_completed_elapsed_ms = (
        round(max(0.0, context.reply_completed_at - context.started_at) * 1000, 1)
        if context.reply_completed_at
        else None
    )
    trace_finalized_at = context.trace_finalized_at or context.finalized_at
    trace_finalized_elapsed_ms = (
        round(max(0.0, trace_finalized_at - context.started_at) * 1000, 1)
        if trace_finalized_at
        else None
    )
    trace_finalize_lag_ms = (
        round(max(0.0, trace_finalized_at - context.reply_completed_at) * 1000, 1)
        if trace_finalized_at and context.reply_completed_at
        else None
    )
    history_policy = _event_extra(event, "astrmai_dialog_history_policy", {})
    if not isinstance(history_policy, Mapping):
        history_policy = {}
    history_debug_enabled = bool(
        _event_extra(event, "astrmai_group_history_debug_trace_enabled", False)
    )
    history_policy_summary = {
        "history_mode": str(history_policy.get("history_mode", "") or ""),
        "group_id": str(history_policy.get("group_id", "") or ""),
        "topic_epoch": int(history_policy.get("topic_epoch", 0) or 0),
        "current_sender_id": str(history_policy.get("current_sender_id", "") or ""),
        "approved_event_ids": [
            str(item)
            for item in list(history_policy.get("approved_event_ids", []) or [])
            if str(item)
        ][:16],
        "provider_session_allowed": bool(history_policy.get("allow_provider_session", False)),
        "rotation_reason": str(history_policy.get("rotation_reason", "") or ""),
        "debug_enabled": history_debug_enabled,
    }
    if history_debug_enabled:
        history_policy_summary.update(
            {
                "thread_key": str(history_policy.get("thread_key", "") or ""),
                "topic_age_seconds": round(
                    max(0.0, float(history_policy.get("topic_age_seconds", 0.0) or 0.0)),
                    3,
                ),
                "continuity_evidence": [
                    str(item)
                    for item in list(history_policy.get("continuity_evidence", []) or [])
                    if str(item)
                ][:12],
            }
        )
    group_context_raw = _event_extra(event, "astrmai_group_context_snapshot", {})
    if not isinstance(group_context_raw, Mapping):
        group_context_raw = {}
    group_context_snapshot = {
        "watermark": int(group_context_raw.get("watermark", 0) or 0),
        "candidate_count": int(group_context_raw.get("candidate_count", 0) or 0),
        "selected_count": int(group_context_raw.get("selected_count", 0) or 0),
        "actor_tail_count": int(group_context_raw.get("actor_tail_count", 0) or 0),
        "pending_direct_count": int(
            group_context_raw.get("pending_direct_count", 0) or 0
        ),
        "bot_turn_count": int(group_context_raw.get("bot_turn_count", 0) or 0),
        "social_incident_count": int(
            group_context_raw.get("social_incident_count", 0) or 0
        ),
        "echo_filtered_count": int(
            group_context_raw.get("echo_filtered_count", 0) or 0
        ),
        "topic_bridge": bool(group_context_raw.get("topic_bridge", False)),
        "topic_epoch": int(group_context_raw.get("topic_epoch", 0) or 0),
        "topic_participant_ids": [
            str(item)
            for item in list(group_context_raw.get("topic_participant_ids", []) or [])
            if str(item)
        ][:16],
        "summary_source_event_ids": [
            str(item)
            for item in list(group_context_raw.get("summary_source_event_ids", []) or [])
            if str(item)
        ][:24],
        "unresolved_actor_ids": [
            str(item)
            for item in list(group_context_raw.get("unresolved_actor_ids", []) or [])
            if str(item)
        ][:16],
        "last_committed_target_actor_id": str(
            group_context_raw.get("last_committed_target_actor_id", "") or ""
        ),
        "exclusion_reasons": [
            str(item)[:80]
            for item in list(group_context_raw.get("exclusion_reasons", []) or [])
            if str(item)
        ][:12],
        "text_chars": int(group_context_raw.get("text_chars", 0) or 0),
        "stale_action": str(
            _event_extra(event, "astrmai_group_stale_action", "") or ""
        )[:40],
        "focus_watermark": int(
            _event_extra(event, "astrmai_group_focus_watermark", 0) or 0
        ),
        "pending_superseded_count": int(
            _event_extra(event, "astrmai_group_pending_superseded_count", 0) or 0
        ),
    }
    return {
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "instrumentation_version": INSTRUMENTATION_VERSION,
        "turn_id": context.turn_id,
        "trace_id": context.trace_id,
        "chat_id": context.chat_id,
        "thread_id": context.thread_id,
        "mode": context.mode,
        "generation": context.generation,
        "message_id_hash": context.message_id_hash,
        "started_at": context.started_at,
        "captured_at": snapshot_captured_at,
        "total_elapsed_ms": round(
            max(0.0, snapshot_captured_at - context.started_at) * 1000,
            1,
        ),
        "reply_completed_at": context.reply_completed_at or None,
        "trace_finalized_at": trace_finalized_at or None,
        "reply_completed_elapsed_ms": reply_completed_elapsed_ms,
        "trace_finalized_elapsed_ms": trace_finalized_elapsed_ms,
        "trace_finalize_lag_ms": trace_finalize_lag_ms,
        "budget": {
            "total_budget_sec": context.total_budget_sec,
            "main_reply_reserve_sec": context.main_reply_reserve_sec,
            "remaining_ms": round(max(0.0, float(remaining or 0.0)) * 1000, 1),
            "exhausted": remaining is not None and remaining <= 0.0,
        },
        "timing_coverage": _timing_coverage_summary(context, snapshot_captured_at),
        "llm_call_ledger": copy.deepcopy(context.calls[-_MAX_ENTRIES:]),
        "context_block_stats": copy.deepcopy(context.context_blocks[-16:]),
        "stage_ledger": copy.deepcopy(context.stages[-_MAX_ENTRIES:]),
        "background_task_ledger": copy.deepcopy(context.background_tasks[-_MAX_ENTRIES:]),
        "vision_observation": copy.deepcopy(context.vision_observation),
        "tool_ledger_summary": _tool_ledger_summary(event, context.calls),
        "reply_stats": copy.deepcopy(context.reply_stats),
        "relationship_observation": {
            "event_id": str(_event_extra(event, "astrmai_relationship_event_id", "") or ""),
            "event_type": str(_event_extra(event, "astrmai_relationship_event_type", "") or ""),
            "source": str(_event_extra(event, "astrmai_relationship_event_source", "") or ""),
            "confidence": float(_event_extra(event, "astrmai_relationship_event_confidence", 0.0) or 0.0),
            "disposition": str(_event_extra(event, "astrmai_relationship_event_disposition", "") or ""),
            "policy_version": str(_event_extra(event, "astrmai_relationship_policy_version", "") or ""),
            "delta": copy.deepcopy(_event_extra(event, "astrmai_relationship_delta", {}) or {}),
        },
        "expression_observation": {
            "bot_expression_tag": str(_event_extra(event, "astrmai_bot_expression_tag", "") or ""),
            "source": str(_event_extra(event, "astrmai_expression_source", "") or ""),
            "disposition": str(_event_extra(event, "astrmai_expression_disposition", "") or ""),
            "meme_tag": str(_event_extra(event, "astrmai_meme_tag", "") or ""),
            "bot_state_snapshot": copy.deepcopy(_event_extra(event, "astrmai_bot_state_snapshot", {}) or {}),
        },
        "dialog_history_policy": history_policy_summary,
        "group_context_snapshot": group_context_snapshot,
    }


def _read_list(event: Any, key: str) -> list[dict[str, Any]]:
    context = current_turn_telemetry(event)
    if context is not None:
        if key == CALL_LEDGER_KEY:
            return context.calls
        if key == CONTEXT_BLOCK_STATS_KEY:
            return context.context_blocks
        if key == STAGE_LEDGER_KEY:
            return context.stages
    if event is None or not hasattr(event, "get_extra"):
        return []
    value = event.get_extra(key, [])
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _write_list(event: Any, key: str, value: Iterable[Mapping[str, Any]]) -> None:
    normalized = [dict(item) for item in list(value)[-_MAX_ENTRIES:]]
    context = current_turn_telemetry(event)
    if context is not None:
        if key == CALL_LEDGER_KEY:
            context.calls[:] = normalized
        elif key == CONTEXT_BLOCK_STATS_KEY:
            context.context_blocks[:] = normalized
        elif key == STAGE_LEDGER_KEY:
            context.stages[:] = normalized
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra(key, getattr(
                context,
                "calls" if key == CALL_LEDGER_KEY else "context_blocks" if key == CONTEXT_BLOCK_STATS_KEY else "stages",
            ))
        return
    if event is None or not hasattr(event, "set_extra"):
        return
    event.set_extra(key, normalized)


def _count_tools(tools: Any) -> int:
    if tools is None:
        return 0
    if isinstance(tools, Mapping):
        for key in ("tools", "functions", "items"):
            value = tools.get(key)
            if isinstance(value, (list, tuple, set)):
                return len(value)
        return len(tools)
    for attr in ("tools", "functions", "items"):
        value = getattr(tools, attr, None)
        if isinstance(value, (list, tuple, set)):
            return len(value)
    try:
        return len(tools)
    except Exception:
        return 0


def _tool_ledger_summary(event: Any, calls: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build a compact, content-free summary of tool disclosure and execution."""
    execution_trace = _event_extra(event, "astrmai_tool_execution_trace", [])
    execution_trace = (
        [item for item in execution_trace if isinstance(item, Mapping)]
        if isinstance(execution_trace, list)
        else []
    )
    lifecycle_trace = _event_extra(event, "astrmai_tool_lifecycle_trace", [])
    lifecycle_trace = (
        [item for item in lifecycle_trace if isinstance(item, Mapping)]
        if isinstance(lifecycle_trace, list)
        else []
    )
    turn_context = _event_extra(event, TELEMETRY_CONTEXT_KEY)
    tools_state = getattr(turn_context, "tools", None)
    if tools_state is None:
        tools_state = getattr(_event_extra(event, "astrmai_turn_context"), "tools", None)

    def _names(value: Any, limit: int = 32) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        result: list[str] = []
        for item in value:
            name = str(item or "").strip()
            if name and name not in result:
                result.append(name[:120])
            if len(result) >= limit:
                break
        return result

    candidates = _names(
        getattr(tools_state, "available_tools", None)
        or getattr(tools_state, "initial_tools", None)
        or _event_extra(event, "astrmai_available_tools", [])
    )
    selected = _names(
        [
            item.get("tool_name") or item.get("tool")
            for item in execution_trace
            if str(item.get("status", "success") or "success") == "success"
        ]
    )
    missing = _names(
        _event_extra(event, "astrmai_required_tool_missing", [])
        or _event_extra(event, "astrmai_missing_required_tools", [])
    )
    failures = [
        str(item.get("reason") or item.get("error_kind") or item.get("status") or "").strip()
        for item in [*execution_trace, *lifecycle_trace]
        if str(item.get("status", "") or "").lower() in {"failed", "failure", "error", "missing"}
    ]
    tool_calls = list(calls)
    tool_loop_steps = sum(
        1
        for item in tool_calls
        if str(item.get("stage", "") or "").startswith("gateway.tool")
    )
    return {
        "tool_disclosure_tier": str(
            getattr(tools_state, "disclosure_tier", "")
            or _event_extra(event, "astrmai_tool_tier", "")
            or ""
        ).strip(),
        "tool_candidates": candidates,
        "selected_tool": selected,
        "tool_call_count": len(execution_trace),
        "tool_loop_steps": tool_loop_steps,
        "tool_failure_reason": next((item[:120] for item in failures if item), ""),
        "required_tool_missing": missing,
        "final_reply_after_tool": bool(selected and getattr(turn_context, "reply_completed_at", 0.0)),
    }


def begin_llm_call(
    event: Any,
    *,
    stage: str,
    critical_path: bool = True,
    family: str = "",
    pool: str = "",
    prompt: Any = "",
    system_prompt: Any = "",
    contexts: Iterable[Any] | None = None,
    tools: Any = None,
    max_steps: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Record a privacy-safe logical LLM call on the current event."""
    context = current_turn_telemetry(event)
    if context is None:
        return ""
    context_items = list(contexts or [])
    call_id = uuid.uuid4().hex[:12]
    entry: dict[str, Any] = {
        "call_id": call_id,
        "turn_id": context.turn_id,
        "sequence": context.next_sequence(),
        "stage": str(stage or ""),
        "critical_path": bool(critical_path),
        "family": str(family or ""),
        "pool": str(pool or ""),
        "status": "pending",
        "started_at": time.time(),
        "elapsed_ms": 0.0,
        "system_chars": _safe_len(system_prompt),
        "prompt_chars": _safe_len(prompt),
        "context_count": len(context_items),
        "context_chars": sum(_safe_len(item) for item in context_items),
        "context_item_max_chars": max((_safe_len(item) for item in context_items), default=0),
        "tool_count": _count_tools(tools),
        "max_steps": max(0, int(max_steps or 0)),
        "attempts": 0,
        "model_attempts": [],
    }
    if metadata:
        entry["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    entries = _read_list(event, CALL_LEDGER_KEY)
    entries.append(entry)
    _write_list(event, CALL_LEDGER_KEY, entries)
    return call_id


def finish_llm_call(
    event: Any,
    call_id: str,
    *,
    status: str = "success",
    model: str = "",
    provider: str = "",
    output: Any = "",
    error_kind: str = "",
    error: str = "",
    attempts: int = 0,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not call_id:
        return
    entries = _read_list(event, CALL_LEDGER_KEY)
    now = time.time()
    for entry in reversed(entries):
        if str(entry.get("call_id", "")) != call_id:
            continue
        started_at = float(entry.get("started_at", now) or now)
        entry.update(
            {
                "status": str(status or "success"),
                "finished_at": now,
                "elapsed_ms": round(max(0.0, now - started_at) * 1000, 1),
                "model": str(model or ""),
                "provider": str(provider or ""),
                "output_chars": _safe_len(output),
                "attempts": max(
                    len(entry.get("model_attempts", []) or []),
                    max(0, int(attempts or 0)),
                ),
                "error_kind": str(error_kind or ""),
                "error_hash": _text_hash(error),
            }
        )
        if metadata:
            merged = dict(entry.get("metadata") or {})
            merged.update(
                {
                    str(key): value
                    for key, value in metadata.items()
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            )
            entry["metadata"] = merged
        break
    _write_list(event, CALL_LEDGER_KEY, entries)


def record_llm_attempt(
    event: Any,
    call_id: str,
    *,
    model: str,
    status: str,
    elapsed_ms: float = 0.0,
    error_kind: str = "",
    fallback: bool = False,
    retry_index: int = 0,
) -> None:
    if not call_id:
        return
    entries = _read_list(event, CALL_LEDGER_KEY)
    for entry in reversed(entries):
        if str(entry.get("call_id", "")) != call_id:
            continue
        attempts = list(entry.get("model_attempts", []) or [])
        attempts.append(
            {
                "model": str(model or ""),
                "status": str(status or ""),
                "elapsed_ms": round(max(0.0, float(elapsed_ms or 0.0)), 1),
                "error_kind": str(error_kind or ""),
                "fallback": bool(fallback),
                "retry_index": max(0, int(retry_index or 0)),
            }
        )
        entry["model_attempts"] = attempts[-32:]
        break
    _write_list(event, CALL_LEDGER_KEY, entries)


def begin_stage(
    event: Any,
    stage: str,
    *,
    critical_path: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    context = current_turn_telemetry(event)
    if context is None:
        return ""
    stage_id = uuid.uuid4().hex[:12]
    entry: dict[str, Any] = {
        "stage_id": stage_id,
        "turn_id": context.turn_id,
        "sequence": context.next_sequence(),
        "stage": str(stage or ""),
        "critical_path": bool(critical_path),
        "status": "pending",
        "started_at": time.time(),
        "elapsed_ms": 0.0,
    }
    if metadata:
        entry["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    entries = _read_list(event, STAGE_LEDGER_KEY)
    entries.append(entry)
    _write_list(event, STAGE_LEDGER_KEY, entries)
    return stage_id


def finish_stage(
    event: Any,
    stage_id: str,
    *,
    status: str = "success",
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if not stage_id:
        return
    entries = _read_list(event, STAGE_LEDGER_KEY)
    now = time.time()
    for entry in reversed(entries):
        if str(entry.get("stage_id", "")) != stage_id:
            continue
        started_at = float(entry.get("started_at", now) or now)
        entry.update(
            {
                "status": str(status or "success"),
                "finished_at": now,
                "elapsed_ms": round(max(0.0, now - started_at) * 1000, 1),
                "reason": str(reason or "")[:120],
            }
        )
        if metadata:
            merged = dict(entry.get("metadata") or {})
            merged.update(
                {
                    str(key): value
                    for key, value in metadata.items()
                    if isinstance(value, (str, int, float, bool)) or value is None
                }
            )
            entry["metadata"] = merged
        break
    _write_list(event, STAGE_LEDGER_KEY, entries)


@contextmanager
def observe_stage(
    event: Any,
    stage: str,
    *,
    critical_path: bool = True,
    metadata: Mapping[str, Any] | None = None,
):
    span_metadata = {
        str(key): value
        for key, value in dict(metadata or {}).items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    stage_id = begin_stage(
        event,
        stage,
        critical_path=critical_path,
        metadata=span_metadata,
    )
    try:
        yield span_metadata
    except BaseException as exc:
        finish_stage(
            event,
            stage_id,
            status="cancelled" if exc.__class__.__name__ == "CancelledError" else "error",
            reason=exc.__class__.__name__,
            metadata=span_metadata,
        )
        raise
    else:
        finish_stage(event, stage_id, metadata=span_metadata)


def record_context_block_stats(
    event: Any,
    *,
    stage: str,
    blocks: Mapping[str, Any],
    total_chars: int | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Store block sizes and hashes, never the block contents."""
    if current_turn_telemetry(event) is None:
        return
    normalized: dict[str, dict[str, Any]] = {}
    first_block_by_hash: dict[str, str] = {}
    duplicate_pairs: list[dict[str, str]] = []
    for name, value in list(blocks.items())[:_MAX_BLOCKS]:
        text = str(value or "")
        block_name = str(name)
        block_hash = _text_hash(text)
        normalized[block_name] = {
            "chars": len(text),
            "nonempty": bool(text.strip()),
            "hash": block_hash,
        }
        if text.strip() and block_hash:
            duplicate_of = first_block_by_hash.get(block_hash)
            if duplicate_of:
                normalized[block_name]["duplicate_of"] = duplicate_of
                duplicate_pairs.append({"block": block_name, "duplicate_of": duplicate_of})
            else:
                first_block_by_hash[block_hash] = block_name
    payload = {
        "stage": str(stage or ""),
        "recorded_at": time.time(),
        "total_chars": int(total_chars if total_chars is not None else sum(item["chars"] for item in normalized.values())),
        "nonempty_count": sum(1 for item in normalized.values() if item["nonempty"]),
        "duplicate_block_count": len(duplicate_pairs),
        "duplicate_pairs": duplicate_pairs[:16],
        "blocks": normalized,
    }
    if metadata:
        payload["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    existing = _read_list(event, CONTEXT_BLOCK_STATS_KEY)
    existing.append(payload)
    _write_list(event, CONTEXT_BLOCK_STATS_KEY, existing)


def record_reply_stats(
    event: Any,
    *,
    segment_count: int,
    segment_lengths: Iterable[int],
    total_chars: int,
    strategy: str = "",
    send_status: str = "",
    sent_segment_count: int = 0,
    reply_completed: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    context = current_turn_telemetry(event)
    if context is None:
        return
    payload = {
        "segment_count": max(0, int(segment_count or 0)),
        "segment_lengths": [max(0, int(length or 0)) for length in list(segment_lengths)[:32]],
        "total_chars": max(0, int(total_chars or 0)),
        "actual_reply_chars": max(0, int(total_chars or 0)),
        "strategy": str(strategy or ""),
        "send_status": str(send_status or ""),
        "sent_segment_count": max(0, int(sent_segment_count or 0)),
    }
    if metadata:
        for key in (
            "reply_shape_mode",
            "reply_shape_reason",
            "humanlike_short_reply_applied",
            "humanlike_short_reply_constraints",
            "humanlike_short_reply_before_len",
            "humanlike_short_reply_after_len",
        ):
            value = metadata.get(key)
            if isinstance(value, (str, int, float, bool)):
                payload[key] = value
    if event is not None and hasattr(event, "get_extra"):
        freshness_state = str(event.get_extra("astrmai_reply_freshness_state", "") or "")
        stale_reason = str(event.get_extra("astrmai_reply_stale_reason", "") or "")
        stale_category = str(event.get_extra("astrmai_reply_stale_category", "") or "")
        if freshness_state:
            payload["freshness_state"] = freshness_state
        if stale_reason:
            payload["stale_reason"] = stale_reason.split(":", 1)[0]
        if stale_category:
            payload["stale_category"] = stale_category
        for key in ("astrmai_reply_age_sec", "astrmai_reply_max_age_sec"):
            value = event.get_extra(key, None)
            if isinstance(value, (int, float)):
                payload[key.removeprefix("astrmai_")] = round(max(0.0, float(value)), 1)
    context.reply_stats.clear()
    context.reply_stats.update(payload)
    if reply_completed:
        context.reply_completed_at = time.time()
        context.reply_stats["reply_completed_at"] = context.reply_completed_at
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra(REPLY_STATS_KEY, context.reply_stats)


def record_vision_observation(
    event: Any,
    payload: Mapping[str, Any],
) -> None:
    """Store privacy-safe image/vision barrier facts for the current turn.

    The payload intentionally accepts only counts, statuses, source categories and
    opaque IDs. It must never receive image paths, URLs, descriptions or message
    text.
    """
    context = current_turn_telemetry(event)
    if context is None:
        return
    allowed = {
        "policy",
        "outcome",
        "image_count",
        "raw_image_count",
        "candidate_ref_count",
        "resolved_count",
        "analyzed_count",
        "failed_count",
        "timeout_count",
        "attempt_count",
        "vision_model_attempt_count",
        "image_source",
        "image_resolve_status",
        "vision_barrier_status",
        "vision_wait_ms",
        "vision_timeout_ms",
        "vision_fallback",
        "visual_memory_id",
        "visual_memory_ids",
        "scope",
        "vision_path",
        "vision_call_status",
        "visual_memory_write_status",
        "prompt_injected",
        "fallback_reason",
        "failure_disposition",
        "reply_guard_action",
        "resolve_failure_reasons",
        "selected_message_id",
        "selected_sender_id",
        "selected_pairing_mode",
        "cache_hit_count",
        "cache_miss_count",
        "singleflight_wait_count",
        "asset_ids",
        "binding_count",
        "failure_stage",
        "skip_reason",
        "model_ids",
        "analysis_prompt_version",
        "asset_storage_status",
        "final_status",
        "candidate_count",
        "autonomous_inspection_enabled",
        "autonomous_inspection_disclosed",
        "autonomous_inspection_called",
        "autonomous_inspection_status",
        "autonomous_inspection_dependency",
        "autonomous_inspection_decision_reason",
        "autonomous_inspection_candidate_id",
        "autonomous_inspection_elapsed_ms",
        "autonomous_inspection_cache_hit",
        "autonomous_inspection_fallback",
        "vision_state",
        "image_event_count",
        "image_raw_component_count",
        "image_resolved_count",
        "image_placeholder_count",
        "image_focus_reason",
        "image_focus_allowed",
        "user_asked_about_image",
        "direct_vision_scheduled",
        "direct_vision_resolve_status",
        "direct_vision_model_called",
        "direct_vision_model_status",
        "direct_vision_elapsed_ms",
        "direct_vision_injected",
        "vision_tool_disclosed",
        "vision_tool_required",
        "vision_tool_selected",
        "vision_tool_result_status",
        "reply_mentions_image",
        "has_valid_image_context",
        "image_reply_blocked",
        "dropped_image_count",
        "source_format",
        "declared_suffix",
        "is_animated",
        "source_frame_count",
        "duration_ms",
        "sampled_frame_count",
        "sampled_indices",
        "sampled_timestamps_ms",
        "preprocess_version",
        "preprocess_status",
        "preprocess_elapsed_ms",
        "preprocess_fallback_reason",
        "model_input_format",
        "contact_sheet_size",
    }
    normalized: dict[str, Any] = {}
    for key in allowed:
        value = payload.get(key)
        if key in {
            "image_source",
            "visual_memory_ids",
            "asset_ids",
            "model_ids",
            "resolve_failure_reasons",
        }:
            if isinstance(value, (list, tuple, set)):
                normalized[key] = [str(item)[:80] for item in value if str(item).strip()][:16]
            elif value is not None and str(value).strip():
                normalized[key] = [str(value)[:80]]
            else:
                normalized[key] = []
        elif key in {"sampled_indices", "sampled_timestamps_ms", "contact_sheet_size"}:
            if isinstance(value, (list, tuple)):
                limit = 2 if key == "contact_sheet_size" else 24
                normalized[key] = [
                    max(0, int(item))
                    for item in value[:limit]
                    if isinstance(item, (int, float))
                ]
            else:
                normalized[key] = []
        elif isinstance(value, bool):
            normalized[key] = value
        elif isinstance(value, (int, float)):
            normalized[key] = max(0, round(float(value), 1))
        elif value is not None:
            normalized[key] = str(value)[:120]
    context.vision_observation.update(normalized)
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra(VISION_OBSERVATION_KEY, context.vision_observation)


def record_background_task(
    event: Any,
    task_name: str,
    *,
    status: str,
    started_at: float | None = None,
    error_kind: str = "",
    error: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Record a privacy-safe result for a fire-and-forget task bound to a turn."""
    context = current_turn_telemetry(event)
    if context is None:
        return
    finished_at = time.time()
    started = float(started_at or finished_at)
    item: dict[str, Any] = {
        "task_name": str(task_name or ""),
        "status": str(status or ""),
        "started_at": started,
        "finished_at": finished_at,
        "elapsed_ms": round(max(0.0, finished_at - started) * 1000, 1),
        "error_kind": str(error_kind or ""),
        "error_hash": _text_hash(error),
    }
    if metadata:
        item["metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
    context.background_tasks.append(item)
    del context.background_tasks[:-_MAX_ENTRIES]
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra(BACKGROUND_TASK_LEDGER_KEY, context.background_tasks)


def attach_background_task_trace(
    task: asyncio.Task,
    event: Any,
    task_name: str,
    *,
    started_at: float | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> asyncio.Task:
    """Consume a task result and write a structured completion record.

    This callback deliberately never re-raises. It is safe for fire-and-forget
    tasks and prevents ``Task exception was never retrieved`` from hiding the
    owning turn's failure reason.
    """
    started = float(started_at or time.time())

    def _done(done_task: asyncio.Task) -> None:
        if done_task.cancelled():
            record_background_task(
                event,
                task_name,
                status="cancelled",
                started_at=started,
                error_kind="cancelled",
                metadata=metadata,
            )
            return
        try:
            error = done_task.exception()
        except asyncio.CancelledError:
            record_background_task(
                event,
                task_name,
                status="cancelled",
                started_at=started,
                error_kind="cancelled",
                metadata=metadata,
            )
            return
        if error is None:
            record_background_task(
                event,
                task_name,
                status="completed",
                started_at=started,
                metadata=metadata,
            )
            return
        record_background_task(
            event,
            task_name,
            status="failed",
            started_at=started,
            error_kind=type(error).__name__,
            error=str(error),
            metadata=metadata,
        )

    task.add_done_callback(_done)
    return task


__all__ = [
    "CALL_LEDGER_KEY",
    "CONTEXT_BLOCK_STATS_KEY",
    "BACKGROUND_TASK_LEDGER_KEY",
    "VISION_OBSERVATION_KEY",
    "INSTRUMENTATION_VERSION",
    "REPLY_STATS_KEY",
    "STAGE_LEDGER_KEY",
    "TELEMETRY_CONTEXT_KEY",
    "TRACE_SCHEMA_VERSION",
    "TurnTelemetryContext",
    "begin_stage",
    "begin_llm_call",
    "bind_turn_telemetry_identity",
    "clamp_timeout_to_turn_budget",
    "configure_turn_budget",
    "current_turn_telemetry",
    "detach_turn_telemetry",
    "ensure_turn_telemetry",
    "finalize_turn_telemetry",
    "finish_stage",
    "finish_llm_call",
    "observe_stage",
    "rebind_turn_telemetry",
    "record_llm_attempt",
    "record_context_block_stats",
    "record_background_task",
    "record_vision_observation",
    "record_reply_stats",
    "attach_background_task_trace",
    "remaining_turn_budget",
    "turn_telemetry_scope",
    "turn_telemetry_snapshot",
]
