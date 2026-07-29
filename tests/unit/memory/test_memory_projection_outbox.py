import asyncio
import importlib
import os
import tempfile
import time
import unittest

from tests.helpers import install_astrbot_stubs


class MemoryProjectionOutboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        install_astrbot_stubs(cls.temp_dir.name)
        cls.contracts = importlib.import_module("astrmai.memory.contracts.memory_query")
        cls.projector_mod = importlib.import_module("astrmai.memory.services.memory_index_projector")
        cls.retrieval_mod = importlib.import_module("astrmai.memory.services.memory_retrieval_service")
        cls.store_mod = importlib.import_module("astrmai.memory.services.v2_store")

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def _db_path(self, name: str) -> str:
        return os.path.join(self.temp_dir.name, name)

    def test_projection_outbox_survives_store_restart(self):
        async def run():
            db_path = self._db_path("outbox-restart.db")
            store = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            self.assertTrue(
                await store.schedule_projection_retry(
                    "mem-restart",
                    "projection_error:TimeoutError",
                    base_delay_sec=2,
                    max_delay_sec=30,
                )
            )

            restarted = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            snapshot = await restarted.projection_retry_snapshot()

            self.assertEqual(
                snapshot,
                {"mem-restart": "projection_error:TimeoutError"},
            )
            self.assertEqual(
                await restarted.pending_projection_ids(["mem-restart", "mem-other"]),
                {"mem-restart"},
            )

        asyncio.run(run())

    def test_projector_consistency_merges_persisted_pending_after_restart(self):
        outer = self

        class _Engine:
            def __init__(self, store):
                self.v2_store = store
                self.retriever = None

            async def _run_documents_query(self, *_args, **_kwargs):
                return []

        async def run():
            db_path = outer._db_path("outbox-consistency.db")
            store = outer.store_mod.MemoryV2Store(db_path, data_path=outer.temp_dir.name)
            written = await store.upsert(
                outer.contracts.MemoryWriteRequest(
                    source="test",
                    kind="memory",
                    session_id="chat-1",
                    content="Persisted pending projection.",
                    dedup_key="pending:restart",
                )
            )
            await store.schedule_projection_retry(written.memory_id, "retriever_not_ready")

            restarted = outer.store_mod.MemoryV2Store(db_path, data_path=outer.temp_dir.name)
            projector = outer.projector_mod.MemoryIndexProjector(_Engine(restarted))
            report = await projector.check_consistency()

            self.assertIn(written.memory_id, report["missing_projection_ids"])
            self.assertEqual(report["pending_projection_count"], 1)
            self.assertEqual(
                report["pending_projection_reasons"][written.memory_id],
                "retriever_not_ready",
            )

        asyncio.run(run())

    def test_due_retry_projects_and_removes_outbox_record(self):
        outer = self

        class _Retriever:
            def __init__(self):
                self.added = []

            async def add_memory(self, content, metadata):
                self.added.append((content, metadata))
                return len(self.added)

        class _Engine:
            def __init__(self, store):
                self.v2_store = store
                self.retriever = _Retriever()

            def _build_memory_metadata(self, **kwargs):
                return dict(kwargs)

            async def _run_documents_query(self, *_args, **_kwargs):
                return []

            async def _execute_documents_write(self, *_args, **_kwargs):
                return 0

        async def run():
            db_path = outer._db_path("outbox-retry.db")
            store = outer.store_mod.MemoryV2Store(db_path, data_path=outer.temp_dir.name)
            written = await store.upsert(
                outer.contracts.MemoryWriteRequest(
                    source="test",
                    kind="memory",
                    session_id="chat-1",
                    content="Retry this projection.",
                    dedup_key="pending:retry",
                )
            )
            await store.schedule_projection_retry(
                written.memory_id,
                "projection_error:TimeoutError",
                base_delay_sec=1,
                max_delay_sec=1,
            )
            async with importlib.import_module("aiosqlite").connect(db_path) as db:
                await db.execute(
                    "UPDATE memory_projection_outbox SET next_retry_at = ? WHERE memory_id = ?",
                    (time.time() - 1, written.memory_id),
                )
                await db.commit()

            engine = _Engine(store)
            projector = outer.projector_mod.MemoryIndexProjector(engine)
            result = await projector.retry_due()

            self.assertEqual(result, {"attempted": 1, "projected": 1, "failed": 0})
            self.assertEqual(len(engine.retriever.added), 1)
            self.assertEqual(await store.projection_retry_snapshot(), {})

        asyncio.run(run())

    def test_pending_canonical_match_is_marked_as_read_your_write_fallback(self):
        async def run():
            db_path = self._db_path("outbox-read-your-write.db")
            store = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            written = await store.upsert(
                self.contracts.MemoryWriteRequest(
                    source="test",
                    kind="fact",
                    session_id="chat-1",
                    content="Alice likes cobalt notebooks.",
                    summary="Alice likes cobalt notebooks.",
                    dedup_key="fact:alice:notebook",
                )
            )
            await store.schedule_projection_retry(written.memory_id, "projection_error:TimeoutError")
            service = self.retrieval_mod.MemoryRetrievalService(store)

            query = self.contracts.MemoryQuery(
                query="cobalt notebooks",
                session_id="chat-1",
                top_k=3,
            )
            rows = await service.retrieve(query)

            self.assertEqual([item.id for item in rows], [written.memory_id])
            self.assertTrue(rows[0].metadata["index_projection_pending"])
            self.assertIn("read_your_write_fallback", rows[0].metadata["matched_by"])
            trace = query.metadata["_trace"]
            self.assertEqual(
                trace["read_your_write_fallback"],
                {"used": True, "candidate_count": 1},
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
