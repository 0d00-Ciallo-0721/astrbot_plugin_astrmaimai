"""WebUI persistence repositories — encapsulate raw SQL behind typed methods.

Each repository accepts a ``db_factory`` callable (``async with db_factory() as db``)
and exposes domain-level methods that hide table names, column names, and SQL dialects.

Usage::

    repo = UserProfileRepository(db_factory)
    user = await repo.get_by_id("user-123")
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable


# ── UserProfileRepository ─────────────────────────────────────────────

class UserProfileRepository:
    """Encapsulates CRUD for the ``user_profiles`` table."""

    def __init__(self, db_factory: Callable):
        self.db_factory = db_factory

    @staticmethod
    def _parse_row(row: dict[str, Any]) -> dict[str, Any]:
        for field in ("tags", "aliases", "persona_slices", "relationship_slices", "profile_metadata"):
            raw = row.get(field)
            if isinstance(raw, str):
                try:
                    row[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass
        return row

    async def list_all(self) -> list[dict[str, Any]]:
        try:
            async with self.db_factory() as db:
                async with db.execute("SELECT * FROM user_profiles ORDER BY user_id") as cursor:
                    rows = await cursor.fetchall()
                    return [self._parse_row(dict(row)) for row in rows]
        except sqlite3.OperationalError:
            return []

    async def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return self._parse_row(dict(row)) if row else None

    async def update(self, user_id: str, set_clauses: str, params: list[Any]) -> bool:
        """Execute ``UPDATE user_profiles SET <set_clauses> WHERE user_id = ?``."""
        async with self.db_factory() as db:
            await db.execute(
                f"UPDATE user_profiles SET {set_clauses} WHERE user_id = ?",
                (*params, user_id),
            )
            await db.commit()
        return True

    async def delete(self, user_id: str) -> bool:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
            await db.commit()
        return True

    async def update_slice(
        self, user_id: str, slice_type: str, slices: list[str], profile_metadata: dict[str, Any]
    ) -> bool:
        """Atomically replace a JSON slice column and profile_metadata."""
        async with self.db_factory() as db:
            await db.execute(
                f"UPDATE user_profiles SET {slice_type} = ?, profile_metadata = ? WHERE user_id = ?",
                (
                    json.dumps(slices, ensure_ascii=False),
                    json.dumps(profile_metadata, ensure_ascii=False),
                    user_id,
                ),
            )
            await db.commit()
        return True


# ── CanonicalMemoryRepository ─────────────────────────────────────────

class CanonicalMemoryRepository:
    """Encapsulates queries against the ``canonical_memories`` table.

    This repository coexists with the v2 memory store: callers should
    prefer the v2 store when available and fall back to this repository
    for direct DB access.
    """

    def __init__(self, db_factory: Callable):
        self.db_factory = db_factory

    async def count(self, where: str = "", params: tuple = ()) -> int:
        where_sql = f"WHERE {where}" if where else ""
        try:
            async with self.db_factory() as db:
                async with db.execute(
                    f"SELECT COUNT(*) FROM canonical_memories {where_sql}", params
                ) as cursor:
                    row = await cursor.fetchone()
                    return int(row[0] if row else 0)
        except Exception:
            return 0

    async def list_paginated(
        self, where: str = "", params: tuple = (), limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        where_sql = f"WHERE {where}" if where else ""
        try:
            async with self.db_factory() as db:
                async with db.execute(
                    f"SELECT * FROM canonical_memories {where_sql} ORDER BY update_time DESC LIMIT ? OFFSET ?",
                    (*params, max(1, min(limit, 500)), max(0, offset)),
                ) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]
        except Exception:
            return []

    async def get_by_id(self, memory_id: str) -> dict[str, Any] | None:
        async with self.db_factory() as db:
            async with db.execute(
                "SELECT * FROM canonical_memories WHERE id = ?", (memory_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update_status(
        self, memory_id: str, status: str, *, reason: str = "", extra_sets: str = "", extra_params: tuple = ()
    ) -> int:
        """Atomically update status (and optional extra columns) for one memory."""
        now = time.time()
        clauses = "status = ?, deleted_reason = ?, update_time = ?"
        params: list[Any] = [status, reason, now]
        if extra_sets:
            clauses += ", " + extra_sets
            params.extend(extra_params)
        params.append(memory_id)
        async with self.db_factory() as db:
            cursor = await db.execute(
                f"UPDATE canonical_memories SET {clauses} WHERE id = ?", tuple(params)
            )
            await db.commit()
            return cursor.rowcount

    async def soft_delete(self, memory_id: str, reason: str = "webui") -> int:
        return await self.update_status(memory_id, "deleted", reason=reason)

    async def restore(self, memory_id: str) -> int:
        now = time.time()
        async with self.db_factory() as db:
            cursor = await db.execute(
                """
                UPDATE canonical_memories
                SET status = 'active', deleted_reason = '', update_time = ?, last_access_time = ?
                WHERE id = ? AND status IN ('stale', 'deleted')
                """,
                (now, now, memory_id),
            )
            await db.commit()
            return cursor.rowcount

    async def mark_stale(self, memory_id: str, reason: str = "webui") -> int:
        now = time.time()
        async with self.db_factory() as db:
            cursor = await db.execute(
                """
                UPDATE canonical_memories
                SET status = 'stale', deleted_reason = ?, update_time = ?
                WHERE id = ? AND status = 'active'
                """,
                (reason, now, memory_id),
            )
            await db.commit()
            return cursor.rowcount

    async def mark_merged(self, memory_id: str, superseded_by: str) -> int:
        now = time.time()
        async with self.db_factory() as db:
            cursor = await db.execute(
                """
                UPDATE canonical_memories
                SET status = 'merged', superseded_by = ?, update_time = ?
                WHERE id = ?
                """,
                (superseded_by, now, memory_id),
            )
            await db.commit()
            return cursor.rowcount

    async def purge(self, memory_id: str) -> int:
        """Hard-delete one row (admin-only)."""
        async with self.db_factory() as db:
            cursor = await db.execute(
                "DELETE FROM canonical_memories WHERE id = ?", (memory_id,)
            )
            await db.commit()
            return cursor.rowcount


__all__ = ["CanonicalMemoryRepository", "UserProfileRepository"]
