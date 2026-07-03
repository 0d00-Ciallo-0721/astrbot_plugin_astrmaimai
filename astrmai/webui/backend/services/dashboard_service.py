from __future__ import annotations

import os
from typing import Callable

import psutil

from ..adapters.plugin_api import PluginApiAdapter
from ..paths import default_db_path
from .dashboard_repository import DashboardRepository


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
        counts = await self._repo.snapshot_counts()
        return {
            "db_size_kb": self._db_size_kb(db_path),
            "webui_mem_mb": self._process_memory_mb(),
            "sys_cpu_percent": self._cpu_percent(),
            "sys_mem_percent": self._memory_percent(),
            "diagnostics": await self.plugin_api.get_runtime_diagnostics(),
            "capabilities": await self.plugin_api.get_capability_overview(),
            **counts,
        }


__all__ = ["DashboardService"]
