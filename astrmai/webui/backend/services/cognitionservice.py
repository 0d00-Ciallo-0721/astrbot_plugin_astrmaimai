from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class CognitionService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self._api = plugin_api

    async def recent_decisions(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).recent_decisions(chat_id=chat_id, limit=limit)

    async def recent_turn_traces(self, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).recent_turn_traces(limit=limit)

    async def recent_tool_traces(self, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).recent_tool_traces(limit=limit)

    async def chat_trace_events(self, chat_id: str, limit: int = 80) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).chat_trace_events(chat_id, limit=limit)

    async def cognition_unified_timeline(self, chat_id: str) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self._api).cognition_unified_timeline(chat_id)


__all__ = ["CognitionService"]
