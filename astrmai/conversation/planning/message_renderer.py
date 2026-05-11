from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent


class MessageRenderer:
    """Single entry point for converting conversation messages into prompt text."""

    LEGACY_MESSAGE_RE = re.compile(r'^<message\s+speaker="([^"]+)">(.*)</message>$', re.IGNORECASE | re.DOTALL)

    @staticmethod
    def _normalize_text(text: str) -> str:
        return str(text or "").strip()

    @classmethod
    def render_event(cls, event: AstrMessageEvent) -> str:
        speaker = cls._normalize_text(event.get_sender_name()) or "群友"
        text = cls._normalize_text(event.get_extra("astrmai_rich_text", event.message_str))
        interaction = cls._normalize_text(event.get_extra("astrmai_interaction_kind", ""))
        if interaction:
            interaction_text = text or interaction
            return f"[互动：{speaker} {interaction_text}]".strip()
        if not text:
            return ""
        return f"{speaker}: {text}"

    @classmethod
    def render_bot_turn(cls, content: str, bot_name: str = "Bot") -> str:
        normalized = cls._normalize_text(content)
        if not normalized:
            return ""
        speaker = cls._normalize_text(bot_name) or "Bot"
        return f"{speaker}: {normalized}"

    @classmethod
    def render_user_turn(cls, content: str, sender_name: str = "") -> str:
        normalized = cls._normalize_text(content)
        if not normalized:
            return ""
        speaker = cls._normalize_text(sender_name) or "用户"
        return f"{speaker}: {normalized}"

    @classmethod
    def render_social_event(cls, content: str) -> str:
        normalized = cls._normalize_text(content)
        if not normalized:
            return ""
        legacy_match = cls.LEGACY_MESSAGE_RE.match(normalized)
        if legacy_match:
            speaker = cls._normalize_text(legacy_match.group(1)) or "用户"
            legacy_content = cls._normalize_text(legacy_match.group(2))
            return cls.render_user_turn(legacy_content, speaker)
        if normalized.startswith("["):
            return normalized[:180]
        return f"[{normalized[:180]}]"


__all__ = ["MessageRenderer"]
