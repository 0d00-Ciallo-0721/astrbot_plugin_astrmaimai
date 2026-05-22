import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
import unittest

import aiosqlite

from tests.helpers import install_astrbot_stubs


class _FakeEvent:
    def __init__(self, text="remember Alice"):
        self.message_str = text
        self.unified_msg_origin = "chat-1"
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "sender-1"


class _FakeThinkEvent(_FakeEvent):
    def __init__(self, text="今天适合散步", think_level=None):
        super().__init__(text)
        if think_level is not None:
            self.set_extra("astrmai_think_level", think_level)


class MemoryV2ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.memory.services.memory_") or name.endswith("v2_store"):
                sys.modules.pop(name, None)
        self.contracts = importlib.import_module("astrmai.memory.contracts.memory_query")
        self.store_mod = importlib.import_module("astrmai.memory.services.v2_store")
        self.retrieval_mod = importlib.import_module("astrmai.memory.services.memory_retrieval_service")
        self.write_mod = importlib.import_module("astrmai.memory.services.memory_write_service")
        self.injection_mod = importlib.import_module("astrmai.memory.services.memory_injection_service")
        self.tool_mod = importlib.import_module("astrmai.memory.services.memory_tool_service")
        self.maintenance_mod = importlib.import_module("astrmai.memory.services.memory_maintenance_service")
        self.migration_mod = importlib.import_module("astrmai.memory.services.memory_migration_service")
        self.projector_mod = importlib.import_module("astrmai.memory.services.memory_index_projector")
        self.expression_mod = importlib.import_module("astrmai.memory.services.expression_pattern_service")
        self.db_path = os.path.join(self.temp_dir.name, "docs.db")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _services(self):
        store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
        retrieval = self.retrieval_mod.MemoryRetrievalService(store)
        writer = self.write_mod.MemoryWriteService(store)
        injection = self.injection_mod.MemoryInjectionService(retrieval)
        tools = self.tool_mod.MemoryToolService(retrieval)
        maintenance = self.maintenance_mod.MemoryMaintenanceService(store)
        return store, retrieval, writer, injection, tools, maintenance

    def test_query_filters_layers_excludes_and_stale_by_default(self):
        async def run():
            store, retrieval, writer, _injection, _tools, _maintenance = self._services()
            request = self.contracts.MemoryWriteRequest(
                source="summary",
                kind="event",
                session_id="chat-1",
                content="Alice likes deterministic memory planning.",
                dedup_key="event:alice",
            )
            first_id = await writer.write(request)
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="profile",
                    kind="profile",
                    session_id="chat-1",
                    content="Alice profile note should be filtered by layer.",
                    dedup_key="profile:alice",
                )
            )
            await store.soft_delete(first_id, reason="unit")

            query = self.contracts.MemoryQuery(query="Alice", session_id="chat-1", layers=["event"])
            self.assertEqual(await retrieval.retrieve(query), [])

            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="event",
                    session_id="chat-1",
                    content="Alice returned to active deterministic memory.",
                    dedup_key="event:alice-2",
                )
            )
            excluded = await retrieval.retrieve(
                self.contracts.MemoryQuery(query="Alice", session_id="chat-1", layers=["event"])
            )
            self.assertEqual(len(excluded), 1)
            query.exclude_ids = [excluded[0].id]
            self.assertEqual(await retrieval.retrieve(query), [])

        asyncio.run(run())

    def test_eav_fact_newer_write_supersedes_older_fact(self):
        async def run():
            store, retrieval, writer, _injection, _tools, _maintenance = self._services()
            older_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="authority_backfill",
                    kind="fact",
                    session_id="chat-1",
                    sender_id="zlj",
                    content="目前有2台服务器",
                    summary="2台服务器",
                    dedup_key="zlj:asset:server_count",
                    metadata={"authority_eav": True, "promotion_entity": "asset", "promotion_attribute": "server_count", "promotion_value": "2"},
                    created_at=1000.0,
                )
            )
            newer_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="authority_backfill",
                    kind="fact",
                    session_id="chat-1",
                    sender_id="zlj",
                    content="目前升级到3台服务器了",
                    summary="3台服务器",
                    dedup_key="zlj:asset:server_count",
                    metadata={"authority_eav": True, "promotion_entity": "asset", "promotion_attribute": "server_count", "promotion_value": "3"},
                    created_at=2000.0,
                )
            )
            older = await store.get_canonical(older_id, include_inactive=True)
            newer = await store.get_canonical(newer_id, include_inactive=True)
            self.assertEqual(older.status, "superseded")
            self.assertEqual(older.superseded_by, newer_id)
            self.assertEqual(newer.status, "active")
            active = await store.get_by_dedup_key("zlj:asset:server_count", include_inactive=False)
            self.assertIsNotNone(active)
            self.assertEqual(active.id, newer_id)
            self.assertEqual(active.status, "active")
            self.assertNotEqual(active.id, older_id)

        asyncio.run(run())

    def test_eav_fact_older_backfill_is_immediately_superseded(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            active_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="authority_backfill",
                    kind="fact",
                    session_id="chat-1",
                    sender_id="zlj",
                    content="目前升级到3台服务器了",
                    summary="3台服务器",
                    dedup_key="zlj:asset:server_count",
                    metadata={"authority_eav": True, "promotion_entity": "asset", "promotion_attribute": "server_count", "promotion_value": "3"},
                    created_at=3000.0,
                )
            )
            late_old_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="authority_backfill",
                    kind="fact",
                    session_id="chat-1",
                    sender_id="zlj",
                    content="其实之前只有2台服务器",
                    summary="2台服务器",
                    dedup_key="zlj:asset:server_count",
                    metadata={"authority_eav": True, "promotion_entity": "asset", "promotion_attribute": "server_count", "promotion_value": "2"},
                    created_at=1000.0,
                )
            )
            active = await store.get_canonical(active_id, include_inactive=True)
            late_old = await store.get_canonical(late_old_id, include_inactive=True)
            self.assertEqual(active.status, "active")
            self.assertEqual(late_old.status, "superseded")
            self.assertEqual(late_old.superseded_by, active_id)

        asyncio.run(run())

    def test_injection_trace_is_recorded_and_tool_excludes_injected_ids(self):
        async def run():
            _store, _retrieval, writer, injection, tools, _maintenance = self._services()
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="event",
                    session_id="chat-1",
                    content="Alice likes blue notebooks.",
                    dedup_key="event:notebook",
                )
            )
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="event",
                    session_id="chat-1",
                    content="Alice likes green bookmarks.",
                    dedup_key="event:bookmark",
                )
            )
            event = _FakeEvent("remember Alice")
            event.set_extra("astrmai_think_level", 2)
            bundle = await injection.build_bundle(event=event, prompt="Alice")
            self.assertTrue(bundle.rendered_prompt_block)
            injected_ids = event.get_extra("astrmai_memory_injection_trace").selected_ids
            self.assertTrue(injected_ids)

            result = await tools.search_memory(query="Alice", session_id="chat-1", event=event)
            self.assertEqual(result.already_injected_ids, injected_ids)
            self.assertFalse(set(injected_ids) & {item.id for item in result.items})

        asyncio.run(run())

    def test_jargon_auto_injection_flows_through_main_memory_bundle(self):
        async def run():
            _store, _retrieval, writer, injection, tools, _maintenance = self._services()
            jargon_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="chat-1",
                    content="bigbird",
                    summary="a raid boss nickname",
                    confidence=0.92,
                    metadata={
                        "meaning": "a raid boss nickname",
                        "scene": "raid call",
                        "review_status": "active",
                    },
                    dedup_key="jargon:chat-1:bigbird",
                    status="active",
                    visibility="auto_and_tool",
                )
            )
            event = _FakeEvent("remember bigbird")
            event.set_extra("astrmai_think_level", 2)
            bundle = await injection.build_bundle(event=event, prompt="bigbird")
            self.assertIn("[jargon]", bundle.rendered_prompt_block)
            self.assertIn("bigbird -> a raid boss nickname (scene: raid call)", bundle.rendered_prompt_block)
            trace = event.get_extra("astrmai_memory_injection_trace")
            self.assertIn("jargon", trace.layers)
            self.assertIn(jargon_id, trace.selected_ids)

            result = await tools.search_memory(
                query="bigbird",
                session_id="chat-1",
                layers=["jargon"],
                event=event,
            )
            self.assertEqual(result.already_injected_ids, trace.selected_ids)
            self.assertEqual(result.items, [])

        asyncio.run(run())

    def test_self_lore_query_uses_query_and_persona_filters(self):
        async def run():
            _store, _retrieval, writer, _injection, tools, _maintenance = self._services()
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="persona_lore",
                    kind="persona_lore",
                    session_id="__self_lore__",
                    persona_id="persona-a",
                    content="I keep a gentle, concise voice.",
                    dedup_key="lore:a",
                )
            )
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="persona_lore",
                    kind="persona_lore",
                    session_id="__self_lore__",
                    persona_id="persona-b",
                    content="I prefer loud ceremonial speeches.",
                    dedup_key="lore:b",
                )
            )
            result = await tools.self_lore_query(query="gentle voice", persona_id="persona-a")
            self.assertEqual(len(result.items), 1)
            self.assertEqual(result.items[0].persona_id, "persona-a")
            self.assertIn("gentle", result.items[0].content)

        asyncio.run(run())

    def test_maintenance_marks_stale_restores_on_access_then_deletes_after_grace(self):
        async def run():
            store, retrieval, writer, _injection, _tools, maintenance = self._services()
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="event",
                    session_id="chat-1",
                    content="Alice stale memory test.",
                    dedup_key="event:stale",
                )
            )
            deleted = await maintenance.apply_daily_decay(decay_rate=1.0, min_score=0.2)
            self.assertEqual(deleted, 0)
            self.assertEqual(
                await retrieval.retrieve(self.contracts.MemoryQuery(query="Alice", session_id="chat-1")),
                [],
            )
            stale = await retrieval.retrieve(
                self.contracts.MemoryQuery(query="Alice", session_id="chat-1", allow_stale=True)
            )
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0].status, "stale")

            restored = await retrieval.retrieve(
                self.contracts.MemoryQuery(query="Alice", session_id="chat-1", allow_stale=True)
            )
            self.assertEqual(restored[0].status, "active")

            await store.soft_delete(restored[0].id, reason="reset")
            async with aiosqlite.connect(self.db_path) as db:
                old_time = time.time() - 8 * 86400
                await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'stale', last_access_time = ?, create_time = ?
                    WHERE id = ?
                    """,
                    (old_time, old_time, restored[0].id),
                )
                await db.commit()
            deleted = await maintenance.apply_daily_decay(decay_rate=0.0, stale_grace_seconds=7 * 86400)
            self.assertEqual(deleted, 1)

        asyncio.run(run())

    def test_store_concurrent_same_session_writes_do_not_raise_database_locked(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            memory_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice concurrent lock test.",
                    dedup_key="lock:chat-1",
                )
            )

            async def _update():
                return await store.update_memory(memory_id, summary="updated summary")

            async def _delete():
                return await store.soft_delete(memory_id, reason="concurrent-test")

            results = await asyncio.gather(_update(), _delete(), return_exceptions=True)
            candidate = await store.get_canonical(memory_id, include_inactive=True)
            return results, candidate

        results, candidate = asyncio.run(run())
        self.assertFalse(any(isinstance(item, Exception) for item in results))
        self.assertIsNotNone(candidate)
        self.assertIn(candidate.status, {"active", "deleted"})

    def test_schema_migration_imports_legacy_documents_once(self):
        async def run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
                await db.execute(
                    "INSERT INTO documents(id, text, metadata) VALUES (1, ?, ?)",
                    (
                        "Alice legacy document memory.",
                        '{"session_id":"chat-1","importance":0.7,"source":"legacy"}',
                    ),
                )
                await db.commit()
            store, retrieval, _writer, _injection, _tools, _maintenance = self._services()
            await store.initialize()
            self.assertEqual(await store.import_legacy_documents(), 1)
            self.assertEqual(await store.import_legacy_documents(), 0)
            rows = await retrieval.retrieve(self.contracts.MemoryQuery(query="legacy Alice", session_id="chat-1"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].metadata["legacy_doc_id"], 1)

        asyncio.run(run())

    def test_visibility_separates_auto_and_tool_retrieval(self):
        async def run():
            _store, retrieval, writer, _injection, tools, _maintenance = self._services()
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="admin",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice tool only note.",
                    visibility="tool_only",
                )
            )
            auto_rows = await retrieval.retrieve(
                self.contracts.MemoryQuery(
                    query="Alice",
                    session_id="chat-1",
                    metadata={"visibility_mode": "auto"},
                )
            )
            tool_rows = await tools.search_memory(query="Alice", session_id="chat-1")
            self.assertEqual(auto_rows, [])
            self.assertEqual(len(tool_rows.items), 1)
            self.assertEqual(tool_rows.items[0].visibility, "tool_only")

        asyncio.run(run())

    def test_review_pending_jargon_is_hidden_from_default_retrieval_until_approved(self):
        async def run():
            store, retrieval, writer, _injection, tools, _maintenance = self._services()
            pending_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="chat-1",
                    content="bigbird",
                    summary="a raid boss nickname",
                    confidence=0.55,
                    metadata={"meaning": "a raid boss nickname", "review_status": "review_pending"},
                    dedup_key="jargon:chat-1:bigbird",
                    status="review_pending",
                    visibility="maintenance_only",
                )
            )
            rows = await retrieval.retrieve(
                self.contracts.MemoryQuery(
                    query="bigbird",
                    session_id="chat-1",
                    layers=["jargon"],
                    intent="jargon",
                )
            )
            self.assertEqual(rows, [])
            await store.update_memory(
                pending_id,
                status="active",
                visibility="auto_and_tool",
                metadata={"meaning": "a raid boss nickname", "review_status": "active"},
            )
            rows = await tools.search_memory(
                query="bigbird",
                session_id="chat-1",
                layers=["jargon"],
            )
            self.assertEqual(len(rows.items), 1)
            self.assertEqual(rows.items[0].kind, "jargon")

        asyncio.run(run())

    def test_projector_rebuilds_without_duplicate_canonical_projection(self):
        class _FakeRetriever:
            def __init__(self):
                self.added = []

            async def add_memory(self, content, metadata):
                self.added.append((content, metadata))
                return len(self.added)

        class _FakeEngine:
            def __init__(self, store):
                self.v2_store = store
                self.retriever = _FakeRetriever()
                self.deleted = []
                self.ready_calls = 0

            async def _ensure_faiss_initialized(self):
                self.ready_calls += 1
                return True

            def _build_memory_metadata(self, **kwargs):
                return dict(kwargs)

            async def _run_documents_query(self, query, params=()):
                return [(len(self.deleted) + 1,)]

            async def _execute_documents_write(self, query, params=()):
                self.deleted.append((query, params))
                return 1

        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            memory_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice projection rebuild memory.",
                )
            )
            engine = _FakeEngine(store)
            projector = self.projector_mod.MemoryIndexProjector(engine)
            await projector.project(memory_id)
            await projector.project(memory_id)
            rebuilt = await projector.rebuild_session("chat-1")
            self.assertEqual(rebuilt, 1)
            self.assertEqual(len(engine.retriever.added), 3)
            self.assertTrue(all(item[1]["canonical_id"] == memory_id for item in engine.retriever.added))
            self.assertGreaterEqual(len(engine.deleted), 3)

        asyncio.run(run())

    def test_hybrid_projection_fallback_must_pass_canonical_status_check(self):
        class _Result:
            content = "Alice deleted projected memory."
            score = 1.0

            def __init__(self, canonical_id):
                self.metadata = {"canonical_id": canonical_id, "session_id": "chat-1"}

        class _Engine:
            def __init__(self, canonical_id):
                self.canonical_id = canonical_id

            async def _search_memories(self, *args, **kwargs):
                return [_Result(self.canonical_id)]

        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            writer = self.write_mod.MemoryWriteService(store)
            memory_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice deleted projected memory.",
                )
            )
            await store.soft_delete(memory_id, reason="unit")
            retrieval = self.retrieval_mod.MemoryRetrievalService(store, engine=_Engine(memory_id))
            rows = await retrieval.retrieve(self.contracts.MemoryQuery(query="Alice", session_id="chat-1"))
            self.assertEqual(rows, [])

        asyncio.run(run())

    def test_instant_memory_gate_writes_directly_to_canonical_store(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            gate_mod = importlib.import_module("astrmai.memory.services.instant_memory_gate")

            class _Engine:
                def __init__(self, write_service):
                    self.write_service = write_service

            class _Config:
                class memory:
                    cleanup_interval = 3600
                    summary_threshold = 30

            turn = self.contracts.CommittedMemoryTurn(
                turn_id="turn-1",
                chat_id="chat-1",
                user_text="我叫小明",
                assistant_text="好的",
                source="test",
            )
            gate = gate_mod.InstantMemoryGate(
                gateway=type("G", (), {"config": _Config(), "context": type("GC", (), {})()})(),
                engine=_Engine(writer),
                config=_Config(),
            )
            result = await gate.process_committed_turn(turn)
            rows = await store.list_candidates(session_id="chat-1", kinds=["fact"], limit=10)
            self.assertTrue(result.hit)
            self.assertTrue(any("小明" in item.summary or "小明" in item.content for item in rows))

        asyncio.run(run())

    def test_instant_memory_llm_backfill_uses_runtime_think_level_signal(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            gate_mod = importlib.import_module("astrmai.memory.services.instant_memory_gate")

            class _Engine:
                def __init__(self, write_service):
                    self.write_service = write_service

            class _Config:
                class memory:
                    cleanup_interval = 3600
                    summary_threshold = 30

            class _Gateway:
                def __init__(self):
                    self.calls = 0
                    self.event = _FakeThinkEvent(think_level=2)
                    self.context = type("GC", (), {"event": self.event})()
                    self.config = _Config()

                async def call_data_process_task(self, *args, **kwargs):
                    self.calls += 1
                    return {"worth": True, "fact": "用户想在周末去植物园散步"}

            gateway = _Gateway()
            gate = gate_mod.InstantMemoryGate(
                gateway=gateway,
                engine=_Engine(writer),
                config=_Config(),
            )
            turn = self.contracts.CommittedMemoryTurn(
                turn_id="turn-2",
                chat_id="chat-1",
                user_text="今天适合散步，我们周末去植物园吧",
                assistant_text="好呀",
                source="test",
                think_level=2,
            )
            result = await gate.run_llm_backfill(turn)
            rows = await store.list_candidates(session_id="chat-1", kinds=["fact"], limit=10)
            self.assertEqual(gateway.calls, 1)
            self.assertTrue(result.hit)
            llm_rows = [item for item in rows if item.source == "instant_gate_llm"]
            self.assertEqual(len(llm_rows), 1)
            self.assertEqual(llm_rows[0].summary, "用户想在周末去植物园散步")
            self.assertIsNotNone(await store.get_by_dedup_key("instant_gate_llm:chat-1:用户想在周末去植物园散步"))

        asyncio.run(run())

    def test_instant_memory_llm_backfill_respects_threshold_and_cooldown(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            gate_mod = importlib.import_module("astrmai.memory.services.instant_memory_gate")

            class _Engine:
                def __init__(self, write_service):
                    self.write_service = write_service

            class _Config:
                class memory:
                    cleanup_interval = 3600
                    summary_threshold = 30

            class _Gateway:
                def __init__(self, think_level):
                    self.calls = 0
                    self.event = _FakeThinkEvent(think_level=think_level)
                    self.context = type("GC", (), {"event": self.event})()
                    self.config = _Config()

                async def call_data_process_task(self, *args, **kwargs):
                    self.calls += 1
                    return {"worth": True, "fact": "用户想找一家周末营业的咖啡馆"}

            low_gateway = _Gateway(think_level=1)
            low_gate = gate_mod.InstantMemoryGate(
                gateway=low_gateway,
                engine=_Engine(writer),
                config=_Config(),
            )
            low_turn = self.contracts.CommittedMemoryTurn(
                turn_id="turn-3",
                chat_id="chat-1",
                user_text="今天风有点大，想找个地方坐坐",
                assistant_text="明白",
                source="test",
                think_level=1,
            )
            self.assertFalse(low_gate.should_run_llm_backfill(low_turn, session_rounds=0, last_check=0.0, now=100.0))
            rows = await store.list_candidates(session_id="chat-1", kinds=["fact"], limit=10)
            self.assertEqual(low_gateway.calls, 0)
            self.assertEqual([item for item in rows if item.source == "instant_gate_llm"], [])

            high_gateway = _Gateway(think_level=3)
            high_gate = gate_mod.InstantMemoryGate(
                gateway=high_gateway,
                engine=_Engine(writer),
                config=_Config(),
            )
            first_turn = self.contracts.CommittedMemoryTurn(
                turn_id="turn-4",
                chat_id="chat-2",
                user_text="今天风有点大，想找个地方坐坐",
                assistant_text="明白",
                source="test",
                think_level=3,
            )
            second_turn = self.contracts.CommittedMemoryTurn(
                turn_id="turn-5",
                chat_id="chat-2",
                user_text="要不顺便看看附近的咖啡馆",
                assistant_text="好",
                source="test",
                think_level=3,
            )
            await high_gate.run_llm_backfill(first_turn)
            self.assertFalse(high_gate.should_run_llm_backfill(second_turn, session_rounds=5, last_check=100.0, now=101.0))
            rows = await store.list_candidates(session_id="chat-2", kinds=["fact"], limit=10)
            self.assertEqual(high_gateway.calls, 1)
            llm_rows = [item for item in rows if item.source == "instant_gate_llm"]
            self.assertEqual(len(llm_rows), 1)

        asyncio.run(run())

    def test_search_prefers_fts_and_basic_terms_still_work(self):
        async def run():
            store, retrieval, writer, _injection, _tools, _maintenance = self._services()
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice likes green bookmarks very much.",
                    dedup_key="fts:green",
                )
            )
            rows = await retrieval.retrieve(self.contracts.MemoryQuery(query="green bookmarks", session_id="chat-1"))
            self.assertEqual(len(rows), 1)
            self.assertIn("green bookmarks", rows[0].content)
            raw = await store.search("green bookmarks", session_id="chat-1", top_k=1)
            self.assertEqual(len(raw), 1)

        asyncio.run(run())

    def test_deep_retrieval_reranks_and_attaches_guidance(self):
        class _Gateway:
            def __init__(self):
                self.calls = 0

            async def call_data_process_task(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"queries": ["green bookmark", "Alice bookmark"]}
                if self.calls == 2:
                    return {"ids": [self.target_id]}
                return {"guidance": "Prefer the bookmark memory when answering."}

        class _Engine:
            def __init__(self, gateway):
                self.gateway = gateway

        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            first_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice likes blue notebooks.",
                    dedup_key="deep:blue",
                )
            )
            target_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Alice likes green bookmarks.",
                    dedup_key="deep:green",
                )
            )
            gateway = _Gateway()
            gateway.target_id = target_id
            retrieval = self.retrieval_mod.MemoryRetrievalService(store, engine=_Engine(gateway))
            rows = await retrieval.retrieve(
                self.contracts.MemoryQuery(query="Alice", session_id="chat-1", policy="deep", top_k=2)
            )
            self.assertEqual(rows[0].id, target_id)
            self.assertIn("bookmark", rows[0].metadata["deep_guidance"])
            self.assertEqual({item.id for item in rows}, {first_id, target_id})

        asyncio.run(run())

    def test_temporal_rerank_promotes_recent_relevant_memory_without_reviving_noise(self):
        async def run():
            scoring_mod = importlib.import_module("astrmai.memory.services.memory_scoring")
            now = time.time()
            old_strong = self.contracts.MemoryCandidate(
                id="old-strong",
                kind="topic",
                source="unit",
                summary="old strong",
                content="old strong",
                relevance_score=0.90,
                created_at=now - 7 * 86400,
                metadata_hydrated=True,
            )
            new_relevant = self.contracts.MemoryCandidate(
                id="new-relevant",
                kind="topic",
                source="unit",
                summary="new relevant",
                content="new relevant",
                relevance_score=0.80,
                created_at=now - 3600,
                metadata_hydrated=True,
            )
            new_noise = self.contracts.MemoryCandidate(
                id="new-noise",
                kind="topic",
                source="unit",
                summary="new noise",
                content="new noise",
                relevance_score=0.05,
                created_at=now - 60,
                metadata_hydrated=True,
            )
            ranked = scoring_mod.rerank_candidates([old_strong, new_relevant, new_noise], now=now)
            self.assertEqual(ranked[0].id, "new-relevant")
            self.assertEqual(ranked[-1].id, "new-noise")

        asyncio.run(run())

    def test_temporal_rerank_keeps_fact_resilient(self):
        async def run():
            scoring_mod = importlib.import_module("astrmai.memory.services.memory_scoring")
            now = time.time()
            fact = self.contracts.MemoryCandidate(
                id="fact-old",
                kind="fact",
                source="unit",
                summary="fact",
                content="fact",
                relevance_score=0.92,
                created_at=now - 730 * 86400,
                metadata_hydrated=True,
            )
            recent_topic = self.contracts.MemoryCandidate(
                id="topic-new",
                kind="topic",
                source="unit",
                summary="topic",
                content="topic",
                relevance_score=0.75,
                created_at=now - 3600,
                metadata_hydrated=True,
            )
            ranked = scoring_mod.rerank_candidates([recent_topic, fact], now=now)
            self.assertEqual(ranked[1].id, "fact-old")
            self.assertGreater(ranked[1].relevance_score, 0.6)

        asyncio.run(run())

    def test_deep_retrieval_hydrates_missing_metadata_before_rerank(self):
        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                return {"ids": []}

        class _HybridResult:
            def __init__(self, memory_id):
                self.content = "hydration candidate"
                self.score = 0.6
                self.metadata = {"id": memory_id, "kind": "memory"}

        class _Engine:
            def __init__(self):
                self.gateway = _Gateway()

            async def _search_memories(self, *args, **kwargs):
                return [_HybridResult("mem-hydrated"), _HybridResult("mem-missing")]

        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            retrieval = self.retrieval_mod.MemoryRetrievalService(store, engine=_Engine())

            async def _fake_batch(ids):
                return {
                    "mem-hydrated": {
                        "kind": "fact",
                        "importance": 0.9,
                        "status": "active",
                        "visibility": "auto_and_tool",
                        "created_at": 1000.0,
                        "updated_at": 1100.0,
                        "last_access_time": 1200.0,
                        "access_count": 7,
                        "decay_score": 0.8,
                        "metadata": {"source": "hydrated"},
                    }
                }

            store.batch_get_memory_meta = _fake_batch
            candidates = [
                self.contracts.MemoryCandidate(
                    id="mem-hydrated",
                    kind="memory",
                    source="hybrid",
                    summary="candidate",
                    content="candidate",
                    importance=0.5,
                    relevance_score=0.7,
                    created_at=0.0,
                ),
                self.contracts.MemoryCandidate(
                    id="mem-missing",
                    kind="memory",
                    source="hybrid",
                    summary="candidate",
                    content="candidate",
                    importance=0.5,
                    relevance_score=0.6,
                    created_at=0.0,
                ),
            ]
            hydrated = await retrieval._hydrate_candidate_metadata(candidates)
            by_id = {item.id: item for item in hydrated}
            self.assertTrue(by_id["mem-hydrated"].metadata_hydrated)
            self.assertEqual(by_id["mem-hydrated"].access_count, 7)
            self.assertEqual(by_id["mem-hydrated"].kind, "fact")
            self.assertFalse(by_id["mem-missing"].metadata_hydrated)
            self.assertEqual(by_id["mem-missing"].created_at, 0.0)

        asyncio.run(run())

    def test_deep_retrieval_limits_llm_rerank_to_temporal_top_window(self):
        class _Gateway:
            def __init__(self):
                self.candidate_batches = []

            async def call_data_process_task(self, *args, **kwargs):
                prompt = kwargs.get("prompt") if "prompt" in kwargs else (args[0] if args else "")
                if "Rewrite the user memory search request" in prompt:
                    return {"queries": []}
                if "Rerank these memory candidates" in prompt:
                    payload = prompt.split("Candidates: ", 1)[1]
                    data = json.loads(payload)
                    self.candidate_batches.append([item["id"] for item in data])
                    return {"ids": list(reversed([item["id"] for item in data]))}
                return {"guidance": "time first"}

        class _Engine:
            def __init__(self, gateway):
                self.gateway = gateway

        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            now = time.time()
            ids = []
            for index in range(20):
                memory_id = await writer.write(
                    self.contracts.MemoryWriteRequest(
                        source="summary",
                        kind="memory",
                        session_id="chat-1",
                        content=f"Candidate {index}",
                        dedup_key=f"deep-window:{index}",
                    )
                )
                ids.append(memory_id)
            async with aiosqlite.connect(self.db_path) as db:
                for index, memory_id in enumerate(ids):
                    created_at = now - index * 86400
                    await db.execute(
                        """
                        UPDATE canonical_memories
                        SET create_time = ?, update_time = ?, importance = ?, access_count = ?, last_access_time = ?
                        WHERE id = ?
                        """,
                        (created_at, created_at, 0.6, 1, created_at, memory_id),
                    )
                await db.commit()
            gateway = _Gateway()
            retrieval = self.retrieval_mod.MemoryRetrievalService(store, engine=_Engine(gateway))
            rows = await retrieval.retrieve(
                self.contracts.MemoryQuery(query="Candidate", session_id="chat-1", policy="deep", top_k=5)
            )
            self.assertEqual(len(gateway.candidate_batches), 1)
            self.assertEqual(len(gateway.candidate_batches[0]), 8)
            self.assertEqual(set(gateway.candidate_batches[0]), set(ids[:8]))
            self.assertEqual(len(rows), 5)

        asyncio.run(run())

    def test_maintenance_temporal_hot_score_marks_low_heat_non_fact_stale(self):
        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            writer = self.write_mod.MemoryWriteService(store)
            config = type(
                "_Config",
                (),
                {
                    "memory": type(
                        "_MemoryCfg",
                        (),
                        {
                            "maintenance_hot_beta": 0.7,
                            "maintenance_temporal_stale_hot_threshold": 0.35,
                        },
                    )()
                },
            )()
            maintenance = self.maintenance_mod.MemoryMaintenanceService(store, config=config)
            topic_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="topic",
                    session_id="chat-1",
                    content="cold topic memory",
                    importance=0.2,
                    dedup_key="topic:cold",
                )
            )
            fact_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="fact",
                    session_id="chat-1",
                    content="durable fact memory",
                    importance=0.8,
                    dedup_key="fact:durable",
                )
            )
            old_ts = time.time() - 40 * 86400
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    UPDATE canonical_memories
                    SET create_time = ?, update_time = ?, last_access_time = ?, access_count = 0
                    WHERE id IN (?, ?)
                    """,
                    (old_ts, old_ts, old_ts, topic_id, fact_id),
                )
                await db.commit()
            report = await maintenance.run_once(policy={"decay_rate": 0.0, "min_score": 0.0, "stale_grace_seconds": 365 * 86400})
            topic = await store.get_canonical(topic_id, include_inactive=True)
            fact = await store.get_canonical(fact_id, include_inactive=True)
            self.assertGreaterEqual(report["marked_stale"], 1)
            self.assertEqual(topic.status, "stale")
            self.assertEqual(fact.status, "active")

        asyncio.run(run())

    def test_maintenance_run_once_keeps_protected_stale_records(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, maintenance = self._services()
            protected_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="persona",
                    kind="persona_lore",
                    session_id="__self_lore__",
                    content="I keep a protected voice memory.",
                    importance=1.0,
                    dedup_key="protected:lore",
                )
            )
            disposable_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Disposable stale memory.",
                    importance=0.2,
                    dedup_key="protected:normal",
                )
            )
            async with aiosqlite.connect(self.db_path) as db:
                old_time = time.time() - 8 * 86400
                await db.execute(
                    """
                    UPDATE canonical_memories
                    SET status = 'stale', last_access_time = ?, create_time = ?
                    WHERE id IN (?, ?)
                    """,
                    (old_time, old_time, protected_id, disposable_id),
                )
                await db.commit()
            report = await maintenance.run_once(policy={"decay_rate": 0.0, "stale_grace_seconds": 7 * 86400})
            self.assertEqual(report["physically_deleted"], 1)
            self.assertIsNotNone(await store.get_canonical(protected_id, include_inactive=True))
            self.assertIsNone(await store.get_canonical(disposable_id, include_inactive=True))

        asyncio.run(run())

    def test_maintenance_run_once_purges_old_jargon_candidates_but_keeps_protected(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, maintenance = self._services()
            old_ts = time.time() - 21 * 86400
            pending_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="chat-1",
                    content="pendingbird",
                    summary="pending meaning",
                    confidence=0.4,
                    metadata={"meaning": "pending meaning", "review_status": "review_pending", "count": 1, "confidence": 0.4},
                    dedup_key="jargon:pendingbird",
                    status="review_pending",
                    visibility="maintenance_only",
                )
            )
            rejected_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="chat-1",
                    content="rejectbird",
                    summary="rejected meaning",
                    confidence=0.4,
                    metadata={"meaning": "rejected meaning", "review_status": "rejected", "count": 1, "confidence": 0.4},
                    dedup_key="jargon:rejectbird",
                    status="rejected",
                    visibility="maintenance_only",
                )
            )
            pending_human_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="chat-1",
                    content="humanbird",
                    summary="needs human eyes",
                    confidence=0.35,
                    metadata={
                        "meaning": "needs human eyes",
                        "review_status": "pending_human",
                        "review_suggestion": "confirm exact meaning",
                        "count": 1,
                        "confidence": 0.35,
                    },
                    dedup_key="jargon:humanbird",
                    status="review_pending",
                    visibility="maintenance_only",
                )
            )
            protected_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="chat-1",
                    content="protectedbird",
                    summary="protected meaning",
                    confidence=0.95,
                    metadata={"meaning": "protected meaning", "review_status": "review_pending", "count": 6, "confidence": 0.95},
                    dedup_key="jargon:protectedbird",
                    status="review_pending",
                    visibility="maintenance_only",
                )
            )
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE canonical_memories SET update_time = ?, create_time = ? WHERE id IN (?, ?, ?, ?)",
                    (old_ts, old_ts, pending_id, rejected_id, pending_human_id, protected_id),
                )
                await db.commit()

            report = await maintenance.run_once(
                policy={
                    "decay_rate": 0.0,
                    "pending_jargon_grace_seconds": 14 * 86400,
                    "rejected_jargon_grace_seconds": 7 * 86400,
                    "protected_jargon_confidence": 0.9,
                    "protected_jargon_count": 5,
                }
            )

            self.assertEqual(report["jargon_pending_deleted"], 1)
            self.assertEqual(report["jargon_pending_human_deleted"], 1)
            self.assertEqual(report["jargon_rejected_deleted"], 1)
            self.assertEqual(report["protected_jargon_skipped"], 1)
            self.assertIsNone(await store.get_canonical(pending_id, include_inactive=True))
            self.assertIsNone(await store.get_canonical(rejected_id, include_inactive=True))
            self.assertIsNone(await store.get_canonical(pending_human_id, include_inactive=True))
            self.assertIsNotNone(await store.get_canonical(protected_id, include_inactive=True))

        asyncio.run(run())

    def test_projector_checks_and_repairs_consistency(self):
        class _Engine:
            def __init__(self, store, rows):
                self.v2_store = store
                self.rows = rows
                self.projected = []
                self.cleaned = []
                self.retriever = type("_Retriever", (), {"add_memory": self._add_memory})()

            async def _add_memory(self, content, metadata):
                self.projected.append((content, metadata))

            async def _ensure_faiss_initialized(self):
                return True

            def _build_memory_metadata(self, **kwargs):
                return dict(kwargs)

            async def _run_documents_query(self, query, params=()):
                if "SELECT id, metadata" in query:
                    return list(self.rows)
                if "SELECT id FROM documents" in query:
                    return [(row[0],) for row in self.rows if row[1] and (not params or params[0] in row[1])]
                return []

            async def _execute_documents_write(self, query, params=()):
                self.cleaned.append((query, params))
                return 1

        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            missing_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Missing projection memory.",
                )
            )
            inactive_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Inactive projection memory.",
                )
            )
            await store.soft_delete(inactive_id, reason="unit")
            rows = [
                (1, f'{{"canonical_id":"{inactive_id}"}}'),
                (2, '{"canonical_id":"missing-orphan"}'),
                (3, f'{{"canonical_id":"{missing_id}"}}'),
                (4, f'{{"canonical_id":"{missing_id}"}}'),
            ]
            engine = _Engine(store, rows)
            projector = self.projector_mod.MemoryIndexProjector(engine)
            report = await projector.check_consistency()
            self.assertIn("missing-orphan", report["orphan_projection_ids"])
            self.assertIn(inactive_id, report["inactive_projection_ids"])
            self.assertIn(missing_id, report["duplicate_projection_ids"])
            repaired = await projector.repair_consistency(report)
            self.assertEqual(repaired["deduplicated"], 1)
            self.assertTrue(engine.cleaned)

        asyncio.run(run())

    def test_migration_report_exposes_counts(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, _maintenance = self._services()
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="summary",
                    kind="memory",
                    session_id="chat-1",
                    content="Report memory.",
                )
            )
            report = await store.migration_report()
            self.assertEqual(report["schema_version"], 2)
            self.assertGreaterEqual(report["canonical_counts"]["active"], 1)
            self.assertIn("migrations", report)

        asyncio.run(run())

    def test_migration_service_dry_run_execute_verify_and_repair(self):
        async def run():
            with open(os.path.join(self.temp_dir.name, "persona_cache.json"), "w", encoding="utf-8") as handle:
                handle.write(
                    '{"persona-a":{"summary":"Gentle and concise.","style":"Soft replies."},"persona-empty":{"summary":"   "}}'
                )
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, text TEXT, metadata TEXT)")
                await db.execute(
                    "INSERT INTO documents(id, text, metadata) VALUES (1, ?, ?)",
                    ("Alice document import memory.", '{"session_id":"chat-1","source":"legacy"}'),
                )
                await db.execute(
                    "INSERT INTO documents(id, text, metadata) VALUES (2, ?, ?)",
                    ("Already projected legacy memory.", '{"session_id":"chat-1","canonical_id":"mem-existing"}'),
                )
                await db.execute(
                    "INSERT INTO documents(id, text, metadata) VALUES (3, ?, ?)",
                    ("", '{"session_id":"chat-1","source":"legacy"}'),
                )
                await db.execute(
                    """
                    CREATE TABLE MemoryEvent (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT,
                        session_id TEXT,
                        narrative TEXT,
                        memory_kind TEXT,
                        tags TEXT,
                        importance REAL
                    )
                    """
                )
                await db.execute(
                    """
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
                    )
                    """
                )
                await db.execute(
                    "INSERT INTO MemoryEvent(event_id, session_id, narrative, memory_kind, tags, importance) VALUES (?, ?, ?, ?, ?, ?)",
                    ("evt-1", "chat-1", "Alice event import memory.", "event", '["legacy"]', 0.8),
                )
                await db.execute(
                    "INSERT INTO MemoryEvent(event_id, session_id, narrative, memory_kind, tags, importance) VALUES (?, ?, ?, ?, ?, ?)",
                    ("evt-empty", "chat-1", "", "event", '["legacy"]', 0.2),
                )
                await db.execute(
                    "INSERT INTO Jargon(content, raw_content, meaning, is_jargon, count, is_complete, group_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("bigbird", "bigbird arrived again", "raid boss nickname", 1, 3, 1, "chat-1", 1.0, 2.0),
                )
                await db.commit()
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            migration = self.migration_mod.MemoryMigrationService(store)

            dry_run = await migration.dry_run()
            self.assertEqual(dry_run["totals"]["importable"], 4)
            self.assertEqual(dry_run["totals"]["duplicates"], 1)
            self.assertEqual(dry_run["totals"]["skipped"], 3)

            executed = await migration.execute()
            self.assertEqual(executed["imported"]["documents"], 1)
            self.assertEqual(executed["imported"]["MemoryEvent"], 1)
            self.assertEqual(executed["imported"]["persona_cache"], 1)
            self.assertEqual(executed["imported"]["Jargon"], 1)

            verified = await migration.verify()
            self.assertIn("migration", verified)
            self.assertEqual(verified["legacy"]["unmapped_memory_events"], 0)
            self.assertEqual(verified["legacy"]["unmapped_jargons"], 0)
            self.assertEqual(verified["jargon"]["missing_meaning"], 0)
            self.assertEqual(verified["jargon"]["missing_review_status"], 0)
            self.assertEqual(verified["jargon"]["active_non_approved_metadata"], 0)
            self.assertEqual(verified["jargon"]["pending_human_without_review_suggestion"], 0)
            self.assertEqual(verified["jargon"]["visibility_anomalies"], 0)
            self.assertTrue(await store.find_ids_by_source_ref("documents:1"))
            self.assertTrue(await store.find_ids_by_source_ref("MemoryEvent:evt-1"))
            self.assertTrue(await store.find_ids_by_source_ref("persona_cache:persona-a"))
            self.assertTrue(await store.find_ids_by_source_ref("Jargon:1"))

            repaired = await migration.repair(verified)
            self.assertEqual(repaired["mode"], "repair")
            self.assertIn("filled_review_status", repaired["jargon"])

        asyncio.run(run())

    def test_expression_pattern_service_writes_retrieves_and_updates_canonical_records(self):
        async def run():
            store, retrieval, writer, _injection, _tools, _maintenance = self._services()
            service = self.expression_mod.ExpressionPatternService(store, writer)
            pattern_id = await service.write_pattern(
                "chat-1",
                {
                    "situation": "daily reply",
                    "expression": "ship it softly",
                    "style": "plain",
                    "content_samples": ["ship it softly"],
                    "count": 2,
                    "review_status": "approved",
                    "shared_scope": "chat-1",
                    "confidence": 0.82,
                },
                source="webui_expression_pattern",
            )
            rows = await retrieval.retrieve(
                self.contracts.MemoryQuery(
                    query="ship it",
                    session_id="chat-1",
                    layers=["expression_pattern"],
                    intent="expression_pattern",
                    metadata={"shared_scope": "chat-1"},
                )
            )
            self.assertEqual([item.id for item in rows], [pattern_id])
            updated = await service.update_review(pattern_id, rejected=True, review_status="rejected", weight_delta=-0.5)
            self.assertEqual(updated.status, "rejected")
            hidden = await retrieval.retrieve(
                self.contracts.MemoryQuery(
                    query="ship it",
                    session_id="chat-1",
                    layers=["expression_pattern"],
                    intent="expression_pattern",
                    metadata={"shared_scope": "chat-1"},
                )
            )
            self.assertEqual(hidden, [])

        asyncio.run(run())

    def test_learning_expression_pattern_cannot_write_directly_to_approved(self):
        async def run():
            store, retrieval, writer, _injection, _tools, _maintenance = self._services()
            service = self.expression_mod.ExpressionPatternService(store, writer)
            pattern_id = await service.write_pattern(
                "chat-1",
                {
                    "situation": "daily reply",
                    "expression": "ship it softly",
                    "style": "plain",
                    "content_samples": ["ship it softly"],
                    "count": 2,
                    "review_status": "approved",
                    "shared_scope": "chat-1",
                    "confidence": 0.82,
                },
                source="learning_expression_pattern",
            )
            pattern = await service.get_pattern(pattern_id)
            self.assertEqual(pattern.review_status, "pending")
            self.assertEqual(pattern.status, "review_pending")
            rows = await retrieval.retrieve(
                self.contracts.MemoryQuery(
                    query="ship it",
                    session_id="chat-1",
                    layers=["expression_pattern"],
                    intent="expression_pattern",
                    metadata={"shared_scope": "chat-1"},
                )
            )
            self.assertEqual(rows, [])

        asyncio.run(run())

    def test_maintenance_purges_old_expression_candidates_but_keeps_protected(self):
        async def run():
            store, _retrieval, writer, _injection, _tools, maintenance = self._services()
            old_ts = time.time() - 30 * 86400
            pending_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_expression_pattern",
                    kind="expression_pattern",
                    session_id="chat-1",
                    content="soft ping",
                    summary="soft ping",
                    metadata={"review_status": "pending", "count": 1, "confidence": 0.4},
                    dedup_key="expression:pending",
                    status="review_pending",
                    visibility="maintenance_only",
                )
            )
            protected_id = await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_expression_pattern",
                    kind="expression_pattern",
                    session_id="chat-1",
                    content="core phrase",
                    summary="core phrase",
                    metadata={"review_status": "pending", "count": 9, "confidence": 0.96},
                    dedup_key="expression:protected",
                    status="review_pending",
                    visibility="maintenance_only",
                )
            )
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE canonical_memories SET create_time = ?, update_time = ? WHERE kind = 'expression_pattern'",
                    (old_ts, old_ts),
                )
                await db.commit()
            report = await maintenance.run_once(
                policy={
                    "decay_rate": 0.0,
                    "pending_expression_grace_seconds": 21 * 86400,
                    "protected_expression_confidence": 0.95,
                    "protected_expression_count": 8,
                }
            )
            self.assertEqual(report["expression_pending_deleted"], 1)
            self.assertEqual(report["protected_expression_skipped"], 1)
            self.assertIsNone(await store.get_canonical(pending_id, include_inactive=True))
            self.assertIsNotNone(await store.get_canonical(protected_id, include_inactive=True))

        asyncio.run(run())

    def test_migration_service_imports_legacy_expression_patterns(self):
        async def run():
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE ExpressionPattern (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT,
                        situation TEXT,
                        expression TEXT,
                        style TEXT,
                        content_list TEXT,
                        count INTEGER,
                        checked INTEGER,
                        rejected INTEGER,
                        modified_by TEXT,
                        source TEXT,
                        shared_scope TEXT,
                        think_level INTEGER,
                        review_status TEXT,
                        review_reason TEXT,
                        review_suggestion TEXT,
                        last_review_time REAL,
                        weight REAL,
                        last_active_time REAL,
                        create_time REAL
                    )
                    """
                )
                await db.execute(
                    """
                    INSERT INTO ExpressionPattern (
                        group_id, situation, expression, style, content_list, count, checked,
                        rejected, modified_by, source, shared_scope, think_level, review_status,
                        review_reason, review_suggestion, last_review_time, weight, last_active_time, create_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "chat-1",
                        "daily reply",
                        "ship it softly",
                        "plain",
                        '["ship it softly"]',
                        3,
                        1,
                        0,
                        "legacy",
                        "joint_mining",
                        "chat-1",
                        1,
                        "approved",
                        "",
                        "",
                        1.0,
                        1.2,
                        2.0,
                        1.0,
                    ),
                )
                await db.commit()
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            migration = self.migration_mod.MemoryMigrationService(store)
            dry_run = await migration.dry_run(["expression_patterns"])
            self.assertEqual(dry_run["sources"]["ExpressionPattern"]["importable"], 1)
            executed = await migration.execute(["expression_patterns"])
            self.assertEqual(executed["imported"]["ExpressionPattern"], 1)
            self.assertTrue(await store.find_ids_by_source_ref("ExpressionPattern:1"))
            verified = await migration.verify()
            self.assertEqual(verified["legacy"]["unmapped_expression_patterns"], 0)
            self.assertEqual(verified["expression_pattern"]["missing_review_status"], 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
