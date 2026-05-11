from __future__ import annotations

import json
import time


class MemoryUiService:
    def __init__(self, db_factory):
        self.db_factory = db_factory

    async def _columns(self, db, table: str) -> set[str]:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
            return {str(row[1]) for row in rows}

    @staticmethod
    def _db_value(value):
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return value

    @classmethod
    def _existing_values(cls, columns: set[str], values: dict) -> dict:
        return {key: cls._db_value(value) for key, value in values.items() if key in columns and value is not None}

    async def _insert(self, db, table: str, values: dict):
        columns = await self._columns(db, table)
        payload = self._existing_values(columns, values)
        if not payload:
            return None
        column_names = list(payload.keys())
        placeholders = ", ".join(["?"] * len(column_names))
        query = f"INSERT INTO {table} ({', '.join(column_names)}) VALUES ({placeholders})"
        return await db.execute(query, [payload[name] for name in column_names])

    async def _update(self, db, table: str, row_filter: str, filter_value, values: dict) -> None:
        columns = await self._columns(db, table)
        payload = self._existing_values(columns, values)
        if not payload:
            return
        assignments = ", ".join(f"{name} = ?" for name in payload)
        await db.execute(
            f"UPDATE {table} SET {assignments} WHERE {row_filter} = ?",
            [*payload.values(), filter_value],
        )

    async def list_events(self):
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM MemoryEvent ORDER BY id DESC LIMIT 100") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def list_reflections(self, month: str):
        async with self.db_factory() as db:
            async with db.execute(
                "SELECT * FROM DailyReflection WHERE date LIKE ? ORDER BY date ASC",
                (f"{month}%",),
            ) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def list_nodes(self):
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM MemoryNode ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def list_jargon(self):
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM Jargon ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def create_event(self, data: dict) -> dict[str, object]:
        now = time.time()
        event_id = data.get("event_id") or f"plugin_page_{int(now * 1000)}"
        importance = data.get("importance", 0.5)
        async with self.db_factory() as db:
            cursor = await self._insert(
                db,
                "MemoryEvent",
                {
                    "event_id": event_id,
                    "session_id": data.get("session_id", "PLUGIN_PAGE_SMOKE"),
                    "date": data.get("date", "2026-01-01"),
                    "narrative": data.get("narrative", ""),
                    "emotion": data.get("emotion", "Neutral"),
                    "importance": importance,
                    "emotional_intensity": data.get("emotional_intensity", importance),
                    "reflection": data.get("reflection", ""),
                    "memory_kind": data.get("memory_kind", "Misc"),
                    "source_layer": data.get("source_layer", "System"),
                    "tags": data.get("tags", ""),
                    "created_at": data.get("created_at", now),
                },
            )
            await db.commit()
            return {"status": "ok", "id": getattr(cursor, "lastrowid", None)}

    async def delete_event(self, event_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM MemoryEvent WHERE id = ?", (event_id,))
            await db.commit()
        return {"status": "ok"}

    async def create_reflection(self, data: dict) -> dict[str, str]:
        now = time.time()
        reflection = data.get("reflection", data.get("summary", ""))
        async with self.db_factory() as db:
            await self._insert(
                db,
                "DailyReflection",
                {
                    "date": data.get("date"),
                    "reflection": reflection,
                    "summary": data.get("summary", reflection),
                    "raw_log": data.get("raw_log", ""),
                    "meta": data.get("meta", "{}"),
                    "created_at": data.get("created_at", now),
                },
            )
            await db.commit()
        return {"status": "ok"}

    async def update_reflection(self, date: str, data: dict) -> dict[str, str]:
        reflection = data.get("reflection", data.get("summary", ""))
        async with self.db_factory() as db:
            await self._update(
                db,
                "DailyReflection",
                "date",
                date,
                {
                    "reflection": reflection,
                    "summary": data.get("summary", reflection),
                    "raw_log": data.get("raw_log"),
                    "meta": data.get("meta"),
                },
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_reflection(self, date: str) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM DailyReflection WHERE date = ?", (date,))
            await db.commit()
        return {"status": "ok"}

    async def create_node(self, data: dict) -> dict[str, object]:
        now = time.time()
        async with self.db_factory() as db:
            cursor = await self._insert(
                db,
                "MemoryNode",
                {
                    "name": data.get("name"),
                    "type": data.get("type"),
                    "description": data.get("description"),
                    "last_updated": data.get("last_updated", now),
                },
            )
            await db.commit()
            return {"status": "ok", "id": getattr(cursor, "lastrowid", None)}

    async def update_node(self, node_id: int, data: dict) -> dict[str, str]:
        now = time.time()
        async with self.db_factory() as db:
            await self._update(
                db,
                "MemoryNode",
                "id",
                node_id,
                {
                    "name": data.get("name"),
                    "type": data.get("type"),
                    "description": data.get("description"),
                    "last_updated": data.get("last_updated", now),
                },
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_node(self, node_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM MemoryNode WHERE id = ?", (node_id,))
            await db.commit()
        return {"status": "ok"}

    async def create_jargon(self, data: dict) -> dict[str, object]:
        now = time.time()
        content = data.get("content", "")
        async with self.db_factory() as db:
            cursor = await self._insert(
                db,
                "Jargon",
                {
                    "content": content,
                    "raw_content": data.get("raw_content", content),
                    "meaning": data.get("meaning", ""),
                    "is_jargon": data.get("is_jargon", 1),
                    "is_complete": data.get("is_complete", 1),
                    "count": data.get("count", 0),
                    "group_id": data.get("group_id", "GLOBAL"),
                    "created_at": data.get("created_at", now),
                    "updated_at": data.get("updated_at", now),
                },
            )
            await db.commit()
            return {"status": "ok", "id": getattr(cursor, "lastrowid", None)}

    async def update_jargon(self, jargon_id: int, data: dict) -> dict[str, str]:
        now = time.time()
        async with self.db_factory() as db:
            await self._update(
                db,
                "Jargon",
                "id",
                jargon_id,
                {
                    "content": data.get("content"),
                    "raw_content": data.get("raw_content"),
                    "meaning": data.get("meaning"),
                    "is_jargon": data.get("is_jargon"),
                    "is_complete": data.get("is_complete"),
                    "group_id": data.get("group_id"),
                    "updated_at": data.get("updated_at", now),
                },
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_jargon(self, jargon_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM Jargon WHERE id = ?", (jargon_id,))
            await db.commit()
        return {"status": "ok"}


__all__ = ["MemoryUiService"]
