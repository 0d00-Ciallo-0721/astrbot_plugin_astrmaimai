from __future__ import annotations

import json
import sqlite3
from typing import Any


SLICE_FIELDS = [
    "memory_points",
    "identity_points",
    "preference_points",
    "relationship_points",
    "speech_style_points",
]

EDITABLE_FIELDS = [
    "nickname",
    "nickname_reason",
    "social_score",
    "identity",
    "tags",
    "persona_analysis",
]


class UserUiService:
    def __init__(self, db_factory):
        self.db_factory = db_factory

    def _parse_slices(self, row_dict: dict[str, Any]) -> dict[str, Any]:
        for field in SLICE_FIELDS:
            value = row_dict.get(field)
            if isinstance(value, str) and value.startswith("[") and value.endswith("]"):
                try:
                    row_dict[field] = json.loads(value)
                except json.JSONDecodeError:
                    row_dict[field] = []
            else:
                row_dict[field] = []
        return row_dict

    async def list_users(self) -> list[dict[str, Any]]:
        try:
            async with self.db_factory() as db:
                async with db.execute("SELECT * FROM UserProfile ORDER BY user_id") as cursor:
                    rows = await cursor.fetchall()
                    return [self._parse_slices(dict(row)) for row in rows]
        except sqlite3.OperationalError:
            return []

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM UserProfile WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return self._parse_slices(dict(row))

    async def update_user(self, user_id: str, data: dict[str, Any]) -> dict[str, str]:
        updates: list[str] = []
        params: list[Any] = []
        for key in EDITABLE_FIELDS:
            if key in data:
                updates.append(f"{key} = ?")
                params.append(data[key])

        if updates:
            params.append(user_id)
            async with self.db_factory() as db:
                await db.execute(f"UPDATE UserProfile SET {', '.join(updates)} WHERE user_id = ?", params)
                await db.commit()
        return {"status": "ok"}

    async def delete_user(self, user_id: str) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM UserProfile WHERE user_id = ?", (user_id,))
            await db.commit()
        return {"status": "ok"}

    async def add_slice(self, user_id: str, slice_type: str, content: str) -> dict[str, Any] | None:
        if slice_type not in SLICE_FIELDS:
            raise ValueError("Invalid slice type")

        async with self.db_factory() as db:
            async with db.execute(f"SELECT {slice_type} FROM UserProfile WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                try:
                    slices = json.loads(row[0] or "[]")
                except json.JSONDecodeError:
                    slices = []

            slices.append(content)
            await db.execute(
                f"UPDATE UserProfile SET {slice_type} = ? WHERE user_id = ?",
                (json.dumps(slices, ensure_ascii=False), user_id),
            )
            await db.commit()
        return {"status": "ok", "items": slices}

    async def update_slice(self, user_id: str, index: int, slice_type: str, content: str) -> dict[str, str] | None:
        if slice_type not in SLICE_FIELDS:
            raise ValueError("Invalid slice type")

        async with self.db_factory() as db:
            async with db.execute(f"SELECT {slice_type} FROM UserProfile WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                try:
                    slices = json.loads(row[0] or "[]")
                except json.JSONDecodeError:
                    slices = []

            if index < 0 or index >= len(slices):
                raise IndexError("Index out of bounds")

            slices[index] = content
            await db.execute(
                f"UPDATE UserProfile SET {slice_type} = ? WHERE user_id = ?",
                (json.dumps(slices, ensure_ascii=False), user_id),
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_slice(self, user_id: str, index: int, slice_type: str) -> dict[str, str] | None:
        if slice_type not in SLICE_FIELDS:
            raise ValueError("Invalid slice type")

        async with self.db_factory() as db:
            async with db.execute(f"SELECT {slice_type} FROM UserProfile WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                try:
                    slices = json.loads(row[0] or "[]")
                except json.JSONDecodeError:
                    slices = []

            if 0 <= index < len(slices):
                slices.pop(index)
                await db.execute(
                    f"UPDATE UserProfile SET {slice_type} = ? WHERE user_id = ?",
                    (json.dumps(slices, ensure_ascii=False), user_id),
                )
                await db.commit()
        return {"status": "ok"}


__all__ = ["EDITABLE_FIELDS", "SLICE_FIELDS", "UserUiService"]
