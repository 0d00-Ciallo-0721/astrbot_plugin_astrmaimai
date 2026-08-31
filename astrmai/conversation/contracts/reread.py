from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RereadActionRequest:
    chat_id: str
    text: str
    fingerprint: str
    trigger_kind: str
    source_event_ids: tuple[str, ...] = field(default_factory=tuple)
    participant_ids: tuple[str, ...] = field(default_factory=tuple)
    explanation: str = ""
    # Stable source identity used for retries when platform event ids are absent.
    source_identity: str = ""


@dataclass(frozen=True, slots=True)
class RereadDispatchResult:
    status: str
    outbound_message_ids: tuple[str, ...] = field(default_factory=tuple)
    detail: str = ""

    @property
    def sent(self) -> bool:
        return self.status == "sent"


__all__ = ["RereadActionRequest", "RereadDispatchResult"]
