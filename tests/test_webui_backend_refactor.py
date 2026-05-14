import asyncio
import importlib
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import aiosqlite


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
                if "UserProfile" in query:
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

    def test_server_mounts_aggregated_api_router(self):
        from pathlib import Path

        path = Path(__file__).resolve().parents[1] / "astrmai" / "webui" / "backend" / "server.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn("from .routes import api_router", content)
        self.assertIn("app.include_router(api_router, prefix=\"/api\")", content)

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
        self.assertTrue(legacy_rows[0]["legacy"])
        self.assertEqual(legacy_rows[0]["canonical_id"], "mem-ui-1")
        self.assertEqual(legacy_delete["mode"], "canonical_soft_delete")

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
                approved = await service.approve_jargon("mem-jargon-1")
                active = await service.list_jargon(status="active", group_id="group-1", query="bigbird")
                rejected = await service.reject_jargon("mem-jargon-1")
                final_detail = await service.get_canonical("mem-jargon-1")
                return pending, approved, active, rejected, final_detail

            pending, approved, active, rejected, final_detail = asyncio.run(_run())

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["legacy_jargon_id"], 7)
        self.assertEqual(approved["status"], "ok")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["status"], "active")
        self.assertEqual(active[0]["scene"], "raid call")
        self.assertEqual(pending[0]["review_reason"], "needs more evidence")
        self.assertEqual(pending[0]["review_suggestion"], "confirm whether it is boss shorthand")
        self.assertEqual(rejected["status"], "ok")
        self.assertEqual(final_detail["data"]["status"], "rejected")
        self.assertEqual(final_detail["data"]["metadata"]["review_status"], "rejected")

    def test_memory_route_file_exposes_jargon_review_endpoints(self):
        path = Path(__file__).resolve().parents[1] / "astrmai" / "webui" / "backend" / "routes" / "memory_routes.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn('@router.post("/jargon/{id}/approve")', content)
        self.assertIn('@router.post("/jargon/{id}/reject")', content)

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

        class _Runtime:
            def __init__(self):
                self.raw_config = {}
                self.config = None
                self.rebuilt = False

            def rebuild_infrastructure_settings(self):
                self.rebuilt = True

        class _Facade:
            def __init__(self):
                self.runtime = _Runtime()

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
            self.assertTrue(adapter.facade.runtime.rebuilt)

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

        class _Facade:
            runtime = _Runtime()

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
                facade=_Facade(),
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

        class _Planner:
            cognitive_decision_history = [{"chat_id": "chat-1", "social_intent": "join"}]
            tool_trace_history = [{"chat_id": "chat-1", "tool_tier": "chat", "tool_count": 2}]
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

        class _Facade:
            runtime = _Runtime()

            def get_runtime_diagnostics(self):
                return self.runtime.build_diagnostics()

            async def get_capability_overview(self):
                return self.runtime.build_capability_overview_sync()

        service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
        status = asyncio.run(service.runtime_status())
        self.assertTrue(status["runtime_bound"])
        decisions = asyncio.run(service.recent_decisions(chat_id="chat-1"))
        self.assertEqual(decisions["items"][0]["social_intent"], "join")
        tools = asyncio.run(service.recent_tool_traces(chat_id="chat-1"))
        self.assertEqual(tools["items"][0]["tool_tier"], "chat")
        turns = asyncio.run(service.recent_turn_traces(chat_id="chat-1"))
        self.assertEqual(turns["items"][0]["tools"]["final_tier"], "chat")
        impulses = asyncio.run(service.heartflow_impulses(chat_id="chat-1"))
        self.assertTrue(impulses["items"][0]["visible_candidate_allowed"])
        timeline = asyncio.run(service.heartflow_timeline(chat_id="chat-1"))
        self.assertEqual(timeline["items"][0]["kind"], "action")
        digests = asyncio.run(service.heartflow_topic_digests())
        self.assertEqual(digests["items"][0]["source"], "heartflow_topic_digest")

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
        self.assertIn("/tools/status", paths)
        self.assertIn("/memory-feedback", paths)
        self.assertIn("/proactive/status", paths)
        self.assertIn("/learning/status", paths)
        self.assertIn("/chats/active", paths)


if __name__ == "__main__":
    unittest.main()
