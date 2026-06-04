from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter


class LearningService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def learning_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": {
                "reflector": self.plugin_api.get_reflector() is not None,
                "dream_agent_bound": self.plugin_api.get_proactive_task() is not None,
                "reflect_tracker": self.plugin_api.facade is not None,
                "auto_check_task": self.plugin_api.facade is not None,
            },
            "runtime_bound": self.plugin_api.facade is not None,
        }

    async def expression_stats(self) -> dict[str, Any]:
        stats = await self._expression_pattern_stats()
        return {"status": "ok", "data": stats}

    async def expression_cooldowns(self) -> dict[str, Any]:
        planner = self.plugin_api.get_planner()
        selector = getattr(planner, "expression_selector", None) if planner else None
        return {
            "status": "ok",
            "data": {
                "recent_patterns": self._as_dict(selector).get("_recent_patterns", {}) if selector else {},
            },
            "runtime_bound": selector is not None,
        }

    async def _expression_pattern_stats(self) -> dict[str, Any]:
        # delegate to repository via AdminUiService for now
        # (avoids circular import with dashboard_repository)
        return {"total": 0, "pending": 0, "approved": 0, "rejected": 0}

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if hasattr(value, "__dict__"):
            return dict(getattr(value, "__dict__", {}) or {})
        return {}


__all__ = ["LearningService"]
