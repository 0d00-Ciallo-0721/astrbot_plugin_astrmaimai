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
    "name",
    "nickname",
    "nickname_reason",
    "social_score",
    "identity",
    "tags",
    "persona_analysis",
]

_MANUAL_LOCK_FIELDS = {
    "name",
    "nickname",
    "nickname_reason",
    "identity",
    "tags",
    "persona_analysis",
}


class UserUiService:
    def __init__(self, db_factory):
        self.db_factory = db_factory

    @staticmethod
    def _parse_json(value: Any, default: Any):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return default
        if value is None:
            return default
        return value

    def _parse_row(self, row_dict: dict[str, Any]) -> dict[str, Any]:
        for field in SLICE_FIELDS:
            row_dict[field] = self._parse_json(row_dict.get(field), [])
        row_dict["tags"] = self._parse_json(row_dict.get("tags"), [])
        row_dict["group_footprints"] = self._parse_json(row_dict.get("group_footprints"), {})
        row_dict["profile_metadata"] = self._parse_json(row_dict.get("profile_metadata"), {})
        return row_dict

    @staticmethod
    def _normalize_tags(value: Any) -> list[str]:
        if isinstance(value, list):
            raw_items = [str(item).strip() for item in value]
        else:
            raw_items = [item.strip() for item in str(value or "").split(",")]
        seen: set[str] = set()
        result: list[str] = []
        for item in raw_items:
            if not item:
                continue
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            result.append(item)
        return result

    async def _load_row(self, db, user_id: str) -> dict[str, Any] | None:
        async with db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._parse_row(dict(row))

    @staticmethod
    def _merge_manual_locks(profile_metadata: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        current = profile_metadata.get("manual_locked_fields", [])
        locks = {str(item).strip() for item in current if str(item).strip()}
        locks.update(field for field in fields if field)
        profile_metadata["manual_locked_fields"] = sorted(locks)
        if "nickname" in fields or "nickname_reason" in fields:
            profile_metadata["nickname_origin"] = "manual"
        return profile_metadata

    async def list_users(self) -> list[dict[str, Any]]:
        try:
            async with self.db_factory() as db:
                async with db.execute("SELECT * FROM user_profiles ORDER BY user_id") as cursor:
                    rows = await cursor.fetchall()
                    return [self._parse_row(dict(row)) for row in rows]
        except sqlite3.OperationalError:
            return []

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        async with self.db_factory() as db:
            return await self._load_row(db, user_id)

    async def update_user(self, user_id: str, data: dict[str, Any]) -> dict[str, str]:
        async with self.db_factory() as db:
            existing = await self._load_row(db, user_id)
            if not existing:
                return {"status": "not_found"}
            updates: list[str] = []
            params: list[Any] = []
            manual_fields: list[str] = []

            for key in EDITABLE_FIELDS:
                if key not in data:
                    continue
                value = data[key]
                if key == "tags":
                    value = json.dumps(self._normalize_tags(value), ensure_ascii=False)
                elif key == "social_score":
                    value = float(value or 0.0)
                else:
                    value = str(value or "").strip()
                updates.append(f"{key} = ?")
                params.append(value)
                if key in _MANUAL_LOCK_FIELDS:
                    manual_fields.append(key)

            profile_metadata = dict(existing.get("profile_metadata", {}) or {})
            profile_metadata = self._merge_manual_locks(profile_metadata, manual_fields)
            updates.append("profile_metadata = ?")
            params.append(json.dumps(profile_metadata, ensure_ascii=False))

            if updates:
                params.append(user_id)
                await db.execute(f"UPDATE user_profiles SET {', '.join(updates)} WHERE user_id = ?", params)
                await db.commit()
        return {"status": "ok"}

    async def delete_user(self, user_id: str) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
            await db.commit()
        return {"status": "ok"}

    async def add_slice(self, user_id: str, slice_type: str, content: str) -> dict[str, Any] | None:
        if slice_type not in SLICE_FIELDS:
            raise ValueError("Invalid slice type")

        async with self.db_factory() as db:
            row = await self._load_row(db, user_id)
            if not row:
                return None
            slices = list(row.get(slice_type, []) or [])
            slices.append(str(content or "").strip())
            profile_metadata = self._merge_manual_locks(dict(row.get("profile_metadata", {}) or {}), [slice_type])
            await db.execute(
                f"UPDATE user_profiles SET {slice_type} = ?, profile_metadata = ? WHERE user_id = ?",
                (
                    json.dumps(slices, ensure_ascii=False),
                    json.dumps(profile_metadata, ensure_ascii=False),
                    user_id,
                ),
            )
            await db.commit()
        return {"status": "ok", "items": slices}

    async def update_slice(self, user_id: str, index: int, slice_type: str, content: str) -> dict[str, str] | None:
        if slice_type not in SLICE_FIELDS:
            raise ValueError("Invalid slice type")

        async with self.db_factory() as db:
            row = await self._load_row(db, user_id)
            if not row:
                return None
            slices = list(row.get(slice_type, []) or [])
            if index < 0 or index >= len(slices):
                raise IndexError("Index out of bounds")
            slices[index] = str(content or "").strip()
            profile_metadata = self._merge_manual_locks(dict(row.get("profile_metadata", {}) or {}), [slice_type])
            await db.execute(
                f"UPDATE user_profiles SET {slice_type} = ?, profile_metadata = ? WHERE user_id = ?",
                (
                    json.dumps(slices, ensure_ascii=False),
                    json.dumps(profile_metadata, ensure_ascii=False),
                    user_id,
                ),
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_slice(self, user_id: str, index: int, slice_type: str) -> dict[str, str] | None:
        if slice_type not in SLICE_FIELDS:
            raise ValueError("Invalid slice type")

        async with self.db_factory() as db:
            row = await self._load_row(db, user_id)
            if not row:
                return None
            slices = list(row.get(slice_type, []) or [])
            if 0 <= index < len(slices):
                slices.pop(index)
                profile_metadata = self._merge_manual_locks(dict(row.get("profile_metadata", {}) or {}), [slice_type])
                await db.execute(
                    f"UPDATE user_profiles SET {slice_type} = ?, profile_metadata = ? WHERE user_id = ?",
                    (
                        json.dumps(slices, ensure_ascii=False),
                        json.dumps(profile_metadata, ensure_ascii=False),
                        user_id,
                    ),
                )
                await db.commit()
        return {"status": "ok"}


__all__ = ["EDITABLE_FIELDS", "SLICE_FIELDS", "UserUiService"]
