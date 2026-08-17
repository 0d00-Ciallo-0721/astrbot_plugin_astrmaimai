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
    turn_target: Any = None
    actor_set: Any = None
    judge_action: str = ""
    prefilter_action: str = ""
    prefilter_reason: str = ""
    retrieve_keys: list[str] = field(default_factory=list)
    is_fast_mode: bool = False
    is_lightweight_event: bool = False
    focus_reason: str = ""
    root_reason: str = ""
    warm_transcript_preview: str = ""
    warm_transcript_source: str = ""
    warm_summary_preview: str = ""
    warm_quotes_preview: str = ""
    warm_topics_preview: str = ""
    recent_transcript_preview: str = ""
    recent_transcript_reason: str = ""
    recent_transcript_used: bool = False
    reply_prompt_focus_anchor: str = ""
    cold_summary_preview: str = ""


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
    dialogue_store_version: str = ""
    compaction_status: str = ""
    compaction_eligibility_reason: str = ""
    recompact_armed: bool = False
    focus_tail_overlap: bool = False
    delta_old_segments: int = 0
    delta_old_message_load: float = 0.0
    delta_old_long_message_count: int = 0
    cold_summary_section_counts: dict[str, int] = field(default_factory=dict)
    prefix_hash: str = ""
    semantic_system_hash: str = ""
    semantic_system_length: int = 0
    prefix_stable: bool = False
    prefix_changed_reason: str = ""
    system_prompt_length: int = 0
    prompt_length: int = 0
    gateway_system_hash: str = ""
    gateway_prompt_hash: str = ""
    provider_visible_system_hash: str = ""
    provider_visible_prompt_hash: str = ""
    post_hook_system_hash: str = ""
    request_session_id: str = ""
    request_cache_control: str = ""
    request_provider_family: str = ""
    request_model_id: str = ""
    usage_input_tokens: int = 0
    usage_input_cached: int = 0
    usage_output_tokens: int = 0
    cache_ready: bool = False
    cache_hit: bool = False
    cache_ready_reasons: list[str] = field(default_factory=list)
    cache_hit_evidence_supported: bool = False
    frozen_prefix_length: int = 0
    semi_stable_length: int = 0
    dynamic_prompt_length: int = 0
    frozen_prefix_blocks: dict[str, int] = field(default_factory=dict)
    semi_stable_blocks: dict[str, int] = field(default_factory=dict)
    dynamic_prompt_blocks: dict[str, int] = field(default_factory=dict)
    system_rules_items: list[dict[str, Any]] = field(default_factory=list)
    system_rules_candidate_items: list[str] = field(default_factory=list)
    message_count_since_last_compaction: int = 0
    next_eval_at_count: int = 80
    final_score: float = 0.0
    count_score: float = 0.0
    closure_score: float = 0.0
    tail_activity_score: float = 0.0
    topic_density_score: float = 0.0
    stability_score: float = 0.0
    benefit_score: float = 0.0
    is_forced: bool = False
    is_safe_to_compact: bool = False
    closure_signals: list[str] = field(default_factory=list)
    tail_activity_signals: list[str] = field(default_factory=list)
    topic_density_signals: list[str] = field(default_factory=list)
    stability_signals: list[str] = field(default_factory=list)
    benefit_signals: list[str] = field(default_factory=list)
    forced_pending_message_delta: int = 0
    last_safe_window_seen_at_count: int = 0
    post_compaction_recovery_rounds: int = 0
    evaluation_count: int = 0
    current_message_count: int = 0
    queued_eval_node: int = 0
    pending_eval_nodes_count: int = 0
    pending_eval_nodes: list[int] = field(default_factory=list)
    force_execute_on_next_safe_hook: bool = False
    safe_hook_block_reason: str = ""
    last_hook_source: str = ""
    last_safe_hook_checked_at: int = 0


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
    actor_whitelist: list[str] = field(default_factory=list)
    suppressed_candidate_ids: list[str] = field(default_factory=list)
    suppressed_candidate_count: int = 0


@dataclass
class ExpressionPatternDecision:
    source: str = ""
    selected_ids: list[str] = field(default_factory=list)
    injected: bool = False
    skip_reason: str = ""
    summary_preview: str = ""


@dataclass
class JargonDecision:
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
    chat_kind: str = ""
    captured_generation: int = 0
    generation_current: bool = True
    claim_token_present: bool = False
    last_real_user_activity_at: float = 0.0
    last_committed_bot_reply_at: float = 0.0
    next_due_at: float = 0.0
    unanswered_count: int = 0
    cancel_reason: str = ""


@dataclass
class ToolDecisionTrace:
    requested_tier: str = ""
    final_tier: str = ""
    disclosure_enabled: bool = False
    disclosure_tier: str = ""
    disclosure_packages: list[str] = field(default_factory=list)
    disclosure_reasons: list[str] = field(default_factory=list)
    disclosure_second_pass_packages: list[str] = field(default_factory=list)
    disclosure_expanded_packages: list[str] = field(default_factory=list)
    disclosure_decisions: list[dict[str, Any]] = field(default_factory=list)
    preselected_tools: list[str] = field(default_factory=list)
    hidden_requestable_tools: list[str] = field(default_factory=list)
    disclosure_request_source: str = ""
    disclosure_requested_tools: list[str] = field(default_factory=list)
    disclosure_rejected_requests: list[dict[str, Any]] = field(default_factory=list)
    explicit_tool_intent: bool = False
    invocation_mode: str = "auto"
    required_tools: list[str] = field(default_factory=list)
    invocation_plans: list[dict[str, Any]] = field(default_factory=list)
    intent_contracts: list[dict[str, Any]] = field(default_factory=list)
    contract_outcomes: list[dict[str, Any]] = field(default_factory=list)
    contract_unsatisfied: list[str] = field(default_factory=list)
    correction_pass_used: bool = False
    correction_packages: list[str] = field(default_factory=list)
    correction_reason: str = ""
    second_pass_resolution: str = ""
    second_pass_selected_tools: list[str] = field(default_factory=list)
    second_pass_reason: str = ""
    second_pass_added_tools: list[str] = field(default_factory=list)
    second_pass_tool_executed: bool = False
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
    removed_by_stance: list[str] = field(default_factory=list)

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
            "stance": "removed_by_stance",
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
    jargon: JargonDecision = field(default_factory=JargonDecision)
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
            "prefilter_action": attention.prefilter_action,
            "prefilter_reason": attention.prefilter_reason,
            "retrieve_keys": list(attention.retrieve_keys or []),
            "is_fast_mode": bool(attention.is_fast_mode),
            "is_lightweight_event": bool(attention.is_lightweight_event),
            "focus_reason": attention.focus_reason,
            "root_reason": attention.root_reason,
            "turn_target": (
                attention.turn_target.as_dict()
                if hasattr(attention.turn_target, "as_dict")
                else dict(attention.turn_target or {})
                if isinstance(attention.turn_target, dict)
                else {}
            ),
            "actor_set": (
                attention.actor_set.as_dict()
                if hasattr(attention.actor_set, "as_dict")
                else dict(attention.actor_set or {})
                if isinstance(attention.actor_set, dict)
                else {}
            ),
            "window_event_count": len(attention.window_events or []),
            "focus_preview": focus_preview,
            "warm_transcript_preview": _preview_text(getattr(attention, "warm_transcript_preview", ""), 180),
            "warm_transcript_source": getattr(attention, "warm_transcript_source", ""),
            "warm_summary_preview": _preview_text(getattr(attention, "warm_summary_preview", ""), 180),
            "warm_quotes_preview": _preview_text(getattr(attention, "warm_quotes_preview", ""), 180),
            "warm_topics_preview": _preview_text(getattr(attention, "warm_topics_preview", ""), 180),
            "recent_transcript_preview": _preview_text(getattr(attention, "recent_transcript_preview", ""), 180),
            "recent_transcript_reason": getattr(attention, "recent_transcript_reason", ""),
            "recent_transcript_used": bool(getattr(attention, "recent_transcript_used", False)),
            "reply_prompt_focus_anchor": _preview_text(getattr(attention, "reply_prompt_focus_anchor", ""), 180),
            "cold_summary_preview": _preview_text(getattr(attention, "cold_summary_preview", ""), 180),
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
            "dialogue_store_version": continuity.dialogue_store_version,
            "compaction_status": continuity.compaction_status,
            "compaction_eligibility_reason": continuity.compaction_eligibility_reason,
            "recompact_armed": bool(continuity.recompact_armed),
            "focus_tail_overlap": bool(continuity.focus_tail_overlap),
            "delta_old_segments": int(continuity.delta_old_segments or 0),
            "delta_old_message_load": float(continuity.delta_old_message_load or 0.0),
            "delta_old_long_message_count": int(continuity.delta_old_long_message_count or 0),
            "cold_summary_section_counts": dict(continuity.cold_summary_section_counts or {}),
            "prefix_hash": continuity.prefix_hash,
            "semantic_system_hash": continuity.semantic_system_hash,
            "semantic_system_length": int(continuity.semantic_system_length or 0),
            "prefix_stable": bool(continuity.prefix_stable),
            "prefix_changed_reason": continuity.prefix_changed_reason,
            "system_prompt_length": int(continuity.system_prompt_length or 0),
            "prompt_length": int(continuity.prompt_length or 0),
            "gateway_system_hash": continuity.gateway_system_hash,
            "gateway_prompt_hash": continuity.gateway_prompt_hash,
            "provider_visible_system_hash": continuity.provider_visible_system_hash,
            "provider_visible_prompt_hash": continuity.provider_visible_prompt_hash,
            "post_hook_system_hash": continuity.post_hook_system_hash,
            "request_session_id": continuity.request_session_id,
            "request_cache_control": continuity.request_cache_control,
            "request_provider_family": continuity.request_provider_family,
            "request_model_id": continuity.request_model_id,
            "usage_input_tokens": int(continuity.usage_input_tokens or 0),
            "usage_input_cached": int(continuity.usage_input_cached or 0),
            "usage_output_tokens": int(continuity.usage_output_tokens or 0),
            "cache_ready": bool(continuity.cache_ready),
            "cache_hit": bool(continuity.cache_hit),
            "cache_ready_reasons": list(continuity.cache_ready_reasons or []),
            "cache_hit_evidence_supported": bool(continuity.cache_hit_evidence_supported),
            "frozen_prefix_length": int(continuity.frozen_prefix_length or 0),
            "semi_stable_length": int(continuity.semi_stable_length or 0),
            "dynamic_prompt_length": int(continuity.dynamic_prompt_length or 0),
            "frozen_prefix_blocks": dict(continuity.frozen_prefix_blocks or {}),
            "semi_stable_blocks": dict(continuity.semi_stable_blocks or {}),
            "dynamic_prompt_blocks": dict(continuity.dynamic_prompt_blocks or {}),
            "system_rules_items": list(continuity.system_rules_items or []),
            "system_rules_candidate_items": list(continuity.system_rules_candidate_items or []),
            "message_count_since_last_compaction": int(continuity.message_count_since_last_compaction or 0),
            "next_eval_at_count": int(continuity.next_eval_at_count or 80),
            "final_score": float(continuity.final_score or 0.0),
            "count_score": float(continuity.count_score or 0.0),
            "closure_score": float(continuity.closure_score or 0.0),
            "tail_activity_score": float(continuity.tail_activity_score or 0.0),
            "topic_density_score": float(continuity.topic_density_score or 0.0),
            "stability_score": float(continuity.stability_score or 0.0),
            "benefit_score": float(continuity.benefit_score or 0.0),
            "is_forced": bool(continuity.is_forced),
            "is_safe_to_compact": bool(continuity.is_safe_to_compact),
            "closure_signals": list(continuity.closure_signals or []),
            "tail_activity_signals": list(continuity.tail_activity_signals or []),
            "topic_density_signals": list(continuity.topic_density_signals or []),
            "stability_signals": list(continuity.stability_signals or []),
            "benefit_signals": list(continuity.benefit_signals or []),
            "forced_pending_message_delta": int(continuity.forced_pending_message_delta or 0),
            "last_safe_window_seen_at_count": int(continuity.last_safe_window_seen_at_count or 0),
            "post_compaction_recovery_rounds": int(continuity.post_compaction_recovery_rounds or 0),
            "evaluation_count": int(continuity.evaluation_count or 0),
            "current_message_count": int(continuity.current_message_count or 0),
            "queued_eval_node": int(continuity.queued_eval_node or 0),
            "pending_eval_nodes_count": int(continuity.pending_eval_nodes_count or 0),
            "pending_eval_nodes": list(continuity.pending_eval_nodes or []),
            "force_execute_on_next_safe_hook": bool(continuity.force_execute_on_next_safe_hook),
            "safe_hook_block_reason": continuity.safe_hook_block_reason,
            "last_hook_source": continuity.last_hook_source,
            "last_safe_hook_checked_at": int(continuity.last_safe_hook_checked_at or 0),
        },
        "conversation_compression": {
            "warm_summary_preview": _preview_text(getattr(attention, "warm_summary_preview", ""), 180),
            "warm_quotes_preview": _preview_text(getattr(attention, "warm_quotes_preview", ""), 180),
            "warm_topics_preview": _preview_text(getattr(attention, "warm_topics_preview", ""), 180),
            "recent_transcript_preview": _preview_text(getattr(attention, "recent_transcript_preview", ""), 180),
            "recent_used": bool(getattr(attention, "recent_transcript_used", False)),
            "recent_reason": getattr(attention, "recent_transcript_reason", ""),
            "reply_prompt_focus_anchor": _preview_text(getattr(attention, "reply_prompt_focus_anchor", ""), 180),
            "compaction_state": continuity.compaction_status,
            "eligibility_reason": continuity.compaction_eligibility_reason,
            "focus_tail_overlap": bool(continuity.focus_tail_overlap),
            "recompact_armed": bool(continuity.recompact_armed),
            "message_count_since_last_compaction": int(continuity.message_count_since_last_compaction or 0),
            "next_eval_at_count": int(continuity.next_eval_at_count or 80),
            "final_score": float(continuity.final_score or 0.0),
            "count_score": float(continuity.count_score or 0.0),
            "closure_score": float(continuity.closure_score or 0.0),
            "tail_activity_score": float(continuity.tail_activity_score or 0.0),
            "topic_density_score": float(continuity.topic_density_score or 0.0),
            "stability_score": float(continuity.stability_score or 0.0),
            "benefit_score": float(continuity.benefit_score or 0.0),
            "is_forced": bool(continuity.is_forced),
            "is_safe_to_compact": bool(continuity.is_safe_to_compact),
            "closure_signals": list(continuity.closure_signals or []),
            "tail_activity_signals": list(continuity.tail_activity_signals or []),
            "topic_density_signals": list(continuity.topic_density_signals or []),
            "stability_signals": list(continuity.stability_signals or []),
            "benefit_signals": list(continuity.benefit_signals or []),
            "forced_pending_message_delta": int(continuity.forced_pending_message_delta or 0),
            "last_safe_window_seen_at_count": int(continuity.last_safe_window_seen_at_count or 0),
            "post_compaction_recovery_rounds": int(continuity.post_compaction_recovery_rounds or 0),
            "evaluation_count": int(continuity.evaluation_count or 0),
            "current_message_count": int(continuity.current_message_count or 0),
            "queued_eval_node": int(continuity.queued_eval_node or 0),
            "pending_eval_nodes_count": int(continuity.pending_eval_nodes_count or 0),
            "pending_eval_nodes": list(continuity.pending_eval_nodes or []),
            "force_execute_on_next_safe_hook": bool(continuity.force_execute_on_next_safe_hook),
            "safe_hook_block_reason": continuity.safe_hook_block_reason,
            "last_hook_source": continuity.last_hook_source,
            "last_safe_hook_checked_at": int(continuity.last_safe_hook_checked_at or 0),
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
            "actor_whitelist": list(memory.actor_whitelist or []),
            "suppressed_candidate_ids": list(memory.suppressed_candidate_ids or []),
            "suppressed_candidate_count": int(memory.suppressed_candidate_count or 0),
        },
        "expression_patterns": {
            "source": turn_context.expression_patterns.source,
            "selected_ids": list(turn_context.expression_patterns.selected_ids or []),
            "injected": bool(turn_context.expression_patterns.injected),
            "skip_reason": turn_context.expression_patterns.skip_reason,
            "summary_preview": _preview_text(turn_context.expression_patterns.summary_preview, 160),
        },
        "jargon": {
            "source": turn_context.jargon.source,
            "selected_ids": list(turn_context.jargon.selected_ids or []),
            "injected": bool(turn_context.jargon.injected),
            "skip_reason": turn_context.jargon.skip_reason,
            "summary_preview": _preview_text(turn_context.jargon.summary_preview, 160),
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
            "disclosure_enabled": bool(tools.disclosure_enabled),
            "disclosure_tier": tools.disclosure_tier,
            "disclosure_packages": list(tools.disclosure_packages or []),
            "disclosure_reasons": list(tools.disclosure_reasons or []),
            "disclosure_second_pass_packages": list(tools.disclosure_second_pass_packages or []),
            "disclosure_expanded_packages": list(tools.disclosure_expanded_packages or []),
            "disclosure_decisions": list(tools.disclosure_decisions or []),
            "preselected_tools": list(tools.preselected_tools or []),
            "hidden_requestable_tools": list(tools.hidden_requestable_tools or []),
            "disclosure_request_source": tools.disclosure_request_source,
            "disclosure_requested_tools": list(tools.disclosure_requested_tools or []),
            "disclosure_rejected_requests": list(tools.disclosure_rejected_requests or []),
            "explicit_tool_intent": bool(tools.explicit_tool_intent),
            "invocation_mode": tools.invocation_mode,
            "required_tools": list(tools.required_tools or []),
            "invocation_plans": list(tools.invocation_plans or []),
            "intent_contracts": list(tools.intent_contracts or []),
            "contract_outcomes": list(tools.contract_outcomes or []),
            "contract_unsatisfied": list(tools.contract_unsatisfied or []),
            "correction_pass_used": bool(tools.correction_pass_used),
            "correction_packages": list(tools.correction_packages or []),
            "correction_reason": tools.correction_reason,
            "second_pass_resolution": tools.second_pass_resolution,
            "second_pass_selected_tools": list(tools.second_pass_selected_tools or []),
            "second_pass_reason": tools.second_pass_reason,
            "second_pass_added_tools": list(tools.second_pass_added_tools or []),
            "second_pass_tool_executed": bool(tools.second_pass_tool_executed),
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
            "removed_by_stance": list(tools.removed_by_stance or []),
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
