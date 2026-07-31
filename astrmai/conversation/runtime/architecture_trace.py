from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..contracts.committed_reply import CommittedBotTurn, ReplyPlan
from ..contracts.conversation_event import ConversationEvent
from ..contracts.turn_context import TurnContext
from ..contracts.turn_target import ActorSet, TurnTarget
from .architecture_rollout import (
    get_architecture_observation,
    rollout_state,
    stable_structure_hash,
)


ARCHITECTURE_TRACE_SCHEMA_VERSION = 1


def _ordered_unique(values: Iterable[Any], *, limit: int = 64) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result[-max(1, int(limit or 1)) :]


def _extra(event: Any, key: str, default: Any = None) -> Any:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except Exception:
        return default


def _canonical_events(event: Any, turn_context: TurnContext) -> list[ConversationEvent]:
    candidates: list[Any] = [
        _extra(event, "astrmai_conversation_event", None),
        *list(turn_context.attention.window_events or []),
    ]
    result: list[ConversationEvent] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, ConversationEvent):
            candidate = _extra(candidate, "astrmai_conversation_event", None)
        if not isinstance(candidate, ConversationEvent) or candidate.event_id in seen:
            continue
        seen.add(candidate.event_id)
        result.append(candidate)
    return result


def _target_payload(value: Any) -> dict[str, Any]:
    target = TurnTarget.from_value(value)
    return {
        "target_kind": target.target_kind.value,
        "target_actor_id": target.target_actor_id,
        "target_event_id": target.target_event_id,
        "topic_epoch": int(target.topic_epoch or 0),
        "source_event_ids": list(target.source_event_ids),
        "evidence": target.evidence,
        "confidence": float(target.confidence or 0.0),
        "resolved_by": target.resolved_by,
    }


def _reply_plan_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, ReplyPlan):
        segments = tuple(value.planned_segments or ())
        return {
            "plan_id": value.plan_id,
            "turn_id": value.turn_id,
            "response_kind": value.response_kind,
            "shape_policy": value.shape_policy,
            "target": _target_payload(value.target),
            "planned_segment_count": len(segments),
            "planned_attachment_count": len(value.planned_attachments or ()),
            "planned_char_count": len(value.planned_text or ""),
            "planned_text_hash": stable_structure_hash(value.planned_text or ""),
        }
    if not isinstance(value, Mapping):
        return {}
    segments = list(value.get("planned_segments", ()) or ())
    text = str(value.get("planned_text", "") or "")
    return {
        "plan_id": str(value.get("plan_id", "") or ""),
        "turn_id": str(value.get("turn_id", "") or ""),
        "response_kind": str(value.get("response_kind", "") or ""),
        "shape_policy": str(value.get("shape_policy", "") or ""),
        "target": _target_payload(value.get("target", {})),
        "planned_segment_count": len(segments),
        "planned_attachment_count": len(value.get("planned_attachments", ()) or ()),
        "planned_char_count": len(text),
        "planned_text_hash": stable_structure_hash(text),
    }


def _reply_commit_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, CommittedBotTurn):
        return {
            "commit_id": value.commit_id,
            "turn_id": value.turn_id,
            "plan_id": value.plan_id,
            "send_status": value.send_status.value,
            "target": _target_payload(value.target),
            "source_event_ids": list(value.source_event_ids),
            "sent_segment_count": len(value.sent_segments or ()),
            "sent_attachment_count": len(value.sent_attachment_refs or ()),
            "outbound_message_count": len(value.outbound_message_ids or ()),
            "partial_send": bool(value.partial_send),
            "reply_hash": value.reply_hash,
            "failure_reason": value.failure_reason,
        }
    if not isinstance(value, Mapping):
        return {}
    return {
        "commit_id": str(value.get("commit_id", "") or ""),
        "turn_id": str(value.get("turn_id", "") or ""),
        "plan_id": str(value.get("plan_id", "") or ""),
        "send_status": str(value.get("send_status", "") or ""),
        "target": _target_payload(value.get("target", {})),
        "source_event_ids": _ordered_unique(value.get("source_event_ids", ()) or ()),
        "sent_segment_count": len(value.get("sent_segments", ()) or ()),
        "sent_attachment_count": len(value.get("sent_attachment_refs", ()) or ()),
        "outbound_message_count": len(value.get("outbound_message_ids", ()) or ()),
        "partial_send": bool(value.get("partial_send", False)),
        "reply_hash": str(value.get("reply_hash", "") or ""),
        "failure_reason": str(value.get("failure_reason", "") or ""),
    }


def build_architecture_trace_contract(
    *,
    event: Any,
    turn_context: TurnContext,
    trace_item: Mapping[str, Any],
    status: str,
    config: Any = None,
) -> dict[str, Any]:
    canonical_events = _canonical_events(event, turn_context)
    focus_event = _extra(event, "astrmai_conversation_event", None)
    target = TurnTarget.from_value(turn_context.attention.turn_target)
    actor_set = ActorSet.from_value(turn_context.attention.actor_set)
    architecture_observation = get_architecture_observation(event)
    memory_funnel = trace_item.get("memory_funnel", {})
    if not isinstance(memory_funnel, Mapping):
        memory_funnel = {}
    memory_filter = {
        "actor_whitelist": list(turn_context.memory.actor_whitelist or actor_set.allowed_actor_ids),
        "selected_count": len(turn_context.memory.selected_ids or []),
        "suppressed_candidate_count": int(
            turn_context.memory.suppressed_candidate_count
            or memory_funnel.get("actor_scope_suppressed_count", 0)
            or 0
        ),
        "suppressed_candidate_ids": _ordered_unique(
            turn_context.memory.suppressed_candidate_ids or (), limit=32
        ),
        "trace_id": turn_context.memory.trace_id,
        "observation": dict(architecture_observation.get("memory_actor_filter", {}) or {}),
    }
    proactive = turn_context.proactive
    participation = {
        "prefilter_action": str(
            _extra(event, "astrmai_attention_prefilter_action", "")
            or turn_context.attention.prefilter_action
            or ""
        ),
        "prefilter_reason": str(
            _extra(event, "astrmai_attention_prefilter_reason", "")
            or turn_context.attention.prefilter_reason
            or ""
        ),
        "shadow_action": str(_extra(event, "astrmai_prefilter_shadow_action", "") or ""),
        "score": float(_extra(event, "astrmai_participation_score", 0.0) or 0.0),
        "signals": _ordered_unique(_extra(event, "astrmai_participation_signals", ()) or ()),
        "phase": str(_extra(event, "astrmai_participation_phase", "") or ""),
        "phase_age_ms": float(_extra(event, "astrmai_participation_phase_age_ms", 0.0) or 0.0),
    }
    context_stats = list(trace_item.get("context_block_stats", ()) or ())
    return {
        "schema_version": ARCHITECTURE_TRACE_SCHEMA_VERSION,
        "turn_id": str(trace_item.get("turn_id", "") or ""),
        "input_event_ids": _ordered_unique(
            [event.event_id for event in canonical_events]
            or target.source_event_ids
        ),
        "canonical_event_status": {
            "present": isinstance(focus_event, ConversationEvent),
            "schema_version": int(getattr(focus_event, "schema_version", 0) or 0),
            "event_id_source": str(getattr(focus_event, "event_id_source", "") or ""),
            "provenance": str(getattr(focus_event, "provenance", "") or ""),
            "message_kind": str(getattr(focus_event, "message_kind", "") or ""),
            "observation": dict(architecture_observation.get("canonical_event", {}) or {}),
        },
        "turn_target": _target_payload(target),
        "actor_whitelist": list(actor_set.allowed_actor_ids),
        "participation_decision": participation,
        "judge_decision": {
            "action": str(turn_context.attention.judge_action or ""),
            "outcome": str(_extra(event, "astrmai_judge_outcome", "") or ""),
            "timeout": bool(_extra(event, "astrmai_judge_timeout", False)),
        },
        "context_block_stats": context_stats,
        "reply_plan": _reply_plan_payload(_extra(event, "astrmai_reply_plan", None)),
        "reply_commit": _reply_commit_payload(
            _extra(event, "astrmai_committed_bot_turn", None)
        ),
        "memory_actor_filter": memory_filter,
        "proactive_observation": {
            "is_proactive": bool(proactive.is_proactive),
            "source": proactive.source,
            "intent_id": proactive.intent_id,
            "dispatch_status": proactive.dispatch_status,
            "blocked_reason": proactive.blocked_reason,
            "captured_generation": int(proactive.captured_generation or 0),
            "generation_current": bool(proactive.generation_current),
            "claim_token_present": bool(proactive.claim_token_present),
            "last_real_user_activity_at": float(proactive.last_real_user_activity_at or 0.0),
            "last_committed_bot_reply_at": float(proactive.last_committed_bot_reply_at or 0.0),
            "next_due_at": float(proactive.next_due_at or 0.0),
            "unanswered_count": int(proactive.unanswered_count or 0),
            "cancel_reason": proactive.cancel_reason,
        },
        "architecture_observation": architecture_observation,
        "rollout": rollout_state(config),
        "status": str(status or ""),
        "elapsed_ms": float(trace_item.get("turn_total_elapsed_ms", 0.0) or 0.0),
    }


__all__ = [
    "ARCHITECTURE_TRACE_SCHEMA_VERSION",
    "build_architecture_trace_contract",
]
