from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FailureKind(str, Enum):
    NONE = "none"
    EMPTY_RESPONSE = "empty_response"
    PROVIDER_FAILURE_TEXT = "provider_failure_text"
    BAD_PAYLOAD = "bad_payload"
    JSON_DECODE_ERROR = "json_decode_error"
    TIMEOUT = "timeout"
    CASCADE_FAILURE = "cascade_failure"
    UNKNOWN = "unknown"


class ReplyMode(str, Enum):
    PLAYFUL_INTERACTION = "playful_interaction"
    EMOTIONAL_SUPPORT = "emotional_support"
    DIRECT_QUESTION = "direct_question"
    CASUAL_FOLLOWUP = "casual_followup"
    IMAGE_REACTION = "image_reaction"
    LATE_RECONNECT = "late_reconnect"
    AMBIENT_IGNORE = "ambient_ignore"


class FreshnessState(str, Enum):
    FRESH = "fresh"
    STALE_BUT_SALVAGEABLE = "stale_but_salvageable"
    EXPIRED = "expired"


@dataclass
class VisionBundle:
    image_urls: List[str] = field(default_factory=list)
    direct_image_urls: List[str] = field(default_factory=list)
    is_direct_request: bool = False
    is_image_only: bool = False
    source: str = ""


@dataclass
class ReplyFreshnessBudget:
    state: FreshnessState = FreshnessState.FRESH
    created_at: float = 0.0
    max_age_seconds: float = 0.0
    salvage_window_seconds: float = 0.0
    latest_activity_ts: float = 0.0
    stale_reason: str = ""


@dataclass
class FocusThreadContext:
    focus_event: Any
    root_event: Any = None
    core_events: List[Any] = field(default_factory=list)
    related_events: List[Any] = field(default_factory=list)
    ambient_events: List[Any] = field(default_factory=list)
    focus_reason: str = ""
    root_reason: str = ""
    focus_message_text: str = ""
    focus_sender_id: str = ""
    focus_sender_name: str = ""
    reply_mode: ReplyMode = ReplyMode.CASUAL_FOLLOWUP
    social_state: str = ""
    thread_signature: str = ""
    freshness_budget: ReplyFreshnessBudget = field(default_factory=ReplyFreshnessBudget)
    vision_bundle: VisionBundle = field(default_factory=VisionBundle)

    def all_thread_events(self) -> List[Any]:
        merged: List[Any] = []
        for candidate in [self.root_event, self.focus_event, *self.core_events, *self.related_events]:
            if candidate is None or candidate in merged:
                continue
            merged.append(candidate)
        return merged

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


@dataclass
class PromptEnvelope:
    raw_user_text: str = ""
    recent_transcript: str = ""
    last_assistant_reply: str = ""
    focus_thread_text: str = ""
    ambient_background_text: str = ""
    focus_reason: str = ""
    focus_thread_reason: str = ""
    near_context_priority: bool = False
    reply_mode: ReplyMode = ReplyMode.CASUAL_FOLLOWUP
    social_state: str = ""
    freshness_state: FreshnessState = FreshnessState.FRESH
    thread_signature: str = ""
    state_block: str = ""
    memory_block: str = ""
    guidance_lines: List[str] = field(default_factory=list)

    def planner_sections(self) -> List[str]:
        sections: List[str] = []
        if self.last_assistant_reply:
            sections.append(f"你上一句刚说过：{self.last_assistant_reply}")
        if self.focus_thread_text:
            sections.append(f"请优先接住这条对话线索并回答：\n{self.focus_thread_text}")
        if self.ambient_background_text:
            sections.append(f"其他背景只作参考，不必逐条回应：\n{self.ambient_background_text}")
        return sections

    def planner_prompt(self) -> str:
        return "\n\n".join(section for section in self.planner_sections() if section)

    def current_block(self) -> str:
        focus_block = self.focus_thread_text or self.raw_user_text
        sections: List[str] = []
        if focus_block:
            sections.append(f"请优先接住这条对话线索并回答：\n{focus_block}")
        if self.ambient_background_text:
            sections.append(f"其他背景只作参考，不必逐条回应：\n{self.ambient_background_text}")
        return "\n\n".join(section for section in sections if section).strip()


@dataclass
class SocialTranscriptTurn:
    speaker_name: str
    target_name: str = ""
    turn_type: str = "message"
    content: str = ""
    relative_time: str = ""
    reply_mode_hint: str = ""


@dataclass
class LLMCallResult:
    ok: bool
    text: str = ""
    parsed_json: Any = None
    error_kind: FailureKind = FailureKind.NONE
    error_message: str = ""
    model_id: str = ""
    provider_family: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    raw_completion: str = ""


@dataclass
class VisibleReplyArtifact:
    visible_text: str
    segments: List[str]
    persistable_text: str
    blocked_reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    sent: bool = False

    @property
    def blocked(self) -> bool:
        return not self.visible_text or bool(self.blocked_reason)


@dataclass
class OutboundPolicy:
    should_send: bool = True
    freshness_state: FreshnessState = FreshnessState.FRESH
    length_class: str = "normal"
    segment_strategy: str = "default"
    late_rewrite_allowed: bool = False
    send_delay_profile: str = "default"
    blocked_reason: str = ""
