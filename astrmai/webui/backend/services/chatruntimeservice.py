from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class ChatRuntimeService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self._api = plugin_api

    async def proactive_status(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).proactive_status()

    async def proactive_intents(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).proactive_intents()

    async def dream_status(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).dream_status()

    async def run_dream_once(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).run_dream_once()

    async def diary_status(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).diary_status()

    async def run_diary_once(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).run_diary_once()

    async def wakeup_status(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).wakeup_status()

    async def run_reflect_once(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).run_reflect_once(chat_id)

    async def active_chats(self, max_age_seconds: float = 1800) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).active_chats(max_age_seconds=max_age_seconds)

    async def chat_activity(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).chat_activity(chat_id)

    async def chat_runtime(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).chat_runtime(chat_id)

    async def clear_chat_runtime(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).clear_chat_runtime(chat_id)

    async def list_memory_feedback(self, limit: int = 30) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).list_memory_feedback(limit=limit)

    async def disable_memory_feedback(self, feedback_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).disable_memory_feedback(feedback_id)

    async def memory_feedback_sources(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).memory_feedback_sources()


__all__ = ["ChatRuntimeService"]
