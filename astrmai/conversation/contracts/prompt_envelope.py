from __future__ import annotations

from dataclasses import dataclass, field

from .focus_context import FreshnessState, ReplyMode


@dataclass
class PromptEnvelope:
    raw_user_text: str = ""
    recent_transcript: str = ""
    recent_transcript_source: str = ""
    recent_transcript_reason: str = ""
    warm_zone_transcript: str = ""
    warm_zone_transcript_source: str = ""
    warm_zone_summary: str = ""
    warm_zone_quotes: str = ""
    warm_topics_preview: str = ""
    warm_zone_has_latest_assistant: bool = False
    warm_zone_quote_event_ids: list[str] = field(default_factory=list)
    last_assistant_reply: str = ""
    focus_message_text: str = ""
    direct_context_text: str = ""
    related_context_text: str = ""
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
    guidance_lines: list[str] = field(default_factory=list)


__all__ = [
    "FreshnessState",
    "PromptEnvelope",
    "ReplyMode",
]
