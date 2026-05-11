from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


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
    image_urls: list[str] = field(default_factory=list)
    direct_image_urls: list[str] = field(default_factory=list)
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
    core_events: list[Any] = field(default_factory=list)
    related_events: list[Any] = field(default_factory=list)
    ambient_events: list[Any] = field(default_factory=list)
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

    def all_thread_events(self) -> list[Any]:
        merged: list[Any] = []
        for candidate in [self.root_event, self.focus_event, *self.core_events, *self.related_events]:
            if candidate is None or candidate in merged:
                continue
            merged.append(candidate)
        return merged

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)


__all__ = [
    "FocusThreadContext",
    "FreshnessState",
    "ReplyFreshnessBudget",
    "ReplyMode",
    "VisionBundle",
]
