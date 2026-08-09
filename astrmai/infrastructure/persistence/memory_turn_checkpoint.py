from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .sqlite_helpers import connect_aiosqlite


class MemoryTurnCheckpointStore:
    """Durable raw turn buffers used across bounded plugin reloads."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)

    async def load_all(self) -> dict[str, dict[str, Any]]:
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT chat_id, session_json FROM memory_turn_checkpoint ORDER BY updated_at"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        restored: dict[str, dict[str, Any]] = {}
        for chat_id, raw_payload in rows:
            try:
                payload = json.loads(str(raw_payload or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and isinstance(payload.get("buffer"), list):
                restored[str(chat_id)] = payload
        return restored

    async def upsert(self, chat_id: str, session_data: dict[str, Any]) -> None:
        payload = json.dumps(session_data, ensure_ascii=False, separators=(",", ":"))
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO memory_turn_checkpoint(chat_id, session_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    session_json = excluded.session_json,
                    updated_at = excluded.updated_at
                """,
                (str(chat_id), payload, now),
            )
            await db.commit()

    async def save_many(self, sessions: dict[str, dict[str, Any]]) -> None:
        rows = [
            (
                str(chat_id),
                json.dumps(session_data, ensure_ascii=False, separators=(",", ":")),
                time.time(),
            )
            for chat_id, session_data in sessions.items()
        ]
        if not rows:
            return
        async with connect_aiosqlite(self.db_path) as db:
            await db.executemany(
                """
                INSERT INTO memory_turn_checkpoint(chat_id, session_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    session_json = excluded.session_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            await db.commit()

    async def delete(self, chat_id: str) -> None:
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                "DELETE FROM memory_turn_checkpoint WHERE chat_id = ?",
                (str(chat_id),),
            )
            await db.commit()


__all__ = ["MemoryTurnCheckpointStore"]
