from __future__ import annotations


class MemoryUiService:
    def __init__(self, db_factory):
        self.db_factory = db_factory

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
        query = """
        INSERT INTO MemoryEvent (narrative, date, emotion, importance, memory_kind, source_layer, tags)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            data.get("narrative", ""),
            data.get("date", "2026-01-01"),
            data.get("emotion", "Neutral"),
            data.get("importance", 0.5),
            data.get("memory_kind", "Misc"),
            data.get("source_layer", "System"),
            data.get("tags", ""),
        )
        async with self.db_factory() as db:
            cursor = await db.execute(query, params)
            await db.commit()
            return {"status": "ok", "id": cursor.lastrowid}

    async def delete_event(self, event_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM MemoryEvent WHERE id = ?", (event_id,))
            await db.commit()
        return {"status": "ok"}

    async def create_reflection(self, data: dict) -> dict[str, str]:
        query = """
        INSERT INTO DailyReflection (date, summary, raw_log, meta)
        VALUES (?, ?, ?, ?)
        """
        params = (data.get("date"), data.get("summary", ""), data.get("raw_log", ""), data.get("meta", "{}"))
        async with self.db_factory() as db:
            await db.execute(query, params)
            await db.commit()
        return {"status": "ok"}

    async def update_reflection(self, date: str, data: dict) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("UPDATE DailyReflection SET summary = ? WHERE date = ?", (data.get("summary"), date))
            await db.commit()
        return {"status": "ok"}

    async def delete_reflection(self, date: str) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM DailyReflection WHERE date = ?", (date,))
            await db.commit()
        return {"status": "ok"}

    async def create_node(self, data: dict) -> dict[str, object]:
        async with self.db_factory() as db:
            cursor = await db.execute(
                "INSERT INTO MemoryNode (name, type, description) VALUES (?, ?, ?)",
                (data.get("name"), data.get("type"), data.get("description")),
            )
            await db.commit()
            return {"status": "ok", "id": cursor.lastrowid}

    async def update_node(self, node_id: int, data: dict) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute(
                "UPDATE MemoryNode SET name = ?, type = ?, description = ? WHERE id = ?",
                (data.get("name"), data.get("type"), data.get("description"), node_id),
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_node(self, node_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM MemoryNode WHERE id = ?", (node_id,))
            await db.commit()
        return {"status": "ok"}

    async def create_jargon(self, data: dict) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute(
                "INSERT INTO Jargon (content, meaning, is_jargon, is_complete, count, group_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    data.get("content"),
                    data.get("meaning"),
                    data.get("is_jargon", 1),
                    data.get("is_complete", 1),
                    0,
                    data.get("group_id", "GLOBAL"),
                ),
            )
            await db.commit()
        return {"status": "ok"}

    async def update_jargon(self, jargon_id: int, data: dict) -> dict[str, str]:
        updates = []
        params = []
        for key in ["meaning", "is_jargon", "is_complete"]:
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])

        if updates:
            params.append(jargon_id)
            async with self.db_factory() as db:
                await db.execute(f"UPDATE Jargon SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
        return {"status": "ok"}

    async def delete_jargon(self, jargon_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM Jargon WHERE id = ?", (jargon_id,))
            await db.commit()
        return {"status": "ok"}


__all__ = ["MemoryUiService"]
