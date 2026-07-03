from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from ...shared.helpers.plugin_helpers import get_event_self_id, resolve_event_scope


@dataclass(slots=True, frozen=True)
class IngressDecision:
    action: Literal["continue", "stop"]
    reason: str = ""

    @property
    def should_stop(self) -> bool:
        return self.action == "stop"

    @classmethod
    def allow(cls, reason: str = "") -> "IngressDecision":
        return cls(action="continue", reason=reason)

    @classmethod
    def stop(cls, reason: str) -> "IngressDecision":
        return cls(action="stop", reason=reason)


@dataclass(slots=True, frozen=True)
class MessageScope:
    umo: str
    platform_type: str
    entity_id: str
    self_id: str
    sender_id: str
    group_id: str

    @property
    def chat_id(self) -> str:
        return self.umo

    @property
    def is_group_chat(self) -> bool:
        return bool(self.group_id)

    @property
    def is_private_chat(self) -> bool:
        return not self.is_group_chat

    @property
    def is_anonymous_sender(self) -> bool:
        return self.sender_id.startswith("80000000")

    @classmethod
    def from_event(cls, event: Any) -> "MessageScope":
        umo, platform_type, entity_id = resolve_event_scope(event)
        return cls(
            umo=umo,
            platform_type=platform_type,
            entity_id=entity_id,
            self_id=get_event_self_id(event),
            sender_id=str(event.get_sender_id()),
            group_id=str(event.get_group_id() or ""),
        )
