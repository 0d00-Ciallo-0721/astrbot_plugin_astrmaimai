from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class ToolsService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def tools_status(self) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        families = getattr(planner, "TOOL_FAMILIES", None)
        data = {
            "chat_tier": sorted(getattr(planner, "CHAT_TOOL_NAMES", set())),
            "guarded_chat_tier": sorted(getattr(planner, "GUARDED_CHAT_TOOL_NAMES", set())),
            "full_only": sorted(getattr(planner, "FULL_ONLY_TOOL_NAMES", set())),
            "families": {k: sorted(v) for k, v in (families or {}).items()} if families else {},
            "tool_count": len(getattr(planner, "CHAT_TOOL_NAMES", set())
                | getattr(planner, "GUARDED_CHAT_TOOL_NAMES", set())
                | getattr(planner, "FULL_ONLY_TOOL_NAMES", set())),
        }
        return {"status": "ok", "data": data, "runtime_bound": planner is not None}

    async def tools_policy(self) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        policy = self._as_dict(getattr(planner, "tool_policy", None)) if planner else {}
        return {"status": "ok", "data": policy, "runtime_bound": planner is not None}

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
