from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
import sqlite3

from ..adapters.plugin_api import PluginApiAdapter


class ReviewUiService:
    def __init__(self, plugin_api: PluginApiAdapter, db_factory):
        self.plugin_api = plugin_api
        self.db_factory = db_factory

    def _pattern_service(self):
        if self.plugin_api:
            return self.plugin_api.get_expression_pattern_service()
        return None

    def _runtime_bound(self) -> bool:
        return self._pattern_service() is not None

    @staticmethod
    def _normalize_item(item: dict) -> dict:
        if "status" not in item and "review_status" in item:
            item["status"] = item.get("review_status")
        return item

    @staticmethod
    def _record_dict(record) -> dict:
        if record is None:
            return {}
        if isinstance(record, dict):
            return dict(record)
        if is_dataclass(record):
            return asdict(record)
        if hasattr(record, "__dict__"):
            return dict(getattr(record, "__dict__", {}) or {})
        return {}

    @staticmethod
    def _canonical_to_review_item(row: dict) -> dict:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        review_status = str(metadata.get("review_status") or row.get("status") or "pending").strip().lower()
        try:
            weight = float(1.0 if metadata.get("weight") is None else metadata.get("weight"))
        except (TypeError, ValueError):
            weight = 1.0
        return {
            "id": str(row.get("id") or ""),
            "group_id": str(row.get("session_id") or ""),
            "situation": str(metadata.get("situation") or ""),
            "expression": str(row.get("content") or ""),
            "style": str(metadata.get("style") or ""),
            "count": int(metadata.get("count") or 1),
            "checked": review_status == "approved",
            "rejected": review_status == "rejected",
            "review_status": review_status,
            "review_reason": str(metadata.get("review_reason") or ""),
            "review_suggestion": str(metadata.get("review_suggestion") or ""),
            "shared_scope": str(metadata.get("shared_scope") or ""),
            "think_level": int(metadata.get("think_level") or 0),
            "weight": weight,
            "modified_by": str(metadata.get("modified_by") or ""),
            "source": str(row.get("source") or ""),
            "content_list": json.dumps(metadata.get("content_samples") or [], ensure_ascii=False),
            "last_review_time": float(metadata.get("last_review_time") or 0.0),
            "last_active_time": float(metadata.get("last_active_time") or row.get("last_access_time") or 0.0),
            "create_time": float(row.get("create_time") or row.get("created_at") or 0.0),
            "status": str(row.get("status") or "review_pending"),
            "legacy": False,
        }

    async def _list_canonical_reviews(self, *, status=None, group_id=None, keyword=None, page: int = 1, page_size: int = 20):
        offset = max(page - 1, 0) * page_size
        get_store = getattr(self.plugin_api, "get_v2_store", None) if self.plugin_api else None
        store = get_store() if callable(get_store) else None
        rows: list[dict] = []
        if store is not None and hasattr(store, "list_canonical"):
            store_offset = 0
            while True:
                result = await store.list_canonical(
                    kind="expression_pattern",
                    limit=500,
                    offset=store_offset,
                    include_inactive=True,
                )
                batch = [dict(item) for item in list(result.get("items", []) or [])]
                rows.extend(batch)
                store_offset += len(batch)
                if not batch or store_offset >= int(result.get("total", 0) or 0):
                    break
        else:
            try:
                async with self.db_factory() as db:
                    async with db.execute(
                        """
                        SELECT id, session_id, source, content, status, create_time, last_access_time, metadata
                        FROM canonical_memories
                        WHERE kind = 'expression_pattern'
                        ORDER BY update_time DESC, create_time DESC
                        """
                    ) as cursor:
                        rows = [dict(row) for row in await cursor.fetchall()]
            except sqlite3.OperationalError:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}
        items = [self._canonical_to_review_item(row) for row in rows]
        filtered = []
        normalized_status = str(status or "").strip().lower()
        normalized_group = str(group_id or "").strip()
        normalized_keyword = str(keyword or "").strip().lower()
        for item in items:
            if normalized_status:
                review_status = str(item.get("review_status") or item.get("status") or "").strip().lower()
                status_value = str(item.get("status") or "").strip().lower()
                if normalized_status not in {review_status, status_value}:
                    continue
            if normalized_group and str(item.get("group_id") or "") != normalized_group:
                continue
            if normalized_keyword:
                haystack = " ".join(
                    [
                        str(item.get("situation") or ""),
                        str(item.get("expression") or ""),
                        str(item.get("review_reason") or ""),
                        str(item.get("review_suggestion") or ""),
                    ]
                ).lower()
                if normalized_keyword not in haystack:
                    continue
            filtered.append(self._normalize_item(item))
        return {
            "items": filtered[offset : offset + page_size],
            "total": len(filtered),
            "page": page,
            "page_size": page_size,
        }

    async def list_pending(self):
        facade_items = await self.plugin_api.list_pending_reviews()
        if self._runtime_bound():
            return facade_items
        result = await self._list_canonical_reviews(status="pending", page=1, page_size=200)
        pending = [
            item
            for item in result["items"]
            if str(item.get("review_status") or "").strip().lower() in {"pending", "revision_needed", "pending_human"}
        ]
        return pending

    async def list_reviews(self, status=None, group_id=None, keyword=None, page: int = 1, page_size: int = 20):
        try:
            page = max(1, int(page or 1))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = max(1, min(int(page_size or 20), 200))
        except (TypeError, ValueError):
            page_size = 20
        get_store = getattr(self.plugin_api, "get_v2_store", None) if self.plugin_api else None
        if self._runtime_bound() and not callable(get_store) and self.db_factory is None:
            facade_items = await self.plugin_api.list_recent_reviews(group_id=group_id or "", limit=10000)
            normalized_status = str(status or "").strip().lower()
            normalized_keyword = str(keyword or "").strip().lower()
            filtered = []
            for raw_item in facade_items:
                item = self._normalize_item(dict(raw_item))
                item_status = str(item.get("review_status") or item.get("status") or "").strip().lower()
                if normalized_status and item_status != normalized_status:
                    continue
                if normalized_keyword:
                    haystack = f"{item.get('situation', '')} {item.get('expression', '')}".lower()
                    if normalized_keyword not in haystack:
                        continue
                filtered.append(item)
            offset = (page - 1) * page_size
            return {
                "items": filtered[offset : offset + page_size],
                "total": len(filtered),
                "page": page,
                "page_size": page_size,
            }
        return await self._list_canonical_reviews(
            status=status,
            group_id=group_id,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )

    _ACTION_MAP = {"approve": "approved", "reject": "rejected", "revise": "revision_needed", "replace": "replace"}

    async def submit_review(
        self,
        review_id: str,
        action: str,
        replacement=None,
        weight=None,
        reason=None,
        *,
        situation=None,
        style=None,
        shared_scope=None,
        review_suggestion=None,
    ):
        mapped = self._ACTION_MAP.get(action)
        if mapped is None:
            return {"status": "error", "message": f"Unknown action: {action!r}"}
        result = await self.plugin_api.submit_review(
            pattern_id=str(review_id),
            decision=mapped,
            reviewer_id="webui",
            replacement_expression=replacement or "",
            style=style or "",
            reason=reason or "",
            weight_delta=float(weight) - 1.0 if weight is not None else 0.0,
        )
        extra_update = {
            key: value
            for key, value in {
                "situation": situation,
                "style": style,
                "shared_scope": shared_scope,
                "review_reason": reason,
                "review_suggestion": review_suggestion,
            }.items()
            if value is not None
        }
        if result and result.get("id") and extra_update:
            service = self._pattern_service()
            if service and hasattr(service, "update_review"):
                await service.update_review(str(review_id), modified_by="human:webui", **extra_update)
        if result and result.get("id"):
            return {"status": "ok", "data": result}
        service = self._pattern_service()
        if service and hasattr(service, "update_review"):
            updated = await service.update_review(
                str(review_id),
                replacement_expression=replacement or None,
                apply_replacement=bool(replacement),
                situation=situation,
                shared_scope=shared_scope,
                review_status=mapped,
                review_reason=reason or "",
                review_suggestion=review_suggestion or "",
                weight_delta=0.0,
                checked=(mapped == "approved"),
                rejected=(mapped == "rejected"),
                modified_by="human:webui",
                style=style,
            )
            if updated:
                if weight is not None:
                    delta = float(weight) - float(getattr(updated, "weight", 1.0) or 1.0)
                    if abs(delta) > 1e-9:
                        await service.adjust_weight(str(review_id), delta)
                        updated = await service.get_pattern(str(review_id))
                data = self._canonical_to_review_item(
                    {
                        "id": getattr(updated, "id", ""),
                        "session_id": getattr(updated, "group_id", ""),
                        "source": getattr(updated, "source", ""),
                        "content": getattr(updated, "expression", ""),
                        "status": getattr(updated, "status", "review_pending"),
                        "create_time": getattr(updated, "create_time", 0.0),
                        "last_access_time": getattr(updated, "last_active_time", 0.0),
                        "metadata": self._record_dict(updated),
                    }
                )
                return {"status": "ok", "data": self._normalize_item(data)}
        return {"status": "degraded", "message": "canonical review runtime unavailable", "readonly": True}

    async def batch_review(self, ids: list[str], action: str):
        if not ids:
            return {"status": "ok", "updated": 0}
        updated = 0
        for review_id in ids:
            result = await self.submit_review(str(review_id), action)
            if result.get("status") == "ok":
                updated += 1
        if updated:
            return {"status": "ok", "updated": updated}
        return {"status": "degraded", "updated": 0, "readonly": True}

    async def create_review(self, data: dict) -> dict[str, object]:
        service = self._pattern_service()
        if service and hasattr(service, "write_pattern"):
            pattern_id = await service.write_pattern(
                str(data.get("group_id") or "GLOBAL"),
                {
                    "situation": data.get("situation", ""),
                    "expression": data.get("expression", ""),
                    "style": data.get("style", ""),
                    "weight": float(data.get("weight", 1.0)),
                    "count": int(data.get("count", 1) or 1),
                    "review_status": str(data.get("review_status") or "approved"),
                    "summary": data.get("summary") or data.get("expression", ""),
                    "shared_scope": data.get("shared_scope", ""),
                },
                source="webui_expression_pattern",
            )
            return {"status": "ok", "id": pattern_id, "mode": "canonical"}
        return {"status": "degraded", "message": "canonical review runtime unavailable", "readonly": True}

    async def update_review_record(self, review_id: str, data: dict) -> dict[str, str]:
        expression = data.get("expression")
        style = data.get("style")
        weight = data.get("weight")
        situation = data.get("situation")
        shared_scope = data.get("shared_scope")
        review_reason = data.get("review_reason")
        review_suggestion = data.get("review_suggestion")
        service = self._pattern_service()
        if service and hasattr(service, "update_review"):
            await service.update_review(
                str(review_id),
                replacement_expression=expression or None,
                apply_replacement=expression is not None,
                situation=situation,
                shared_scope=shared_scope,
                review_reason=review_reason,
                review_suggestion=review_suggestion,
                modified_by="human:webui",
                style=style,
                weight_delta=0.0,
            )
            if weight is not None and hasattr(service, "get_pattern"):
                pattern = await service.get_pattern(str(review_id))
                if pattern:
                    delta = float(weight) - float(getattr(pattern, "weight", 1.0) or 1.0)
                    await service.adjust_weight(str(review_id), delta)
            return {"status": "ok"}
        return {"status": "degraded", "message": "canonical review runtime unavailable", "readonly": True}

    async def delete_review_record(self, review_id: str) -> dict[str, str]:
        service = self._pattern_service()
        if service and hasattr(service, "store"):
            await service.store.update_memory(str(review_id), status="deleted", visibility="maintenance_only")
            return {"status": "ok", "mode": "canonical_soft_delete"}
        return {"status": "degraded", "message": "canonical review runtime unavailable", "readonly": True}


__all__ = ["ReviewUiService"]
