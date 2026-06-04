from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable


class DashboardRepository:
    """Encapsulates raw SQL queries for dashboard data."""

    def __init__(self, db_factory: Callable):
        self.db_factory = db_factory

    async def count_table(self, table: str) -> int:
        try:
            async with self.db_factory() as db:
                async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                    row = await cursor.fetchone()
                    return int(row[0] if row else 0)
        except sqlite3.OperationalError:
            return 0

    async def count_pending_expression_reviews(self) -> int:
        try:
            async with self.db_factory() as db:
                async with db.execute(
                    """
                    SELECT status, metadata
                    FROM canonical_memories
                    WHERE kind = 'expression_pattern'
                    """
                ) as cursor:
                    rows = await cursor.fetchall()
        except sqlite3.OperationalError:
            return 0
        count = 0
        for row in rows:
            status = str(row[0] or "").strip().lower()
            try:
                metadata: dict[str, Any] = json.loads(row[1] or "{}")
            except Exception:
                metadata = {}
            review_status = str((metadata or {}).get("review_status") or "").strip().lower()
            if status == "review_pending" or review_status in {"pending", "revision_needed", "pending_human"}:
                count += 1
        return count

    async def expression_pattern_counts(self) -> tuple[int, int, int, int]:
        """Return (total, pending, approved, rejected) for expression_pattern rows."""
        import json
        try:
            async with self.db_factory() as db:
                async with db.execute(
                    """SELECT status, metadata FROM canonical_memories WHERE kind = 'expression_pattern'"""
                ) as cursor:
                    rows = await cursor.fetchall()
        except sqlite3.OperationalError:
            return 0, 0, 0, 0
        stats = {"total": len(rows), "pending": 0, "approved": 0, "rejected": 0}
        for status_value, metadata_raw in rows:
            status = str(status_value or "").strip().lower()
            try:
                metadata = json.loads(metadata_raw or "{}")
            except Exception:
                metadata = {}
            review_status = str((metadata or {}).get("review_status") or "").strip().lower()
            if status == "review_pending" or review_status in {"pending", "pending_human", "revision_needed"}:
                stats["pending"] += 1
            elif status == "active" and review_status == "approved":
                stats["approved"] += 1
            elif status == "rejected" or review_status == "rejected":
                stats["rejected"] += 1
        return stats["total"], stats["pending"], stats["approved"], stats["rejected"]

    async def snapshot_counts(self) -> dict[str, int]:
        """Return {total_users, total_memory_events, total_canonical_memories, pending_reviews}."""
        return {
            "total_users": await self.count_table("UserProfile"),
            "total_memory_events": await self.count_table("MemoryEvent"),
            "total_canonical_memories": await self.count_table("canonical_memories"),
            "pending_reviews": await self.count_pending_expression_reviews(),
        }


__all__ = ["DashboardRepository"]
