from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .focus_context import FreshnessState


@dataclass
class VisibleReplyArtifact:
    visible_text: str
    segments: list[str]
    persistable_text: str
    blocked_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
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


__all__ = [
    "FreshnessState",
    "OutboundPolicy",
    "VisibleReplyArtifact",
]
