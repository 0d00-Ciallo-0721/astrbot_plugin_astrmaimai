from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SocialFeedbackDecision:
    detected: bool = False
    kind: str = "unrelated"
    action: str = "none"
    observation_id: str = ""
    actor_id: str = ""
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SocialFeedbackEvidence:
    event_id: str
    actor_id: str
    kind: str
    confidence: float
    observed_at: float


@dataclass(slots=True)
class SocialFeedbackObservation:
    observation_id: str
    chat_id: str
    turn_id: str
    thread_id: str
    thread_signature: str
    topic_epoch: int
    turn_generation: int
    bot_turn_ids: list[str]
    outbound_message_ids: list[str]
    target_user_ids: list[str]
    reply_text: str
    bot_id: str
    started_at: float
    last_bot_sent_at: float
    expires_at: float
    status: str = "observing"
    responders: list[str] = field(default_factory=list)
    evidence: list[SocialFeedbackEvidence] = field(default_factory=list)
    strongest_confidence: float = 0.0
    first_feedback_at: float = 0.0
    terminal_reason: str = ""
    feedback_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def merge_outbound(self, *, commit_id: str, message_ids: list[str], sent_at: float, expires_at: float) -> None:
        if commit_id and commit_id not in self.bot_turn_ids:
            self.bot_turn_ids.append(commit_id)
        for message_id in message_ids:
            normalized = str(message_id or "").strip()
            if normalized and normalized not in self.outbound_message_ids:
                self.outbound_message_ids.append(normalized)
        self.last_bot_sent_at = max(self.last_bot_sent_at, float(sent_at or 0.0))
        self.expires_at = max(self.expires_at, float(expires_at or 0.0))


__all__ = [
    "SocialFeedbackDecision",
    "SocialFeedbackEvidence",
    "SocialFeedbackObservation",
]
