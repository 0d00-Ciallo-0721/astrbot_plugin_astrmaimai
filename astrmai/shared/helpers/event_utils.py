from __future__ import annotations

from typing import Any


def safe_get_sender_id(event: Any, default: str = "") -> str:
    try:
        value = event.get_sender_id()
    except Exception:
        value = getattr(event, "sender_id", default)
    return str(value or default)


def safe_get_origin(event: Any, default: str = "") -> str:
    return str(getattr(event, "unified_msg_origin", default) or default)


def safe_get_message_text(event: Any, default: str = "") -> str:
    return str(getattr(event, "message_str", default) or default)


__all__ = ["safe_get_message_text", "safe_get_origin", "safe_get_sender_id"]
