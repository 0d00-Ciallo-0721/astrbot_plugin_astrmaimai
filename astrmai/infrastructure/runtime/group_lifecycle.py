from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ..persistence.sqlite_helpers import connect_aiosqlite


_CURRENT_REGISTRY: "GroupLifecycleRegistry | None" = None


@dataclass(frozen=True, slots=True)
class GroupLifecycleState:
    chat_id: str
    group_id: str
    status: str
    generation: int
    retired_at: float = 0.0
    reason: str = ""


class GroupLifecycleRegistry:
    """Persistent send fence for groups the bot has left."""

    def __init__(self, db_path: Any):
        global _CURRENT_REGISTRY
        self.db_path = str(db_path or "")
        previous = _CURRENT_REGISTRY
        self._cache: dict[str, GroupLifecycleState | None] = (
            dict(previous._cache)
            if previous is not None and previous.db_path == self.db_path
            else {}
        )
        self._lock = asyncio.Lock()
        _CURRENT_REGISTRY = self

    @staticmethod
    def _chat_id(chat_id: str) -> str:
        value = str(chat_id or "").strip()
        if not value:
            raise ValueError("chat_id is required")
        return value

    def _publish_state(self, key: str, state: GroupLifecycleState | None) -> None:
        self._cache[key] = state
        current = _CURRENT_REGISTRY
        if current is not None and current is not self and current.db_path == self.db_path:
            current._cache[key] = state

    async def _read(self, chat_id: str) -> GroupLifecycleState | None:
        if not self.db_path:
            return self._cache.get(chat_id)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id, group_id, status, generation, retired_at, reason "
                "FROM group_lifecycle WHERE chat_id = ?", (chat_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            return GroupLifecycleState(str(row[0]), str(row[1] or ""), str(row[2]), int(row[3] or 0), float(row[4] or 0), str(row[5] or ""))

    async def state(self, chat_id: str) -> GroupLifecycleState | None:
        key = self._chat_id(chat_id)
        if key in self._cache:
            return self._cache[key]
        async with self._lock:
            if key in self._cache:
                return self._cache[key]
            state = await self._read(key)
            self._publish_state(key, state)
            return state

    async def generation(self, chat_id: str) -> int:
        state = await self.state(chat_id)
        return int(state.generation if state else 0)

    async def retire(self, chat_id: str, *, group_id: str = "", reason: str = "", source_event_id: str = "") -> GroupLifecycleState:
        key = self._chat_id(chat_id)
        async with self._lock:
            try:
                previous = await self._read(key)
            except Exception:
                previous = self._cache.get(key)
            generation = int(previous.generation if previous else 0) + 1
            now = time.time()
            state = GroupLifecycleState(key, str(group_id or (previous.group_id if previous else "")), "retired", generation, now, str(reason or ""))
            # A confirmed bot-left notice must close the in-process send fence
            # even when the durable write is temporarily unavailable.
            self._publish_state(key, state)
            if self.db_path:
                async with connect_aiosqlite(self.db_path) as db:
                    await db.execute(
                        "INSERT INTO group_lifecycle(chat_id, group_id, status, generation, retired_at, reason, source_event_id, updated_at) "
                        "VALUES (?, ?, 'retired', ?, ?, ?, ?, ?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET group_id=excluded.group_id, status='retired', generation=excluded.generation, retired_at=excluded.retired_at, reason=excluded.reason, source_event_id=excluded.source_event_id, updated_at=excluded.updated_at",
                        (key, state.group_id, generation, now, state.reason, str(source_event_id or ""), now),
                    )
                    await db.commit()
            return state

    async def activate(self, chat_id: str, *, group_id: str = "", source_event_id: str = "") -> GroupLifecycleState:
        key = self._chat_id(chat_id)
        async with self._lock:
            previous = await self._read(key)
            generation = int(previous.generation if previous else 0) + 1
            now = time.time()
            state = GroupLifecycleState(key, str(group_id or (previous.group_id if previous else "")), "active", generation, 0.0, "")
            if self.db_path:
                async with connect_aiosqlite(self.db_path) as db:
                    await db.execute(
                        "INSERT INTO group_lifecycle(chat_id, group_id, status, generation, retired_at, reason, source_event_id, updated_at) VALUES (?, ?, 'active', ?, 0, '', ?, ?) "
                        "ON CONFLICT(chat_id) DO UPDATE SET group_id=excluded.group_id, status='active', generation=excluded.generation, retired_at=0, reason='', source_event_id=excluded.source_event_id, updated_at=excluded.updated_at",
                        (key, state.group_id, generation, str(source_event_id or ""), now),
                    )
                    await db.commit()
            self._publish_state(key, state)
            return state

    async def allowed(self, chat_id: str, captured_generation: int | None = None) -> bool:
        state = await self.state(chat_id)
        if state is not None and state.status == "retired":
            return False
        return captured_generation is None or int(captured_generation) == int(state.generation if state else 0)

    def is_retired_cached(self, chat_id: str) -> bool:
        """Synchronous fast fence for an already-bound outbound event."""
        key = str(chat_id or "").strip()
        state = self._cache.get(key)
        return bool(state is not None and state.status == "retired")

    async def bind_event(self, event: Any) -> int:
        """Attach the durable group fence to a real group event.

        The retired bit is intentionally copied onto the event so every
        outbound path can fail closed without each sender opening the
        database again.  Private events and malformed notice objects are
        ignored; they must not create lifecycle rows.
        """
        chat_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not chat_id or "GroupMessage:" not in chat_id:
            return 0
        state = await self.state(chat_id)
        generation = int(state.generation if state else 0)
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("astrmai_group_lifecycle_generation", generation)
            setter("astrmai_group_retired", bool(state and state.status == "retired"))
        return generation


def is_group_retired_cached(chat_id: str) -> bool:
    registry = _CURRENT_REGISTRY
    key = str(chat_id or "").strip()
    if not key:
        return False
    return bool(registry is not None and registry.is_retired_cached(key))


def group_generation_allowed_cached(chat_id: str, captured_generation: int | None) -> bool:
    registry = _CURRENT_REGISTRY
    if registry is None or captured_generation is None:
        return True
    state = registry._cache.get(str(chat_id or "").strip())
    return int(captured_generation) == int(state.generation if state else 0)


__all__ = ["GroupLifecycleState", "GroupLifecycleRegistry", "is_group_retired_cached", "group_generation_allowed_cached"]

