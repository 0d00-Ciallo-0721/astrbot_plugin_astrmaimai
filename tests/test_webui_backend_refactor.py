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
        auth_mod = importlib.import_module("astrmai.webui.backend.auth")
        original = os.environ.get("ASTRMAI_WEBUI_SECRET")
        try:
            os.environ["ASTRMAI_WEBUI_SECRET"] = "secret-one"
            token = auth_mod.create_token("codex")
            self.assertEqual(auth_mod.verify_token(token)["sub"], "codex")

            os.environ["ASTRMAI_WEBUI_SECRET"] = "secret-two"
            with self.assertRaises(Exception):
                auth_mod.verify_token(token)
        finally:
            if original is None:
                os.environ.pop("ASTRMAI_WEBUI_SECRET", None)
            else:
                os.environ["ASTRMAI_WEBUI_SECRET"] = original

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

                class _Facade:
                    runtime = _Runtime()

                    def get_runtime_diagnostics(self):
                        return self.runtime.build_diagnostics()

                    async def get_capability_overview(self):
                        return self.runtime.build_capability_overview_sync()

                service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
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

        class _Facade:
            runtime = _Runtime()

        service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
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

        class _Facade:
            runtime = _Runtime()

        service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
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

        class _Facade:
            runtime = _Runtime()

        service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))

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

    def test_dashboard_cognition_tab_renders_context_economy_panel(self):
        root = Path(__file__).resolve().parents[1]
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

        class _Facade:
            runtime = _Runtime()

        service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
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

        class _Facade:
            runtime = _Runtime()

        service = service_mod.AdminUiService(adapter_mod.PluginApiAdapter(facade=_Facade()))
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
        self.assertIn("/cognition/scheduler/status", paths)
        self.assertIn("/cognition/scheduler/due-selection", paths)
        self.assertIn("/cognition/scheduler/chats/{chat_id}", paths)
        self.assertIn("/tools/status", paths)
        self.assertIn("/memory-feedback", paths)
        self.assertIn("/proactive/status", paths)
        self.assertIn("/learning/status", paths)
        self.assertIn("/chats/active", paths)


if __name__ == "__main__":
    unittest.main()
