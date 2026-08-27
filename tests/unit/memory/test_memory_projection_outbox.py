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

    def test_projection_retry_enters_dead_letter_after_max_attempts(self):
        async def run():
            db_path = self._db_path("outbox-dead-letter.db")
            store = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            self.assertTrue(
                await store.schedule_projection_retry(
                    "mem-dead",
                    "vector_delete_unavailable",
                    base_delay_sec=1,
                    max_delay_sec=1,
                    max_attempts=2,
                )
            )
            self.assertTrue(
                await store.schedule_projection_retry(
                    "mem-dead",
                    "vector_delete_unavailable",
                    base_delay_sec=1,
                    max_delay_sec=1,
                    max_attempts=2,
                )
            )
            self.assertFalse(
                await store.schedule_projection_retry(
                    "mem-dead",
                    "vector_delete_unavailable",
                    base_delay_sec=1,
                    max_delay_sec=1,
                    max_attempts=2,
                )
            )

            diagnostics = await store.projection_retry_diagnostics()
            self.assertEqual(diagnostics["pending_count"], 0)
            self.assertEqual(diagnostics["dead_letter_count"], 1)
            self.assertEqual(
                diagnostics["dead_letter_count_by_reason"],
                {"vector_delete_unavailable": 1},
            )
            self.assertEqual(diagnostics["max_attempts"], 3)

        asyncio.run(run())

    def test_projection_diagnostics_keep_all_pending_rows_and_capabilities(self):
        async def run():
            db_path = self._db_path("outbox-large-diagnostics.db")
            store = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)
            await store.initialize()
            import aiosqlite

            rows = [
                (
                    f"mem-{index}",
                    "pending",
                    1,
                    index + 1,
                    0.0,
                    "retriever_not_ready",
                    time.time() - 10,
                    time.time() - 1,
                )
                for index in range(1001)
            ]
            async with aiosqlite.connect(db_path) as db:
                await db.executemany(
                    """
                    INSERT INTO memory_projection_outbox(
                        memory_id, status, attempts, revision, next_retry_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                await db.commit()

            diagnostics = await store.projection_retry_diagnostics()
            self.assertEqual(diagnostics["pending_projection_count"], 1001)
            self.assertEqual(diagnostics["pending_by_reason"], {"retriever_not_ready": 1001})
            snapshot = await store.projection_retry_snapshot_with_revisions()
            self.assertEqual(len(snapshot), 1001)

            engine = SimpleNamespace(
                v2_store=store,
                retriever=object(),
                _is_ready=True,
                _vector_state="ready",
                faiss_db=SimpleNamespace(delete=lambda *_args: None),
                _execute_documents_write=lambda *_args, **_kwargs: None,
            )
            projector = self.projector_mod.MemoryIndexProjector(engine)
            await projector._refresh_outbox_diagnostics()
            status = projector.describe_status()
            self.assertEqual(status["pending_projection_count"], 1001)
            self.assertTrue(status["retriever_ready"])
            self.assertTrue(status["vector_delete_supported"])
            self.assertTrue(status["fts_delete_supported"])
            self.assertTrue(status["repair_required"])

        from types import SimpleNamespace

        asyncio.run(run())

    def test_permanently_unavailable_vector_delete_enters_repair_required(self):
        async def run():
            db_path = self._db_path("outbox-vector-unavailable.db")
            store = self.store_mod.MemoryV2Store(db_path, data_path=self.temp_dir.name)

            class _Engine:
                v2_store = store
                retriever = None
                faiss_db = None
                config = SimpleNamespace(
                    timing=SimpleNamespace(
                        projection_retry_max_attempts=1,
                        projection_retry_base_delay_sec=1,
                        projection_retry_max_delay_sec=1,
                    )
                )

                async def _run_documents_query(self, *_args, **_kwargs):
                    return [(1, "doc-1")]

                async def _execute_documents_write(self, *_args, **_kwargs):
                    return 0

            projector = self.projector_mod.MemoryIndexProjector(_Engine())
            self.assertEqual(await projector.cleanup_deleted(["mem-unavailable"]), 0)
            self.assertEqual(await projector.cleanup_deleted(["mem-unavailable"]), 0)
            await projector._refresh_outbox_diagnostics()
            status = projector.describe_status()
            self.assertTrue(status["repair_required"])
            self.assertEqual(status["dead_letter_count"], 1)
            self.assertFalse(status["vector_delete_supported"])
            self.assertEqual(
                status["dead_letter_count_by_reason"],
                {"vector_delete_unavailable": 1},
            )

        from types import SimpleNamespace

        asyncio.run(run())

    def test_projection_single_flight_waits_for_duplicate_owner(self):
        async def run():
            projector = self.projector_mod.MemoryIndexProjector.__new__(self.projector_mod.MemoryIndexProjector)
            projector._projection_inflight_ids = set()
            calls = []

            async def _project_once(memory_id, request=None):
                calls.append(memory_id)
                await asyncio.sleep(0.02)
                return True

            projector._project_once = _project_once
            first, second = await asyncio.gather(
                projector.project("same-memory"),
                projector.project("same-memory"),
            )
            self.assertEqual((first, second), (True, True))
            self.assertEqual(calls, ["same-memory"])

        asyncio.run(run())

    def test_projection_caller_cancellation_does_not_cancel_owner_task(self):
        async def run():
            projector = self.projector_mod.MemoryIndexProjector.__new__(self.projector_mod.MemoryIndexProjector)
            projector._projection_inflight_tasks = {}
            projector._projection_inflight_ids = set()
            projector._projection_latest_requests = {}
            projector._projection_request_versions = {}
            projector._background_tasks = set()
            projector._accepting = True
            started = asyncio.Event()
            release = asyncio.Event()

            async def _project_once(memory_id, request=None):
                started.set()
                await release.wait()
                return True

            projector._project_once = _project_once
            caller = asyncio.create_task(projector.project("cancelled-memory"))
            await started.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            owner = projector._projection_inflight_tasks["cancelled-memory"]
            self.assertFalse(owner.done())
            release.set()
            self.assertTrue(await asyncio.wait_for(asyncio.shield(owner), timeout=0.5))
            await asyncio.sleep(0)
            self.assertFalse(projector._projection_inflight_tasks)

        asyncio.run(run())

    def test_projection_latest_request_is_applied_after_older_owner_finishes(self):
        async def run():
            from astrmai.memory.contracts.memory_query import MemoryWriteRequest

            projector = self.projector_mod.MemoryIndexProjector.__new__(self.projector_mod.MemoryIndexProjector)
            projector._projection_inflight_tasks = {}
            projector._projection_inflight_ids = set()
            projector._projection_latest_requests = {}
            projector._projection_request_versions = {}
            projector._background_tasks = set()
            projector._accepting = True
            first_started = asyncio.Event()
            release_first = asyncio.Event()
            contents = []

            async def _project_once(memory_id, request=None):
                contents.append(request.content if request is not None else "")
                if len(contents) == 1:
                    first_started.set()
                    await release_first.wait()
                return True

            projector._project_once = _project_once
            request_a = MemoryWriteRequest(source="test", kind="memory", session_id="s", content="old")
            request_b = MemoryWriteRequest(source="test", kind="memory", session_id="s", content="new")
            first = asyncio.create_task(projector.project("same-memory", request_a))
            await first_started.wait()
            second = asyncio.create_task(projector.project("same-memory", request_b))
            await asyncio.sleep(0)
            release_first.set()
            self.assertEqual(await asyncio.wait_for(first, timeout=0.5), True)
            self.assertEqual(await asyncio.wait_for(second, timeout=0.5), True)
            self.assertEqual(contents, ["old", "new"])

        asyncio.run(run())

    def test_projection_stop_waits_for_active_owner_and_rejects_new_work(self):
        async def run():
            projector = self.projector_mod.MemoryIndexProjector.__new__(self.projector_mod.MemoryIndexProjector)
            projector._projection_inflight_tasks = {}
            projector._projection_inflight_ids = set()
            projector._projection_latest_requests = {}
            projector._projection_request_versions = {}
            projector._background_tasks = set()
            projector._accepting = True
            projector._retry_task = None
            projector._retry_stop = asyncio.Event()
            release = asyncio.Event()

            async def _mark_pending_persisted(memory_id, reason):
                return True

            projector._mark_pending_persisted = _mark_pending_persisted

            async def _project_once(memory_id, request=None):
                await release.wait()
                return True

            projector._project_once = _project_once
            active = asyncio.create_task(projector.project("stopping-memory"))
            await asyncio.sleep(0)
            stopper = asyncio.create_task(projector.stop())
            await asyncio.sleep(0)
            self.assertFalse(stopper.done())
            self.assertFalse(await projector.project("new-memory"))
            release.set()
            await asyncio.wait_for(stopper, timeout=0.5)
            self.assertTrue(await active)
            self.assertFalse(projector._projection_inflight_tasks)

        asyncio.run(run())

    def test_projection_owner_exception_is_consumed_when_all_callers_cancel(self):
        async def run():
            projector = self.projector_mod.MemoryIndexProjector.__new__(self.projector_mod.MemoryIndexProjector)
            projector._projection_inflight_tasks = {}
            projector._projection_inflight_ids = set()
            projector._projection_latest_requests = {}
            projector._projection_request_versions = {}
            projector._background_tasks = set()
            projector._projection_task_diagnostics = []
            projector._accepting = True
            started = asyncio.Event()
            release = asyncio.Event()

            async def _project_once(memory_id, request=None):
                started.set()
                await release.wait()
                raise RuntimeError("projection boom")

            projector._project_once = _project_once
            loop = asyncio.get_running_loop()
            errors = []
            loop.set_exception_handler(lambda _loop, context: errors.append(context))
            caller = asyncio.create_task(projector.project("failed-memory"))
            await started.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            release.set()
            await asyncio.sleep(0.05)
            self.assertEqual(errors, [])
            self.assertEqual(projector._projection_task_diagnostics[0]["event"], "projection_task_failed")

        asyncio.run(run())

    def test_pending_outbox_persistence_timeout_is_bounded_and_retryable(self):
        async def run():
            never = asyncio.Event()

            class _Store:
                async def schedule_projection_retry(self, *_args, **_kwargs):
                    await never.wait()

            engine = type(
                "_Engine",
                (),
                {
                    "v2_store": _Store(),
                    "config": type(
                        "_Config",
                        (),
                        {
                            "timing": type(
                                "_Timing",
                                (),
                                {
                                    "projection_lock_timeout_sec": 0.05,
                                    "shutdown_cancel_grace_sec": 0.05,
                                },
                            )()
                        },
                    )(),
                },
            )()
            projector = self.projector_mod.MemoryIndexProjector(engine)
            started = time.monotonic()

            scheduled = await projector._mark_pending_persisted(
                "bounded-memory",
                "shutdown_rejected",
            )

            self.assertFalse(scheduled)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(
                projector.pending_reason("bounded-memory"),
                "shutdown_rejected",
            )
            self.assertFalse(projector.retry_scheduled("bounded-memory"))
            status = projector.describe_status()
            self.assertEqual(status["projection_persistence_timeout_total"], 1)
            self.assertEqual(
                status["projection_task_diagnostics"][-1]["operation"],
                "schedule_projection_retry",
            )

        asyncio.run(run())

    def test_outbox_diagnostics_timeout_preserves_last_snapshot(self):
        async def run():
            never = asyncio.Event()

            class _Store:
                async def projection_retry_diagnostics(self):
                    await never.wait()

            engine = type(
                "_Engine",
                (),
                {
                    "v2_store": _Store(),
                    "config": type(
                        "_Config",
                        (),
                        {
                            "timing": type(
                                "_Timing",
                                (),
                                {"projection_lock_timeout_sec": 0.05},
                            )()
                        },
                    )(),
                },
            )()
            projector = self.projector_mod.MemoryIndexProjector(engine)
            projector._outbox_diagnostics = {"pending_count": 3}

            snapshot = await projector._refresh_outbox_diagnostics()

            self.assertEqual(snapshot["pending_count"], 3)
            self.assertEqual(projector._persistence_timeout_total, 1)

        asyncio.run(run())

    def test_confirm_projection_outbox_timeout_preserves_retryable_pending(self):
        async def run():
            never = asyncio.Event()

            class _Store:
                async def complete_projection_retry(self, _memory_id):
                    await never.wait()

            engine = type(
                "_Engine",
                (),
                {
                    "v2_store": _Store(),
                    "config": type(
                        "_Config",
                        (),
                        {
                            "timing": type(
                                "_Timing",
                                (),
                                {"projection_lock_timeout_sec": 0.05},
                            )()
                        },
                    )(),
                },
            )()
            projector = self.projector_mod.MemoryIndexProjector(engine)
            started = time.monotonic()

            confirmed = await projector.confirm_projection_outbox({"memory-1"})

            self.assertFalse(confirmed)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(projector.pending_reason("memory-1"), "outbox_ack_failed")
            self.assertEqual(projector._persistence_timeout_total, 1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
