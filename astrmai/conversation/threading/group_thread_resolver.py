from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class GroupThreadResolution:
    chat_id: str
    thread_id: str
    source: str = "chat"
    confidence: float = 0.0


def _component_type(component: Any) -> str:
    return str(getattr(component, "type", component.__class__.__name__)).lstrip("_").lower()


def _reply_component_identity(component: Any) -> str:
    for attr_name in ("message_id", "id", "reply_id", "target_id"):
        value = str(getattr(component, attr_name, "") or "").strip()
        if value:
            return value
    sender_id = str(getattr(component, "sender_id", "") or "").strip()
    if sender_id:
        return f"reply_sender:{sender_id}"
    return ""


def resolve_group_thread(event: Any, chat_id: str = "") -> GroupThreadResolution:
    normalized_chat_id = str(chat_id or getattr(event, "unified_msg_origin", "") or "").strip()
    if not normalized_chat_id:
        normalized_chat_id = "unknown"

    try:
        signature = str(event.get_extra("astrmai_thread_signature", "") or "").strip()
    except Exception:
        signature = ""
    if signature:
        return GroupThreadResolution(normalized_chat_id, signature, "thread_signature", 1.0)

    message_chain = getattr(getattr(event, "message_obj", None), "message", None) or []
    for component in message_chain:
        if _component_type(component) != "reply":
            continue
        reply_identity = _reply_component_identity(component)
        if reply_identity:
            return GroupThreadResolution(normalized_chat_id, f"reply:{reply_identity}", "reply_component", 0.9)

    try:
        sender_id = str(event.get_sender_id() or "").strip()
    except Exception:
        sender_id = ""
    if sender_id:
        return GroupThreadResolution(normalized_chat_id, f"sender:{sender_id}", "sender", 0.6)

    return GroupThreadResolution(normalized_chat_id, normalized_chat_id, "chat", 0.3)


__all__ = ["GroupThreadResolution", "resolve_group_thread"]
