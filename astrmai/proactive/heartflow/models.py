from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class HeartflowChatState:
    chat_id: str
    last_tick_ts: float
    last_activity_ts: float
    interest: float
    engagement: float
    talk_willingness: float
    silence_pressure: float
    fatigue: float
    mood_bias: float
    current_focus: str
    recent_impulse: str
    cooldown_tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HeartflowSessionState:
    chat_id: str
    started_at: float
    last_activity_ts: float
    last_tick_ts: float
    expires_at: float
    tick_count: int = 0
    recent_message_count: int = 0
    recent_direct_count: int = 0
    recent_bot_reply_count: int = 0
    consecutive_observe_count: int = 0
    consecutive_no_reply_count: int = 0
    consecutive_prepare_count: int = 0
    last_impulse: str = ""
    last_visible_candidate_ts: float = 0.0
    last_bot_reply_ts: float = 0.0
    last_user_direct_ts: float = 0.0
    talk_frequency_adjust: float = 1.0
    insert_pressure: float = 0.0
    reply_pressure: float = 0.0
    direct_relevance: float = 0.0
    visible_candidate_score: float = 0.0
    frequency_components: dict[str, float] = field(default_factory=dict)
    topic_heat: float = 0.0
    low_cost_retained: bool = False


@dataclass(slots=True)
class HeartflowActionDecision:
    chat_id: str
    timestamp: float
    action_type: str
    reason: str
    guidance: str
    should_dispatch_candidate: bool = False
    blocked_reason: str = ""
    safety_checks: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class HeartflowPulse:
    chat_id: str
    timestamp: float
    pulse_type: str
    reason: str
    guidance: str
    suggested_social_intent: str
    suggested_action_tier: str
    urgency: float
    visible_action_allowed: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class HeartflowImpulseDecision:
    chat_id: str
    timestamp: float
    pulse_type: str
    visible_candidate_allowed: bool = False
    requires_synthetic_event: bool = False
    hidden_only: bool = True
    dispatch_enabled: bool = False
    synthetic_event_queued: bool = False
    blocked_reason: str = ""
    safety_checks: dict[str, object] = field(default_factory=dict)
    synthetic_event_preview: str = ""


@dataclass(slots=True)
class HeartflowTopicDigest:
    chat_id: str
    timestamp: float
    status: str
    summary: str = ""
    guidance: str = ""
    tags: list[str] = field(default_factory=list)
    importance: float = 0.0
    skip_reason: str = ""
    next_allowed_ts: float = 0.0
    source: str = "heartflow_topic_digest"


__all__ = [
    "HeartflowActionDecision",
    "HeartflowChatState",
    "HeartflowImpulseDecision",
    "HeartflowPulse",
    "HeartflowSessionState",
    "HeartflowTopicDigest",
]
