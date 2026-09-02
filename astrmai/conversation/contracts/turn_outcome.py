from __future__ import annotations

import time
import threading
import weakref
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


TURN_OUTCOME_EXTRA_KEY = "astrmai_turn_outcome"
_EVENT_LOCK_INIT_GUARD = threading.Lock()
_EVENT_LOCK_REGISTRY: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()
_EVENT_LOCK_FALLBACK: OrderedDict[int, tuple[Any, Any]] = OrderedDict()
_EVENT_LOCK_FALLBACK_MAX = 1024


class TurnOutcomeStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FALLBACK = "fallback"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    SHUTDOWN = "shutdown"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    RETRYABLE = "retryable"
    SUPERSEDED = "superseded"


_HARD_TERMINAL_STATUSES = {
    TurnOutcomeStatus.SKIPPED,
    TurnOutcomeStatus.CANCELLED,
    TurnOutcomeStatus.SHUTDOWN,
    TurnOutcomeStatus.BUDGET_EXHAUSTED,
    TurnOutcomeStatus.SUPERSEDED,
}

_ALLOWED_STATUS_TRANSITIONS = {
    TurnOutcomeStatus.ACTIVE: {
        TurnOutcomeStatus.COMPLETED,
        TurnOutcomeStatus.FALLBACK,
        TurnOutcomeStatus.FAILED,
        TurnOutcomeStatus.RETRYABLE,
        TurnOutcomeStatus.SKIPPED,
        TurnOutcomeStatus.CANCELLED,
        TurnOutcomeStatus.SHUTDOWN,
        TurnOutcomeStatus.BUDGET_EXHAUSTED,
        TurnOutcomeStatus.SUPERSEDED,
    },
    TurnOutcomeStatus.FAILED: {
        TurnOutcomeStatus.ACTIVE,
        TurnOutcomeStatus.RETRYABLE,
        TurnOutcomeStatus.SKIPPED,
        TurnOutcomeStatus.CANCELLED,
        TurnOutcomeStatus.SHUTDOWN,
        TurnOutcomeStatus.BUDGET_EXHAUSTED,
        TurnOutcomeStatus.SUPERSEDED,
    },
    TurnOutcomeStatus.RETRYABLE: {
        TurnOutcomeStatus.ACTIVE,
        TurnOutcomeStatus.COMPLETED,
        TurnOutcomeStatus.FALLBACK,
        TurnOutcomeStatus.FAILED,
        TurnOutcomeStatus.SKIPPED,
        TurnOutcomeStatus.CANCELLED,
        TurnOutcomeStatus.SHUTDOWN,
        TurnOutcomeStatus.BUDGET_EXHAUSTED,
        TurnOutcomeStatus.SUPERSEDED,
    },
}


@dataclass(frozen=True, slots=True)
class TurnDecision:
    allowed: bool
    reason: str
    terminal_status: TurnOutcomeStatus
    claim_kind: str
    turn_id: str
    trace_id: str

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(slots=True)
class TurnOutcome:
    turn_id: str = ""
    trace_id: str = ""
    turn_generation: int = 0
    reply_sent: bool = False
    reply_sent_segments: int = 0
    tool_actions_sent: bool = False
    tool_action_count: int = 0
    fallback_sent: bool = False
    system2_handled: bool = False
    deferred_replayed: bool = False
    deferred_replay_claimed: bool = False
    terminal_reason: str = ""
    terminal_status: TurnOutcomeStatus = TurnOutcomeStatus.ACTIVE
    updated_at: float = 0.0
    output_claim: str = ""
    last_text_kind: str = ""
    completion_callback_claimed: bool = False
    completion_callback_completed: bool = False
    tool_action_keys: list[str] = field(default_factory=list)
    tool_action_claimed_keys: list[str] = field(default_factory=list)
    uncertain_tool_action_keys: list[str] = field(default_factory=list)
    tool_action_uncertain: bool = False
    malformed: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["terminal_status"] = self.terminal_status.value
        return payload

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TurnOutcome":
        status = TurnOutcomeStatus(str(value.get("terminal_status", "active") or "active"))
        return cls(
            turn_id=str(value.get("turn_id", "") or ""),
            trace_id=str(value.get("trace_id", "") or ""),
            turn_generation=_nonnegative_int(value.get("turn_generation", 0)),
            reply_sent=_strict_bool(value.get("reply_sent", False)),
            reply_sent_segments=_nonnegative_int(value.get("reply_sent_segments", 0)),
            tool_actions_sent=_strict_bool(value.get("tool_actions_sent", False)),
            tool_action_count=_nonnegative_int(value.get("tool_action_count", 0)),
            fallback_sent=_strict_bool(value.get("fallback_sent", False)),
            system2_handled=_strict_bool(value.get("system2_handled", False)),
            deferred_replayed=_strict_bool(value.get("deferred_replayed", False)),
            deferred_replay_claimed=_strict_bool(value.get("deferred_replay_claimed", False)),
            terminal_reason=str(value.get("terminal_reason", "") or ""),
            terminal_status=status,
            updated_at=float(value.get("updated_at", 0.0) or 0.0),
            output_claim=str(value.get("output_claim", "") or ""),
            last_text_kind=str(value.get("last_text_kind", "") or ""),
            completion_callback_claimed=_strict_bool(
                value.get("completion_callback_claimed", False)
            ),
            completion_callback_completed=_strict_bool(
                value.get("completion_callback_completed", False)
            ),
            tool_action_keys=_string_list(value.get("tool_action_keys", [])),
            tool_action_claimed_keys=_string_list(value.get("tool_action_claimed_keys", [])),
            uncertain_tool_action_keys=_string_list(
                value.get("uncertain_tool_action_keys", [])
            ),
            tool_action_uncertain=_strict_bool(
                value.get("tool_action_uncertain", False)
            ),
            malformed=_strict_bool(value.get("malformed", False)),
        )


def _strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ValueError("boolean field has invalid type")


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("integer field has invalid type")
    result = int(value or 0)
    if result < 0:
        raise ValueError("integer field must be nonnegative")
    return result


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("list field has invalid type")
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _get_extra(event: Any, key: str, default: Any = None) -> Any:
    if event is None or not hasattr(event, "get_extra"):
        return default
    try:
        return event.get_extra(key, default)
    except Exception:
        return default


def _set_extra(event: Any, key: str, value: Any) -> None:
    if event is not None and hasattr(event, "set_extra"):
        event.set_extra(key, value)


def _event_lock(event: Any):
    """Return a lock scoped to one event, never a process-wide lock."""
    lock_key = "_astrmai_turn_outcome_lock"
    lock = _get_extra(event, lock_key)
    if lock is not None and hasattr(lock, "__enter__"):
        return lock
    if event is None:
        return _NullLock()
    with _EVENT_LOCK_INIT_GUARD:
        lock = _get_extra(event, lock_key)
        if lock is None or not hasattr(lock, "__enter__"):
            lock = threading.RLock()
            try:
                _EVENT_LOCK_REGISTRY[event] = lock
            except (TypeError, ValueError):
                event_id = id(event)
                existing = _EVENT_LOCK_FALLBACK.get(event_id)
                if existing is not None and existing[0] is event:
                    lock = existing[1]
                else:
                    lock = threading.RLock()
                _EVENT_LOCK_FALLBACK[event_id] = (event, lock)
                _EVENT_LOCK_FALLBACK.move_to_end(event_id)
                _prune_fallback_locks()
            try:
                if hasattr(event, "set_extra"):
                    event.set_extra(lock_key, lock)
            except Exception:
                pass
        return lock


def _prune_fallback_locks() -> None:
    """Drop only terminal entries; never evict a live event's lock."""
    for event_id, entry in list(_EVENT_LOCK_FALLBACK.items()):
        event = entry[0]
        raw = _get_extra(event, TURN_OUTCOME_EXTRA_KEY)
        status = raw.get("terminal_status") if isinstance(raw, Mapping) else None
        if status in {
            TurnOutcomeStatus.COMPLETED.value,
            TurnOutcomeStatus.FALLBACK.value,
            *(item.value for item in _HARD_TERMINAL_STATUSES),
        }:
            _EVENT_LOCK_FALLBACK.pop(event_id, None)
    # Active entries are intentionally retained even when the soft bound is
    # exceeded; dropping one would create a second lock for the same event.


def _drop_fallback_lock(event: Any) -> None:
    event_id = id(event)
    entry = _EVENT_LOCK_FALLBACK.get(event_id)
    if entry is not None and entry[0] is event:
        _EVENT_LOCK_FALLBACK.pop(event_id, None)


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _identity(event: Any) -> tuple[str, str]:
    trace_id = str(_get_extra(event, "astrmai_trace_id", "") or "").strip()
    turn = _get_extra(event, "astrmai_turn_identity")
    if trace_id:
        return trace_id, trace_id
    if turn is not None:
        turn_id = ":".join(
            (
                str(getattr(turn, "mode", "") or ""),
                str(getattr(turn, "chat_id", "") or ""),
                str(getattr(turn, "thread_id", "") or ""),
                str(int(getattr(turn, "generation", 0) or 0)),
            )
        ).strip(":")
        if turn_id:
            return turn_id, ""
    message_obj = getattr(event, "message_obj", None)
    message_id = str(
        getattr(message_obj, "message_id", "")
        or getattr(event, "message_id", "")
        or ""
    ).strip()
    origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if message_id or origin:
        return f"legacy:{origin}:{message_id}", ""
    return f"legacy-object:{id(event)}", ""


def _decision(
    event: Any,
    outcome: TurnOutcome,
    *,
    allowed: bool,
    reason: str,
    claim_kind: str,
) -> TurnDecision:
    return TurnDecision(
        allowed=bool(allowed),
        reason=str(reason or ""),
        terminal_status=outcome.terminal_status,
        claim_kind=str(claim_kind or ""),
        turn_id=str(outcome.turn_id or _identity(event)[0]),
        trace_id=str(outcome.trace_id or _identity(event)[1]),
    )


def _legacy_status(event: Any, *, reply_sent: bool, fallback_sent: bool) -> TurnOutcomeStatus:
    execution_status = str(_get_extra(event, "astrmai_execution_status", "") or "").strip()
    if fallback_sent or execution_status == "fallback_sent":
        return TurnOutcomeStatus.FALLBACK
    if reply_sent or execution_status in {"sent", "partial_sent", "completed", "reread_dispatched"}:
        return TurnOutcomeStatus.COMPLETED
    if execution_status in {"shutdown_rejected", "shutdown"}:
        return TurnOutcomeStatus.SHUTDOWN
    if execution_status in {"cancelled", "task_cancelled"}:
        return TurnOutcomeStatus.CANCELLED
    if execution_status in {"turn_budget_exhausted", "budget_exhausted"}:
        return TurnOutcomeStatus.BUDGET_EXHAUSTED
    if execution_status in {"stale_drop", "superseded"}:
        return TurnOutcomeStatus.SUPERSEDED
    if execution_status in {"fatal_no_send", "send_failed", "failed"}:
        return TurnOutcomeStatus.FAILED
    return TurnOutcomeStatus.ACTIVE


def _from_legacy(event: Any) -> TurnOutcome:
    turn_id, trace_id = _identity(event)
    turn_identity = _get_extra(event, "astrmai_turn_identity")
    reply_sent = bool(_get_extra(event, "astrmai_reply_sent", False))
    fallback_sent = bool(_get_extra(event, "fallback_sent", False)) or str(
        _get_extra(event, "astrmai_execution_status", "") or ""
    ) == "fallback_sent"
    system2_handled = bool(
        _get_extra(event, "astrmai_system2_failure_handled", False)
    )
    deferred_replayed = bool(_get_extra(event, "deferred_replayed", False)) or bool(
        _get_extra(event, "deferred_replayed_at", 0.0)
    )
    status = _legacy_status(
        event,
        reply_sent=reply_sent,
        fallback_sent=fallback_sent,
    )
    reason = str(
        _get_extra(event, "terminal_reason", "")
        or _get_extra(event, "deferred_terminal_reason", "")
        or _get_extra(event, "astrmai_execution_status", "")
        or ""
    )
    if status is TurnOutcomeStatus.ACTIVE and system2_handled:
        status = TurnOutcomeStatus.SKIPPED
        reason = reason or "system2_already_handled"
    return TurnOutcome(
        turn_id=turn_id,
        trace_id=trace_id,
        turn_generation=_nonnegative_int(getattr(turn_identity, "generation", 0) if turn_identity is not None else 0),
        reply_sent=reply_sent,
        reply_sent_segments=_nonnegative_int(
            _get_extra(event, "astrmai_reply_sent_segment_count", 0)
        ),
        tool_actions_sent=bool(_get_extra(event, "tool_actions_sent", False)),
        tool_action_count=_nonnegative_int(
            _get_extra(event, "tool_action_count", 0)
        ),
        fallback_sent=fallback_sent,
        system2_handled=system2_handled,
        deferred_replayed=deferred_replayed,
        terminal_reason=reason,
        terminal_status=status,
        completion_callback_completed=bool(
            _get_extra(event, "astrmai_proactive_completed", False)
        ),
        updated_at=time.time(),
    )


def _malformed_outcome(event: Any) -> TurnOutcome:
    turn_id, trace_id = _identity(event)
    return TurnOutcome(
        turn_id=turn_id,
        trace_id=trace_id,
        terminal_reason="malformed_turn_outcome",
        terminal_status=TurnOutcomeStatus.FAILED,
        malformed=True,
        updated_at=time.time(),
    )


def _sync_legacy(event: Any, outcome: TurnOutcome) -> None:
    _set_extra(event, "astrmai_reply_sent", bool(outcome.reply_sent))
    _set_extra(
        event,
        "astrmai_reply_sent_segment_count",
        int(outcome.reply_sent_segments),
    )
    _set_extra(event, "fallback_sent", bool(outcome.fallback_sent))
    _set_extra(event, "tool_actions_sent", bool(outcome.tool_actions_sent))
    _set_extra(event, "tool_action_count", int(outcome.tool_action_count))
    _set_extra(
        event,
        "astrmai_system2_failure_handled",
        bool(outcome.system2_handled),
    )
    _set_extra(event, "deferred_replayed", bool(outcome.deferred_replayed))
    _set_extra(
        event,
        "astrmai_proactive_completed",
        bool(outcome.completion_callback_completed),
    )
    _set_extra(event, "terminal_reason", str(outcome.terminal_reason or ""))
    execution_status = {
        TurnOutcomeStatus.COMPLETED: "sent",
        TurnOutcomeStatus.FALLBACK: "fallback_sent",
        TurnOutcomeStatus.CANCELLED: "cancelled",
        TurnOutcomeStatus.SHUTDOWN: "shutdown_rejected",
        TurnOutcomeStatus.BUDGET_EXHAUSTED: "turn_budget_exhausted",
        TurnOutcomeStatus.SUPERSEDED: "stale_drop",
    }.get(outcome.terminal_status)
    if execution_status:
        _set_extra(event, "astrmai_execution_status", execution_status)


def _persist(event: Any, outcome: TurnOutcome) -> TurnOutcome:
    outcome.updated_at = time.time()
    _set_extra(event, TURN_OUTCOME_EXTRA_KEY, outcome.to_dict())
    _sync_legacy(event, outcome)
    turn_context = _get_extra(event, "astrmai_turn_context")
    if turn_context is not None and hasattr(turn_context, "outcome"):
        turn_context.outcome = outcome
    if outcome.terminal_status in {
        TurnOutcomeStatus.COMPLETED,
        TurnOutcomeStatus.FALLBACK,
        *_HARD_TERMINAL_STATUSES,
    }:
        _drop_fallback_lock(event)
    return outcome


def _merge_legacy(event: Any, outcome: TurnOutcome) -> TurnOutcome:
    if outcome.malformed:
        return outcome
    legacy_reply_sent = bool(_get_extra(event, "astrmai_reply_sent", False))
    legacy_fallback_sent = bool(_get_extra(event, "fallback_sent", False)) or str(
        _get_extra(event, "astrmai_execution_status", "") or ""
    ) == "fallback_sent"
    # A live output claim means the legacy flag may have been emitted by the
    # transport just before the typed outcome is settled. Do not let that
    # compatibility write preempt the actual reply/fallback kind.
    if legacy_reply_sent and not outcome.reply_sent and not outcome.output_claim:
        outcome.reply_sent = True
        outcome.fallback_sent = legacy_fallback_sent
        outcome.reply_sent_segments = max(
            outcome.reply_sent_segments,
            _nonnegative_int(
                _get_extra(event, "astrmai_reply_sent_segment_count", 0)
            ),
        )
        outcome.terminal_status = (
            TurnOutcomeStatus.FALLBACK
            if legacy_fallback_sent
            else TurnOutcomeStatus.COMPLETED
        )
        outcome.terminal_reason = outcome.terminal_reason or "legacy_reply_sent"
    if bool(_get_extra(event, "astrmai_system2_failure_handled", False)):
        outcome.system2_handled = True
    if bool(_get_extra(event, "deferred_replayed", False)) or bool(
        _get_extra(event, "deferred_replayed_at", 0.0)
    ):
        outcome.deferred_replayed = True
    raw_outcome = _get_extra(event, TURN_OUTCOME_EXTRA_KEY)
    has_completion_fields = isinstance(raw_outcome, Mapping) and any(
        key in raw_outcome
        for key in ("completion_callback_claimed", "completion_callback_completed")
    )
    if (
        bool(_get_extra(event, "astrmai_proactive_completed", False))
        and not has_completion_fields
        and not outcome.completion_callback_claimed
        and not outcome.completion_callback_completed
    ):
        outcome.completion_callback_completed = True
    legacy_status = _legacy_status(
        event,
        reply_sent=legacy_reply_sent,
        fallback_sent=legacy_fallback_sent,
    )
    if not outcome.reply_sent and not outcome.fallback_sent:
        if legacy_status in _HARD_TERMINAL_STATUSES:
            outcome.terminal_status = legacy_status
            outcome.terminal_reason = str(
                _get_extra(event, "astrmai_execution_status", "")
                or legacy_status.value
            )
        elif legacy_status is TurnOutcomeStatus.FAILED and outcome.terminal_status is TurnOutcomeStatus.ACTIVE:
            outcome.terminal_status = TurnOutcomeStatus.FAILED
            outcome.terminal_reason = str(
                _get_extra(event, "astrmai_execution_status", "")
                or "legacy_failure"
            )
    return outcome


def get_turn_outcome(event: Any) -> TurnOutcome | None:
    raw = _get_extra(event, TURN_OUTCOME_EXTRA_KEY)
    if raw is None:
        return None
    if isinstance(raw, TurnOutcome):
        outcome = raw
    elif isinstance(raw, Mapping):
        try:
            outcome = TurnOutcome.from_mapping(raw)
        except (TypeError, ValueError):
            outcome = _malformed_outcome(event)
    else:
        outcome = _malformed_outcome(event)
    current_turn_id, _ = _identity(event)
    if outcome.turn_id and current_turn_id and outcome.turn_id != current_turn_id:
        outcome.terminal_status = TurnOutcomeStatus.SUPERSEDED
        outcome.terminal_reason = "turn_identity_mismatch"
    current_turn = _get_extra(event, "astrmai_turn_identity")
    current_generation = _nonnegative_int(getattr(current_turn, "generation", 0) if current_turn is not None else 0)
    if outcome.turn_generation and current_generation and outcome.turn_generation != current_generation:
        outcome.terminal_status = TurnOutcomeStatus.SUPERSEDED
        outcome.terminal_reason = "turn_generation_mismatch"
    return outcome


def _ensure_turn_outcome_unlocked(event: Any) -> TurnOutcome:
    outcome = get_turn_outcome(event)
    if outcome is None:
        outcome = _from_legacy(event)
    else:
        outcome = _merge_legacy(event, outcome)
    return _persist(event, outcome)


def ensure_turn_outcome(event: Any) -> TurnOutcome:
    with _event_lock(event):
        return _ensure_turn_outcome_unlocked(event)


def _normalize_text_kind(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized in {"fallback", "reread"}:
        return normalized
    return "reply"


def _can_send_text_unlocked(event: Any, kind: str = "reply") -> TurnDecision:
    outcome = _ensure_turn_outcome_unlocked(event)
    normalized_kind = _normalize_text_kind(kind)
    if outcome.malformed:
        return _decision(event, outcome, allowed=False, reason="malformed_turn_outcome", claim_kind=normalized_kind)
    if outcome.output_claim:
            return _decision(event, outcome, allowed=False, reason="output_claim_exists", claim_kind=normalized_kind)
    if outcome.deferred_replay_claimed and normalized_kind not in {"reply", "reread"}:
        return _decision(event, outcome, allowed=False, reason="deferred_replay_claimed", claim_kind=normalized_kind)
    if outcome.reply_sent or outcome.fallback_sent:
        if not (
            normalized_kind == "reread"
            and outcome.last_text_kind == "reread"
            and not outcome.fallback_sent
        ):
            return _decision(event, outcome, allowed=False, reason="output_already_sent", claim_kind=normalized_kind)
    if outcome.terminal_status in _HARD_TERMINAL_STATUSES:
        return _decision(event, outcome, allowed=False, reason="terminal_status", claim_kind=normalized_kind)
    if normalized_kind == "reply" and outcome.system2_handled:
        return _decision(event, outcome, allowed=False, reason="system2_already_handled", claim_kind=normalized_kind)
    if outcome.terminal_status not in {
        TurnOutcomeStatus.ACTIVE,
        TurnOutcomeStatus.FAILED,
        TurnOutcomeStatus.RETRYABLE,
    } and not (
        normalized_kind == "reread"
        and outcome.terminal_status is TurnOutcomeStatus.COMPLETED
        and outcome.last_text_kind == "reread"
    ):
        return _decision(event, outcome, allowed=False, reason="unknown_terminal_status", claim_kind=normalized_kind)
    return _decision(event, outcome, allowed=True, reason="available", claim_kind=normalized_kind)


def can_send_text(event: Any, kind: str = "reply") -> TurnDecision:
    with _event_lock(event):
        return _can_send_text_unlocked(event, kind)


def can_send_fallback(event: Any) -> TurnDecision:
    return can_send_text(event, "fallback")


def claim_text_output(event: Any, kind: str = "reply") -> TurnDecision:
    normalized_kind = _normalize_text_kind(kind)
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        decision = _can_send_text_unlocked(event, normalized_kind)
        if not decision.allowed:
            return decision
        if outcome.deferred_replay_claimed:
            outcome.deferred_replay_claimed = False
        if outcome.terminal_status in {
            TurnOutcomeStatus.FAILED,
            TurnOutcomeStatus.RETRYABLE,
        }:
            outcome.terminal_status = TurnOutcomeStatus.ACTIVE
            outcome.terminal_reason = "retry_claim"
        outcome.output_claim = normalized_kind
        _persist(event, outcome)
        return _decision(event, outcome, allowed=True, reason="claimed", claim_kind=normalized_kind)


def record_text_sent(
    event: Any,
    *,
    segments: int,
    kind: str = "reply",
    reason: str = "",
) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        normalized_kind = _normalize_text_kind(kind)
        if outcome.terminal_status in _HARD_TERMINAL_STATUSES:
            return outcome
        if outcome.fallback_sent and normalized_kind != "fallback":
            return outcome
        if (
            outcome.reply_sent
            and outcome.terminal_status is TurnOutcomeStatus.COMPLETED
            and not (normalized_kind == "reread" and outcome.last_text_kind == "reread")
        ):
            return outcome
        if outcome.terminal_status not in {
            TurnOutcomeStatus.ACTIVE,
            TurnOutcomeStatus.RETRYABLE,
        }:
            return outcome
        outcome.output_claim = ""
        outcome.last_text_kind = normalized_kind
        outcome.reply_sent = True
        outcome.reply_sent_segments = max(
            int(outcome.reply_sent_segments),
            _nonnegative_int(segments),
        )
        outcome.fallback_sent = normalized_kind == "fallback"
        outcome.terminal_status = (
            TurnOutcomeStatus.FALLBACK
            if outcome.fallback_sent
            else TurnOutcomeStatus.COMPLETED
        )
        outcome.terminal_reason = str(reason or f"{normalized_kind}_sent")
        return _persist(event, outcome)


def record_text_failed(event: Any, reason: str) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        outcome.output_claim = ""
        if outcome.reply_sent or outcome.fallback_sent:
            return _persist(event, outcome)
        if outcome.terminal_status in {
            TurnOutcomeStatus.ACTIVE,
            TurnOutcomeStatus.RETRYABLE,
        }:
            outcome.terminal_status = TurnOutcomeStatus.FAILED
            outcome.terminal_reason = str(reason or "text_send_failed")
        return _persist(event, outcome)


def release_text_output(event: Any, kind: str = "reply") -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        normalized_kind = _normalize_text_kind(kind)
        if outcome.output_claim == normalized_kind:
            outcome.output_claim = ""
        return _persist(event, outcome)


def mark_system2_handled(event: Any, reason: str = "") -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        outcome.system2_handled = True
        if (
            not outcome.reply_sent
            and not outcome.output_claim
            and outcome.terminal_status is TurnOutcomeStatus.ACTIVE
        ):
            outcome.terminal_status = TurnOutcomeStatus.SKIPPED
            outcome.terminal_reason = str(reason or "system2_handled_without_reply")
        return _persist(event, outcome)


def mark_deferred_replayed(event: Any, *, reply_sent: bool) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        outcome.deferred_replayed = True
        outcome.deferred_replay_claimed = False
        if not outcome.reply_sent and not reply_sent and outcome.terminal_status is TurnOutcomeStatus.ACTIVE:
            outcome.terminal_status = TurnOutcomeStatus.SKIPPED
            outcome.terminal_reason = "deferred_replayed_without_output"
        return _persist(event, outcome)


def mark_terminal(
    event: Any,
    status: TurnOutcomeStatus | str,
    reason: str,
) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        try:
            requested = status if isinstance(status, TurnOutcomeStatus) else TurnOutcomeStatus(str(status))
        except (TypeError, ValueError):
            outcome.malformed = True
            outcome.terminal_status = TurnOutcomeStatus.FAILED
            outcome.terminal_reason = "invalid_terminal_status"
            return _persist(event, outcome)
        current = outcome.terminal_status
        if current is requested:
            return _persist(event, outcome)
        if requested not in _ALLOWED_STATUS_TRANSITIONS.get(current, set()):
            if current in {
                TurnOutcomeStatus.COMPLETED,
                TurnOutcomeStatus.FALLBACK,
                * _HARD_TERMINAL_STATUSES,
            }:
                return _persist(event, outcome)
            outcome.malformed = True
            outcome.terminal_status = TurnOutcomeStatus.FAILED
            outcome.terminal_reason = "invalid_terminal_transition"
            return _persist(event, outcome)
        outcome.output_claim = ""
        outcome.completion_callback_claimed = False
        outcome.terminal_status = requested
        outcome.terminal_reason = str(reason or requested.value)
        return _persist(event, outcome)


def can_retry(event: Any) -> bool:
    outcome = ensure_turn_outcome(event)
    return bool(
        not outcome.malformed
        and not outcome.reply_sent
        and not outcome.fallback_sent
        and outcome.terminal_status in {
            TurnOutcomeStatus.ACTIVE,
            TurnOutcomeStatus.FAILED,
            TurnOutcomeStatus.RETRYABLE,
        }
    )


def can_deferred_replay(event: Any) -> TurnDecision:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        if outcome.malformed:
            return _decision(event, outcome, allowed=False, reason="malformed_turn_outcome", claim_kind="deferred_replay")
        if outcome.deferred_replayed:
            return _decision(event, outcome, allowed=False, reason="deferred_already_replayed", claim_kind="deferred_replay")
        if outcome.output_claim or outcome.deferred_replay_claimed:
            return _decision(event, outcome, allowed=False, reason="output_claim_exists", claim_kind="deferred_replay")
        if outcome.system2_handled or outcome.completion_callback_completed:
            return _decision(event, outcome, allowed=False, reason="turn_already_handled", claim_kind="deferred_replay")
        if not can_retry(event):
            return _decision(event, outcome, allowed=False, reason="terminal_status", claim_kind="deferred_replay")
        return _decision(event, outcome, allowed=True, reason="available", claim_kind="deferred_replay")


def claim_deferred_replay(event: Any) -> TurnDecision:
    with _event_lock(event):
        decision = can_deferred_replay(event)
        if not decision.allowed:
            return decision
        outcome = _ensure_turn_outcome_unlocked(event)
        outcome.deferred_replay_claimed = True
        _persist(event, outcome)
        return _decision(event, outcome, allowed=True, reason="claimed", claim_kind="deferred_replay")


def release_deferred_replay(event: Any) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        outcome.deferred_replay_claimed = False
        return _persist(event, outcome)


def claim_completion_callback(event: Any) -> TurnDecision:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        if outcome.malformed:
            return _decision(event, outcome, allowed=False, reason="malformed_turn_outcome", claim_kind="completion_callback")
        if outcome.completion_callback_claimed:
            return _decision(event, outcome, allowed=False, reason="completion_callback_claimed", claim_kind="completion_callback")
        if outcome.completion_callback_completed:
            return _decision(event, outcome, allowed=False, reason="completion_callback_completed", claim_kind="completion_callback")
        if outcome.terminal_status in _HARD_TERMINAL_STATUSES:
            return _decision(event, outcome, allowed=False, reason="terminal_status", claim_kind="completion_callback")
        outcome.completion_callback_claimed = True
        _persist(event, outcome)
        return _decision(event, outcome, allowed=True, reason="claimed", claim_kind="completion_callback")


def settle_completion_callback(event: Any, *, succeeded: bool) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        if not outcome.completion_callback_claimed:
            return outcome
        outcome.completion_callback_claimed = False
        if outcome.terminal_status not in _HARD_TERMINAL_STATUSES:
            outcome.completion_callback_completed = bool(succeeded)
        return _persist(event, outcome)


def _can_send_tool_action_unlocked(event: Any, action_key: str) -> TurnDecision:
    key = str(action_key or "").strip()
    outcome = _ensure_turn_outcome_unlocked(event)
    if not key:
        return _decision(event, outcome, allowed=False, reason="missing_action_key", claim_kind="tool_action")
    if outcome.malformed:
        return _decision(event, outcome, allowed=False, reason="malformed_turn_outcome", claim_kind="tool_action")
    if outcome.terminal_status in _HARD_TERMINAL_STATUSES:
        return _decision(event, outcome, allowed=False, reason="terminal_status", claim_kind="tool_action")
    if key in outcome.tool_action_keys:
        return _decision(event, outcome, allowed=False, reason="tool_action_already_sent", claim_kind="tool_action")
    if key in outcome.uncertain_tool_action_keys:
        return _decision(event, outcome, allowed=False, reason="tool_action_uncertain", claim_kind="tool_action")
    if key in outcome.tool_action_claimed_keys:
        return _decision(event, outcome, allowed=False, reason="tool_action_claimed", claim_kind="tool_action")
    return _decision(event, outcome, allowed=True, reason="available", claim_kind="tool_action")


def can_send_tool_action(event: Any, action_key: str) -> TurnDecision:
    with _event_lock(event):
        return _can_send_tool_action_unlocked(event, action_key)


def claim_tool_action(event: Any, action_key: str) -> TurnDecision:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        decision = _can_send_tool_action_unlocked(event, action_key)
        if not decision.allowed:
            return decision
        key = str(action_key or "").strip()
        outcome.tool_action_claimed_keys.append(key)
        _persist(event, outcome)
        return _decision(event, outcome, allowed=True, reason="claimed", claim_kind="tool_action")


def release_tool_action(event: Any, action_key: str) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        key = str(action_key or "").strip()
        if key:
            outcome.tool_action_claimed_keys = [
                item for item in outcome.tool_action_claimed_keys if item != key
            ]
        return _persist(event, outcome)


def record_tool_action_result(
    event: Any,
    action_key: str,
    status: str,
) -> TurnOutcome:
    with _event_lock(event):
        outcome = _ensure_turn_outcome_unlocked(event)
        key = str(action_key or "").strip()
        normalized_status = str(status or "failed").strip().lower()
        if not key or outcome.malformed:
            return _persist(event, outcome)
        outcome.tool_action_claimed_keys = [
            item for item in outcome.tool_action_claimed_keys if item != key
        ]
        if outcome.terminal_status in {
            TurnOutcomeStatus.CANCELLED,
            TurnOutcomeStatus.SHUTDOWN,
            TurnOutcomeStatus.BUDGET_EXHAUSTED,
            TurnOutcomeStatus.SUPERSEDED,
        }:
            return _persist(event, outcome)
        if normalized_status in {"sent", "duplicate"}:
            if key not in outcome.tool_action_keys:
                outcome.tool_action_keys.append(key)
            outcome.uncertain_tool_action_keys = [
                item for item in outcome.uncertain_tool_action_keys if item != key
            ]
            outcome.tool_actions_sent = True
            outcome.tool_action_count = len(outcome.tool_action_keys)
        elif normalized_status in {"uncertain", "in_flight"}:
            if key not in outcome.uncertain_tool_action_keys:
                outcome.uncertain_tool_action_keys.append(key)
            outcome.tool_action_uncertain = True
        return _persist(event, outcome)


__all__ = [
    "TURN_OUTCOME_EXTRA_KEY",
    "TurnDecision",
    "TurnOutcome",
    "TurnOutcomeStatus",
    "can_deferred_replay",
    "claim_deferred_replay",
    "can_retry",
    "can_send_fallback",
    "can_send_text",
    "can_send_tool_action",
    "claim_tool_action",
    "claim_completion_callback",
    "claim_text_output",
    "ensure_turn_outcome",
    "get_turn_outcome",
    "mark_deferred_replayed",
    "mark_system2_handled",
    "mark_terminal",
    "record_text_failed",
    "record_text_sent",
    "release_deferred_replay",
    "record_tool_action_result",
    "release_tool_action",
    "release_text_output",
    "settle_completion_callback",
]
