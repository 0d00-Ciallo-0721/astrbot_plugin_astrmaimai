from __future__ import annotations

import json
import time

from astrmai.memory.contracts.memory_query import MemoryWriteRequest

from ..adapters.plugin_api import PluginApiAdapter


class MemoryUiService:
    def __init__(self, db_factory, plugin_api: PluginApiAdapter | None = None):
        self.db_factory = db_factory
        self.plugin_api = plugin_api

    def _memory_engine(self):
        runtime = self.plugin_api.get_runtime() if self.plugin_api else None
        return getattr(runtime, "memory_engine", None) if runtime else None

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
                items = []
                for row in rows:
                    item = dict(row)
                    item["legacy"] = True
                    item["canonical_id"] = self._extract_canonical_id(item)
                    items.append(item)
                return items

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
        engine = self._memory_engine()
        store = getattr(engine, "v2_store", None) if engine else None
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
        store = getattr(engine, "v2_store", None) if engine else None
        if store and hasattr(store, "get_canonical"):
            item = await store.get_canonical(memory_id, include_inactive=True)
            if not item:
                return {"status": "not_found", "data": None, "runtime_bound": True}
            return {"status": "ok", "data": store._candidate_to_dict(item), "runtime_bound": True}
        async with self.db_factory() as db:
            try:
                async with db.execute("SELECT * FROM canonical_memories WHERE id = ?", (memory_id,)) as cursor:
                    row = await cursor.fetchone()
                return {
                    "status": "ok" if row else "not_found",
                    "data": self._canonical_row(row) if row else None,
                    "runtime_bound": False,
                }
            except Exception:
                return {"status": "not_found", "data": None, "runtime_bound": False}

    async def delete_canonical(self, memory_id: str) -> dict:
        engine = self._memory_engine()
        maintenance = getattr(engine, "maintenance_service", None) if engine else None
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
        maintenance = getattr(engine, "maintenance_service", None) if engine else None
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
        maintenance = getattr(engine, "maintenance_service", None) if engine else None
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
        maintenance = getattr(engine, "maintenance_service", None) if engine else None
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

    async def migration_report(self) -> dict:
        engine = self._memory_engine()
        migration = getattr(engine, "migration_service", None) if engine else None
        if migration and hasattr(migration, "latest_report"):
            return {"status": "ok", "runtime_bound": True, "data": await migration.latest_report()}
        store = getattr(engine, "v2_store", None) if engine else None
        if store and hasattr(store, "migration_report"):
            return {"status": "ok", "runtime_bound": True, "data": await store.migration_report()}
        return {"status": "ok", "runtime_bound": False, "data": {}}

    async def migration_dry_run(self, sources: list[str] | None = None) -> dict:
        engine = self._memory_engine()
        migration = getattr(engine, "migration_service", None) if engine else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.dry_run(import_sources=sources)}

    async def migration_execute(self, sources: list[str] | None = None) -> dict:
        engine = self._memory_engine()
        migration = getattr(engine, "migration_service", None) if engine else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.execute(import_sources=sources)}

    async def migration_verify(self) -> dict:
        engine = self._memory_engine()
        migration = getattr(engine, "migration_service", None) if engine else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.verify()}

    async def migration_repair(self, report: dict | None = None) -> dict:
        engine = self._memory_engine()
        migration = getattr(engine, "migration_service", None) if engine else None
        if not migration:
            return {"status": "ok", "runtime_bound": False, "data": {}}
        return {"status": "ok", "runtime_bound": True, "data": await migration.repair(report)}

    async def index_status(self) -> dict:
        engine = self._memory_engine()
        projector = getattr(engine, "index_projector", None) if engine else None
        if projector and hasattr(projector, "check_consistency"):
            return {"status": "ok", "runtime_bound": True, "data": await projector.check_consistency()}
        return {"status": "ok", "runtime_bound": False, "data": {}}

    async def rebuild_index(self, session_id: str = "") -> dict:
        engine = self._memory_engine()
        projector = getattr(engine, "index_projector", None) if engine else None
        if not projector:
            return {"status": "ok", "changed": False, "runtime_bound": False, "rebuilt": 0}
        rebuilt = await (projector.rebuild_session(session_id) if session_id else projector.rebuild_all())
        return {"status": "ok", "changed": bool(rebuilt), "runtime_bound": True, "rebuilt": int(rebuilt or 0)}

    async def repair_index(self) -> dict:
        engine = self._memory_engine()
        projector = getattr(engine, "index_projector", None) if engine else None
        if not projector:
            return {"status": "ok", "changed": False, "runtime_bound": False, "data": {}}
        data = await projector.repair_consistency()
        return {"status": "ok", "changed": any(int(v or 0) for v in data.values()), "runtime_bound": True, "data": data}

    async def run_maintenance(self, policy: dict | None = None) -> dict:
        engine = self._memory_engine()
        maintenance = getattr(engine, "maintenance_service", None) if engine else None
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

    async def list_jargon(self):
        async with self.db_factory() as db:
            async with db.execute("SELECT * FROM Jargon ORDER BY id DESC") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def create_event(self, data: dict) -> dict[str, object]:
        now = time.time()
        event_id = data.get("event_id") or f"plugin_page_{int(now * 1000)}"
        importance = data.get("importance", 0.5)
        content = str(data.get("narrative") or data.get("reflection") or "").strip()
        tags = data.get("tags", [])
        if isinstance(tags, str):
            tags = [item.strip() for item in tags.split(",") if item.strip()]
        request = MemoryWriteRequest(
            source=str(data.get("source_layer") or "webui_legacy_event"),
            kind=str(data.get("memory_kind") or "event"),
            session_id=str(data.get("session_id") or "PLUGIN_PAGE_SMOKE"),
            content=content,
            summary=content[:240],
            tags=list(tags or []),
            importance=float(importance or 0.5),
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
        writer = getattr(engine, "write_service", None) if engine else None
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

    async def delete_event(self, event_id: int) -> dict[str, object]:
        async with self.db_factory() as db:
            try:
                async with db.execute("SELECT * FROM MemoryEvent WHERE id = ?", (event_id,)) as cursor:
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
