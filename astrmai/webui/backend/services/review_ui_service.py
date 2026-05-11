from __future__ import annotations

import time

from ..adapters.plugin_api import PluginApiAdapter


class ReviewUiService:
    def __init__(self, plugin_api: PluginApiAdapter, db_factory):
        self.plugin_api = plugin_api
        self.db_factory = db_factory

    async def _columns(self, db) -> set[str]:
        async with db.execute("PRAGMA table_info(ExpressionPattern)") as cursor:
            rows = await cursor.fetchall()
            return {str(row[1]) for row in rows}

    @staticmethod
    def _normalize_item(item: dict) -> dict:
        if "status" not in item and "review_status" in item:
            item["status"] = item.get("review_status")
        return item

    async def list_pending(self):
        facade_items = await self.plugin_api.list_pending_reviews()
        if facade_items:
            return facade_items
        async with self.db_factory() as db:
            columns = await self._columns(db)
            if "status" in columns:
                query = "SELECT * FROM ExpressionPattern WHERE status='pending' ORDER BY id DESC"
                params: list[object] = []
            elif "review_status" in columns:
                query = (
                    "SELECT * FROM ExpressionPattern "
                    "WHERE review_status IN (?, ?, ?) ORDER BY id DESC"
                )
                params = ["pending", "revision_needed", "pending_human"]
            else:
                return []
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [self._normalize_item(dict(row)) for row in rows]

    async def list_reviews(self, status=None, group_id=None, keyword=None, page: int = 1, page_size: int = 20):
        offset = (page - 1) * page_size
        async with self.db_factory() as db:
            columns = await self._columns(db)
            filters = ["1=1"]
            params: list[object] = []
            if status:
                if "status" in columns:
                    filters.append("status = ?")
                    params.append(status)
                elif "review_status" in columns:
                    filters.append("review_status = ?")
                    params.append(status)
            if group_id:
                filters.append("group_id = ?")
                params.append(group_id)
            if keyword:
                filters.append("situation LIKE ?")
                params.append(f"%{keyword}%")
            where = " AND ".join(filters)
            query = f"SELECT * FROM ExpressionPattern WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?"
            async with db.execute(query, [*params, page_size, offset]) as cursor:
                rows = await cursor.fetchall()
                items = [self._normalize_item(dict(row)) for row in rows]
            count_query = f"SELECT COUNT(*) FROM ExpressionPattern WHERE {where}"
            async with db.execute(count_query, params) as cursor:
                total = (await cursor.fetchone())[0]
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    async def submit_review(self, review_id: int, action: str, replacement=None, weight=None, reason=None):
        mapped = "approved" if action == "approve" else "rejected"
        async with self.db_factory() as db:
            columns = await self._columns(db)
            updates = []
            params: list[object] = []
            if "status" in columns:
                updates.append("status = ?")
                params.append(mapped)
            if "review_status" in columns:
                updates.append("review_status = ?")
                params.append(mapped)
            if "checked" in columns:
                updates.append("checked = ?")
                params.append(1 if mapped == "approved" else 0)
            if "rejected" in columns:
                updates.append("rejected = ?")
                params.append(1 if mapped == "rejected" else 0)
            if "last_review_time" in columns:
                updates.append("last_review_time = ?")
                params.append(time.time())
            if replacement and "expression" in columns:
                updates.append("expression = ?")
                params.append(replacement)
            if weight is not None and "weight" in columns:
                updates.append("weight = ?")
                params.append(weight)
            if reason and "review_reason" in columns:
                updates.append("review_reason = ?")
                params.append(reason)
            if not updates:
                return {"status": "ok"}
            params.append(review_id)
            await db.execute(f"UPDATE ExpressionPattern SET {', '.join(updates)} WHERE id = ?", params)
            await db.commit()
        return {"status": "ok"}

    async def batch_review(self, ids: list[int], action: str):
        if not ids:
            return {"status": "ok", "updated": 0}
        mapped = "approved" if action == "approve" else "rejected"
        placeholders = ",".join(["?"] * len(ids))
        async with self.db_factory() as db:
            columns = await self._columns(db)
            updates = []
            params: list[object] = []
            if "status" in columns:
                updates.append("status = ?")
                params.append(mapped)
            if "review_status" in columns:
                updates.append("review_status = ?")
                params.append(mapped)
            if "checked" in columns:
                updates.append("checked = ?")
                params.append(1 if mapped == "approved" else 0)
            if "rejected" in columns:
                updates.append("rejected = ?")
                params.append(1 if mapped == "rejected" else 0)
            if "last_review_time" in columns:
                updates.append("last_review_time = ?")
                params.append(time.time())
            if not updates:
                return {"status": "ok", "updated": 0}
            await db.execute(
                f"UPDATE ExpressionPattern SET {', '.join(updates)} WHERE id IN ({placeholders})",
                [*params, *ids],
            )
            await db.commit()
        return {"status": "ok", "updated": len(ids)}

    async def create_review(self, data: dict) -> dict[str, object]:
        async with self.db_factory() as db:
            columns = await self._columns(db)
            if "status" in columns:
                query = """
                INSERT INTO ExpressionPattern (situation, expression, style, weight, status, count, group_id)
                VALUES (?, ?, ?, ?, 'approved', 0, ?)
                """
                params = (
                    data.get("situation", ""),
                    data.get("expression", ""),
                    data.get("style", ""),
                    float(data.get("weight", 1.0)),
                    data.get("group_id", "GLOBAL"),
                )
            else:
                now = time.time()
                query = """
                INSERT INTO ExpressionPattern (
                    situation, expression, style, content_list, count, checked, rejected,
                    modified_by, source, shared_scope, think_level, review_status,
                    review_reason, review_suggestion, last_review_time, weight,
                    last_active_time, create_time, group_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                params = (
                    data.get("situation", ""),
                    data.get("expression", ""),
                    data.get("style", ""),
                    "[]",
                    0,
                    1,
                    0,
                    "plugin_page",
                    "plugin_page",
                    "",
                    0,
                    "approved",
                    "",
                    "",
                    now,
                    float(data.get("weight", 1.0)),
                    now,
                    now,
                    data.get("group_id", "GLOBAL"),
                )
            cursor = await db.execute(query, params)
            await db.commit()
            return {"status": "ok", "id": cursor.lastrowid}

    async def update_review_record(self, review_id: int, data: dict) -> dict[str, str]:
        expression = data.get("expression")
        style = data.get("style")
        weight = data.get("weight")

        async with self.db_factory() as db:
            columns = await self._columns(db)
            updates = []
            params: list[object] = []
            if expression is not None and "expression" in columns:
                updates.append("expression = ?")
                params.append(expression)
            if style is not None and "style" in columns:
                updates.append("style = ?")
                params.append(style)
            if weight is not None and "weight" in columns:
                updates.append("weight = ?")
                params.append(float(weight))

            if updates:
                params.append(review_id)
                await db.execute(f"UPDATE ExpressionPattern SET {', '.join(updates)} WHERE id = ?", params)
                await db.commit()
        return {"status": "ok"}

    async def delete_review_record(self, review_id: int) -> dict[str, str]:
        async with self.db_factory() as db:
            await db.execute("DELETE FROM ExpressionPattern WHERE id = ?", (review_id,))
            await db.commit()
        return {"status": "ok"}


__all__ = ["ReviewUiService"]
