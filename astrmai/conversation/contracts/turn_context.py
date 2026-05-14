from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


TURN_CONTEXT_EXTRA_KEY = "astrmai_turn_context"


@dataclass
class PerceptionSnapshot:
    chat_id: str = ""
    self_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    rich_text: str = ""
    timestamp: float = 0.0
    image_urls: list[str] = field(default_factory=list)
    is_private: bool = False
    is_direct_wakeup: bool = False
    is_at_bot: bool = False
    is_reply_to_bot: bool = False
    is_name_only_wakeup: bool = False
    is_strong_wakeup: bool = False

    def as_event_context(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "self_id": self.self_id,
            "sender_id": self.sender_id,
            "msg_str": self.text,
            "extracted_images": list(self.image_urls),
            "is_private": self.is_private,
        }


@dataclass
class AttentionSnapshot:
    window_events: list[Any] = field(default_factory=list)
    focus_thread: Any = None
    judge_action: str = ""
    retrieve_keys: list[str] = field(default_factory=list)
    is_fast_mode: bool = False
    is_lightweight_event: bool = False
    focus_reason: str = ""
    root_reason: str = ""


@dataclass
class CognitiveSnapshot:
    action: str = ""
    intent: str = ""
    memory_policy: str = ""
    think_level: int = 1
    think_reason: str = ""
    think_signals: list[str] = field(default_factory=list)
    cognitive_loop_ran: bool = False
    cognitive_loop_skipped_reason: str = ""
    cognitive_loop_skip_signals: list[str] = field(default_factory=list)
    readonly_tools_allowed: bool = False
    readonly_tools_skip_reason: str = ""
    reply_need: str = ""
    social_intent: str = ""
    action_tier: str = ""
    allowed_action_families: list[str] = field(default_factory=list)
    stance: str = ""
    state_bias: str = ""
    risk_flags: list[str] = field(default_factory=list)
    attack_confidence: float = 0.0
    inner_monologue: str = ""


@dataclass
class ContinuitySnapshot:
    agency_reflection_summary: str = ""
    agency_cooldown_tags: list[str] = field(default_factory=list)
    memory_feedback_summary: str = ""
    heartflow_context: str = ""
    heartflow_interest: float = 0.0
    heartflow_talk_willingness: float = 0.0
    heartflow_pulse: str = ""
    heartflow_action: str = ""
    heartflow_urgency: float = 0.0
    heartflow_talk_frequency_adjust: float = 0.0
    heartflow_insert_pressure: float = 0.0
    heartflow_reply_pressure: float = 0.0
    heartflow_candidate_score: float = 0.0
    conversation_summary: str = ""
    current_topic: str = ""
    current_goal: str = ""
    goal_status: str = ""
    continuity_weight: str = ""
    turn_count: int = 0


@dataclass
class MemoryInjectionDecision:
    policy: str = ""
    source: str = ""
    retrieve_keys: list[str] = field(default_factory=list)
    layers: list[str] = field(default_factory=list)
    selected_ids: list[str] = field(default_factory=list)
    trace_id: str = ""
    injected: bool = False
    skip_reason: str = ""
    summary_preview: str = ""


@dataclass
class ExpressionPatternDecision:
    source: str = ""
    selected_ids: list[str] = field(default_factory=list)
    injected: bool = False
    skip_reason: str = ""
    summary_preview: str = ""


@dataclass
class FollowUpSnapshot:
    eligible: bool = False
    skipped_reason: str = ""
    signals: list[str] = field(default_factory=list)
    probability: float = 0.0
    llm_checked: bool = False
    followed: bool = False
    reason: str = ""
    cooldown_until: float = 0.0


@dataclass
class SideInputSnapshot:
    timings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ProactiveSnapshot:
    is_proactive: bool = False
    source: str = ""
    intent_id: str = ""
    reason: str = ""
    guidance_preview: str = ""
    dispatch_status: str = ""
    blocked_reason: str = ""
    synthetic_event_queued: bool = False
    reply_sent: bool = False
    energy_cost: float = 0.0
    cooldown_seconds: float = 0.0


@dataclass
class ToolDecisionTrace:
    requested_tier: str = ""
    final_tier: str = ""
    explicit_tool_intent: bool = False
    social_intent: str = ""
    allowed_families: list[str] = field(default_factory=list)
    initial_tools: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    family_filtered_tools: list[str] = field(default_factory=list)
    filtered_tools: list[str] = field(default_factory=list)
    filter_reasons: list[str] = field(default_factory=list)
    filter_steps: list[dict[str, Any]] = field(default_factory=list)
    removed_by_energy: list[str] = field(default_factory=list)
    removed_by_mood: list[str] = field(default_factory=list)
    removed_by_hostility: list[str] = field(default_factory=list)
    removed_by_cooldown: list[str] = field(default_factory=list)
    removed_by_caution: list[str] = field(default_factory=list)
    removed_by_social_intent: list[str] = field(default_factory=list)

    def record_step(
        self,
        stage: str,
        before: list[str],
        after: list[str],
        reason: str,
        *,
        category: str = "",
    ) -> None:
        removed = [name for name in before if name not in set(after)]
        self.filter_steps.append(
            {
                "stage": str(stage or ""),
                "reason": str(reason or ""),
                "category": str(category or ""),
                "before": list(before or []),
                "after": list(after or []),
                "removed": removed,
            }
        )
        if reason and reason not in self.filter_reasons:
            self.filter_reasons.append(str(reason))
        bucket_name = {
            "energy": "removed_by_energy",
            "mood": "removed_by_mood",
            "hostility": "removed_by_hostility",
            "cooldown": "removed_by_cooldown",
            "caution": "removed_by_caution",
            "social_intent": "removed_by_social_intent",
        }.get(str(category or ""))
        if bucket_name:
            bucket = getattr(self, bucket_name)
            for name in removed:
                if name not in bucket:
                    bucket.append(name)


@dataclass
class ToolSnapshot(ToolDecisionTrace):
    pass


@dataclass
class TurnContext:
    perception: PerceptionSnapshot = field(default_factory=PerceptionSnapshot)
    attention: AttentionSnapshot = field(default_factory=AttentionSnapshot)
    cognitive: CognitiveSnapshot = field(default_factory=CognitiveSnapshot)
    continuity: ContinuitySnapshot = field(default_factory=ContinuitySnapshot)
    memory: MemoryInjectionDecision = field(default_factory=MemoryInjectionDecision)
    expression_patterns: ExpressionPatternDecision = field(default_factory=ExpressionPatternDecision)
    follow_up: FollowUpSnapshot = field(default_factory=FollowUpSnapshot)
    side_inputs: SideInputSnapshot = field(default_factory=SideInputSnapshot)
    proactive: ProactiveSnapshot = field(default_factory=ProactiveSnapshot)
    tools: ToolSnapshot = field(default_factory=ToolSnapshot)
    prompt_envelope: Any = None

    def attach_to_event(self, event: Any) -> "TurnContext":
        if hasattr(event, "set_extra"):
            event.set_extra(TURN_CONTEXT_EXTRA_KEY, self)
        return self


def get_turn_context(event: Any) -> TurnContext | None:
    if not event or not hasattr(event, "get_extra"):
        return None
    value = event.get_extra(TURN_CONTEXT_EXTRA_KEY, None)
    return value if isinstance(value, TurnContext) else None


def ensure_turn_context(event: Any) -> TurnContext:
    current = get_turn_context(event)
    if current is not None:
        return current
    return TurnContext().attach_to_event(event)


def _preview_text(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def build_turn_trace_summary(
    turn_context: TurnContext,
    *,
    created_at: float = 0.0,
    status: str = "executed",
    reply_sent: bool = False,
    reply_preview: str = "",
) -> dict[str, Any]:
    perception = turn_context.perception
    attention = turn_context.attention
    cognitive = turn_context.cognitive
    continuity = turn_context.continuity
    memory = turn_context.memory
    follow_up = turn_context.follow_up
    tools = turn_context.tools

    focus_preview = ""
    prompt_envelope = turn_context.prompt_envelope
    if prompt_envelope is not None:
        focus_preview = _preview_text(
            getattr(prompt_envelope, "focus_message_text", "")
            or getattr(prompt_envelope, "raw_user_text", "")
            or ""
        )

    return {
        "created_at": float(created_at or 0.0),
        "status": str(status or ""),
        "chat_id": perception.chat_id,
        "reply_sent": bool(reply_sent),
        "reply_preview": _preview_text(reply_preview, 120),
        "perception": {
            "chat_id": perception.chat_id,
            "sender_id": perception.sender_id,
            "sender_name": perception.sender_name,
            "text_preview": _preview_text(perception.text or perception.rich_text),
            "image_count": len(perception.image_urls or []),
            "is_private": bool(perception.is_private),
            "is_direct_wakeup": bool(perception.is_direct_wakeup),
            "is_at_bot": bool(perception.is_at_bot),
            "is_reply_to_bot": bool(perception.is_reply_to_bot),
            "is_name_only_wakeup": bool(perception.is_name_only_wakeup),
            "is_strong_wakeup": bool(perception.is_strong_wakeup),
        },
        "attention": {
            "judge_action": attention.judge_action,
            "retrieve_keys": list(attention.retrieve_keys or []),
            "is_fast_mode": bool(attention.is_fast_mode),
            "is_lightweight_event": bool(attention.is_lightweight_event),
            "focus_reason": attention.focus_reason,
            "root_reason": attention.root_reason,
            "window_event_count": len(attention.window_events or []),
            "focus_preview": focus_preview,
        },
        "cognitive": {
            "action": cognitive.action,
            "intent": cognitive.intent,
            "memory_policy": cognitive.memory_policy,
            "think_level": int(cognitive.think_level or 0),
            "think_reason": cognitive.think_reason,
            "think_signals": list(cognitive.think_signals or []),
            "cognitive_loop_ran": bool(cognitive.cognitive_loop_ran),
            "cognitive_loop_skipped_reason": cognitive.cognitive_loop_skipped_reason,
            "cognitive_loop_skip_signals": list(cognitive.cognitive_loop_skip_signals or []),
            "readonly_tools_allowed": bool(cognitive.readonly_tools_allowed),
            "readonly_tools_skip_reason": cognitive.readonly_tools_skip_reason,
            "reply_need": cognitive.reply_need,
            "social_intent": cognitive.social_intent,
            "action_tier": cognitive.action_tier,
            "allowed_action_families": list(cognitive.allowed_action_families or []),
            "stance": cognitive.stance,
            "state_bias": _preview_text(cognitive.state_bias, 120),
            "risk_flags": list(cognitive.risk_flags or []),
            "attack_confidence": float(cognitive.attack_confidence or 0.0),
        },
        "continuity": {
            "has_heartflow_context": bool(continuity.heartflow_context),
            "heartflow_interest": float(continuity.heartflow_interest or 0.0),
            "heartflow_talk_willingness": float(continuity.heartflow_talk_willingness or 0.0),
            "heartflow_pulse": continuity.heartflow_pulse,
            "heartflow_action": continuity.heartflow_action,
            "heartflow_urgency": float(continuity.heartflow_urgency or 0.0),
            "heartflow_talk_frequency_adjust": float(continuity.heartflow_talk_frequency_adjust or 0.0),
            "heartflow_insert_pressure": float(continuity.heartflow_insert_pressure or 0.0),
            "heartflow_reply_pressure": float(continuity.heartflow_reply_pressure or 0.0),
            "heartflow_candidate_score": float(continuity.heartflow_candidate_score or 0.0),
            "agency_cooldown_tags": list(continuity.agency_cooldown_tags or []),
            "has_agency_reflection": bool(continuity.agency_reflection_summary),
            "has_memory_feedback": bool(continuity.memory_feedback_summary),
            "conversation_summary_preview": _preview_text(continuity.conversation_summary, 180),
            "current_topic_preview": _preview_text(continuity.current_topic, 160),
            "current_goal_preview": _preview_text(continuity.current_goal, 180),
            "goal_status": continuity.goal_status,
            "continuity_weight": continuity.continuity_weight,
            "turn_count": int(continuity.turn_count or 0),
            "agency_reflection_preview": _preview_text(continuity.agency_reflection_summary, 160),
            "memory_feedback_preview": _preview_text(continuity.memory_feedback_summary, 160),
        },
        "memory": {
            "policy": memory.policy,
            "source": memory.source,
            "retrieve_keys": list(memory.retrieve_keys or []),
            "layers": list(memory.layers or []),
            "selected_ids": list(memory.selected_ids or []),
            "trace_id": memory.trace_id,
            "injected": bool(memory.injected),
            "skip_reason": memory.skip_reason,
            "summary_preview": _preview_text(memory.summary_preview, 160),
        },
        "expression_patterns": {
            "source": turn_context.expression_patterns.source,
            "selected_ids": list(turn_context.expression_patterns.selected_ids or []),
            "injected": bool(turn_context.expression_patterns.injected),
            "skip_reason": turn_context.expression_patterns.skip_reason,
            "summary_preview": _preview_text(turn_context.expression_patterns.summary_preview, 160),
        },
        "follow_up": {
            "eligible": bool(follow_up.eligible),
            "skipped_reason": follow_up.skipped_reason,
            "signals": list(follow_up.signals or []),
            "probability": float(follow_up.probability or 0.0),
            "llm_checked": bool(follow_up.llm_checked),
            "followed": bool(follow_up.followed),
            "reason": _preview_text(follow_up.reason, 120),
            "cooldown_until": float(follow_up.cooldown_until or 0.0),
        },
        "side_inputs": {
            "timings": list(turn_context.side_inputs.timings or []),
        },
        "proactive": {
            "is_proactive": bool(turn_context.proactive.is_proactive),
            "source": turn_context.proactive.source,
            "intent_id": turn_context.proactive.intent_id,
            "reason": _preview_text(turn_context.proactive.reason, 160),
            "guidance_preview": _preview_text(turn_context.proactive.guidance_preview, 160),
            "dispatch_status": turn_context.proactive.dispatch_status,
            "blocked_reason": turn_context.proactive.blocked_reason,
            "synthetic_event_queued": bool(turn_context.proactive.synthetic_event_queued),
            "reply_sent": bool(turn_context.proactive.reply_sent),
            "energy_cost": float(turn_context.proactive.energy_cost or 0.0),
            "cooldown_seconds": float(turn_context.proactive.cooldown_seconds or 0.0),
        },
        "tools": {
            "requested_tier": tools.requested_tier,
            "final_tier": tools.final_tier,
            "explicit_tool_intent": bool(tools.explicit_tool_intent),
            "social_intent": tools.social_intent,
            "allowed_families": list(tools.allowed_families or []),
            "initial_tools": list(tools.initial_tools or []),
            "available_tools": list(tools.available_tools or []),
            "family_filtered_tools": list(tools.family_filtered_tools or []),
            "filtered_tools": list(tools.filtered_tools or []),
            "filter_reasons": list(tools.filter_reasons or []),
            "filter_steps": list(tools.filter_steps or []),
            "removed_by_energy": list(tools.removed_by_energy or []),
            "removed_by_mood": list(tools.removed_by_mood or []),
            "removed_by_hostility": list(tools.removed_by_hostility or []),
            "removed_by_cooldown": list(tools.removed_by_cooldown or []),
            "removed_by_caution": list(tools.removed_by_caution or []),
            "removed_by_social_intent": list(tools.removed_by_social_intent or []),
        },
    }


__all__ = [
    "AttentionSnapshot",
    "CognitiveSnapshot",
    "ContinuitySnapshot",
    "ExpressionPatternDecision",
    "FollowUpSnapshot",
    "MemoryInjectionDecision",
    "PerceptionSnapshot",
    "ProactiveSnapshot",
    "SideInputSnapshot",
    "ToolDecisionTrace",
    "ToolSnapshot",
    "TURN_CONTEXT_EXTRA_KEY",
    "TurnContext",
    "build_turn_trace_summary",
    "ensure_turn_context",
    "get_turn_context",
]
