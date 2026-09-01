from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class LearningMessageEnvelope:
    """Immutable, serializable ingress payload for detached learning work."""

    event_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    content: str
    conversation_event: Mapping[str, Any] | None = None
    received_at: float = 0.0

    @classmethod
    def from_event(cls, event: Any) -> "LearningMessageEnvelope":
        get_extra = getattr(event, "get_extra", lambda *_args, **_kwargs: None)
        event_id = str(
            get_extra("astrmai_event_id", None)
            or get_extra("event_id", None)
            or getattr(getattr(event, "message_obj", None), "message_id", "")
            or ""
        ).strip()
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        content = str(get_extra("astrmai_rich_text", getattr(event, "message_str", "")) or "")
        timestamp = float(getattr(getattr(event, "message_obj", None), "timestamp", 0.0) or 0.0)
        if not event_id:
            event_id = "evt_" + hashlib.sha256(
                "\x1f".join((chat_id, sender_id, content, str(timestamp))).encode("utf-8")
            ).hexdigest()[:24]
        return cls(
            event_id=event_id,
            chat_id=chat_id,
            sender_id=sender_id,
            sender_name=str(getattr(event, "get_sender_name", lambda: "")() or ""),
            content=content,
            conversation_event=get_extra("astrmai_conversation_event", None),
            received_at=timestamp or time.time(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "content": self.content,
            "conversation_event": dict(self.conversation_event or {}),
            "received_at": self.received_at,
        }


__all__ = ["LearningMessageEnvelope"]
