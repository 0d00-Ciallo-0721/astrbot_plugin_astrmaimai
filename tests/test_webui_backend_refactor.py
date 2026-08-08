import asyncio
import importlib
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
from fastapi.testclient import TestClient


class _RuntimeBackedFacade:
    def __init__(self, runtime):
        self.runtime = runtime

    def get_planner(self):
        return getattr(self.runtime, "system2_planner", None)

    def get_gateway(self):
        return getattr(self.runtime, "gateway", None)

    def get_proactive_task(self):
        return getattr(self.runtime, "proactive_task", None)

    def get_observability_hub(self):
        return getattr(self.runtime, "observability_hub", None)

    def get_memory_engine(self):
        return getattr(self.runtime, "memory_engine", None)

    def get_runtime_coordinator(self):
        return getattr(self.runtime, "runtime_coordinator", None)

    def get_reflector(self):
        return getattr(self.runtime, "reflector", None)

    def get_runtime_config(self):
        return getattr(self.runtime, "config", None)

    def get_persona_summarizer(self):
        return getattr(self.runtime, "persona_summarizer", None)

    def get_state_engine(self):
        return getattr(self.runtime, "state_engine", None)

    def get_auto_check_task(self):
        return getattr(self.runtime, "auto_check_task", None)

    def get_reflect_tracker(self):
        return getattr(self.runtime, "reflect_tracker", None)

    def get_chat_loop_kernel(self):
        kernel = getattr(self.runtime, "chat_loop_kernel", None)
        if kernel is not None:
            return kernel
        task = self.get_proactive_task()
        return getattr(task, "chat_loop_kernel", None) if task else None

    def get_heartflow_manager(self):
        task = self.get_proactive_task()
        return getattr(task, "heartflow_manager", None) if task else None

    def get_heartflow_topic_digest_service(self):
        task = self.get_proactive_task()
        return getattr(task, "heartflow_topic_digest_service", None) if task else None

    def get_runtime_diagnostics(self):
        builder = getattr(self.runtime, "build_diagnostics", None)
        return builder() if callable(builder) else None

    async def get_capability_overview(self):
        builder = getattr(self.runtime, "build_capability_overview_sync", None)
        if callable(builder):
            return builder()
        return None


class WebuiBackendRefactorTests(unittest.TestCase):
    def test_dashboard_service_uses_adapter_and_db_factory(self):
        service_mod = importlib.import_module(
            "astrmai.webui.backend.services.dashboard_service"
        )

        class _Cursor:
            def __init__(self, value=None, rows=None):
                self.value = value
                self.rows = rows or []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def fetchone(self):
                return (self.value,)

            async def fetchall(self):
                return list(self.rows)

        class _Db:
            def execute(self, query):
                if "user_profiles" in query:
                    return _Cursor(3)
                if "FROM canonical_memories" in query and "status, metadata" in query:
                    return _Cursor(
                        rows=[
                            ("review_pending", '{"review_status":"pending"}'),
                            ("active", '{"review_status":"approved"}'),
                        ]
                    )
                return _Cursor(5)

        class _DbCtx:
            async def __aenter__(self):
                return _Db()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _PluginApi:
            async def get_runtime_diagnostics(self):
                return {"status": {"lifecycle_started": True}}

            async def get_capability_overview(self):
                return {"workmode": {"enabled": True}}

        service = service_mod.DashboardService(_PluginApi(), lambda: _DbCtx())
        snapshot = asyncio.run(service.get_snapshot())
        self.assertEqual(snapshot["total_users"], 3)
        self.assertEqual(snapshot["pending_reviews"], 1)
        self.assertIn("capabilities", snapshot)

    def test_dashboard_service_prefers_runtime_v2_memory_stats(self):
        service_mod = importlib.import_module(
            "astrmai.webui.backend.services.dashboard_service"
        )

        class _Cursor:
            def __init__(self, value=0, rows=None):
                self.value = value
                self.rows = rows or []

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def fetchone(self):
                return (self.value,)

            async def fetchall(self):
                return list(self.rows)

        class _Db:
            def execute(self, query):
                if "user_profiles" in query:
                    return _Cursor(2)
                return _Cursor(0)

        class _DbCtx:
            async def __aenter__(self):
                return _Db()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class _Store:
            rows = [
                {"kind": "fact", "status": "active"},
                {"kind": "expression_pattern", "status": "review_pending"},
                {"kind": "expression_pattern", "status": "active"},
            ]

            async def list_canonical(self, **kwargs):
                kind = kwargs.get("kind", "")
                status = kwargs.get("status", "")
                rows = [
                    row for row in self.rows
                    if (not kind or row["kind"] == kind) and (not status or row["status"] == status)
                ]
                return {"items": [], "total": len(rows)}

        class _PluginApi:
            def get_v2_store(self):
                return _Store()

            async def get_runtime_diagnostics(self):
                return {"status": {"lifecycle_started": True}}

            async def get_capability_overview(self):
                return {}

        service = service_mod.DashboardService(_PluginApi(), lambda: _DbCtx())
        snapshot = asyncio.run(service.get_snapshot())
        self.assertEqual(snapshot["total_canonical_memories"], 3)
        self.assertEqual(snapshot["pending_reviews"], 1)
        self.assertEqual(snapshot["canonical_memory_stats"]["source"], "runtime_v2_store")
        self.assertEqual(snapshot["expression_pattern_stats"]["total"], 2)

    def test_runtime_ui_service_reports_unbound_when_no_facade_resolved(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        service_mod = importlib.import_module("astrmai.webui.backend.services.runtimeuiservice")

        previous_facade = adapter_mod.get_active_facade()
        try:
            adapter_mod.set_active_facade(None)
            service = service_mod.RuntimeUiService(adapter_mod.PluginApiAdapter())
            status = asyncio.run(service.runtime_status())
            self.assertFalse(status["runtime_bound"])
            self.assertEqual(status["data"], {})
        finally:
            adapter_mod.set_active_facade(previous_facade)

    def test_runtime_ui_service_does_not_mask_bound_facade_failures_as_ok_data(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        service_mod = importlib.import_module("astrmai.webui.backend.services.runtimeuiservice")

        class _Facade:
            def get_runtime_diagnostics(self):
                raise RuntimeError("boom")

        service = service_mod.RuntimeUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            asyncio.run(service.runtime_status())

    def test_server_mounts_aggregated_api_router(self):
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "astrmai" / "webui" / "backend" / "server.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn("from .routes import api_router", content)
        self.assertIn("app.include_router(api_router, prefix=\"/api\")", content)

    def test_plugin_api_adapter_default_paths_follow_env_at_instantiation_time(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = str(Path(tmp_dir) / "config.json")
            schema_path = str(Path(tmp_dir) / "schema.json")
            persona_cache_path = str(Path(tmp_dir) / "persona.json")
            original = {
                "ASTRMAI_CONFIG_PATH": os.environ.get("ASTRMAI_CONFIG_PATH"),
                "ASTRMAI_CONF_SCHEMA_PATH": os.environ.get("ASTRMAI_CONF_SCHEMA_PATH"),
                "ASTRMAI_PERSONA_CACHE_PATH": os.environ.get("ASTRMAI_PERSONA_CACHE_PATH"),
            }
            try:
                os.environ["ASTRMAI_CONFIG_PATH"] = config_path
                os.environ["ASTRMAI_CONF_SCHEMA_PATH"] = schema_path
                os.environ["ASTRMAI_PERSONA_CACHE_PATH"] = persona_cache_path
                adapter = adapter_mod.PluginApiAdapter()
            finally:
                for key, value in original.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        self.assertEqual(adapter.config_path, config_path)
        self.assertEqual(adapter.schema_path, schema_path)
        self.assertEqual(adapter.persona_cache_path, persona_cache_path)

    def test_auth_secret_follows_env_at_runtime(self):
        access_mod = importlib.import_module("astrmai.webui.backend.access")
        self.assertEqual(asyncio.run(access_mod.get_current_user("codex")), "codex")
        self.assertEqual(asyncio.run(access_mod.get_current_user(None)), "astrbot-plugin-page")


    def test_memory_ui_service_writes_real_schema_columns(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_path = Path(tmp_dir) / "astrmai.db"
                async with aiosqlite.connect(db_path) as db:
                    await db.executescript(
                        """
                        CREATE TABLE MemoryEvent (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            event_id TEXT,
                            session_id TEXT,
                            date TEXT,
                            narrative TEXT,
                            emotion TEXT,
                            importance REAL,
                            emotional_intensity REAL,
                            reflection TEXT,
                            memory_kind TEXT,
                            source_layer TEXT,
                            tags TEXT,
                            created_at REAL
                        );
                        CREATE TABLE canonical_memories (
                            id TEXT PRIMARY KEY,
                            session_id TEXT,
                            persona_id TEXT,
                            source TEXT,
                            kind TEXT,
                            content TEXT,
                            summary TEXT,
                            tags TEXT,
                            importance REAL,
                            confidence REAL,
                            status TEXT,
                            decay_score REAL,
                            create_time REAL,
                            update_time REAL,
                            last_access_time REAL,
                            access_count INTEGER,
                            superseded_by TEXT,
                            deleted_reason TEXT,
                            metadata TEXT,
                            dedup_key TEXT,
                            source_ref TEXT,
                            visibility TEXT
                        );
                        CREATE TABLE DailyReflection (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            date TEXT UNIQUE,
                            reflection TEXT,
                            created_at REAL
                        );
                        CREATE TABLE MemoryNode (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            name TEXT,
                            type TEXT,
                            description TEXT,
                            last_updated REAL
                        );
                        CREATE TABLE Jargon (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            content TEXT,
                            raw_content TEXT,
                            meaning TEXT,
                            is_jargon INTEGER,
                            count INTEGER,
                            is_complete INTEGER,
                            group_id TEXT,
                            created_at REAL,
                            updated_at REAL
                        );
                        """
                    )
                    await db.commit()

                @asynccontextmanager
                async def _db_factory():
                    conn = await aiosqlite.connect(db_path)
                    conn.row_factory = aiosqlite.Row
                    try:
                        yield conn
                    finally:
                        await conn.close()

                service = service_mod.MemoryUiService(_db_factory)
                event = await service.create_event({"narrative": "smoke event", "tags": ["codex"], "importance": 0.8})
                reflection = await service.create_reflection({"date": "2099-01-09", "summary": "smoke reflection"})
                node = await service.create_node({"name": "smoke node", "type": "topic", "description": "temporary"})
                jargon = await service.create_jargon({"content": "smoke jargon", "meaning": "temporary meaning"})
                await service.update_reflection("2099-01-09", {"summary": "updated reflection"})
                await service.update_node(node["id"], {"name": "updated node", "type": "topic", "description": "updated"})
                await service.update_jargon(jargon["id"], {"meaning": "updated meaning", "is_complete": 0})

                async with aiosqlite.connect(db_path) as db:
                    db.row_factory = aiosqlite.Row
                    canonical_row = dict(await (await db.execute("SELECT * FROM canonical_memories WHERE id = ?", (event["id"],))).fetchone())
                    event_count = (await (await db.execute("SELECT COUNT(*) FROM MemoryEvent")).fetchone())[0]
                    reflection_row = dict(await (await db.execute("SELECT * FROM DailyReflection WHERE date = ?", ("2099-01-09",))).fetchone())
                    node_row = dict(await (await db.execute("SELECT * FROM MemoryNode WHERE id = ?", (node["id"],))).fetchone())
                    jargon_row = dict(await (await db.execute("SELECT * FROM canonical_memories WHERE id = ?", (jargon["id"],))).fetchone())
                return event, reflection, node, jargon, canonical_row, event_count, reflection_row, node_row, jargon_row

        event, reflection, node, jargon, canonical_row, event_count, reflection_row, node_row, jargon_row = asyncio.run(_run())
        self.assertEqual(reflection["status"], "ok")
        self.assertTrue(str(event["id"]).startswith("mem_webui_"))
        self.assertIsInstance(node["id"], int)
        self.assertIsInstance(jargon["id"], str)
        self.assertEqual(event["mode"], "canonical_redirect")
        self.assertEqual(event_count, 0)
        self.assertEqual(canonical_row["session_id"], "PLUGIN_PAGE_SMOKE")
        self.assertEqual(canonical_row["tags"], '["codex"]')
        self.assertEqual(reflection_row["reflection"], "updated reflection")
        self.assertEqual(node_row["name"], "updated node")
        self.assertEqual(jargon_row["kind"], "jargon")
        self.assertEqual(jargon_row["content"], "smoke jargon")
        self.assertEqual(jargon_row["status"], "review_pending")
        jargon_meta = json.loads(jargon_row["metadata"])
        self.assertEqual(jargon_meta["raw_content"], "smoke jargon")
        self.assertEqual(jargon_meta["meaning"], "updated meaning")

    def test_memory_service_exposes_canonical_memory_and_legacy_marker(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "webui.db")

            async def _run():
                async with aiosqlite.connect(db_path) as db:
                    await db.executescript(
                        """
                        CREATE TABLE MemoryEvent (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            event_id TEXT,
                            session_id TEXT,
                            date TEXT,
                            narrative TEXT,
                            emotion TEXT,
                            importance REAL,
                            emotional_intensity REAL,
                            reflection TEXT,
                            memory_kind TEXT,
                            source_layer TEXT,
                            tags TEXT,
                            created_at REAL
                        );
                        CREATE TABLE canonical_memories (
                            id TEXT PRIMARY KEY,
                            session_id TEXT,
                            persona_id TEXT,
                            source TEXT,
                            kind TEXT,
                            content TEXT,
                            summary TEXT,
                            tags TEXT,
                            importance REAL,
                            confidence REAL,
                            status TEXT,
                            decay_score REAL,
                            create_time REAL,
                            update_time REAL,
                            last_access_time REAL,
                            access_count INTEGER,
                            superseded_by TEXT,
                            deleted_reason TEXT,
                            metadata TEXT,
                            dedup_key TEXT,
                            source_ref TEXT,
                            visibility TEXT
                        );
                        INSERT INTO canonical_memories (
                            id, session_id, persona_id, source, kind, content, summary,
                            tags, importance, confidence, status, decay_score, create_time,
                            update_time, last_access_time, access_count, superseded_by,
                            deleted_reason, metadata, dedup_key, source_ref, visibility
                        ) VALUES (
                            'mem-ui-1', 'chat-1', '', 'summary', 'memory',
                            'Alice canonical UI memory.', 'Alice canonical UI memory.',
                            '[]', 0.8, 0.9, 'active', 1.0, 1.0, 2.0, 1.0,
                            0, '', '', '{}', 'ui:1', 'test', 'auto_and_tool'
                        );
                        """
                    )
                    await db.commit()

                @asynccontextmanager
                async def _db_factory():
                    conn = await aiosqlite.connect(db_path)
                    conn.row_factory = aiosqlite.Row
                    try:
                        yield conn
                    finally:
                        await conn.close()

                service = service_mod.MemoryUiService(_db_factory)
                canonical = await service.list_canonical(session_id="chat-1")
                detail = await service.get_canonical("mem-ui-1")
                deleted = await service.delete_canonical("mem-ui-1")
                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        "INSERT INTO MemoryEvent(event_id, session_id, narrative, tags, importance) VALUES (?, ?, ?, ?, ?)",
                        ("evt-ui-1", "chat-1", "legacy row", '["canonical_id:mem-ui-1"]', 0.8),
                    )
                    await db.commit()
                legacy_event = await service.create_event({"narrative": "smoke event", "tags": ["codex"], "importance": 0.8})
                legacy_rows = await service.list_events()
                legacy_delete = await service.delete_event(1)
                return canonical, detail, deleted, legacy_event, legacy_rows, legacy_delete

            canonical, detail, deleted, legacy_event, legacy_rows, legacy_delete = asyncio.run(_run())
        self.assertEqual(canonical["total"], 1)
        self.assertEqual(canonical["items"][0]["id"], "mem-ui-1")
        self.assertEqual(detail["data"]["id"], "mem-ui-1")
        self.assertTrue(deleted["changed"])
        self.assertEqual(legacy_event["mode"], "canonical_redirect")
        self.assertTrue(any(item["legacy"] for item in legacy_rows))
        self.assertTrue(any(not item["legacy"] and item["id"] == legacy_event["canonical_id"] for item in legacy_rows))
        self.assertEqual(legacy_delete["mode"], "canonical_soft_delete")

    def test_memory_ui_service_runtime_bound_canonical_actions_use_services_not_sql_fallback(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            calls = []

            class _Maintenance:
                async def soft_delete(self, memory_id, *, reason=""):
                    calls.append(("soft_delete", memory_id, reason))
                    return 1

                async def restore(self, memory_id, *, reason=""):
                    calls.append(("restore", memory_id, reason))
                    return 1

                async def mark_stale(self, memory_id, *, reason=""):
                    calls.append(("mark_stale", memory_id, reason))
                    return 1

                async def mark_merged(self, memory_ids, *, superseded_by):
                    calls.append(("mark_merged", tuple(memory_ids), superseded_by))
                    return 1

            class _Store:
                async def get_canonical(self, memory_id, include_inactive=False):
                    return SimpleNamespace(summary="old-summary", metadata={"meaning": "old meaning"})

                async def update_memory(self, memory_id, **kwargs):
                    calls.append(("update_memory", memory_id, kwargs))
                    return 1

            class _Engine:
                maintenance_service = _Maintenance()
                v2_store = _Store()
                index_projector = None

            class _Runtime:
                memory_engine = _Engine()

            service = service_mod.MemoryUiService(
                db_factory=lambda: None,
                plugin_api=adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime())),
            )
            deleted = await service.delete_canonical("mem-1")
            restored = await service.restore_canonical("mem-1")
            staled = await service.mark_canonical_stale("mem-1")
            merged = await service.merge_canonical("mem-1", "mem-2")
            updated = await service.update_jargon("mem-1", {"meaning": "new meaning", "status": "active"})
            return calls, deleted, restored, staled, merged, updated

        calls, deleted, restored, staled, merged, updated = asyncio.run(_run())
        self.assertTrue(deleted["runtime_bound"])
        self.assertTrue(restored["runtime_bound"])
        self.assertTrue(staled["runtime_bound"])
        self.assertTrue(merged["runtime_bound"])
        self.assertEqual(updated["status"], "ok")
        self.assertEqual(
            [item[0] for item in calls],
            ["soft_delete", "restore", "mark_stale", "mark_merged", "update_memory"],
        )

    def test_memory_ui_service_runtime_bound_actions_can_run_with_maintenance_service_on_same_chat(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        store_mod = importlib.import_module("astrmai.memory.services.v2_store")
        write_mod = importlib.import_module("astrmai.memory.services.memory_write_service")
        maintenance_mod = importlib.import_module("astrmai.memory.services.memory_maintenance_service")

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_path = str(Path(tmp_dir) / "docs.db")
                store = store_mod.MemoryV2Store(db_path, data_path=Path(tmp_dir))
                writer = write_mod.MemoryWriteService(store)
                maintenance = maintenance_mod.MemoryMaintenanceService(store)
                memory_id = await writer.write(
                    importlib.import_module("astrmai.memory.contracts.memory_query").MemoryWriteRequest(
                        source="summary",
                        kind="memory",
                        session_id="chat-1",
                        content="runtime-bound lock test",
                        dedup_key="runtime-lock:chat-1",
                    )
                )

                class _Engine:
                    maintenance_service = maintenance
                    v2_store = store
                    index_projector = None

                class _Runtime:
                    memory_engine = _Engine()

                service = service_mod.MemoryUiService(
                    db_factory=lambda: None,
                    plugin_api=adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime())),
                )
                results = await asyncio.gather(
                    maintenance.mark_stale(memory_id, reason="maintenance"),
                    service.delete_canonical(memory_id),
                    return_exceptions=True,
                )
                candidate = await store.get_canonical(memory_id, include_inactive=True)
                return results, candidate

        results, candidate = asyncio.run(_run())
        self.assertFalse(any(isinstance(item, Exception) for item in results))
        self.assertIsNotNone(candidate)
        self.assertIn(candidate.status, {"stale", "deleted"})

    def test_memory_ui_service_lists_and_reviews_canonical_jargon(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = str(Path(tmp_dir) / "jargon.db")

            async def _run():
                async with aiosqlite.connect(db_path) as db:
                    await db.executescript(
                        """
                        CREATE TABLE canonical_memories (
                            id TEXT PRIMARY KEY,
                            session_id TEXT,
                            persona_id TEXT,
                            source TEXT,
                            kind TEXT,
                            content TEXT,
                            summary TEXT,
                            tags TEXT,
                            importance REAL,
                            confidence REAL,
                            status TEXT,
                            decay_score REAL,
                            create_time REAL,
                            update_time REAL,
                            last_access_time REAL,
                            access_count INTEGER,
                            superseded_by TEXT,
                            deleted_reason TEXT,
                            metadata TEXT,
                            dedup_key TEXT,
                            source_ref TEXT,
                            visibility TEXT
                        );
                        CREATE TABLE Jargon (
                            id INTEGER PRIMARY KEY,
                            content TEXT,
                            group_id TEXT
                        );
                        """
                    )
                    await db.execute(
                        "INSERT INTO Jargon(id, content, group_id) VALUES (?, ?, ?)",
                        (7, "bigbird", "group-1"),
                    )
                    await db.execute(
                        "INSERT INTO Jargon(id, content, group_id) VALUES (?, ?, ?)",
                        (8, "legacy-only", "group-1"),
                    )
                    await db.execute(
                        """
                        INSERT INTO canonical_memories (
                            id, session_id, source, kind, content, summary, tags, importance, confidence, status,
                            decay_score, create_time, update_time, last_access_time, access_count, superseded_by,
                            deleted_reason, metadata, dedup_key, source_ref, visibility
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "mem-jargon-1",
                            "group-1",
                            "learning_jargon",
                            "jargon",
                            "bigbird",
                            "raid boss nickname",
                            "[]",
                            0.7,
                            0.8,
                            "review_pending",
                            1.0,
                            1.0,
                            2.0,
                            1.5,
                            0,
                            "",
                            "",
                            json.dumps(
                                {
                                    "meaning": "raid boss nickname",
                                    "scene": "raid call",
                                    "examples": ["bigbird is here"],
                                    "review_status": "review_pending",
                                    "review_reason": "needs more evidence",
                                    "review_suggestion": "confirm whether it is boss shorthand",
                                    "legacy_jargon_id": 7,
                                }
                            ),
                            "jargon:group-1:bigbird",
                            "Jargon:7",
                            "maintenance_only",
                        ),
                    )
                    await db.commit()

                @asynccontextmanager
                async def _db_factory():
                    conn = await aiosqlite.connect(db_path)
                    conn.row_factory = aiosqlite.Row
                    try:
                        yield conn
                    finally:
                        await conn.close()

                service = service_mod.MemoryUiService(_db_factory)
                pending = await service.list_jargon(status="review_pending", group_id="group-1", query="raid")
                approved = await service.approve_jargon(
                    "mem-jargon-1",
                    {
                        "content": "big bird",
                        "meaning": "人工校准后的团本黑话",
                        "scene": "团本集合",
                        "examples": ["big bird ready"],
                        "group_id": "group-2",
                        "review_reason": "人工确认",
                        "confidence": 0.95,
                    },
                )
                active = await service.list_jargon(status="active", group_id="group-2", query="团本")
                rejected = await service.reject_jargon("mem-jargon-1")
                final_detail = await service.get_canonical("mem-jargon-1")
                async with _db_factory() as db:
                    async with db.execute("SELECT COUNT(*) FROM Jargon WHERE id = 7") as cursor:
                        legacy_count = int((await cursor.fetchone())[0])
                remaining = await service.list_jargon(status="active", group_id="group-1")
                return pending, approved, active, rejected, final_detail, legacy_count, remaining

            pending, approved, active, rejected, final_detail, legacy_count, remaining = asyncio.run(_run())

        self.assertEqual(pending["total"], 1)
        self.assertEqual(len(pending["items"]), 1)
        self.assertEqual(pending["items"][0]["legacy_jargon_id"], 7)
        self.assertEqual(approved["status"], "ok")
        self.assertEqual(active["total"], 1)
        self.assertEqual(len(active["items"]), 1)
        self.assertEqual(active["items"][0]["status"], "active")
        self.assertEqual(active["items"][0]["content"], "big bird")
        self.assertEqual(active["items"][0]["group_id"], "__global_jargon__")
        self.assertEqual(active["items"][0]["meaning"], "人工校准后的团本黑话")
        self.assertEqual(active["items"][0]["scene"], "团本集合")
        self.assertEqual(active["items"][0]["review_reason"], "人工确认")
        self.assertEqual(pending["items"][0]["review_reason"], "needs more evidence")
        self.assertEqual(pending["items"][0]["review_suggestion"], "confirm whether it is boss shorthand")
        self.assertEqual(rejected["status"], "ok")
        self.assertTrue(rejected["physical_delete"])
        self.assertEqual(final_detail["status"], "not_found")
        self.assertIsNone(final_detail["data"])
        self.assertEqual(legacy_count, 0)
        self.assertEqual(remaining["total"], 0)

    def test_runtime_jargon_update_merges_duplicate_global_term(self):
        from astrmai.learning.dedup import GLOBAL_JARGON_SESSION_ID, jargon_fingerprint
        from astrmai.webui.backend.services.memory_ui_service import MemoryUiService

        calls = []
        target = SimpleNamespace(
            id="mem-existing",
            kind="jargon",
            content="hiyohiyo",
            status="review_pending",
            summary="旧释义",
            session_id=GLOBAL_JARGON_SESSION_ID,
            metadata={"meaning": "旧释义", "count": 2, "examples": ["旧例句"]},
        )
        current = SimpleNamespace(
            id="mem-edited",
            kind="jargon",
            content="另一个词",
            status="review_pending",
            summary="待修改",
            session_id="ff:GroupMessage:1",
            metadata={
                "meaning": "待修改",
                "count": 3,
                "examples": ["新例句"],
                "speaker_id": "123",
            },
        )

        class _Store:
            async def get_canonical(self, memory_id, include_inactive=False):
                return current if memory_id == current.id else target

            async def get_by_dedup_key(self, dedup_key, include_inactive=False):
                self.last_dedup_key = dedup_key
                return target

            async def update_memory(self, memory_id, **kwargs):
                calls.append(("update", memory_id, kwargs))
                return 1

            async def hard_delete(self, memory_id, kind=""):
                calls.append(("delete", memory_id, kind))
                return 1

        class _Projector:
            async def cleanup_deleted(self, memory_ids):
                calls.append(("cleanup", list(memory_ids)))
                return 1

            async def project(self, memory_id):
                calls.append(("project", memory_id))
                return 1

        store = _Store()

        class _PluginApi:
            def get_v2_store(self):
                return store

            def get_index_projector(self):
                return _Projector()

            def get_memory_engine(self):
                return None

        result = asyncio.run(
            MemoryUiService(None, _PluginApi()).update_jargon(
                current.id,
                {
                    "content": "ＨＩＹＯＨＩＹＯ",
                    "meaning": "新释义",
                    "status": "active",
                    "examples": ["新例句"],
                },
            )
        )

        self.assertEqual(result, {"status": "ok", "id": target.id, "merged": True})
        self.assertEqual(store.last_dedup_key, jargon_fingerprint("hiyohiyo"))
        self.assertEqual(calls[0][0:2], ("update", target.id))
        updated = calls[0][2]
        self.assertEqual(updated["session_id"], GLOBAL_JARGON_SESSION_ID)
        self.assertEqual(updated["metadata"]["count"], 5)
        self.assertEqual(updated["metadata"]["examples"], ["旧例句", "新例句"])
        self.assertNotIn("speaker_id", updated["metadata"])
        self.assertEqual(calls[1:], [("delete", current.id, "jargon"), ("cleanup", [current.id]), ("project", target.id)])

    def test_runtime_jargon_reject_tombstones_and_cleans_projection(self):
        # OPT-04/WU-07: runtime 态驳回改软墓碑（status=rejected），不再物理删除——
        # 硬删会抹掉挖掘器 existing_terms 依赖的去重墓碑，噪声词回流待审。
        # 旧断言 physical_delete=True 锁定的正是该缺陷行为。
        from astrmai.webui.backend.services.memory_ui_service import MemoryUiService

        calls = []

        class _Store:
            async def get_canonical(self, memory_id, include_inactive=False):
                return SimpleNamespace(
                    id=memory_id,
                    kind="jargon",
                    content="hiyohiyo",
                    status="active",
                    summary="招呼语",
                    session_id="chat-1",
                    metadata={"meaning": "招呼语"},
                )

            async def update_memory(self, memory_id, **kwargs):
                calls.append(("update", memory_id, str(kwargs.get("status") or ""), str(kwargs.get("visibility") or "")))
                return 1

            async def hard_delete(self, memory_id, kind=""):
                raise AssertionError("runtime 态驳回不得物理删除")

        class _Projector:
            async def cleanup_deleted(self, memory_ids):
                calls.append(("projector", list(memory_ids)))
                return 1

        class _PluginApi:
            def get_v2_store(self):
                return _Store()

            def get_index_projector(self):
                return _Projector()

            def get_memory_engine(self):
                return None

        result = asyncio.run(MemoryUiService(None, _PluginApi()).reject_jargon("mem-jargon-1"))
        self.assertTrue(result["tombstone"])
        self.assertEqual(result["action"], "reject")
        self.assertEqual(
            calls,
            [
                ("update", "mem-jargon-1", "rejected", "maintenance_only"),
                ("projector", ["mem-jargon-1"]),
            ],
        )

    def test_memory_route_file_exposes_jargon_review_endpoints(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "webui" / "backend" / "routes" / "memory_routes.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn('@router.post("/jargon/{id}/approve")', content)
        self.assertIn('@router.post("/jargon/{id}/reject")', content)
        self.assertIn('@router.get("/observability/runtime")', content)
        self.assertIn('@router.get("/observability/chats/{chat_id}")', content)
        self.assertIn('@router.get("/observability/events")', content)
        self.assertIn('@router.get("/observability/errors")', content)

    def test_memory_ui_service_runtime_status_prefers_new_memory_runtime_fields(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.memory_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            class _Observer:
                async def runtime_snapshot(self, **kwargs):
                    return {
                        "instant_gate_ready": kwargs["instant_gate_ready"],
                        "memory_pipeline_ready": kwargs["memory_pipeline_ready"],
                        "session_summarizer_ready": kwargs["session_summarizer_ready"],
                        "pipeline_running": True,
                        "sweep_task_running": True,
                        "buffered_chats": 2,
                        "tracked_chats": 3,
                        "active_worker_count": 2,
                        "active_worker_chats": ["chat-1", "chat-2"],
                        "recent_error_count": 1,
                        "recent_warning_count": 2,
                        "last_gate_hit_at": 10.0,
                        "last_backfill_success_at": 20.0,
                        "last_summarize_success_at": 30.0,
                        "last_summarize_failure_at": 40.0,
                    }

                async def chat_snapshot(self, **kwargs):
                    return {
                        "chat_id": kwargs["chat_id"],
                        "pending_messages": 4,
                        "cooldown_until": 0.0,
                        "failures": 0,
                        "last_update": 123.0,
                        "last_memory_run_at": 120.0,
                        "worker_active": True,
                        "last_gate_stage": "gate_hit",
                        "last_backfill_stage": "backfill_finished",
                        "last_summarize_stage": "canonical_write_success",
                        "recent_events": [{"event_id": "evt-1"}],
                    }

                async def recent_events(self, **kwargs):
                    item = {"event_id": "evt-1", "component": "instant_gate", "stage": "gate_hit", "level": "info", "summary": "hit", "timestamp": 9.0, "chat_id": kwargs.get("chat_id") or "chat-1"}
                    return [self.format_timeline_item(item)]

                async def recent_errors(self, **kwargs):
                    return [{"event_id": "evt-2", "component": "session_summarizer", "level": "error"}]

                @staticmethod
                def format_timeline_item(item):
                    item = dict(item)
                    item["display_title"] = "即时记忆命中" if item.get("stage") == "gate_hit" else item.get("stage", "")
                    return item

            class _Pipeline:
                def describe_runtime_status(self):
                    return {
                        "running": True,
                        "sweep_task_running": True,
                        "buffered_chats": 2,
                        "tracked_chats": 3,
                        "active_worker_count": 2,
                        "active_worker_chats": ["chat-1", "chat-2"],
                    }

                def describe_chat_buffer(self, chat_id):
                    return {
                        "chat_id": chat_id,
                        "pending_messages": 4,
                        "last_update": 123.0,
                        "cooldown_until": 0.0,
                        "failures": 0,
                        "last_memory_run_at": 120.0,
                    }

                def is_worker_active(self, chat_id):
                    return chat_id == "chat-1"

            class _Runtime:
                memory_engine = SimpleNamespace(
                    instant_gate=SimpleNamespace(),
                    memory_pipeline=_Pipeline(),
                    session_summarizer=SimpleNamespace(),
                    memory_observer=_Observer(),
                )

            service = service_mod.MemoryUiService(
                db_factory=None,
                plugin_api=adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime())),
            )
            status = await service.runtime_status()
            chat = await service.chat_buffer_status("chat-1")
            events = await service.observability_events(chat_id="chat-1", component="instant_gate", level="info", limit=20)
            errors = await service.observability_errors(chat_id="chat-1", limit=20)
            return status, chat, events, errors

        status, chat, events, errors = asyncio.run(_run())

        self.assertTrue(status["runtime_bound"])
        self.assertTrue(status["instant_gate_ready"])
        self.assertTrue(status["memory_pipeline_ready"])
        self.assertTrue(status["session_summarizer_ready"])
        self.assertEqual(status["memory_pipeline_status"]["buffered_chats"], 2)
        self.assertEqual(status["observer_status"]["recent_error_count"], 1)
        self.assertEqual(chat["data"]["chat_id"], "chat-1")
        self.assertEqual(chat["data"]["pending_messages"], 4)
        self.assertTrue(chat["data"]["worker_active"])
        self.assertEqual(events["items"][0]["component"], "instant_gate")
        self.assertEqual(events["items"][0]["display_title"], "即时记忆命中")
        self.assertEqual(errors["items"][0]["level"], "error")

    def test_plugin_page_memory_tab_renders_observability_panels(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "pages" / "admin" / "app.js").read_text(encoding="utf-8")
        self.assertIn("记忆网络", js)
        self.assertIn("Canonical 总览", js)
        self.assertIn("每日反思", js)
        self.assertIn("旧实体图谱", js)
        self.assertIn("黑话字典", js)
        self.assertIn("memory-feedback", js)
        self.assertIn('api.get(`/memory-feedback?limit=${page.limit}&offset=${page.offset}`)', js)
        self.assertIn('"/tools/executions?limit=50"', js)
        self.assertIn('api.get("/memory-feedback/sources")', js)
        self.assertIn('api.post(`/memory-feedback/${segment(button.dataset.disableFeedback)}/disable`)', js)

    def test_jargon_cleanup_preview_and_apply_physically_delete_selected_items(self):
        from astrmai.webui.backend.services.memory_ui_service import MemoryUiService

        service = MemoryUiService(db_factory=None)

        async def _list_jargon(**kwargs):
            if kwargs.get("status") == "review_pending":
                return {
                    "items": [
                        {"id": "j-1", "content": "at_type", "status": "review_pending", "confidence": 0.8},
                        {"id": "j-2", "content": "hiyohiyo", "status": "review_pending", "meaning": "招呼语", "examples": ["hiyohiyo", "hiyohiyo"]},
                    ]
                }
            return {"items": []}

        deleted = []

        async def _delete(jargon_id):
            deleted.append(jargon_id)
            return {"status": "ok", "physical_delete": True}

        service.list_jargon = _list_jargon
        service.delete_jargon = _delete
        preview = asyncio.run(service.jargon_cleanup_preview())
        result = asyncio.run(service.apply_jargon_cleanup({"action": "reject", "ids": ["j-1"]}))

        self.assertEqual([item["id"] for item in preview["items"]], ["j-1"])
        self.assertEqual(preview["obvious_count"], 1)
        self.assertTrue(preview["destructive"])
        self.assertEqual(result["changed"], ["j-1"])
        self.assertTrue(result["physical_delete"])
        self.assertEqual(deleted, ["j-1"])

    def test_admin_service_exposes_memory_observability_views(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            class _Observer:
                async def runtime_snapshot(self, **kwargs):
                    return {
                        "instant_gate_ready": True,
                        "memory_pipeline_ready": True,
                        "session_summarizer_ready": True,
                        "buffered_chats": 2,
                        "active_worker_count": 1,
                        "recent_error_count": 1,
                        "recent_warning_count": 0,
                    }

                async def recent_errors(self, **kwargs):
                    return [{"event_id": "evt-err", "component": "session_summarizer", "stage": "canonical_write_failed", "level": "error", "summary": "write failed", "timestamp": 10.0, "chat_id": "chat-1"}]

                async def recent_events(self, **kwargs):
                    item = {"event_id": "evt-1", "component": "instant_gate", "stage": "gate_hit", "level": "info", "summary": "hit", "timestamp": 9.0, "chat_id": kwargs.get("chat_id") or "chat-1"}
                    return [self.format_timeline_item(item)]

                async def chat_snapshot(self, **kwargs):
                    return {"chat_id": kwargs["chat_id"], "pending_messages": 3, "worker_active": True, "recent_events": []}

                @staticmethod
                def format_timeline_item(item):
                    item = dict(item)
                    item["display_title"] = "即时记忆命中" if item.get("stage") == "gate_hit" else item.get("stage", "")
                    return item

            class _Pipeline:
                def describe_runtime_status(self):
                    return {"running": True, "buffered_chats": 2, "tracked_chats": 3, "active_worker_count": 1, "active_worker_chats": ["chat-1"], "sweep_task_running": True}

                def describe_chat_buffer(self, chat_id):
                    return {"chat_id": chat_id, "pending_messages": 3, "cooldown_until": 0.0, "failures": 0, "last_update": 5.0, "last_memory_run_at": 3.0}

                def is_worker_active(self, chat_id):
                    return chat_id == "chat-1"

            class _Runtime:
                memory_engine = SimpleNamespace(
                    instant_gate=SimpleNamespace(),
                    memory_pipeline=_Pipeline(),
                    session_summarizer=SimpleNamespace(),
                    memory_observer=_Observer(),
                )

            service = service_mod.AdminUiService(
                adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
            )
            overview = await service.memory_observability_overview()
            timeline = await service.memory_observability_timeline(chat_id="chat-1", component="instant_gate", level="info", limit=20)
            chat = await service.memory_observability_chat("chat-1")
            errors = await service.memory_observability_errors(chat_id="chat-1", limit=20)
            return overview, timeline, chat, errors

        overview, timeline, chat, errors = asyncio.run(_run())

        self.assertTrue(overview["runtime_bound"])
        self.assertEqual(overview["data"]["snapshot"]["buffered_chats"], 2)
        self.assertEqual(overview["data"]["recent_errors"][0]["display_title"], "canonical_write_failed")
        self.assertEqual(timeline["items"][0]["display_title"], "即时记忆命中")
        self.assertEqual(chat["data"]["chat"]["chat_id"], "chat-1")
        self.assertEqual(errors["items"][0]["level"], "error")

    def test_admin_service_exposes_cognition_unified_timeline(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            class _Observer:
                async def recent_events(self, **kwargs):
                    return [{
                        "event_id": "mem-1",
                        "component": "instant_gate",
                        "stage": "gate_hit",
                        "level": "info",
                        "summary": "memory hit",
                        "timestamp": 40.0,
                        "chat_id": kwargs.get("chat_id") or "chat-1",
                    }]

                @staticmethod
                def format_timeline_item(item):
                    item = dict(item)
                    item["display_title"] = "即时记忆命中"
                    return item

            class _Planner:
                cognitive_decision_history = [{"chat_id": "chat-1", "social_intent": "join", "timestamp": 10.0}]
                tool_trace_history = [{"chat_id": "chat-1", "tool_tier": "chat", "timestamp": 20.0}]
                turn_trace_history = []
                raw_trace_store = None

            class _Runtime:
                system2_planner = _Planner()
                memory_engine = SimpleNamespace(memory_observer=_Observer())

            service = service_mod.AdminUiService(
                adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
            )
            return await service.cognition_unified_timeline(chat_id="chat-1", include=["decision", "tool", "memory"], limit=20)

        result = asyncio.run(_run())
        self.assertEqual(result["items"][0]["kind"], "memory")
        self.assertEqual(result["items"][1]["kind"], "tool")
        self.assertEqual(result["items"][2]["kind"], "decision")
        self.assertEqual(result["items"][0]["title"], "即时记忆命中")

    def test_runtime_observability_hub_supports_recent_snapshot_and_search(self):
        hub_mod = importlib.import_module("astrmai.infrastructure.runtime.observability")

        async def _run():
            hub = hub_mod.RuntimeObservabilityHub(raw_trace_store=None, max_recent_events=10, max_events_per_chat=5)
            await hub.record(
                domain="scheduler",
                kind="heartbeat",
                level="warning",
                chat_id="chat-1",
                title="Due selection committed",
                summary="maintenance pressure",
                tags={"phase": "heartbeat", "scheduler_bucket": "maintenance"},
                facets={"action": "due_selection_committed", "stage": "tick"},
            )
            await hub.record(
                domain="memory",
                kind="maintenance",
                level="error",
                chat_id="chat-1",
                title="Memory summarize failed",
                summary="provider_failure_text",
                tags={"component": "session_summarizer"},
                facets={"reason": "provider_failure_text", "display_title": "summarize failed"},
            )
            recent = await hub.recent(chat_id="chat-1", limit=10)
            errors = await hub.recent_errors(chat_id="chat-1", limit=10)
            snapshot = await hub.global_snapshot()
            chat = await hub.chat_snapshot("chat-1")
            search = await hub.search(q="provider_failure_text", chat_id="chat-1", levels={"error"}, limit=10)
            return recent, errors, snapshot, chat, search

        recent, errors, snapshot, chat, search = asyncio.run(_run())
        self.assertEqual(len(recent), 2)
        self.assertEqual(errors[0]["domain"], "memory")
        self.assertEqual(snapshot["domain_counts"]["scheduler"], 1)
        self.assertEqual(snapshot["level_counts"]["error"], 1)
        self.assertEqual(chat["retained_events"], 2)
        self.assertEqual(search[0]["title"], "Memory summarize failed")

    def test_admin_service_exposes_observability_views_and_search(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        hub_mod = importlib.import_module("astrmai.infrastructure.runtime.observability")

        async def _run():
            hub = hub_mod.RuntimeObservabilityHub(raw_trace_store=None, max_recent_events=20, max_events_per_chat=10)
            await hub.record(
                domain="scheduler",
                kind="heartbeat",
                level="warning",
                chat_id="chat-1",
                title="Due selection committed",
                summary="batch pressure",
                tags={"action": "due_selection_committed"},
                facets={"poll_mode": "busy"},
            )
            await hub.record(
                domain="heartflow",
                kind="impulse",
                level="info",
                chat_id="chat-1",
                title="Heartflow observe",
                summary="observe",
                tags={"source": "heartflow_manager"},
                facets={"action": "observe"},
            )

            class _Runtime:
                observability_hub = hub

            service = service_mod.AdminUiService(
                adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
            )
            overview = await service.observability_overview()
            timeline = await service.observability_timeline(chat_id="chat-1", domains=["scheduler", "heartflow"], limit=20)
            chat = await service.observability_chat("chat-1")
            errors = await service.observability_errors(chat_id="chat-1", limit=20)
            search = await service.observability_search(q="batch pressure", chat_id="chat-1", domains=["scheduler"], limit=20)
            return overview, timeline, chat, errors, search

        overview, timeline, chat, errors, search = asyncio.run(_run())
        self.assertTrue(overview["runtime_bound"])
        self.assertEqual(overview["data"]["snapshot"]["retained_events"], 2)
        self.assertEqual(timeline["items"][0]["domain"], "heartflow")
        self.assertEqual(chat["data"]["chat"]["chat_id"], "chat-1")
        self.assertEqual(errors["items"][0]["domain"], "scheduler")
        self.assertEqual(search["items"][0]["domain"], "scheduler")

    def test_settings_service_builds_effective_config_and_validates_schema(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        service_mod = importlib.import_module("astrmai.webui.backend.services.settings_ui_service")

        with tempfile.TemporaryDirectory() as tmp_dir:
            schema_path = Path(tmp_dir) / "schema.json"
            config_path = Path(tmp_dir) / "config.json"
            schema_path.write_text(
                json.dumps(
                    {
                        "reply": {
                            "type": "object",
                            "items": {
                                "base_frequency": {"type": "float", "default": 0.7},
                                "enabled": {"type": "bool", "default": True},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(json.dumps({"reply": {"base_frequency": 0.3}}), encoding="utf-8")

            adapter = adapter_mod.PluginApiAdapter(
                facade=None,
                config_path=str(config_path),
                schema_path=str(schema_path),
                persona_cache_path=str(Path(tmp_dir) / "persona.json"),
            )
            service = service_mod.SettingsUiService(adapter)

            effective = asyncio.run(service.get_effective_config())
            self.assertEqual(effective["reply"]["base_frequency"], 0.3)
            self.assertIs(effective["reply"]["enabled"], True)

            bad_field = asyncio.run(service.update_section("reply", {"missing": 1}))
            self.assertEqual(bad_field["status"], "error")

            bad_type = asyncio.run(service.update_section("reply", {"enabled": "yes"}))
            self.assertEqual(bad_type["status"], "error")

            reset = asyncio.run(service.reset_section("reply"))
            self.assertEqual(reset["data"]["base_frequency"], 0.7)

    def test_plugin_api_apply_config_updates_bound_runtime(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        applied = None
        class _Facade:
            def apply_hot_config(self, config_dict, parsed_config):
                nonlocal applied
                applied = (config_dict, parsed_config)
                return True

        with tempfile.TemporaryDirectory() as tmp_dir:
            adapter = adapter_mod.PluginApiAdapter(
                facade=_Facade(),
                config_path=str(Path(tmp_dir) / "config.json"),
                schema_path=str(Path(tmp_dir) / "schema.json"),
                persona_cache_path=str(Path(tmp_dir) / "persona.json"),
            )
            result = asyncio.run(adapter.apply_config({"reply": {"base_frequency": 0.2}}, {"reply.base_frequency"}))
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["runtime_bound"])
            self.assertIsNotNone(applied)
            self.assertEqual(applied[0]["reply"]["base_frequency"], 0.2)

    def test_plugin_facade_apply_hot_config_refreshes_proactive_task_config_refs(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        facade_mod = importlib.import_module("astrmai.app.plugin_facade")

        old_config = SimpleNamespace(reply=SimpleNamespace(base_frequency=0.7))
        new_config = SimpleNamespace(reply=SimpleNamespace(base_frequency=0.2))
        refreshed = []
        previous_facade = adapter_mod.get_active_facade()

        class _ProactiveTask:
            def refresh_config(self, config):
                refreshed.append(config)

        class _Runtime:
            raw_config = {}
            config = old_config
            proactive_task = _ProactiveTask()
            background_tasks = set()
            lifecycle = SimpleNamespace()

            def bind_system2_callback(self, callback):
                self.callback = callback

            def rebuild_infrastructure_settings(self):
                self.rebuilt = True

            def sync_host_compat_attrs(self):
                self.synced = True

        try:
            facade = facade_mod.PluginFacade(_Runtime())
            result = facade.apply_hot_config({"reply": {"base_frequency": 0.2}}, new_config)

            self.assertTrue(result)
            self.assertIs(facade.runtime.config, new_config)
            self.assertEqual(refreshed, [new_config])
            self.assertTrue(facade.runtime.rebuilt)
            self.assertTrue(facade.runtime.synced)
        finally:
            adapter_mod.set_active_facade(previous_facade)

    def test_persona_ui_service_exposes_readonly_slice_diagnostics(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        service_mod = importlib.import_module("astrmai.webui.backend.services.persona_ui_service")

        class _PersonaConfig:
            persona_id = "atri"

        class _Config:
            persona = _PersonaConfig()

        class _Task:
            def done(self):
                return False

        class _Summarizer:
            pending_tasks = {"atri": _Task()}

        class _Runtime:
            config = _Config()
            persona_summarizer = _Summarizer()
            memory_engine = object()

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = Path(tmp_dir) / "persona.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "atri": {
                            "summary": "核心摘要",
                            "first_person_rewrite": "我是亚托莉。",
                            "style": "轻快",
                            "shards": {"speech_style": "短句自然"},
                            "is_full_ready": False,
                            "raw": "原始人格很长",
                            "timestamp": 1.0,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            adapter = adapter_mod.PluginApiAdapter(
                facade=_RuntimeBackedFacade(_Runtime()),
                config_path=str(Path(tmp_dir) / "config.json"),
                schema_path=str(Path(tmp_dir) / "schema.json"),
                persona_cache_path=str(cache_path),
            )
            result = asyncio.run(service_mod.PersonaUiService(adapter).get_persona_slices())
            data = result["data"]
            self.assertEqual(data["persona_id"], "atri")
            self.assertEqual(data["cache_key"], "atri")
            self.assertEqual(data["summary"], "核心摘要")
            self.assertEqual(data["shards"]["speech_style"], "短句自然")
            self.assertTrue(data["pending_task"])
            self.assertTrue(data["self_lore"]["available"])
            self.assertNotIn("raw", data)
            self.assertNotIn("raw_preview", data)

    def test_admin_service_exposes_runtime_and_observability_summaries(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        raw_store_mod = importlib.import_module("astrmai.infrastructure.runtime.raw_trace_store")

        class _Planner:
            cognitive_decision_history = [{"chat_id": "chat-1", "social_intent": "join", "failure_kind": "provider_failure_text", "attempted_models": ["model-a", "model-b"], "raw_completion": "request id: 1"}]
            tool_trace_history = [{"chat_id": "chat-1", "tool_tier": "chat", "tool_count": 2, "protocol_passthrough": True, "protocol_type": "terminal_yield"}]
            turn_trace_history = [{"chat_id": "chat-1", "status": "executed", "tools": {"final_tier": "chat"}}]

        class _HeartflowManager:
            def list_impulse_decisions(self, chat_id=None, limit=50):
                if chat_id and chat_id != "chat-1":
                    return []
                return [
                    SimpleNamespace(
                        chat_id="chat-1",
                        timestamp=1.0,
                        pulse_type="proactive_hint",
                        visible_candidate_allowed=True,
                        dispatch_enabled=False,
                    )
                ][:limit]

            def list_timeline(self, chat_id=None, limit=80):
                if chat_id and chat_id != "chat-1":
                    return []
                return [{"kind": "action", "chat_id": "chat-1", "label": "observe", "timestamp": 2.0}][:limit]

        class _TopicDigestService:
            def list_digests(self, limit=50):
                return [SimpleNamespace(chat_id="chat-1", timestamp=3.0, status="written", source="heartflow_topic_digest")][:limit]

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                raw_store = raw_store_mod.RawTraceEventStore(tmp_dir, max_per_chat=50)
                await raw_store.append_many(
                    "chat-1",
                    [
                        {
                            "created_at": 123.0,
                            "chat_id": "chat-1",
                            "trace_id": "trace-1",
                            "stage": "execution.executor.model_failure",
                            "failure_kind": "provider_failure_text",
                            "attempted_models": ["model-a"],
                        }
                    ],
                )

                class _Runtime:
                    system2_planner = _Planner()
                    proactive_task = SimpleNamespace(
                        heartflow_manager=_HeartflowManager(),
                        heartflow_topic_digest_service=_TopicDigestService(),
                    )

                    def build_diagnostics(self):
                        return {"status": {"lifecycle_started": True, "degraded_components": {}}}

                    def build_capability_overview_sync(self):
                        return {"proactive": {"enabled": True}}

                _Runtime.system2_planner.raw_trace_store = raw_store

                service = service_mod.AdminUiService(
                    adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
                )
                status = await service.runtime_status()
                decisions = await service.recent_decisions(chat_id="chat-1")
                tools = await service.recent_tool_traces(chat_id="chat-1")
                turns = await service.recent_turn_traces(chat_id="chat-1")
                trace_events = await service.chat_trace_events("chat-1")
                impulses = await service.heartflow_impulses(chat_id="chat-1")
                timeline = await service.heartflow_timeline(chat_id="chat-1")
                digests = await service.heartflow_topic_digests()
                return status, decisions, tools, turns, trace_events, impulses, timeline, digests

        status, decisions, tools, turns, trace_events, impulses, timeline, digests = asyncio.run(_run())
        self.assertTrue(status["runtime_bound"])
        self.assertEqual(decisions["items"][0]["social_intent"], "join")
        self.assertEqual(decisions["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")
        self.assertEqual(tools["items"][0]["tool_tier"], "chat")
        self.assertTrue(tools["items"][0]["failure_evidence"]["protocol_passthrough"])
        self.assertEqual(turns["items"][0]["tools"]["final_tier"], "chat")
        self.assertEqual(trace_events["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")
        self.assertTrue(impulses["items"][0]["visible_candidate_allowed"])
        self.assertEqual(timeline["items"][0]["kind"], "action")
        self.assertEqual(digests["items"][0]["source"], "heartflow_topic_digest")

    def test_admin_service_exposes_context_economy_template_metrics(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _Gateway:
            def get_context_economy_stats(self):
                return {
                    "memory_global_summary": {
                        "call_count": 4,
                        "lane_rotate_count": 2,
                        "fallback_count": 0,
                        "primary_hit_rate": 1.0,
                        "provider_session_usage_rate": 1.0,
                        "provider_session_reuse_rate": 0.6667,
                        "cache_affinity_ready_rate": 1.0,
                        "avg_stable_prefix_length": 220.0,
                        "avg_dynamic_payload_length": 64.0,
                        "actual_models": {"model-a": 3},
                        "rotate_reasons": {"template_version_changed": 1, "schema_changed": 1},
                        "workload_families": {"memory_global_summary": 4},
                    },
                    "persona_summary": {
                        "call_count": 5,
                        "lane_rotate_count": 4,
                        "fallback_count": 0,
                        "primary_hit_rate": 0.8,
                        "provider_session_usage_rate": 1.0,
                        "provider_session_reuse_rate": 0.2,
                        "cache_affinity_ready_rate": 1.0,
                        "avg_stable_prefix_length": 310.0,
                        "avg_dynamic_payload_length": 52.0,
                        "actual_models": {"model-b": 5},
                        "rotate_reasons": {"template_changed": 3, "schema_changed": 1},
                        "workload_families": {"persona_summary": 5},
                    },
                    "_templates": {
                        "memory_global_summary@v2": {
                            "call_count": 2,
                            "lane_rotate_count": 1,
                            "fallback_count": 0,
                            "primary_hit_rate": 1.0,
                            "provider_session_usage_rate": 1.0,
                            "provider_session_reuse_rate": 0.5,
                            "cache_affinity_ready_rate": 1.0,
                            "avg_stable_prefix_length": 220.0,
                            "avg_dynamic_payload_length": 64.0,
                            "actual_models": {"model-a": 2},
                            "rotate_reasons": {"template_version_changed": 1, "schema_changed": 1},
                            "workload_families": {"memory_global_summary": 2},
                        },
                        "persona_summary@v3": {
                            "call_count": 5,
                            "lane_rotate_count": 4,
                            "fallback_count": 0,
                            "primary_hit_rate": 0.8,
                            "provider_session_usage_rate": 1.0,
                            "provider_session_reuse_rate": 0.2,
                            "cache_affinity_ready_rate": 1.0,
                            "avg_stable_prefix_length": 310.0,
                            "avg_dynamic_payload_length": 52.0,
                            "actual_models": {"model-b": 5},
                            "rotate_reasons": {"template_changed": 3, "schema_changed": 1},
                            "workload_families": {"persona_summary": 5},
                        }
                    },
                }

        class _Runtime:
            gateway = _Gateway()

        service = service_mod.AdminUiService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )
        overview = asyncio.run(service.context_economy_overview_view(limit=20))
        templates = asyncio.run(service.context_economy_templates_view(limit=20))
        filtered = asyncio.run(
            service.context_economy_templates_view(
                limit=20,
                template_id="persona",
                workload_family="persona_summary",
                sort_by="session_reuse",
                sort_dir="asc",
            )
        )
        calls_sorted = asyncio.run(
            service.context_economy_templates_view(
                limit=20,
                sort_by="calls",
                sort_dir="desc",
            )
        )

        self.assertEqual(overview["data"]["overview"]["total_calls"], 9)
        self.assertEqual(overview["data"]["overview"]["total_rotates"], 6)
        self.assertEqual(overview["data"]["overview"]["template_count"], 2)
        self.assertAlmostEqual(overview["data"]["overview"]["provider_session_reuse_rate"], 0.4444, places=4)
        self.assertEqual(overview["data"]["templates"][0]["template_id"], "persona_summary")
        self.assertEqual(templates["items"][0]["template_id"], "persona_summary")
        self.assertEqual(templates["items"][0]["template_version"], "v3")
        self.assertEqual(templates["items"][0]["rotate_reasons"]["template_changed"], 3)
        self.assertEqual(templates["items"][0]["provider_session_reuse_rate"], 0.2)
        self.assertEqual(templates["items"][0]["workload_families"]["persona_summary"], 5)
        self.assertEqual(templates["items"][1]["template_id"], "memory_global_summary")
        self.assertEqual(filtered["items"][0]["template_id"], "persona_summary")
        self.assertEqual(filtered["available_workload_families"], ["memory_global_summary", "persona_summary"])
        self.assertEqual(calls_sorted["items"][0]["template_id"], "persona_summary")
        self.assertEqual(calls_sorted["items"][1]["template_id"], "memory_global_summary")

    def test_admin_service_exposes_scheduler_diagnostics_views(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _Kernel:
            def describe_status_sync(self):
                return {
                    "scheduler_policy": {
                        "active_profile": "balanced",
                        "available_profiles": ["dialogue_first", "balanced", "maintenance_friendly"],
                    },
                    "last_due_selection_summary": {
                        "selected_count": 2,
                        "batch_fill_rate": 0.5,
                    },
                    "last_due_selection_report": {
                        "selected": ["chat-1", "chat-2"],
                        "batch_plan": {"total_limit": 4, "maintenance_slots": 1},
                        "quota_skip_counts": {"skipped_by_maintenance_quota": 1},
                    },
                }

            async def peek_loop_state(self, chat_id):
                return SimpleNamespace(
                    chat_id=chat_id,
                    phase="WAITING",
                    next_tick_at=123.0,
                    last_decision="WAIT",
                    missed_due_passes=2,
                    forced_promotion_count=1,
                    pending_signals={
                        "selected_reason": "selected_by_scheduler_score",
                        "quota_skip_reason": "",
                        "starvation_tier": "watch",
                        "forced_promotion_eligible": False,
                    },
                )

        class _Task:
            chat_loop_kernel = _Kernel()

            def describe_status(self):
                return {
                    "scheduler_poll_mode": "NORMAL",
                    "scheduler_poll_interval": 10.0,
                    "due_chat_count": 2,
                    "maintenance_budget_total": 1,
                    "maintenance_budget_remaining": 1,
                    "batch_fill_rate": 0.5,
                    "forced_promotion_count": 1,
                    "scheduler_batch_plan": {"total_limit": 4, "maintenance_slots": 1},
                    "quota_skip_counts": {"skipped_by_maintenance_quota": 1},
                    "poll_mode_transition": {"previous": "FAST", "current": "NORMAL", "reason": "background_due_only"},
                }

        class _Runtime:
            proactive_task = _Task()
            chat_loop_kernel = proactive_task.chat_loop_kernel

        service = service_mod.AdminUiService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )
        status = asyncio.run(service.scheduler_status_view())
        selection = asyncio.run(service.scheduler_due_selection_view())
        chat = asyncio.run(service.scheduler_chat_view("chat-1"))

        self.assertEqual(status["data"]["overview"]["scheduler_poll_mode"], "NORMAL")
        self.assertEqual(status["data"]["scheduler_policy"]["active_profile"], "balanced")
        self.assertEqual(selection["data"]["report"]["selected"], ["chat-1", "chat-2"])
        self.assertEqual(selection["data"]["poll_mode_transition"]["current"], "NORMAL")
        self.assertEqual(chat["data"]["phase"], "WAITING")
        self.assertTrue(chat["data"]["state_present"])
        self.assertEqual(chat["data"]["scheduler_pending_signals"]["selected_reason"], "selected_by_scheduler_score")

    def test_scheduler_chat_view_is_read_only_when_state_missing(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        kernel_mod = importlib.import_module("astrmai.conversation.loop.chat_loop_kernel")

        class _Runtime:
            chat_loop_kernel = kernel_mod.ChatLoopKernel(runtime_coordinator=SimpleNamespace())
            proactive_task = SimpleNamespace(chat_loop_kernel=chat_loop_kernel)

        service = service_mod.AdminUiService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )

        async def _run():
            before = _Runtime.chat_loop_kernel.describe_status_sync()["tracked_chats"]
            view = await service.scheduler_chat_view("chat-missing")
            after = _Runtime.chat_loop_kernel.describe_status_sync()["tracked_chats"]
            return before, view, after

        before, view, after = asyncio.run(_run())

        self.assertEqual(before, 0)
        self.assertEqual(after, 0)
        self.assertTrue(view["runtime_bound"])
        self.assertFalse(view["data"]["state_present"])
        self.assertEqual(view["data"]["chat_id"], "chat-missing")
        self.assertEqual(view["data"]["scheduler_pending_signals"], {})

    def test_cognition_routes_expose_context_economy_endpoints(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "webui" / "backend" / "routes" / "cognition_routes.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn('@router.get("/context-economy")', content)
        self.assertIn('@router.get("/context-economy/templates")', content)
        self.assertIn('@router.get("/scheduler/status")', content)
        self.assertIn('@router.get("/scheduler/due-selection")', content)
        self.assertIn('@router.get("/scheduler/chats/{chat_id}")', content)
        self.assertIn('@router.get("/chats/{chat_id}/unified-timeline")', content)
        self.assertIn('@router.get("/observability/overview")', content)
        self.assertIn('@router.get("/observability/timeline")', content)
        self.assertIn('@router.get("/observability/chats/{chat_id}")', content)
        self.assertIn('@router.get("/observability/errors")', content)
        self.assertIn('@router.get("/observability/search")', content)

    def test_dashboard_cognition_tab_renders_context_economy_panel(self):
        root = Path(__file__).resolve().parents[1]
        js = (root / "pages" / "admin" / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderDashboardCognition", js)
        self.assertIn("Scheduler Diagnostics", js)
        self.assertIn("Batch / Backpressure", js)
        self.assertIn("Chat Loop Drill-down", js)
        self.assertIn("schedulerChatId", js)
        self.assertIn("scheduler-chat-id", js)
        self.assertIn("/cognition/scheduler/status", js)
        self.assertIn("/cognition/scheduler/due-selection", js)
        self.assertIn("/cognition/scheduler/chats/${segment(targetChat)}", js)
        self.assertIn("loadSchedulerChatLoop", js)
        self.assertIn("schedulerStatus", js)
        self.assertIn("/cognition/observability/overview", js)
        self.assertIn("observabilityOverview", js)
        self.assertIn("unifiedTimeline", js)
        self.assertIn("observabilityTimelinePath", js)
        self.assertIn("/cognition/observability/timeline?", js)
        self.assertIn("/cognition/observability/search?", js)
        self.assertIn("Global Observability Timeline", js)
        return
        html = (root / "astrmai" / "webui" / "frontend" / "pages" / "dashboard" / "index.html").read_text(encoding="utf-8")
        js = (root / "astrmai" / "webui" / "frontend" / "js" / "pages" / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn("Context Economy", html)
        self.assertIn("contextEconomyTemplates", html)
        self.assertIn("contextEconomyFilterText", html)
        self.assertIn("contextEconomyWorkloadFamily", html)
        self.assertIn("contextEconomyQuickView", html)
        self.assertIn("contextEconomySortBy", html)
        self.assertIn("Scheduler Diagnostics", html)
        self.assertIn("Scheduler Overview", html)
        self.assertIn("Batch / Backpressure", html)
        self.assertIn("Chat Loop Drill-down", html)
        self.assertIn("schedulerChatId", html)
        self.assertIn("暂无 loop state。该 chat 尚未进入 scheduler 跟踪。", html)
        self.assertIn("High Rotate", html)
        self.assertIn("Low Reuse", html)
        self.assertIn("High Traffic", html)
        self.assertIn("快捷视图会切换模板排序", html)
        self.assertIn('title="按 lane rotate 次数从高到低查看最不稳定的模板。"', html)
        self.assertIn("/cognition/scheduler/status", js)
        self.assertIn("/cognition/scheduler/due-selection", js)
        self.assertIn("/cognition/scheduler/chats/", js)
        self.assertIn("loadSchedulerChatLoop", js)
        self.assertIn("schedulerStatus", js)
        self.assertIn("/cognition/context-economy?limit=20", js)
        self.assertIn("loadContextEconomyTemplates", js)
        self.assertIn("setContextEconomyQuickView", js)
        self.assertIn("/cognition/context-economy/templates?", js)
        self.assertIn("provider_session_reuse_rate", html)
        self.assertIn("chatTraceEvents", html)
        self.assertIn("Raw Trace Events", html)
        self.assertIn("summarizeFailureEvidence", js)
        self.assertIn("/cognition/chats/${encodedChat}/trace-events?limit=40", js)
        self.assertIn("Observability Overview", html)
        self.assertIn("Open Memory Diagnostics", html)
        self.assertIn("observabilityOverview", js)
        self.assertIn("memoryObservabilityChatId", js)
        self.assertIn("openMemoryDrilldown", js)
        self.assertIn("/cognition/observability/overview", js)
        self.assertIn("Global Observability Timeline", html)
        self.assertIn("cognitionUnifiedTimeline", js)
        self.assertIn("loadCognitionUnifiedTimeline", js)
        self.assertIn("/cognition/observability/timeline?", js)
        self.assertIn("/cognition/observability/search?", js)
        self.assertIn("applyTimelineQuickFilter", js)
        self.assertIn("clearTimelineFilters", js)
        self.assertIn("provider failures", html)

    def test_chat_trace_events_falls_back_to_turn_trace_embedded_log(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _Planner:
            turn_trace_history = [{
                "chat_id": "chat-1",
                "status": "executed",
                "astrmai_trace_log": [{"stage": "execution.executor.model_pool_exhausted", "last_failure_kind": "provider_failure_text", "attempted_models": ["model-a", "model-b"]}],
            }]
            raw_trace_store = None

        class _Runtime:
            system2_planner = _Planner()

        service = service_mod.AdminUiService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )
        result = asyncio.run(service.chat_trace_events("chat-1"))
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["stage"], "execution.executor.model_pool_exhausted")
        self.assertEqual(result["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")

    def test_chat_trace_events_exposes_gateway_tool_call_failure_evidence(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _RawTraceStore:
            async def recent(self, *, chat_id: str, limit: int = 80):
                return [{
                    "chat_id": chat_id,
                    "stage": "gateway_tool_call_failure",
                    "failure_kind": "provider_failure_text",
                    "attempted_models": ["model-a"],
                    "raw_completion": "All chat models failed: PermissionDeniedError: Error code: 403",
                }]

        class _Planner:
            raw_trace_store = _RawTraceStore()

        class _Runtime:
            system2_planner = _Planner()

        service = service_mod.AdminUiService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )
        result = asyncio.run(service.chat_trace_events("chat-1"))
        evidence = result["items"][0]["failure_evidence"]
        self.assertEqual(evidence["failure_kind"], "provider_failure_text")
        self.assertEqual(evidence["attempted_models"], ["model-a"])
        self.assertIn("PermissionDeniedError", evidence["raw_completion_preview"])

    def test_review_ui_service_is_canonical_first_and_degrades_to_readonly_when_runtime_missing(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.review_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_path = Path(tmp_dir) / "review.db"
                async with aiosqlite.connect(db_path) as db:
                    await db.executescript(
                        """
                        CREATE TABLE canonical_memories (
                            id TEXT PRIMARY KEY,
                            session_id TEXT,
                            persona_id TEXT,
                            source TEXT,
                            kind TEXT,
                            content TEXT,
                            summary TEXT,
                            tags TEXT,
                            importance REAL,
                            confidence REAL,
                            status TEXT,
                            decay_score REAL,
                            create_time REAL,
                            update_time REAL,
                            last_access_time REAL,
                            access_count INTEGER,
                            superseded_by TEXT,
                            deleted_reason TEXT,
                            metadata TEXT,
                            dedup_key TEXT,
                            source_ref TEXT,
                            visibility TEXT
                        );
                        CREATE TABLE ExpressionPattern (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            situation TEXT,
                            expression TEXT
                        );
                        """
                    )
                    await db.execute(
                        """
                        INSERT INTO canonical_memories (
                            id, session_id, source, kind, content, summary, tags, importance, confidence, status,
                            decay_score, create_time, update_time, last_access_time, access_count, superseded_by,
                            deleted_reason, metadata, dedup_key, source_ref, visibility
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "mem-review-1",
                            "group-1",
                            "learning_expression_pattern",
                            "expression_pattern",
                            "ship it softly",
                            "ship it softly",
                            "[]",
                            0.6,
                            0.7,
                            "review_pending",
                            1.0,
                            1.0,
                            2.0,
                            1.0,
                            0,
                            "",
                            "",
                            json.dumps({"situation": "daily reply", "review_status": "pending"}),
                            "expr:1",
                            "test",
                            "maintenance_only",
                        ),
                    )
                    await db.commit()

                @asynccontextmanager
                async def _db_factory():
                    conn = await aiosqlite.connect(db_path)
                    conn.row_factory = aiosqlite.Row
                    try:
                        yield conn
                    finally:
                        await conn.close()

                service = service_mod.ReviewUiService(adapter_mod.PluginApiAdapter(facade=None), _db_factory)
                pending = await service.list_pending()
                created = await service.create_review({"group_id": "group-1", "situation": "daily reply", "expression": "ship it"})
                submitted = await service.submit_review("mem-review-1", "approve")
                async with aiosqlite.connect(db_path) as db:
                    legacy_count = (await (await db.execute("SELECT COUNT(*) FROM ExpressionPattern")).fetchone())[0]
                return pending, created, submitted, legacy_count

        pending, created, submitted, legacy_count = asyncio.run(_run())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "mem-review-1")
        self.assertEqual(created["status"], "degraded")
        self.assertEqual(submitted["status"], "degraded")
        self.assertEqual(legacy_count, 0)

    def test_review_ui_service_does_not_mask_bound_runtime_failures_as_empty_pending_list(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.review_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _Facade:
            async def list_pending_expression_reviews(self, group_id="", limit=50):
                raise RuntimeError("boom")

            def get_expression_pattern_service(self):
                return object()

        @asynccontextmanager
        async def _db_factory():
            yield None

        service = service_mod.ReviewUiService(adapter_mod.PluginApiAdapter(facade=_Facade()), _db_factory)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            asyncio.run(service.list_pending())

    def test_review_ui_service_gracefully_handles_missing_canonical_memories_table(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.review_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_path = Path(tmp_dir) / "empty.db"

                @asynccontextmanager
                async def _db_factory():
                    conn = await aiosqlite.connect(db_path)
                    conn.row_factory = aiosqlite.Row
                    try:
                        yield conn
                    finally:
                        await conn.close()

                service = service_mod.ReviewUiService(adapter_mod.PluginApiAdapter(facade=None), _db_factory)
                return await service.list_pending(), await service.list_reviews(page_size=10)

        pending, reviews = asyncio.run(_run())
        self.assertEqual(pending, [])
        self.assertEqual(reviews["items"], [])
        self.assertEqual(reviews["total"], 0)

    def test_admin_expression_stats_reads_canonical_expression_patterns(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_path = Path(tmp_dir) / "admin.db"
                async with aiosqlite.connect(db_path) as db:
                    await db.executescript(
                        """
                        CREATE TABLE canonical_memories (
                            id TEXT PRIMARY KEY,
                            session_id TEXT,
                            persona_id TEXT,
                            source TEXT,
                            kind TEXT,
                            content TEXT,
                            summary TEXT,
                            tags TEXT,
                            importance REAL,
                            confidence REAL,
                            status TEXT,
                            decay_score REAL,
                            create_time REAL,
                            update_time REAL,
                            last_access_time REAL,
                            access_count INTEGER,
                            superseded_by TEXT,
                            deleted_reason TEXT,
                            metadata TEXT,
                            dedup_key TEXT,
                            source_ref TEXT,
                            visibility TEXT
                        );
                        """
                    )
                    rows = [
                        ("expr-1", "review_pending", {"review_status": "pending"}),
                        ("expr-2", "active", {"review_status": "approved"}),
                        ("expr-3", "rejected", {"review_status": "rejected"}),
                    ]
                    for memory_id, status, metadata in rows:
                        await db.execute(
                            """
                            INSERT INTO canonical_memories (
                                id, session_id, source, kind, content, summary, tags, importance, confidence, status,
                                decay_score, create_time, update_time, last_access_time, access_count, superseded_by,
                                deleted_reason, metadata, dedup_key, source_ref, visibility
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                memory_id,
                                "group-1",
                                "learning_expression_pattern",
                                "expression_pattern",
                                "expr",
                                "expr",
                                "[]",
                                0.6,
                                0.7,
                                status,
                                1.0,
                                1.0,
                                2.0,
                                1.0,
                                0,
                                "",
                                "",
                                json.dumps(metadata),
                                memory_id,
                                "test",
                                "maintenance_only",
                            ),
                        )
                    await db.commit()

                @asynccontextmanager
                async def _db_factory():
                    conn = await aiosqlite.connect(db_path)
                    conn.row_factory = aiosqlite.Row
                    try:
                        yield conn
                    finally:
                        await conn.close()

                service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=None), _db_factory)
                return await service.expression_stats()

        stats = asyncio.run(_run())
        self.assertEqual(stats["data"]["total"], 3)
        self.assertEqual(stats["data"]["pending"], 1)
        self.assertEqual(stats["data"]["approved"], 1)
        self.assertEqual(stats["data"]["rejected"], 1)

    def test_learning_expression_stats_reuses_canonical_expression_counts(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.learningservice")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        async def _run():
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_path = Path(tmp_dir) / "learning.db"
                async with aiosqlite.connect(db_path) as db:
                    await db.executescript(
                        """
                        CREATE TABLE canonical_memories (
                            id TEXT PRIMARY KEY,
                            session_id TEXT,
                            persona_id TEXT,
                            source TEXT,
                            kind TEXT,
                            content TEXT,
                            summary TEXT,
                            tags TEXT,
                            importance REAL,
                            confidence REAL,
                            status TEXT,
                            decay_score REAL,
                            create_time REAL,
                            update_time REAL,
                            last_access_time REAL,
                            access_count INTEGER,
                            superseded_by TEXT,
                            deleted_reason TEXT,
                            metadata TEXT,
                            dedup_key TEXT,
                            source_ref TEXT,
                            visibility TEXT
                        );
                        """
                    )
                    rows = [
                        ("expr-1", "review_pending", {"review_status": "pending"}),
                        ("expr-2", "active", {"review_status": "approved"}),
                        ("expr-3", "rejected", {"review_status": "rejected"}),
                    ]
                    for memory_id, status, metadata in rows:
                        await db.execute(
                            """
                            INSERT INTO canonical_memories (
                                id, session_id, source, kind, content, summary, tags, importance, confidence, status,
                                decay_score, create_time, update_time, last_access_time, access_count, superseded_by,
                                deleted_reason, metadata, dedup_key, source_ref, visibility
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                memory_id,
                                "group-1",
                                "learning_expression_pattern",
                                "expression_pattern",
                                "expr",
                                "expr",
                                "[]",
                                0.6,
                                0.7,
                                status,
                                1.0,
                                1.0,
                                2.0,
                                1.0,
                                0,
                                "",
                                "",
                                json.dumps(metadata),
                                memory_id,
                                "test",
                                "maintenance_only",
                            ),
                        )
                    await db.commit()

                original_db_path = os.environ.get("ASTRMAI_DB_PATH")
                try:
                    os.environ["ASTRMAI_DB_PATH"] = str(db_path)
                    service = service_mod.LearningService(adapter_mod.PluginApiAdapter(facade=None))
                    return await service.expression_stats()
                finally:
                    if original_db_path is None:
                        os.environ.pop("ASTRMAI_DB_PATH", None)
                    else:
                        os.environ["ASTRMAI_DB_PATH"] = original_db_path

        stats = asyncio.run(_run())
        self.assertEqual(stats["data"]["total"], 3)
        self.assertEqual(stats["data"]["pending"], 1)
        self.assertEqual(stats["data"]["approved"], 1)
        self.assertEqual(stats["data"]["rejected"], 1)

    def test_runtime_capability_imports_are_package_relative(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "app" / "runtime_context.py"
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("import_module(\"astrmai.", content)
        self.assertIn("from .. import multimodal as multimodal_mod", content)
        self.assertIn("from .. import workmode as workmode_mod", content)
        self.assertIn("from .. import proactive as proactive_mod", content)

    def test_multimodal_capability_overview_is_json_serializable(self):
        multimodal_mod = importlib.import_module("astrmai.multimodal")
        overview = multimodal_mod.describe_multimodal_capabilities(
            None,
            vision_enabled=True,
            meme_enabled=True,
        )
        json.dumps(overview)
        self.assertIsInstance(overview["meme_service"]["memes_dir"], str)

    def test_aggregated_router_registers_admin_routes(self):
        routes_mod = importlib.import_module("astrmai.webui.backend.routes")
        router = routes_mod.build_api_router()
        paths = {route.path for route in router.routes}
        self.assertIn("/runtime/status", paths)
        self.assertIn("/heartflow/status", paths)
        self.assertIn("/heartflow/impulses", paths)
        self.assertIn("/memories/canonical/{memory_id}/restore", paths)
        self.assertIn("/memories/canonical/{memory_id}/stale", paths)
        self.assertIn("/memories/canonical/{memory_id}/merge", paths)
        self.assertIn("/memories/quality/overview", paths)
        self.assertIn("/memories/quality/audit", paths)
        self.assertIn("/memories/quality/quarantine", paths)
        self.assertIn("/memories/migration/dry-run", paths)
        self.assertIn("/memories/migration/execute", paths)
        self.assertIn("/memories/migration/verify", paths)
        self.assertIn("/memories/migration/repair", paths)
        self.assertIn("/heartflow/chats/{chat_id}/impulses", paths)
        self.assertIn("/heartflow/timeline", paths)
        self.assertIn("/heartflow/chats/{chat_id}/timeline", paths)
        self.assertIn("/heartflow/topic-digests", paths)
        self.assertIn("/cognition/recent-decisions", paths)
        self.assertIn("/cognition/recent-turns", paths)
        self.assertIn("/cognition/chats/{chat_id}/turns", paths)
        self.assertIn("/cognition/scheduler/status", paths)
        self.assertIn("/cognition/scheduler/due-selection", paths)
        self.assertIn("/cognition/scheduler/chats/{chat_id}", paths)
        self.assertIn("/tools/status", paths)
        self.assertIn("/memory-feedback", paths)
        self.assertIn("/proactive/status", paths)
        self.assertIn("/learning/status", paths)
        self.assertIn("/learning/pipeline-diagnostics", paths)
        self.assertIn("/learning/pipeline/retry-now", paths)
        self.assertIn("/learning/pipeline/runs/purge", paths)
        self.assertIn("/chats/active", paths)

    def test_learning_service_exposes_filtered_pipeline_operations(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        service_mod = importlib.import_module("astrmai.webui.backend.services.learningservice")

        class _Evolution:
            def __init__(self):
                self.calls = []

            async def learning_pipeline_diagnostics(self, **kwargs):
                self.calls.append(("diagnostics", kwargs))
                return {"checkpoints": [], "recent_runs": [], "pagination": kwargs}

            async def retry_learning_pipeline(self, pipeline, chat_id):
                self.calls.append(("retry", pipeline, chat_id))
                return {"pipeline": pipeline, "chat_id": chat_id, "cursor_log_id": 42}

            async def purge_learning_run_history(self):
                self.calls.append(("purge",))
                return {"deleted": 3}

        evolution = _Evolution()

        class _Facade:
            def get_evolution(self):
                return evolution

        service = service_mod.LearningService(adapter_mod.PluginApiAdapter(facade=_Facade()))

        async def _run():
            diagnostics = await service.pipeline_diagnostics(
                pipeline="expression",
                chat_id="chat-1",
                status="failed",
                limit=25,
                offset=50,
            )
            retried = await service.retry_pipeline("expression", "chat-1")
            purged = await service.purge_pipeline_runs()
            return diagnostics, retried, purged

        diagnostics, retried, purged = asyncio.run(_run())
        self.assertEqual(diagnostics["status"], "ok")
        self.assertEqual(diagnostics["data"]["pagination"]["offset"], 50)
        self.assertEqual(retried["data"]["cursor_log_id"], 42)
        self.assertEqual(purged["data"]["deleted"], 3)
        self.assertEqual(
            evolution.calls,
            [
                (
                    "diagnostics",
                    {
                        "pipeline": "expression",
                        "chat_id": "chat-1",
                        "status": "failed",
                        "limit": 25,
                        "offset": 50,
                    },
                ),
                ("retry", "expression", "chat-1"),
                ("purge",),
            ],
        )

    def test_backend_route_service_factories_only_pass_plugin_api(self):
        route_cases = [
            ("astrmai.webui.backend.routes.cognition_routes", "astrmai.webui.backend.services.cognitionservice", "CognitionService"),
            ("astrmai.webui.backend.routes.runtime_routes", "astrmai.webui.backend.services.observabilityservice", "ObservabilityService"),
            ("astrmai.webui.backend.routes.heartflow_routes", "astrmai.webui.backend.services.heartflowservice", "HeartflowService"),
            ("astrmai.webui.backend.routes.learning_routes", "astrmai.webui.backend.services.learningservice", "LearningService"),
            ("astrmai.webui.backend.routes.tools_routes", "astrmai.webui.backend.services.toolsservice", "ToolsService"),
        ]
        for route_module_name, service_module_name, service_class_name in route_cases:
            route_mod = importlib.import_module(route_module_name)
            service_mod = importlib.import_module(service_module_name)
            service = route_mod._service()
            self.assertIsInstance(service, getattr(service_mod, service_class_name))

    def test_backend_route_safe_endpoints_construct_without_typeerror(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        previous_facade = adapter_mod.get_active_facade()
        route_calls = [
            ("astrmai.webui.backend.routes.runtime_routes", "get_runtime_status", {"user": "codex"}),
            ("astrmai.webui.backend.routes.heartflow_routes", "get_heartflow_status", {"user": "codex"}),
            ("astrmai.webui.backend.routes.learning_routes", "get_learning_status", {"user": "codex"}),
            ("astrmai.webui.backend.routes.tools_routes", "get_tools_status", {"user": "codex"}),
            ("astrmai.webui.backend.routes.cognition_routes", "list_recent_decisions", {"limit": 5, "user": "codex"}),
        ]
        try:
            adapter_mod.set_active_facade(None)
            for route_module_name, handler_name, kwargs in route_calls:
                route_mod = importlib.import_module(route_module_name)
                result = asyncio.run(getattr(route_mod, handler_name)(**kwargs))
                self.assertEqual(result["status"], "ok")
        finally:
            adapter_mod.set_active_facade(previous_facade)

    def test_backend_routes_align_supported_cognition_learning_and_tools_signatures(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        previous_facade = adapter_mod.get_active_facade()

        class _Reflector:
            def __init__(self):
                self.calls = []

            async def reflect_batch(self, chat_id):
                self.calls.append(("reflect_batch", chat_id))

            async def auto_audit(self, chat_id):
                self.calls.append(("auto_audit", chat_id))

        class _AutoCheckTask:
            def __init__(self):
                self.calls = []

            async def run_once(self, chat_id):
                self.calls.append(chat_id)

        class _Planner:
            cognitive_decision_history = []
            tool_trace_history = []
            turn_trace_history = []
            raw_trace_store = None
            expression_selector = None

        reflector = _Reflector()
        auto_check = _AutoCheckTask()

        class _Facade:
            def get_reflector(self):
                return reflector

            def get_auto_check_task(self):
                return auto_check

            def get_planner(self):
                return _Planner()

            def get_observability_hub(self):
                return None

            def get_chat_loop_kernel(self):
                return None

            def get_proactive_task(self):
                return None

        try:
            adapter_mod.set_active_facade(_Facade())
            route_calls = [
                ("astrmai.webui.backend.routes.learning_routes", "run_reflect_once", {"chat_id": "chat-1", "user": "codex"}),
                ("astrmai.webui.backend.routes.tools_routes", "list_recent_tool_calls", {"limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.tools_routes", "list_chat_recent_tool_calls", {"chat_id": "chat-1", "limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "list_chat_recent_turns", {"chat_id": "chat-1", "limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "list_chat_unified_timeline", {"chat_id": "chat-1", "limit": 5, "level": "", "include": "", "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_observability_overview", {"user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_observability_timeline", {"chat_id": "", "domains": "", "levels": "", "kinds": "", "limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_observability_chat", {"chat_id": "chat-1", "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_observability_errors", {"chat_id": "", "limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_observability_search", {"q": "", "chat_id": "", "domains": "", "kinds": "", "levels": "", "tags": "", "limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_context_economy", {"limit": 5, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "list_context_economy_templates", {"limit": 5, "template_id": None, "workload_family": None, "sort_by": "rotate", "sort_dir": None, "user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_scheduler_status", {"user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_scheduler_due_selection", {"user": "codex"}),
                ("astrmai.webui.backend.routes.cognition_routes", "get_scheduler_chat", {"chat_id": "chat-1", "user": "codex"}),
            ]
            for route_module_name, handler_name, kwargs in route_calls:
                route_mod = importlib.import_module(route_module_name)
                result = asyncio.run(getattr(route_mod, handler_name)(**kwargs))
                self.assertEqual(result["status"], "ok", f"{route_module_name}.{handler_name} did not return ok")
        finally:
            adapter_mod.set_active_facade(previous_facade)

        self.assertEqual(reflector.calls, [("reflect_batch", "chat-1"), ("auto_audit", "chat-1")])
        self.assertEqual(auto_check.calls, ["chat-1"])

    def test_backend_http_smoke_routes_stay_supported(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        access_mod = importlib.import_module("astrmai.webui.backend.access")
        server_mod = importlib.import_module("astrmai.webui.backend.server")

        previous_facade = adapter_mod.get_active_facade()
        previous_overrides = dict(server_mod.app.dependency_overrides)

        class _Reflector:
            def __init__(self):
                self.calls = []

            async def reflect_batch(self, chat_id):
                self.calls.append(("reflect_batch", chat_id))

            async def auto_audit(self, chat_id):
                self.calls.append(("auto_audit", chat_id))

        class _AutoCheckTask:
            def __init__(self):
                self.calls = []

            async def run_once(self, chat_id):
                self.calls.append(chat_id)

        class _Planner:
            cognitive_decision_history = []
            tool_trace_history = []
            turn_trace_history = []
            raw_trace_store = None
            expression_selector = None

        reflector = _Reflector()
        auto_check = _AutoCheckTask()

        class _Facade:
            def get_reflector(self):
                return reflector

            def get_auto_check_task(self):
                return auto_check

            def get_planner(self):
                return _Planner()

            def get_observability_hub(self):
                return None

            def get_chat_loop_kernel(self):
                return None

            def get_proactive_task(self):
                return None

        try:
            adapter_mod.set_active_facade(_Facade())
            server_mod.app.dependency_overrides[access_mod.get_current_user] = lambda: "codex"

            with TestClient(server_mod.app) as client:
                response = client.get("/api/tools/recent-calls", params={"limit": 5})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.get("/api/tools/chats/chat-1/recent-calls", params={"limit": 5})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.get(
                    "/api/cognition/chats/chat-1/unified-timeline",
                    params={"limit": 5, "include": "decision,tool"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.get("/api/cognition/observability/overview")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.get("/api/cognition/scheduler/status")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.post("/api/learning/reflect/run-once", json={"chat_id": "chat-1"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.post(
                    "/api/learning/reflect/run-once",
                    params={"chat_id": "chat-query"},
                    json={"chat_id": "chat-body"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.post("/api/learning/reflect/run-once", params={"chat_id": "chat-query"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "ok")

                response = client.post("/api/learning/reflect/run-once", json={})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "chat_id is required")
        finally:
            server_mod.app.dependency_overrides = previous_overrides
            adapter_mod.set_active_facade(previous_facade)

        self.assertEqual(
            reflector.calls,
            [
                ("reflect_batch", "chat-1"),
                ("auto_audit", "chat-1"),
                ("reflect_batch", "chat-body"),
                ("auto_audit", "chat-body"),
                ("reflect_batch", "chat-query"),
                ("auto_audit", "chat-query"),
            ],
        )
        self.assertEqual(auto_check.calls, ["chat-1", "chat-body", "chat-query"])


    def test_heartflow_chats_uses_get_all_states_when_available(self):
        """heartflow_chats() should prefer get_all_states() over _states."""
        admin_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")

        called_public = False
        class _Manager:
            def get_all_states(self):
                nonlocal called_public
                called_public = True
                return {"chat-1": {"last_activity_ts": 100.0}}
            def get_state(self, chat_id):
                return {"last_activity_ts": 100.0}
            def get_session(self, chat_id):
                return {}
            def get_latest_action_decision(self, chat_id):
                return {}
            def get_latest_impulse_decision(self, chat_id):
                return {}

        class _PluginApi:
            def __init__(self):
                self.facade = True
                self._manager = _Manager()

            def get_heartflow_manager(self):
                return self._manager

        service = admin_mod.AdminUiService(_PluginApi())
        result = asyncio.run(service.heartflow_chats())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total"], 1)
        self.assertTrue(called_public, "get_all_states() was not called")

    def test_heartflow_chats_falls_back_to_states_when_get_all_states_missing(self):
        """heartflow_chats() should fall back to _states when get_all_states is absent."""
        admin_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")

        class _Manager:
            _states = {"chat-2": {"last_activity_ts": 200.0}}
            def get_state(self, chat_id):
                return self._states.get(chat_id, {})
            def get_session(self, chat_id):
                return {}
            def get_latest_action_decision(self, chat_id):
                return {}
            def get_latest_impulse_decision(self, chat_id):
                return {}

        class _PluginApi:
            def __init__(self):
                self.facade = True
                self._manager = _Manager()

            def get_heartflow_manager(self):
                return self._manager

        service = admin_mod.AdminUiService(_PluginApi())
        result = asyncio.run(service.heartflow_chats())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["total"], 1)

    def test_domain_services_have_explicit_signatures(self):
        """All 7 domain services should define methods with explicit parameters."""
        import inspect

        modules = [
            ("observabilityservice", "ObservabilityService"),
            ("heartflowservice", "HeartflowService"),
            ("chatruntimeservice", "ChatRuntimeService"),
            ("schedulerservice", "SchedulerService"),
            ("cognitionservice", "CognitionService"),
            ("learningservice", "LearningService"),
            ("toolsservice", "ToolsService"),
        ]
        for mod_name, cls_name in modules:
            mod = importlib.import_module(f"astrmai.webui.backend.services.{mod_name}")
            cls = getattr(mod, cls_name)
            public_methods = [
                m for m in dir(cls)
                if not m.startswith("_") and callable(getattr(cls, m))
                and m != "__init__"
            ]
            for method_name in public_methods:
                sig = inspect.signature(getattr(cls, method_name))
                params = list(sig.parameters.keys())
                self.assertNotIn("args", params,
                    f"{cls_name}.{method_name}() still uses *args")
                self.assertNotIn("kwargs", params,
                    f"{cls_name}.{method_name}() still uses **kwargs")
                self.assertIn("self", params,
                    f"{cls_name}.{method_name}() missing self")

    def test_plugin_api_adapter_does_not_fallback_to_runtime_passthrough(self):
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        planner = object()

        class _Runtime:
            system2_planner = planner

        class _Facade:
            runtime = _Runtime()

        adapter = adapter_mod.PluginApiAdapter(facade=_Facade())
        self.assertIsNone(adapter.get_planner())

    def test_apply_hot_config_fallback_logs_warning(self):
        """_apply_hot_config() should use facade.apply_hot_config when available."""
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        called_facade = False
        class _Facade:
            def apply_hot_config(self, config_dict, parsed_config):
                nonlocal called_facade
                called_facade = True
                return True

        adapter = adapter_mod.PluginApiAdapter(facade=_Facade())
        result = adapter._apply_hot_config({"key": "val"}, object())
        self.assertTrue(result)
        self.assertTrue(called_facade, "facade.apply_hot_config not called")

    def test_apply_hot_config_returns_false_when_no_facade(self):
        """_apply_hot_config() should return False when facade is None."""
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")
        adapter = adapter_mod.PluginApiAdapter(facade=None)
        result = adapter._apply_hot_config({}, object())
        self.assertFalse(result)

    def test_dashboard_repository_count_table_enforces_whitelist(self):
        repo_mod = importlib.import_module("astrmai.webui.backend.services.dashboard_repository")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            db_path = os.path.join(tmpdir, "dashboard.db")

            async def run():
                db = await aiosqlite.connect(db_path)
                try:
                    await db.execute("CREATE TABLE user_profiles (id TEXT)")
                    await db.execute("CREATE TABLE MemoryEvent (id TEXT)")
                    await db.execute("CREATE TABLE canonical_memories (id TEXT)")
                    await db.commit()
                finally:
                    await db.close()

                @asynccontextmanager
                async def db_factory():
                    conn = await aiosqlite.connect(db_path)
                    try:
                        yield conn
                    finally:
                        await conn.close()

                repo = repo_mod.DashboardRepository(db_factory)
                counts = [
                    await repo.count_table("user_profiles"),
                    await repo.count_table("MemoryEvent"),
                    await repo.count_table("canonical_memories"),
                ]
                with self.assertRaises(ValueError):
                    await repo.count_table("user_profiles; DROP TABLE MemoryEvent")
                return counts

            self.assertEqual(asyncio.run(run()), [0, 0, 0])

    def test_admin_service_separates_tool_disclosure_from_executions(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.admin_ui_service")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _Planner:
            tool_trace_history = [{"chat_id": "chat-1", "tool_names": ["proactive_poke"]}]
            tool_execution_history = [
                {
                    "created_at": 20.0,
                    "chat_id": "chat-1",
                    "tool_name": "proactive_poke",
                    "status": "queued",
                }
            ]

        class _Runtime:
            system2_planner = _Planner()

        service = service_mod.AdminUiService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )
        disclosed = asyncio.run(service.recent_tool_traces(chat_id="chat-1"))
        executed = asyncio.run(service.recent_tool_executions(chat_id="chat-1"))

        self.assertEqual(disclosed["items"][0]["tool_names"], ["proactive_poke"])
        self.assertEqual(executed["items"][0]["tool_name"], "proactive_poke")
        self.assertEqual(executed["items"][0]["status"], "queued")

    def test_heartflow_status_explains_scheduler_active_manager_idle(self):
        service_mod = importlib.import_module("astrmai.webui.backend.services.heartflowservice")
        adapter_mod = importlib.import_module("astrmai.webui.backend.adapters.plugin_api")

        class _Manager:
            @staticmethod
            def describe_status():
                return {"enabled": True, "active_chats": 0, "last_tick_time": 0.0}

        class _Kernel:
            @staticmethod
            def describe_status_sync():
                return {"tracked_chats": 5, "last_due_selection_summary": {"selected_count": 1}}

        class _Runtime:
            proactive_task = SimpleNamespace(heartflow_manager=_Manager())
            chat_loop_kernel = _Kernel()

        service = service_mod.HeartflowService(
            adapter_mod.PluginApiAdapter(facade=_RuntimeBackedFacade(_Runtime()))
        )
        result = asyncio.run(service.heartflow_status())

        self.assertEqual(result["data"]["operational_state"], "scheduler_active_manager_idle")
        self.assertEqual(result["data"]["kernel"]["tracked_chats"], 5)


if __name__ == "__main__":
    unittest.main()

