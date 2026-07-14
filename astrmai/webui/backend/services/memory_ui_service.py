from __future__ import annotations

import json
import time

from ....memory.contracts.memory_query import MemoryWriteRequest

from ..adapters.plugin_api import PluginApiAdapter


class MemoryUiService:
    def __init__(self, db_factory, plugin_api: PluginApiAdapter | None = None):
        self.db_factory = db_factory
        self.plugin_api = plugin_api

    def _memory_engine(self):
        return self.plugin_api.get_memory_engine() if self.plugin_api else None

    async def runtime_status(self) -> dict[str, object]:
        engine = self._memory_engine()
        instant_gate = self.plugin_api.get_instant_gate() if self.plugin_api else None
        memory_pipeline = self.plugin_api.get_memory_pipeline() if self.plugin_api else None
        session_summarizer = self.plugin_api.get_session_summarizer() if self.plugin_api else None
        pipeline_status = (
            memory_pipeline.describe_runtime_status()
            if memory_pipeline is not None and hasattr(memory_pipeline, "describe_runtime_status")
            else {}
        )
        observer = self.plugin_api.get_memory_observer() if self.plugin_api else None
        observer_snapshot = (
            await observer.runtime_snapshot(
                instant_gate_ready=bool(instant_gate is not None),
                memory_pipeline_ready=bool(memory_pipeline is not None),
                session_summarizer_ready=bool(session_summarizer is not None),
                pipeline_status=pipeline_status,
            )
            if observer is not None and hasattr(observer, "runtime_snapshot")
            else {}
        )
        return {
            "status": "ok",
            "runtime_bound": bool(engine is not None),
            "instant_gate_ready": bool(instant_gate is not None),
            "memory_pipeline_ready": bool(memory_pipeline is not None),
            "session_summarizer_ready": bool(session_summarizer is not None),
            "memory_pipeline_status": dict(pipeline_status or {}),
            "observer_status": dict(observer_snapshot or {}),
        }

    async def chat_buffer_status(self, chat_id: str) -> dict[str, object]:
        engine = self._memory_engine()
        memory_pipeline = self.plugin_api.get_memory_pipeline() if self.plugin_api else None
        observer = self.plugin_api.get_memory_observer() if self.plugin_api else None
        if memory_pipeline is not None and hasattr(memory_pipeline, "describe_chat_buffer"):
            data = memory_pipeline.describe_chat_buffer(chat_id)
            if observer is not None and hasattr(observer, "chat_snapshot"):
                data = await observer.chat_snapshot(
                    chat_id=str(chat_id or ""),
                    pipeline_buffer=data,
                    worker_active=bool(getattr(memory_pipeline, "is_worker_active", lambda _: False)(chat_id)),
                    limit=20,
                )
            return {
                "status": "ok",
                "runtime_bound": True,
                "data": data,
            }
        return {
            "status": "ok",
            "runtime_bound": False,
            "data": {
                "chat_id": str(chat_id or ""),
                "pending_messages": 0,
                "last_update": 0.0,
                "cooldown_until": 0.0,
                "failures": 0,
                "last_memory_run_at": 0.0,
            },
        }

    async def observability_runtime(self) -> dict[str, object]:
        return await self.runtime_status()

    async def observability_chat(self, chat_id: str) -> dict[str, object]:
        return await self.chat_buffer_status(chat_id)

    async def observability_events(
        self,
        *,
        chat_id: str = "",
        component: str = "",
        level: str = "",
        limit: int = 50,
    ) -> dict[str, object]:
        engine = self._memory_engine()
        observer = self.plugin_api.get_memory_observer() if self.plugin_api else None
        if observer is not None and hasattr(observer, "recent_events"):
            items = await observer.recent_events(
                chat_id=str(chat_id or "") or None,
                component=str(component or ""),
                level=str(level or ""),
                limit=limit,
            )
            items = [self.plugin_api.format_timeline_item(item) for item in list(items or [])]
            return {"status": "ok", "runtime_bound": True, "items": list(items or [])}
        return {"status": "ok", "runtime_bound": False, "items": []}

    async def observability_errors(self, *, chat_id: str = "", limit: int = 50) -> dict[str, object]:
        engine = self._memory_engine()
        observer = self.plugin_api.get_memory_observer() if self.plugin_api else None
        if observer is not None and hasattr(observer, "recent_errors"):
            items = await observer.recent_errors(chat_id=str(chat_id or "") or None, limit=limit)
            items = [self.plugin_api.format_timeline_item(item) for item in list(items or [])]
            return {"status": "ok", "runtime_bound": True, "items": list(items or [])}
        return {"status": "ok", "runtime_bound": False, "items": []}

    _ALLOWED_TABLES = {"canonical_memories", "DailyReflection", "MemoryNode"}

    def _validate_table(self, table: str) -> None:
        if table not in self._ALLOWED_TABLES:
            raise ValueError(f"Table {table!r} is not in the allowed whitelist")

    async def _columns(self, db, table: str) -> set[str]:
        self._validate_table(table)
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
        canonical = await self.list_canonical(kind="event", limit=100)
        canonical_items = []
        canonical_ids: set[str] = set()
        for row in list(canonical.get("items", []) or []):
            item = dict(row)
            item["legacy"] = False
            item["canonical_id"] = str(item.get("id") or "")
            canonical_ids.add(item["canonical_id"])
            canonical_items.append(item)
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM MemoryEvent ORDER BY id DESC LIMIT 100") as cursor:
                rows = await cursor.fetchall()
                legacy_items = []
                for row in rows:
                    item = dict(row)
                    item["legacy"] = True
                    item["canonical_id"] = self._extract_canonical_id(item)
                    if item["canonical_id"] and item["canonical_id"] in canonical_ids:
                        continue
                    legacy_items.append(item)
                return (canonical_items + legacy_items)[:100]

    @staticmethod
    def _extract_canonical_id(item: dict) -> str:
        for value in (item.get("tags"), item.get("metadata"), item.get("meta")):
            if not value:
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            marker = "canonical_id:"
            if marker in text:
                return text.split(marker, 1)[1].split(",", 1)[0].split("]", 1)[0].strip(" '\"")
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("canonical_id"):
                    return str(parsed["canonical_id"])
                if isinstance(parsed, list):
                    for entry in parsed:
                        if str(entry).startswith(marker):
                            return str(entry).split(marker, 1)[1].strip()
            except Exception:
                continue
        return ""

    async def list_canonical(
        self,
        *,
        session_id: str = "",
        persona_id: str = "",
        kind: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        try:
            limit = max(1, min(int(limit or 100), 500))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        engine = self._memory_engine()
        store = self.plugin_api.get_v2_store() if self.plugin_api else None
        if store and hasattr(store, "list_canonical"):
            result = await store.list_canonical(
                session_id=session_id,
                persona_id=persona_id,
                kind=kind,
                status=status,
                limit=limit,
                offset=offset,
            )
            return {"status": "ok", "runtime_bound": True, **result}
        async with self.db_factory() as db:
            try:
                where = []
                params = []
                if session_id:
                    where.append("session_id = ?")
                    params.append(session_id)
                if persona_id:
                    where.append("persona_id = ?")
                    params.append(persona_id)
                if kind:
                    where.append("kind = ?")
                    params.append(kind)
                if status:
                    where.append("status = ?")
                    params.append(status)
                where_sql = f"WHERE {' AND '.join(where)}" if where else ""
                async with db.execute(f"SELECT COUNT(*) FROM canonical_memories {where_sql}", tuple(params)) as cursor:
                    total_row = await cursor.fetchone()
                async with db.execute(
                    f"SELECT * FROM canonical_memories {where_sql} ORDER BY update_time DESC LIMIT ? OFFSET ?",
                    (*params, max(1, min(int(limit or 100), 500)), max(0, int(offset or 0))),
                ) as cursor:
                    rows = await cursor.fetchall()
                return {
                    "status": "ok",
                    "runtime_bound": False,
                    "items": [self._canonical_row(row) for row in rows],
                    "total": int(total_row[0] if total_row else 0),
                }
            except Exception:
                return {"status": "ok", "runtime_bound": False, "items": [], "total": 0}

    async def get_canonical(self, memory_id: str) -> dict:
        engine = self._memory_engine()
        store = self.plugin_api.get_v2_store() if self.plugin_api else None
        if store and hasattr(store, "get_canonical"):
            item = await store.get_canonical(memory_id, include_inactive=True)
            if not item:
                return {"status": "not_found", "data": None, "runtime_bound": True}
            return {"status": "ok", "data": self.plugin_api.candidate_to_dict(item), "runtime_bound": True}
        from ..repositories import CanonicalMemoryRepository
        repo = CanonicalMemoryRepository(self.db_factory)
        row = await repo.get_by_id(memory_id)
        return {
            "status": "ok" if row else "not_found",
            "data": self._canonical_row(row) if row else None,
            "runtime_bound": False,
        }

    async def delete_canonical(self, memory_id: str) -> dict:
        engine = self._memory_engine()
        maintenance = self.plugin_api.get_maintenance_service() if self.plugin_api else None
        if maintenance and hasattr(maintenance, "soft_delete"):
            changed = await maintenance.soft_delete(memory_id, reason="webui")
            return {"status": "ok", "changed": bool(changed), "runtime_bound": True}
        async with self.db_factory() as db:
            try:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'deleted', deleted_reason = 'webui', update_time = ?
                    WHERE id = ?
                    """,
                    (time.time(), memory_id),
                )
                await db.commit()
                return {"status": "ok", "changed": bool(cursor.rowcount), "runtime_bound": False}
            except Exception:
                return {"status": "ok", "changed": False, "runtime_bound": False}

    async def restore_canonical(self, memory_id: str) -> dict:
        engine = self._memory_engine()
        maintenance = self.plugin_api.get_maintenance_service() if self.plugin_api else None
        if maintenance and hasattr(maintenance, "restore"):
            changed = await maintenance.restore(memory_id, reason="webui")
            return {"status": "ok", "changed": bool(changed), "runtime_bound": True}
        async with self.db_factory() as db:
            try:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'active', deleted_reason = '', update_time = ?, last_access_time = ?
                    WHERE id = ? AND status IN ('stale', 'deleted')
                    """,
                    (time.time(), time.time(), memory_id),
                )
                await db.commit()
                return {"status": "ok", "changed": bool(cursor.rowcount), "runtime_bound": False}
            except Exception:
                return {"status": "ok", "changed": False, "runtime_bound": False}

    async def mark_canonical_stale(self, memory_id: str) -> dict:
        engine = self._memory_engine()
        maintenance = self.plugin_api.get_maintenance_service() if self.plugin_api else None
        if maintenance and hasattr(maintenance, "mark_stale"):
            changed = await maintenance.mark_stale(memory_id, reason="webui")
            return {"status": "ok", "changed": bool(changed), "runtime_bound": True}
        async with self.db_factory() as db:
            try:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'stale', deleted_reason = 'webui', update_time = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (time.time(), memory_id),
                )
                await db.commit()
                return {"status": "ok", "changed": bool(cursor.rowcount), "runtime_bound": False}
            except Exception:
                return {"status": "ok", "changed": False, "runtime_bound": False}

    async def merge_canonical(self, memory_id: str, target_id: str) -> dict:
        if not target_id:
            return {"status": "error", "message": "target_id required", "changed": False}
        engine = self._memory_engine()
        maintenance = self.plugin_api.get_maintenance_service() if self.plugin_api else None
        if maintenance and hasattr(maintenance, "mark_merged"):
            changed = await maintenance.mark_merged([memory_id], superseded_by=target_id)
            return {"status": "ok", "changed": bool(changed), "runtime_bound": True}
        async with self.db_factory() as db:
            try:
                cursor = await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'merged', superseded_by = ?, update_time = ?
                    WHERE id = ?
                    """,
                    (target_id, time.time(), memory_id),
                )
                await db.commit()
                return {"status": "ok", "changed": bool(cursor.rowcount), "runtime_bound": False}
            except Exception:
                return {"status": "ok", "changed": False, "runtime_bound": False}

    @staticmethod
    def _canonical_row(row) -> dict:
        item = dict(row)
        for key in ("tags", "metadata"):
            try:
                item[key] = json.loads(item.get(key) or ("[]" if key == "tags" else "{}"))
            except Exception:
                item[key] = [] if key == "tags" else {}
        return item

    @staticmethod
    def _canonical_jargon_view(item: dict) -> dict:
        metadata = dict(item.get("metadata") or {})
        status = str(item.get("status") or "active")
        review_status = str(metadata.get("review_status") or ("approved" if status == "active" else status))
        return {
            "id": item.get("id"),
            "canonical_id": item.get("id"),
            "content": str(item.get("content") or ""),
            "raw_content": str(metadata.get("raw_content") or item.get("content") or ""),
            "meaning": str(metadata.get("meaning") or item.get("summary") or ""),
            "is_jargon": 1 if status == "active" else 0,
            "is_complete": 1 if status == "active" else 0,
            "count": int(metadata.get("count") or 1),
            "group_id": str(item.get("session_id") or "GLOBAL"),
            "status": status,
            "scene": str(metadata.get("scene") or ""),
            "examples": list(metadata.get("examples") or []),
            "review_status": review_status,
            "review_reason": str(metadata.get("review_reason") or ""),
            "review_suggestion": str(metadata.get("review_suggestion") or ""),
            "confidence": float(item.get("confidence") or metadata.get("confidence") or 0.0),
            "importance": float(item.get("importance") or 0.0),
            "source": str(item.get("source") or ""),
            "visibility": str(item.get("visibility") or ""),
            "last_access_time": float(item.get("last_access_time") or 0.0),
            "legacy_jargon_id": metadata.get("legacy_jargon_id"),
            "created_at": float(item.get("created_at") or item.get("create_time") or 0.0),
            "updated_at": float(item.get("updated_at") or item.get("update_time") or 0.0),
            "legacy": False,
        }

    async def migration_report(self) -> dict:
        engine = self._memory_engine()
        migration = self.plugin_api.get_migration_service() if self.plugin_api else None
        if migration and hasattr(migration, "latest_report"):
            return {"status": "ok", "runtime_bound": True, "data": await migration.latest_report()}
        store = self.plugin_api.get_v2_store() if self.plugin_api else None
        if store and hasattr(store, "migration_report"):
            return {"status": "ok", "runtime_bound": True, "data": await store.migration_report()}
        return {"status": "ok", "runtime_bound": False, "data": {}}

    async def migration_dry_run(self, sources: list[str] | None = None) -> dict:
        engine = self._memory_engine()
        migration = self.plugin_api.get_migration_service() if self.plugin_api else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.dry_run(import_sources=sources)}

    async def migration_execute(self, sources: list[str] | None = None) -> dict:
        engine = self._memory_engine()
        migration = self.plugin_api.get_migration_service() if self.plugin_api else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.execute(import_sources=sources)}

    async def migration_verify(self) -> dict:
        engine = self._memory_engine()
        migration = self.plugin_api.get_migration_service() if self.plugin_api else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.verify()}

    async def migration_repair(self, report: dict | None = None) -> dict:
        engine = self._memory_engine()
        migration = self.plugin_api.get_migration_service() if self.plugin_api else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.repair(report)}

    async def index_status(self) -> dict:
        engine = self._memory_engine()
        projector = self.plugin_api.get_index_projector() if self.plugin_api else None
        if projector and hasattr(projector, "check_consistency"):
            return {"status": "ok", "runtime_bound": True, "data": await projector.check_consistency()}
        return {"status": "ok", "runtime_bound": False, "data": {}}

    async def rebuild_index(self, session_id: str = "") -> dict:
        engine = self._memory_engine()
        projector = self.plugin_api.get_index_projector() if self.plugin_api else None
        if not projector:
            return {"status": "ok", "changed": False, "runtime_bound": False, "rebuilt": 0}
        rebuilt = await (projector.rebuild_session(session_id) if session_id else projector.rebuild_all())
        return {"status": "ok", "changed": bool(rebuilt), "runtime_bound": True, "rebuilt": int(rebuilt or 0)}

    async def repair_index(self) -> dict:
        engine = self._memory_engine()
        projector = self.plugin_api.get_index_projector() if self.plugin_api else None
        if not projector:
            return {"status": "ok", "changed": False, "runtime_bound": False, "data": {}}
        data = await projector.repair_consistency()
        return {"status": "ok", "changed": any(int(v or 0) for v in data.values()), "runtime_bound": True, "data": data}

    async def run_maintenance(self, policy: dict | None = None) -> dict:
        engine = self._memory_engine()
        maintenance = self.plugin_api.get_maintenance_service() if self.plugin_api else None
        if not maintenance or not hasattr(maintenance, "run_once"):
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await maintenance.run_once(policy=policy or {})}

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

    async def list_jargon(self, *, status: str = "", group_id: str = "", query: str = ""):
        engine = self._memory_engine()
        store = self.plugin_api.get_v2_store() if self.plugin_api else None
        if store and hasattr(store, "list_canonical"):
            result = await store.list_canonical(kind="jargon", status=status, session_id=group_id, limit=200)
            items = [self._canonical_jargon_view(item) for item in result.get("items", [])]
            return self._filter_jargon_rows(items, query=query)
        async with self.db_factory() as db:
            try:
                where = ["kind = 'jargon'"]
                params = []
                if status:
                    where.append("status = ?")
                    params.append(status)
                if group_id:
                    where.append("session_id = ?")
                    params.append(group_id)
                async with db.execute(
                    f"SELECT * FROM canonical_memories WHERE {' AND '.join(where)} ORDER BY update_time DESC LIMIT 200",
                    tuple(params),
                ) as cursor:
                    rows = await cursor.fetchall()
                if rows:
                    items = [self._canonical_jargon_view(self._canonical_row(row)) for row in rows]
                    return self._filter_jargon_rows(items, query=query)
            except Exception:
                pass
            legacy_sql = "SELECT * FROM Jargon"
            legacy_params = []
            if group_id:
                legacy_sql += " WHERE group_id = ?"
                legacy_params.append(group_id)
            legacy_sql += " ORDER BY id DESC"
            async with db.execute(legacy_sql, tuple(legacy_params)) as cursor:
                rows = await cursor.fetchall()
            items = [dict(row) | {"legacy": True, "review_status": "legacy_readonly", "status": "legacy"} for row in rows]
            return self._filter_jargon_rows(items, query=query)

    @staticmethod
    def _filter_jargon_rows(items: list[dict], *, query: str = "") -> list[dict]:
        lowered = str(query or "").strip().lower()
        if not lowered:
            return items
        filtered = []
        for item in items:
            haystack = "\n".join(
                [
                    str(item.get("content") or ""),
                    str(item.get("meaning") or ""),
                    str(item.get("scene") or ""),
                    " ".join(str(example) for example in item.get("examples", []) or []),
                ]
            ).lower()
            if lowered in haystack:
                filtered.append(item)
        return filtered

    async def create_event(self, data: dict) -> dict[str, object]:
        now = time.time()
        event_id = data.get("event_id") or f"plugin_page_{int(now * 1000)}"
        raw_importance = data.get("importance", 0.5)
        try:
            importance = float(0.5 if raw_importance is None else raw_importance)
        except (TypeError, ValueError):
            importance = 0.5
        content = str(data.get("narrative") or data.get("reflection") or "").strip()
        tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        request = MemoryWriteRequest(
            source=str(data.get("source_layer") or "webui_legacy_event"),
            kind="event",
            session_id=str(data.get("session_id") or "PLUGIN_PAGE_SMOKE"),
            content=content,
            summary=content[:240],
            tags=list(tags or []),
            importance=importance,
            confidence=0.75,
            metadata={
                "legacy_event_id": event_id,
                "emotion": data.get("emotion", "Neutral"),
                "reflection": data.get("reflection", ""),
                "legacy_write_redirect": True,
            },
            dedup_key=f"webui_event:{event_id}",
            source_ref=f"webui_legacy_event:{event_id}",
        )
        engine = self._memory_engine()
        writer = self.plugin_api.get_write_service() if self.plugin_api else None
        if writer and hasattr(writer, "write"):
            canonical_id = await writer.write(request)
            return {
                "status": "ok",
                "id": canonical_id,
                "canonical_id": canonical_id,
                "legacy": False,
                "mode": "canonical_redirect",
                "runtime_bound": True,
            }
        async with self.db_factory() as db:
            try:
                canonical_id = f"mem_webui_{int(now * 1000)}"
                await self._insert(
                    db,
                    "canonical_memories",
                    {
                        "id": canonical_id,
                        "session_id": request.session_id,
                        "persona_id": request.persona_id,
                        "source": request.source,
                        "kind": request.kind,
                        "content": request.content,
                        "summary": request.summary,
                        "tags": request.tags,
                        "importance": request.importance,
                        "confidence": request.confidence,
                        "status": "active",
                        "decay_score": 1.0,
                        "create_time": now,
                        "update_time": now,
                        "last_access_time": now,
                        "access_count": 0,
                        "superseded_by": "",
                        "deleted_reason": "",
                        "metadata": request.metadata,
                        "dedup_key": request.dedup_key,
                        "source_ref": request.source_ref,
                        "visibility": request.visibility,
                    },
                )
                await db.commit()
                return {
                    "status": "ok",
                    "id": canonical_id,
                    "canonical_id": canonical_id,
                    "legacy": False,
                    "mode": "canonical_redirect",
                    "runtime_bound": False,
                }
            except Exception:
                return {
                    "status": "readonly",
                    "changed": False,
                    "legacy": True,
                    "message": "Legacy MemoryEvent writes are disabled; canonical runtime is unavailable.",
                }

    async def delete_event(self, event_id: str | int) -> dict[str, object]:
        raw_event_id = str(event_id or "").strip()
        if raw_event_id and not raw_event_id.isdigit():
            result = await self.delete_canonical(raw_event_id)
            result.update({"legacy": False, "mode": "canonical_soft_delete"})
            return result
        async with self.db_factory() as db:
            try:
                async with db.execute("SELECT * FROM MemoryEvent WHERE id = ?", (int(raw_event_id or 0),)) as cursor:
                    row = await cursor.fetchone()
                item = dict(row) if row else {}
            except Exception:
                item = {}
        canonical_id = self._extract_canonical_id(item) if item else ""
        if canonical_id:
            result = await self.delete_canonical(canonical_id)
            result.update({"legacy": True, "mode": "canonical_soft_delete"})
            return result
        return {
            "status": "readonly",
            "changed": False,
            "legacy": True,
            "message": "Legacy MemoryEvent rows are readonly; no canonical mapping was found.",
        }

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
        content = str(data.get("content", "") or "").strip()
        meaning = str(data.get("meaning", "") or "").strip()
        group_id = str(data.get("group_id", "GLOBAL") or "GLOBAL")
        is_jargon = bool(int(data.get("is_jargon", 1) or 0))
        is_complete = bool(int(data.get("is_complete", 1) or 0))
        confidence = float(data.get("confidence", 0.9 if is_jargon and meaning else 0.55) or 0.55)
        requested_status = str(data.get("status", "") or "").strip().lower()
        auto_status = "active" if is_jargon and is_complete and meaning else "review_pending"
        allowed_statuses = {"active", "review_pending", "rejected", "stale"}
        status = requested_status if requested_status in allowed_statuses else auto_status
        review_status = "approved" if status == "active" else ("rejected" if status == "rejected" else "review_pending")
        metadata = {
            "raw_content": str(data.get("raw_content", content) or content),
            "meaning": meaning,
            "count": int(data.get("count", 1) or 1),
            "scene": str(data.get("scene", "") or ""),
            "examples": list(data.get("examples", []) or []),
            "review_status": review_status,
            "review_reason": str(data.get("review_reason", "") or ""),
            "review_suggestion": str(data.get("review_suggestion", "") or ""),
            "confidence": confidence,
        }
        request = MemoryWriteRequest(
            source="webui_jargon",
            kind="jargon",
            session_id=group_id,
            content=content,
            summary=meaning or content,
            importance=float(data.get("importance", 0.7) or 0.7),
            confidence=confidence,
            metadata=metadata,
            dedup_key=f"jargon:{group_id}:{content.lower()}",
            source_ref=f"webui_jargon:{group_id}:{content.lower()}",
            visibility="auto_and_tool" if status == "active" else "maintenance_only",
            status=status,
        )
        engine = self._memory_engine()
        writer = self.plugin_api.get_write_service() if self.plugin_api else None
        if writer and hasattr(writer, "write"):
            memory_id = await writer.write(request)
            return {"status": "ok", "id": memory_id, "canonical_id": memory_id, "legacy": False}
        async with self.db_factory() as db:
            canonical_id = f"mem_jargon_{int(now * 1000)}"
            await self._insert(
                db,
                "canonical_memories",
                {
                    "id": canonical_id,
                    "session_id": request.session_id,
                    "persona_id": request.persona_id,
                    "source": request.source,
                    "kind": request.kind,
                    "content": request.content,
                    "summary": request.summary,
                    "tags": request.tags,
                    "importance": request.importance,
                    "confidence": request.confidence,
                    "status": request.status,
                    "decay_score": 1.0,
                    "create_time": data.get("created_at", now),
                    "update_time": data.get("updated_at", now),
                    "last_access_time": data.get("updated_at", now),
                    "access_count": 0,
                    "superseded_by": "",
                    "deleted_reason": "",
                    "metadata": request.metadata,
                    "dedup_key": request.dedup_key,
                    "source_ref": request.source_ref,
                    "visibility": request.visibility,
                },
            )
            await db.commit()
            return {"status": "ok", "id": canonical_id, "canonical_id": canonical_id, "legacy": False}

    async def update_jargon(self, jargon_id: str, data: dict) -> dict[str, str]:
        now = time.time()
        engine = self._memory_engine()
        store = self.plugin_api.get_v2_store() if self.plugin_api else None
        projector = self.plugin_api.get_index_projector() if self.plugin_api else None
        current = await store.get_canonical(str(jargon_id), include_inactive=True) if store and hasattr(store, "get_canonical") else None
        metadata = dict(getattr(current, "metadata", {}) or {})
        if not metadata:
            async with self.db_factory() as db:
                try:
                    async with db.execute("SELECT * FROM canonical_memories WHERE id = ?", (jargon_id,)) as cursor:
                        row = await cursor.fetchone()
                    if row:
                        current_row = self._canonical_row(row)
                        metadata = dict(current_row.get("metadata") or {})
                        if current is None:
                            current = type(
                                "_CurrentJargon",
                                (),
                                {
                                    "summary": current_row.get("summary", ""),
                                    "metadata": metadata,
                                },
                            )()
                except Exception:
                    pass
        if "meaning" in data:
            metadata["meaning"] = str(data.get("meaning") or "").strip()
        if "raw_content" in data:
            metadata["raw_content"] = str(data.get("raw_content") or "").strip()
        if "scene" in data:
            metadata["scene"] = str(data.get("scene") or "").strip()
        if "examples" in data:
            metadata["examples"] = list(data.get("examples", []) or [])
        if "count" in data:
            metadata["count"] = int(data.get("count", 1) or 1)
        next_status = str(data.get("status") or "").strip()
        if not next_status:
            is_jargon = bool(int(data.get("is_jargon", 1) or 0))
            is_complete = bool(int(data.get("is_complete", 1) or 0))
            next_status = "active" if is_jargon and metadata.get("meaning") and is_complete else "review_pending"
        if next_status == "active":
            metadata["review_status"] = "approved"
        elif next_status == "rejected":
            metadata["review_status"] = "rejected"
        else:
            metadata["review_status"] = str(data.get("review_status") or metadata.get("review_status") or "review_pending")
        if "review_reason" in data:
            metadata["review_reason"] = str(data.get("review_reason") or "")
        if "review_suggestion" in data:
            metadata["review_suggestion"] = str(data.get("review_suggestion") or "")
        summary = str(metadata.get("meaning") or data.get("summary") or getattr(current, "summary", "") or "").strip()
        content = data.get("content")
        visibility = "auto_and_tool" if next_status == "active" else "maintenance_only"
        if store and hasattr(store, "update_memory"):
            changed = await store.update_memory(
                str(jargon_id),
                content=str(content) if content is not None else None,
                summary=summary if summary else None,
                status=next_status,
                metadata=metadata,
                visibility=visibility,
            )
            if changed and projector:
                if next_status == "active":
                    await projector.project(str(jargon_id))
                else:
                    await projector.cleanup_deleted([str(jargon_id)])
            return {"status": "ok"}
        async with self.db_factory() as db:
            await self._update(
                db,
                "canonical_memories",
                "id",
                jargon_id,
                {
                    "content": content,
                    "summary": summary or None,
                    "metadata": metadata,
                    "status": next_status,
                    "visibility": visibility,
                    "update_time": data.get("updated_at", now),
                },
            )
            await db.commit()
        return {"status": "ok"}

    async def delete_jargon(self, jargon_id: str) -> dict[str, str]:
        result = await self.delete_canonical(str(jargon_id))
        result.update({"legacy": False, "mode": "canonical_soft_delete"})
        return result

    async def approve_jargon(self, jargon_id: str) -> dict[str, str]:
        return await self.update_jargon(str(jargon_id), {"status": "active"})

    async def reject_jargon(self, jargon_id: str) -> dict[str, str]:
        return await self.update_jargon(str(jargon_id), {"status": "rejected"})


__all__ = ["MemoryUiService"]
