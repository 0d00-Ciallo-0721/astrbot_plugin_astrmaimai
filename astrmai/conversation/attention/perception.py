from __future__ import annotations

from typing import Any

from ..contracts.turn_context import PerceptionSnapshot, ensure_turn_context


class PerceptionBuilder:
    """Builds the per-turn perception snapshot from an AstrBot event."""

    def __init__(self, gate: Any):
        self.gate = gate

    def build(self, event: Any) -> PerceptionSnapshot:
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        chat_id = str(getattr(event, "unified_msg_origin", "") or group_id or "default")
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        sender_name = str(getattr(event, "get_sender_name", lambda: "")() or "")
        text = str(getattr(event, "message_str", "") or "")
        rich_text = str(event.get_extra("astrmai_rich_text", text) if hasattr(event, "get_extra") else text)
        direct_urls = (
            list(event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", [])) or [])
            if hasattr(event, "get_extra")
            else []
        )
        extracted_urls = (
            list(event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", [])) or [])
            if hasattr(event, "get_extra")
            else []
        )
        image_urls = list(dict.fromkeys(direct_urls + extracted_urls))
        is_direct, is_at_bot, is_reply_to_bot, is_name_only = self.gate._resolve_wakeup_flags(event, self_id, text)

        snapshot = PerceptionSnapshot(
            chat_id=chat_id,
            self_id=self_id,
            sender_id=sender_id,
            sender_name=sender_name,
            text=text,
            rich_text=rich_text,
            timestamp=float(getattr(event, "timestamp", 0.0) or 0.0),
            image_urls=image_urls,
            is_private=not bool(group_id),
            is_direct_wakeup=is_direct,
            is_at_bot=is_at_bot,
            is_reply_to_bot=is_reply_to_bot,
            is_name_only_wakeup=is_name_only,
            is_strong_wakeup=bool(is_direct or is_at_bot or is_reply_to_bot or is_name_only),
        )
        ensure_turn_context(event).perception = snapshot
        return snapshot


__all__ = ["PerceptionBuilder"]
