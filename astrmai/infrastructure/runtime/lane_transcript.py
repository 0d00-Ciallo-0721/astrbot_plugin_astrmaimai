from __future__ import annotations

import time
from typing import List, Optional

from astrbot.api import logger

from ...conversation.planning.message_renderer import MessageRenderer
from ..gateway.output_guard import is_safe_visible_text, sanitize_visible_reply_text


class LaneTranscriptMixin:
    async def get_recent_transcript(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        max_turns: int = 4,
        max_age_seconds: Optional[float] = None,
    ) -> str:
        lane_umo, _conversation_id, history, _ = await self.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
        )
        if not history:
            return ""

        if max_age_seconds is not None and max_age_seconds > 0:
            now = time.time()
            history = [
                message
                for message in history
                if isinstance(message, dict)
                and self._message_timestamp(message) > 0
                and now - self._message_timestamp(message) <= max_age_seconds
            ]
            if not history:
                return ""

        recent_messages = history[-max(max_turns * 2, 2):]
        bot_name = "Bot"
        if self.settings.nicknames:
            bot_name = self.settings.nicknames[0] or bot_name
        speaker_names = list(dict.fromkeys(["Bot", bot_name, *list(self.settings.nicknames or [])]))

        lines: List[str] = []
        for message in recent_messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip()
            content = self._stringify_content(message.get("content", ""))
            if not content:
                continue
            if role == "assistant":
                normalized_assistant = sanitize_visible_reply_text(content, fallback_text="", speaker_names=speaker_names)
                if not normalized_assistant:
                    continue
                rendered = MessageRenderer.render_bot_turn(normalized_assistant, bot_name)
                if rendered:
                    lines.append(rendered)
                continue
            if role == "user" and self._looks_like_social_rendered_line(content):
                rendered = MessageRenderer.render_social_event(content)
                if rendered:
                    lines.append(rendered)
                continue
            if role == "user":
                if not is_safe_visible_text(content):
                    continue
                if content.startswith("[") and "说:" in content:
                    rendered = MessageRenderer.render_social_event(content[:180])
                else:
                    sender_name = str(message.get("sender_name", "") or message.get("name", "") or "").strip()
                    rendered = MessageRenderer.render_user_turn(content[:180], sender_name)
                if rendered:
                    lines.append(rendered)

        transcript = "\n".join(lines)
        if self.settings.debug_mode and transcript:
            logger.debug(f"[LaneManager] recent transcript for {lane_umo}: {transcript[:200]!r}")
        return transcript
