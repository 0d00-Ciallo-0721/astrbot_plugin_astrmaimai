from __future__ import annotations
from typing import Any

from ..adapters.plugin_api import PluginApiAdapter
from ..db import get_db
from .dashboard_repository import DashboardRepository
from .runtime_memory_stats import canonical_kind_review_stats


class LearningService:
    def __init__(self, plugin_api: PluginApiAdapter):
        self.plugin_api = plugin_api

    async def learning_status(self) -> dict[str, Any]:
        evolution = self.plugin_api.get_evolution()
        diagnostics = evolution.describe_learning_runtime() if evolution and hasattr(evolution, "describe_learning_runtime") else {}
        backlog = await evolution.backlog_overview() if evolution and hasattr(evolution, "backlog_overview") else {}
        return {
            "status": "ok",
            "data": {
                "reflector": self.plugin_api.get_reflector() is not None,
                "dream_agent_bound": self.plugin_api.get_proactive_task() is not None,
                "reflect_tracker": self.plugin_api.get_reflect_tracker() is not None,
                "auto_check_task": self.plugin_api.get_auto_check_task() is not None,
                "expression_patterns": await self._expression_pattern_stats(),
                "jargons": await canonical_kind_review_stats(self.plugin_api, kind="jargon"),
                "diagnostics": diagnostics,
                "backlog": backlog,
            },
            "runtime_bound": self.plugin_api.has_bound_facade(),
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

    async def run_reflect_once(self, chat_id: str) -> dict[str, Any]:
        from .chatruntimeservice import ChatRuntimeService
        return await ChatRuntimeService(self.plugin_api).run_reflect_once(chat_id)

    async def run_expression_backfill(
        self,
        chat_id: str,
        *,
        limit: int = 120,
        max_age_seconds: float = 604800,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        evolution = self.plugin_api.get_evolution()
        if not evolution or not hasattr(evolution, "run_expression_backfill"):
            return {"status": "error", "message": "表达学习运行时未绑定"}
        return await evolution.run_expression_backfill(
            chat_id,
            limit=limit,
            max_age_seconds=max_age_seconds,
            dry_run=dry_run,
        )

    async def _expression_pattern_stats(self) -> dict[str, Any]:
        return await canonical_kind_review_stats(
            self.plugin_api,
            kind="expression_pattern",
            legacy_expression_repo=DashboardRepository(get_db),
        )

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
