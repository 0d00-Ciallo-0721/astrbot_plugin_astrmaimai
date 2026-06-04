from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class HeartflowService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self._api = plugin_api

    async def heartflow_status(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_status()

    async def heartflow_chats(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_chats()

    async def heartflow_chat(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_chat(chat_id)

    async def heartflow_impulses(self, chat_id: str, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_impulses(chat_id, limit=limit)

    async def heartflow_timeline(self, chat_id: str | None = None, limit: int = 80) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_timeline(chat_id=chat_id, limit=limit)

    async def heartflow_topic_digests(self, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_topic_digests(limit=limit)

    async def heartflow_hidden_context(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).heartflow_hidden_context(chat_id)

    async def clear_heartflow_cooldowns(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).clear_heartflow_cooldowns(chat_id)


__all__ = ["HeartflowService"]
