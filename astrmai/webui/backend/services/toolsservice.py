from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class ToolsService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def tools_status(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService

        return await AdminUiService(self.plugin_api).tools_status()

    async def tools_catalog(self) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService

        return await AdminUiService(self.plugin_api).tools_catalog()

    async def tools_policy(self) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        policy = self._as_dict(getattr(planner, "tool_policy", None)) if planner else {}
        return {"status": "ok", "data": policy, "runtime_bound": planner is not None}

    async def recent_tool_traces(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self.plugin_api).recent_tool_traces(chat_id=chat_id, limit=limit)

    async def recent_tool_executions(self, chat_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        from .admin_ui_service import AdminUiService
        return await AdminUiService(self.plugin_api).recent_tool_executions(chat_id=chat_id, limit=limit)

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "__dict__"):
            return dict(getattr(value, "__dict__", {}) or {})
        return {}


__all__ = ["ToolsService"]
