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

    async def get_snapshot(self) -> dict:
        db_path = default_db_path()
        counts = await self._repo.snapshot_counts()
        return {
            "db_size_kb": round((os.path.getsize(db_path) if os.path.exists(db_path) else 0) / 1024, 2),
            "webui_mem_mb": round(psutil.Process().memory_info().rss / 1024 / 1024, 2),
            "sys_cpu_percent": psutil.cpu_percent(interval=0.1),
            "sys_mem_percent": psutil.virtual_memory().percent,
            "diagnostics": await self.plugin_api.get_runtime_diagnostics(),
            "capabilities": await self.plugin_api.get_capability_overview(),
            **counts,
        }


__all__ = ["DashboardService"]
