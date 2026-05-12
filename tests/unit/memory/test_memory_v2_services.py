import asyncio
import importlib
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
                    "INSERT INTO MemoryEvent(event_id, session_id, narrative, memory_kind, tags, importance) VALUES (?, ?, ?, ?, ?, ?)",
                    ("evt-1", "chat-1", "Alice event import memory.", "event", '["legacy"]', 0.8),
                )
                await db.execute(
                    "INSERT INTO MemoryEvent(event_id, session_id, narrative, memory_kind, tags, importance) VALUES (?, ?, ?, ?, ?, ?)",
                    ("evt-empty", "chat-1", "", "event", '["legacy"]', 0.2),
                )
                await db.commit()
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            migration = self.migration_mod.MemoryMigrationService(store)

            dry_run = await migration.dry_run()
            self.assertEqual(dry_run["totals"]["importable"], 3)
            self.assertEqual(dry_run["totals"]["duplicates"], 1)
            self.assertEqual(dry_run["totals"]["skipped"], 3)

            executed = await migration.execute()
            self.assertEqual(executed["imported"]["documents"], 1)
            self.assertEqual(executed["imported"]["MemoryEvent"], 1)
            self.assertEqual(executed["imported"]["persona_cache"], 1)

            verified = await migration.verify()
            self.assertIn("migration", verified)
            self.assertEqual(verified["legacy"]["unmapped_memory_events"], 0)
            self.assertTrue(await store.find_ids_by_source_ref("documents:1"))
            self.assertTrue(await store.find_ids_by_source_ref("MemoryEvent:evt-1"))
            self.assertTrue(await store.find_ids_by_source_ref("persona_cache:persona-a"))

            repaired = await migration.repair(verified)
            self.assertEqual(repaired["mode"], "repair")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
