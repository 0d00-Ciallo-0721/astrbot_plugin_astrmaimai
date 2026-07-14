import asyncio
import importlib
import json
import os
import tempfile
import time
import unittest

import aiosqlite

from tests.helpers import install_astrbot_stubs


class Round6MemoryRetrievalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        self.contracts = importlib.import_module("astrmai.memory.contracts.memory_query")
        self.store_mod = importlib.import_module("astrmai.memory.services.v2_store")
        self.retrieval_mod = importlib.import_module("astrmai.memory.services.memory_retrieval_service")
        self.hybrid_mod = importlib.import_module("astrmai.memory.retrieval.hybrid_retriever")
        self.utils_mod = importlib.import_module("astrmai.memory.utils")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_canonical_fts_preserves_distinct_bm25_order(self):
        async def run():
            store = self.store_mod.MemoryV2Store(
                os.path.join(self.temp_dir.name, "memory_v2.db"),
                data_path=self.temp_dir.name,
            )
            for index, content in enumerate(
                (
                    "orchid orchid orchid greenhouse",
                    "orchid greenhouse notes",
                    "orchid unrelated filler words across a much longer document",
                )
            ):
                await store.upsert(
                    self.contracts.MemoryWriteRequest(
                        source="test",
                        kind="memory",
                        session_id="chat-1",
                        content=content,
                        importance=0.1 if index == 0 else 1.0,
                        confidence=0.8,
                        dedup_key=f"fts-rank:{index}",
                    )
                )

            rows = await store.search("orchid", session_id="chat-1", top_k=3, track_access=False)

            scores = [item.relevance_score for item in rows]
            self.assertEqual(scores, sorted(scores, reverse=True))
            self.assertGreater(len(set(scores)), 1)
            self.assertTrue(rows[0].content.startswith("orchid orchid orchid"))

        asyncio.run(run())

    def test_candidate_collection_is_read_only_until_final_selection(self):
        async def run():
            store = self.store_mod.MemoryV2Store(
                os.path.join(self.temp_dir.name, "memory_v2.db"),
                data_path=self.temp_dir.name,
            )
            ids = []
            for index in range(2):
                result = await store.upsert(
                        self.contracts.MemoryWriteRequest(
                            source="test",
                            kind="memory",
                            session_id="chat-1",
                            content=f"shared orchid memory {index}",
                            dedup_key=f"read-only:{index}",
                        )
                    )
                ids.append(result.memory_id)

            candidates = await store.search(
                "orchid",
                session_id="chat-1",
                top_k=2,
                track_access=False,
            )
            before = await store.batch_get_memory_meta(ids)
            self.assertTrue(all(item["access_count"] == 0 for item in before.values()))

            await store.finalize_access(candidates[:1], allow_stale=False)
            after = await store.batch_get_memory_meta(ids)
            self.assertEqual(after[candidates[0].id]["access_count"], 1)
            discarded_id = next(memory_id for memory_id in ids if memory_id != candidates[0].id)
            self.assertEqual(after[discarded_id]["access_count"], 0)

        asyncio.run(run())

    def test_legacy_canonical_rows_are_projected_into_fts_on_initialize(self):
        async def run():
            legacy_path = os.path.join(self.temp_dir.name, "docs.db")
            target_path = os.path.join(self.temp_dir.name, "memory_v2.db")
            async with aiosqlite.connect(legacy_path) as db:
                await db.execute(
                    """
                    CREATE TABLE canonical_memories (
                        id TEXT PRIMARY KEY, session_id TEXT, source TEXT, kind TEXT,
                        content TEXT, summary TEXT, tags TEXT, importance REAL,
                        confidence REAL, status TEXT, create_time REAL, update_time REAL,
                        last_access_time REAL, access_count INTEGER, metadata TEXT, dedup_key TEXT
                    )
                    """
                )
                await db.execute(
                    "INSERT INTO canonical_memories VALUES (?, ?, 'legacy', 'memory', ?, ?, '[]', 0.5, 0.8, 'active', 1, 1, 0, 0, '{}', ?)",
                    ("legacy-1", "chat-1", "rarelegacytoken", "rarelegacytoken", "legacy:1"),
                )
                await db.commit()

            store = self.store_mod.MemoryV2Store(
                target_path,
                data_path=self.temp_dir.name,
                legacy_db_path=legacy_path,
            )
            rows = await store.search("rarelegacytoken", session_id="chat-1", top_k=3, track_access=False)

            self.assertEqual([item.id for item in rows], ["legacy-1"])
            self.assertEqual(rows[0].metadata["matched_by"], ["canonical_fts"])

        asyncio.run(run())

    def test_candidate_limit_controls_source_pool_without_second_multiplier(self):
        outer = self

        class Store:
            def __init__(self):
                self.calls = []
                self.finalized = []

            async def search(self, _query, **kwargs):
                self.calls.append(kwargs)
                candidate_count = kwargs.get("candidate_limit") or kwargs["top_k"]
                return [
                    outer.contracts.MemoryCandidate(
                        id=f"m-{index}",
                        kind="memory",
                        source="canonical",
                        summary=f"memory {index}",
                        content=f"memory {index}",
                        relevance_score=1.0 - index / 100,
                    )
                    for index in range(candidate_count)
                ]

            async def finalize_access(self, candidates, *, allow_stale=False):
                self.finalized = [item.id for item in candidates]

        store = Store()
        service = self.retrieval_mod.MemoryRetrievalService(store)
        query = self.contracts.MemoryQuery(
            query="memory",
            top_k=3,
            metadata={"candidate_limit": 24},
        )

        rows = asyncio.run(service.retrieve(query))

        self.assertEqual(store.calls[0]["top_k"], 3)
        self.assertEqual(store.calls[0]["candidate_limit"], 24)
        self.assertEqual(len(rows), 3)
        self.assertEqual(store.finalized, ["m-0", "m-1", "m-2"])

    def test_specialized_policy_records_access_exactly_once_after_final_selection(self):
        async def run():
            store = self.store_mod.MemoryV2Store(
                os.path.join(self.temp_dir.name, "memory_v2.db"),
                data_path=self.temp_dir.name,
            )
            result = await store.upsert(
                self.contracts.MemoryWriteRequest(
                    source="test",
                    kind="jargon",
                    session_id="group-1",
                    content="bigbird",
                    summary="raid boss nickname",
                    metadata={"meaning": "raid boss nickname", "review_status": "active"},
                    dedup_key="jargon:group-1:bigbird",
                )
            )
            service = self.retrieval_mod.MemoryRetrievalService(store)

            rows = await service.retrieve(
                self.contracts.MemoryQuery(
                    query="raid boss",
                    session_id="group-1",
                    intent="jargon",
                    layers=["jargon"],
                    top_k=1,
                )
            )
            metadata = await store.batch_get_memory_meta([result.memory_id])

            self.assertEqual([item.id for item in rows], [result.memory_id])
            self.assertEqual(metadata[result.memory_id]["access_count"], 1)

        asyncio.run(run())

    def test_initialize_repairs_balanced_missing_and_duplicate_fts_rows(self):
        async def run():
            db_path = os.path.join(self.temp_dir.name, "memory_v2.db")
            store = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            first = await store.upsert(
                self.contracts.MemoryWriteRequest(
                    source="test", kind="memory", session_id="chat-1",
                    content="first projection token", dedup_key="projection:first",
                )
            )
            second = await store.upsert(
                self.contracts.MemoryWriteRequest(
                    source="test", kind="memory", session_id="chat-1",
                    content="seconduniquetoken", dedup_key="projection:second",
                )
            )
            async with aiosqlite.connect(db_path) as db:
                await db.execute("DELETE FROM canonical_fts WHERE memory_id = ?", (second.memory_id,))
                await db.execute(
                    "INSERT INTO canonical_fts(memory_id, content, summary, tags) "
                    "SELECT id, content, summary, tags FROM canonical_memories WHERE id = ?",
                    (first.memory_id,),
                )
                await db.commit()

            restarted = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            rows = await restarted.search(
                "seconduniquetoken", session_id="chat-1", top_k=1, track_access=False,
            )

            self.assertEqual([item.id for item in rows], [second.memory_id])
            self.assertEqual(rows[0].metadata["matched_by"], ["canonical_fts"])

        asyncio.run(run())

    def test_hybrid_rrf_relevance_is_normalized_before_static_weighting(self):
        retriever = self.hybrid_mod.HybridRetriever(None, None, config=None)
        strong = self.utils_mod.SearchResult(
            doc_id=1,
            score=0.032,
            content="strong",
            metadata={"importance": 0.5, "create_time": time.time()},
        )
        weak = self.utils_mod.SearchResult(
            doc_id=2,
            score=0.016,
            content="weak",
            metadata={"importance": 1.0, "create_time": time.time()},
        )

        rows = retriever._apply_weighting([strong, weak])

        self.assertEqual(rows[0].doc_id, 1)
        self.assertGreater(rows[0].score, rows[1].score)
        self.assertLessEqual(rows[0].score, 1.0)

    def test_memory_engine_refreshes_temporal_scoring_and_live_hybrid_config(self):
        engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        old_config = type("Config", (), {
            "provider": type("Provider", (), {"embedding_models": ["ed-model"]})(),
            "memory": type("Memory", (), {"deep_temporal_alpha": 0.7, "time_decay_rate": 0.01})(),
        })()
        new_config = type("Config", (), {
            "provider": type("Provider", (), {"embedding_models": ["ed-model"]})(),
            "memory": type("Memory", (), {"deep_temporal_alpha": 0.2, "time_decay_rate": 0.2})(),
        })()
        engine = engine_mod.MemoryEngine.__new__(engine_mod.MemoryEngine)
        engine.config = old_config
        engine.embedding_models = ["ed-model"]
        engine.injection_service = None
        engine.retrieval_service = self.retrieval_mod.MemoryRetrievalService(object())
        engine.retriever = self.hybrid_mod.HybridRetriever(None, None, config=old_config)

        engine_mod.MemoryEngine.refresh_config(engine, new_config)

        self.assertEqual(engine.retrieval_service.scoring.deep_temporal_alpha, 0.2)
        self.assertIs(engine.retriever.config, new_config)

    def test_legacy_documents_import_reads_legacy_database_and_missing_table_is_retryable(self):
        async def run():
            target_path = os.path.join(self.temp_dir.name, "memory_v2.db")
            legacy_path = os.path.join(self.temp_dir.name, "docs.db")
            async with aiosqlite.connect(legacy_path) as db:
                await db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, page_content TEXT, metadata TEXT)")
                await db.execute(
                    "INSERT INTO documents(page_content, metadata) VALUES (?, ?)",
                    ("legacy imported preference", json.dumps({"session_id": "chat-1", "kind": "memory"})),
                )
                await db.commit()

            store = self.store_mod.MemoryV2Store(
                target_path,
                data_path=self.temp_dir.name,
                legacy_db_path=legacy_path,
            )
            imported = await store.import_legacy_documents()
            rows = await store.list_candidates(session_id="chat-1", limit=10)

            self.assertEqual(imported, 1)
            self.assertEqual([item.content for item in rows], ["legacy imported preference"])

            missing_source = os.path.join(self.temp_dir.name, "missing-docs.db")
            retry_store = self.store_mod.MemoryV2Store(
                os.path.join(self.temp_dir.name, "retry-v2.db"),
                data_path=self.temp_dir.name,
                legacy_db_path=missing_source,
            )
            self.assertEqual(await retry_store.import_legacy_documents(), 0)
            self.assertFalse(await retry_store.migration_applied("2_legacy_documents_import"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
