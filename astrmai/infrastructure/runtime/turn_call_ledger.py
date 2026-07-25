from __future__ import annotations

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
TELEMETRY_CONTEXT_KEY = "astrmai_turn_telemetry"
TRACE_SCHEMA_VERSION = 2
INSTRUMENTATION_VERSION = "2026.07.25-v2"
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
    sequence: int = 0
    calls: list[dict[str, Any]] = field(default_factory=list)
    context_blocks: list[dict[str, Any]] = field(default_factory=list)
    stages: list[dict[str, Any]] = field(default_factory=list)
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


def turn_telemetry_snapshot(event: Any = None) -> dict[str, Any]:
    context = current_turn_telemetry(event)
    if context is None:
        return {}
    captured_at = time.time()
    remaining = remaining_turn_budget(event)
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
        "captured_at": captured_at,
        "total_elapsed_ms": round(max(0.0, captured_at - context.started_at) * 1000, 1),
        "budget": {
            "total_budget_sec": context.total_budget_sec,
            "main_reply_reserve_sec": context.main_reply_reserve_sec,
            "remaining_ms": round(max(0.0, float(remaining or 0.0)) * 1000, 1),
            "exhausted": remaining is not None and remaining <= 0.0,
        },
        "llm_call_ledger": copy.deepcopy(context.calls[-_MAX_ENTRIES:]),
        "context_block_stats": copy.deepcopy(context.context_blocks[-16:]),
        "stage_ledger": copy.deepcopy(context.stages[-_MAX_ENTRIES:]),
        "reply_stats": copy.deepcopy(context.reply_stats),
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
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra(REPLY_STATS_KEY, context.reply_stats)


__all__ = [
    "CALL_LEDGER_KEY",
    "CONTEXT_BLOCK_STATS_KEY",
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
    "ensure_turn_telemetry",
    "finalize_turn_telemetry",
    "finish_stage",
    "finish_llm_call",
    "observe_stage",
    "record_llm_attempt",
    "record_context_block_stats",
    "record_reply_stats",
    "remaining_turn_budget",
    "turn_telemetry_scope",
    "turn_telemetry_snapshot",
]
