from __future__ import annotations

import random
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..contracts.reply_artifact import VisibleReplyArtifact


class TTSBridge:
    """Optional bridge to an external AstrBot TTS plugin."""

    def __init__(self, config=None):
        self.config = config

    def refresh_config(self, config) -> None:
        self.config = config

    def _settings(self):
        return getattr(self.config, "tts", None)

    @staticmethod
    def _is_private_chat(event: AstrMessageEvent, chat_id: str) -> bool:
        if "FriendMessage" in str(chat_id or ""):
            return True
        try:
            return not bool(event.get_group_id())
        except Exception:
            return False

    @staticmethod
    def _is_direct_group_trigger(event: AstrMessageEvent) -> bool:
        try:
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                return True
            if bool(event.get_extra("astrmai_poke_event", False)):
                return True
            if bool(event.get_extra("astrmai_peer_poke_event", False)):
                return True
            if bool(event.get_extra("astrmai_is_direct_call", False)):
                return True
            if bool(event.get_extra("astrmai_is_reply_to_bot", False)):
                return True
            turn_context = event.get_extra("astrmai_turn_context", None)
            scope = getattr(turn_context, "scope", None)
            if bool(getattr(scope, "is_at_bot", False)) or bool(getattr(scope, "is_reply_to_bot", False)):
                return True
        except Exception:
            return False
        return False

    def should_try_tts(self, event: AstrMessageEvent, chat_id: str, artifact: VisibleReplyArtifact) -> bool:
        settings = self._settings()
        if settings is None or not bool(getattr(settings, "enabled", False)):
            return False
        text = str(getattr(artifact, "visible_text", "") or "").strip()
        if not text:
            return False
        min_len = int(getattr(settings, "min_text_length", 2) or 2)
        max_len = int(getattr(settings, "max_text_length", 120) or 120)
        if len(text) < min_len or len(text) > max_len:
            return False
        if self._is_private_chat(event, chat_id):
            return bool(getattr(settings, "enable_private", True))
        if not bool(getattr(settings, "enable_group", False)):
            return False
        if bool(getattr(settings, "group_require_direct_trigger", True)) and not self._is_direct_group_trigger(event):
            return False
        probability = max(0, min(100, int(getattr(settings, "group_probability", 10) or 0)))
        if probability <= 0:
            return False
        if probability < 100 and random.randint(1, 100) > probability:
            return False
        return True

    def should_send_text(self) -> bool:
        settings = self._settings()
        if settings is None:
            return True
        return bool(getattr(settings, "send_text_with_audio", True))

    def _find_tts_plugin(self, context: Any):
        settings = self._settings()
        plugin_name = str(getattr(settings, "plugin_name", "astrbot_plugin_tts_llm") or "astrbot_plugin_tts_llm")
        candidates = {plugin_name, plugin_name.lower()}
        try:
            stars = context.get_all_stars()
        except Exception:
            return None
        for metadata in stars or []:
            root = str(getattr(metadata, "root_dir_name", "") or "")
            name = str(getattr(metadata, "name", "") or "")
            if root in candidates or root.lower() in candidates or name in candidates or name.lower() in candidates:
                plugin = getattr(metadata, "star_cls", None)
                if plugin is not None and hasattr(plugin, "hiy_tts_from_text"):
                    return plugin
        return None

    async def build_tts_chain(self, event: AstrMessageEvent, context: Any, artifact: VisibleReplyArtifact):
        plugin = self._find_tts_plugin(context)
        if plugin is None:
            logger.debug("[TTSBridge] TTS plugin unavailable; skip voice reply")
            return None
        try:
            return await plugin.hiy_tts_from_text(
                event=event,
                text=str(artifact.visible_text or ""),
                visible_text="",
            )
        except Exception as exc:
            settings = self._settings()
            if bool(getattr(settings, "silent_on_failure", True)):
                logger.debug(f"[TTSBridge] TTS degraded silently: {exc}")
            else:
                logger.warning(f"[TTSBridge] TTS failed: {exc}")
            return None


__all__ = ["TTSBridge"]
