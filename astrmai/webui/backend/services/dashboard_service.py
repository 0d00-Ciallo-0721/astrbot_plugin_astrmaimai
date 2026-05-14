from __future__ import annotations

import json
import os
import psutil
import sqlite3
from typing import Callable

from ..adapters.plugin_api import PluginApiAdapter
from ..paths import default_db_path


class DashboardService:
    def __init__(self, plugin_api: PluginApiAdapter, db_factory: Callable):
        self.plugin_api = plugin_api
        self.db_factory = db_factory

    async def _count_pending_expression_reviews(self, db) -> int:
        async with db.execute(
            """
            SELECT status, metadata
            FROM canonical_memories
            WHERE kind = 'expression_pattern'
            """
        ) as cursor:
            rows = await cursor.fetchall()
        count = 0
        for row in rows:
            status = str(row[0] or "").strip().lower()
            try:
                metadata = json.loads(row[1] or "{}")
            except Exception:
                metadata = {}
            review_status = str((metadata or {}).get("review_status") or "").strip().lower()
            if status == "review_pending" or review_status in {"pending", "revision_needed", "pending_human"}:
                count += 1
        return count

    async def get_snapshot(self) -> dict:
        db_path = default_db_path()
        stats = {
            "db_size_kb": round((os.path.getsize(db_path) if os.path.exists(db_path) else 0) / 1024, 2),
            "webui_mem_mb": round(psutil.Process().memory_info().rss / 1024 / 1024, 2),
            "sys_cpu_percent": psutil.cpu_percent(interval=0.1),
            "sys_mem_percent": psutil.virtual_memory().percent,
            "total_users": 0,
            "pending_reviews": 0,
            "total_memory_events": 0,
            "total_canonical_memories": 0,
            "diagnostics": await self.plugin_api.get_runtime_diagnostics(),
            "capabilities": await self.plugin_api.get_capability_overview(),
        }
        try:
            async with self.db_factory() as db:
                async with db.execute("SELECT COUNT(*) FROM UserProfile") as cursor:
                    stats["total_users"] = (await cursor.fetchone())[0]
                async with db.execute("SELECT COUNT(*) FROM MemoryEvent") as cursor:
                    stats["total_memory_events"] = (await cursor.fetchone())[0]
                try:
                    async with db.execute("SELECT COUNT(*) FROM canonical_memories") as cursor:
                        stats["total_canonical_memories"] = (await cursor.fetchone())[0]
                    stats["pending_reviews"] = await self._count_pending_expression_reviews(db)
                except sqlite3.OperationalError:
                    stats["total_canonical_memories"] = 0
                    stats["pending_reviews"] = 0
        except sqlite3.OperationalError:
            pass
        return stats


__all__ = ["DashboardService"]
