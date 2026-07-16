from __future__ import annotations

import os
from typing import Callable

import psutil

from ..adapters.plugin_api import PluginApiAdapter
from ..paths import default_db_path
from .dashboard_repository import DashboardRepository
from .runtime_memory_stats import canonical_kind_review_stats, canonical_memory_stats


class DashboardService:
    def __init__(self, plugin_api: PluginApiAdapter, db_factory: Callable):
        self.plugin_api = plugin_api
        self._repo = DashboardRepository(db_factory)

    @staticmethod
    def _db_size_kb(db_path: str) -> float:
        try:
            return round((os.path.getsize(db_path) if os.path.exists(db_path) else 0) / 1024, 2)
        except Exception:
            return 0

    @staticmethod
    def _process_memory_mb() -> float:
        try:
            return round(psutil.Process().memory_info().rss / 1024 / 1024, 2)
        except Exception:
            return 0

    @staticmethod
    def _cpu_percent() -> float:
        try:
            return psutil.cpu_percent(interval=0.1)
        except Exception:
            return 0

    @staticmethod
    def _memory_percent() -> float:
        try:
            return psutil.virtual_memory().percent
        except Exception:
            return 0

    async def get_snapshot(self) -> dict:
        db_path = default_db_path()
        degraded: dict[str, str] = {}
        try:
            counts = await self._repo.snapshot_counts()
            count_errors = dict(counts.pop("_degraded", {}) or {})
            degraded.update({f"counts.{key}": value for key, value in count_errors.items()})
        except Exception as exc:
            counts = {}
            degraded["counts"] = str(exc)
        try:
            memory_stats = await canonical_memory_stats(self.plugin_api, self._repo)
            counts["total_canonical_memories"] = int(memory_stats.get("total", 0) or 0)
            counts["canonical_memory_stats"] = memory_stats
            if memory_stats.get("degraded"):
                degraded["counts.canonical_memory_stats"] = str(memory_stats.get("degraded_reason", "degraded"))
        except Exception as exc:
            degraded["counts.canonical_memory_stats"] = str(exc)
        try:
            expression_stats = await canonical_kind_review_stats(
                self.plugin_api,
                kind="expression_pattern",
                legacy_expression_repo=self._repo,
            )
            counts["pending_reviews"] = int(expression_stats.get("pending", 0) or 0)
            counts["expression_pattern_stats"] = expression_stats
        except Exception as exc:
            degraded["counts.expression_pattern_stats"] = str(exc)
        try:
            diagnostics = await self.plugin_api.get_runtime_diagnostics()
        except Exception as exc:
            diagnostics = {"status": "degraded", "error": str(exc)}
            degraded["diagnostics"] = str(exc)
        try:
            capabilities = await self.plugin_api.get_capability_overview()
        except Exception as exc:
            capabilities = {"status": "degraded", "error": str(exc)}
            degraded["capabilities"] = str(exc)
        return {
            "db_size_kb": self._db_size_kb(db_path),
            "webui_mem_mb": self._process_memory_mb(),
            "sys_cpu_percent": self._cpu_percent(),
            "sys_mem_percent": self._memory_percent(),
            "diagnostics": diagnostics,
            "capabilities": capabilities,
            "degraded": bool(degraded),
            "degraded_components": degraded,
            **counts,
        }


__all__ = ["DashboardService"]
