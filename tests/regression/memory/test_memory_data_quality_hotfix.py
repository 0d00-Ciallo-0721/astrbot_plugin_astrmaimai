"""OPT-05 回归测试：记忆数据质量与维护调度（ML-03 / ML-04 / ML-10 / WU-04）。

守护不变式：
1. ML-03: like/dislike 多值属性的 dedup_key 含归一化 value——不同偏好共存，
   同一偏好复述仍去重；单值属性（display_name 等）保持 attribute 级覆盖。
2. ML-04: 索引清理必须先走 FaissVecDB.delete（内部按 doc_id 反查 int id 并删
   embedding），后兜底 SQL——旧实现只删行，幽灵向量永不回收。
3. ML-10: 会话缓冲渲染成摘要解析器认识的 "[序号] 发送者: 内容" 格式，
   speaker 不再全落 unknown。
4. WU-04: 记忆维护有真实调度方；purge 未开启时策略把宽限期推到天文数字
   （索引修复照跑、零物理删除）；按日节流。
"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrmai.memory.services.instant_memory_gate import InstantMemoryGate
from astrmai.memory.contracts.memory_query import MemoryWriteRequest
from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline
from astrmai.memory.services.session_memory_summarizer import SessionMemorySummarizer
from astrmai.proactive.proactive_task import ProactiveTask


class PreferenceDedupKeyTests(unittest.TestCase):
    """ML-03：多值偏好共存。"""

    def _claim(self, attribute, value):
        return SimpleNamespace(subject_id="111", entity="user", attribute=attribute, value=value)

    def test_different_like_values_get_distinct_keys(self):
        key_coffee = InstantMemoryGate._authority_dedup_key(self._claim("like", "咖啡"))
        key_cat = InstantMemoryGate._authority_dedup_key(self._claim("like", "猫"))
        self.assertNotEqual(key_coffee, key_cat)

    def test_same_value_normalizes_to_same_key(self):
        key_a = InstantMemoryGate._authority_dedup_key(self._claim("like", "咖 啡"))
        key_b = InstantMemoryGate._authority_dedup_key(self._claim("LIKE", "咖啡"))
        # 属性大小写不影响多值判定，值去空白后一致
        self.assertEqual(key_a.split(":")[-1], key_b.split(":")[-1])

    def test_single_value_attribute_unchanged(self):
        key = InstantMemoryGate._authority_dedup_key(self._claim("display_name", "阿黄"))
        self.assertEqual(key, "111:user:display_name")


class BufferSpeakerRenderTests(unittest.TestCase):
    """ML-10：缓冲行渲染与摘要解析器闭环。"""

    def test_dict_entry_renders_parseable_line(self):
        line = MemoryTurnPipeline._render_buffer_line(3, {"sender": "12345", "text": "我明天去北京"})
        self.assertEqual(line, "[3] 12345: 我明天去北京")

    def test_legacy_string_entry_passthrough(self):
        self.assertEqual(
            MemoryTurnPipeline._render_buffer_line(1, "用户/旁白：旧格式"),
            "用户/旁白：旧格式",
        )

    def test_summarizer_parser_extracts_sender_from_rendered_line(self):
        rendered = "\n".join(
            [
                MemoryTurnPipeline._render_buffer_line(1, {"sender": "12345", "text": "我明天去北京"}),
                MemoryTurnPipeline._render_buffer_line(2, {"sender": "Bot", "text": "好的记住了"}),
            ]
        )
        messages = SessionMemorySummarizer._build_topic_messages(rendered)
        self.assertEqual(messages[0]["sender"], "12345")
        self.assertEqual(messages[1]["sender"], "Bot")
        self.assertNotIn("unknown", {m["sender"] for m in messages})


class FaissAwareCleanupTests(unittest.TestCase):
    """ML-04：faiss 删除先于行删除，嵌入真正回收。"""

    def _projector(self, calls, with_faiss=True, sql_delete_count=2):
        class _Faiss:
            async def delete(self, doc_id):
                calls.append(("faiss", doc_id))

        class _Engine:
            faiss_db = _Faiss() if with_faiss else None

            async def _run_documents_query(self, sql, params, db_path=None):
                return [(7, "doc-abc"), (8, "doc-def")]

            async def _execute_documents_write(self, sql, params, db_path=None):
                calls.append(("sql_delete", params[0]))
                return sql_delete_count

        projector = MemoryIndexProjector.__new__(MemoryIndexProjector)
        projector.engine = _Engine()
        projector._pending_projection_ids = set()
        projector._pending_projection_reasons = {}
        projector._pending_projection_scheduled = {}

        async def _fts(ids):
            calls.append(("fts", tuple(ids)))

        projector._delete_fts_rows = _fts
        projector._clear_pending = lambda memory_id: None
        projector._documents_db_path = lambda: None
        return projector

    def test_faiss_delete_runs_before_row_delete(self):
        calls = []
        projector = self._projector(calls)

        deleted = asyncio.run(projector.cleanup_deleted(["mem-1"]))

        self.assertEqual(deleted, 2)
        self.assertEqual(calls[0], ("faiss", "doc-abc"))
        self.assertEqual(calls[1], ("faiss", "doc-def"))
        self.assertEqual(calls[2], ("sql_delete", "mem-1"))
        self.assertEqual(calls[3], ("fts", (7, 8)))

    def test_cleanup_count_uses_projection_ids_when_sql_delete_already_happened(self):
        calls = []
        projector = self._projector(calls, sql_delete_count=0)

        deleted = asyncio.run(projector.cleanup_deleted(["mem-1"]))

        self.assertEqual(deleted, 2)
        self.assertEqual(calls[2], ("sql_delete", "mem-1"))

    def test_consistency_reports_exact_faiss_id_differences(self):
        class _Store:
            async def list_projectable(self):
                return [SimpleNamespace(id="mem-1")]

            async def get_canonical(self, memory_id, include_inactive=False):
                return SimpleNamespace(status="active", visibility="auto_and_tool")

        class _Engine:
            v2_store = _Store()
            faiss_db = SimpleNamespace(
                embedding_storage=SimpleNamespace(
                    index=SimpleNamespace(id_map=[2], ntotal=1),
                ),
            )

            async def _run_documents_query(self, sql, params=(), db_path=None):
                return [(3, '{"canonical_id": "mem-1"}')]

        projector = MemoryIndexProjector(_Engine())
        report = asyncio.run(projector.check_consistency())

        self.assertTrue(report["faiss_id_set_observed"])
        self.assertEqual(report["faiss_ids_missing_from_documents"], [2])
        self.assertEqual(report["document_ids_missing_from_faiss"], [3])

    def test_projectors_sharing_lock_do_not_interleave_writes(self):
        async def run():
            shared_lock = asyncio.Lock()
            first_started = asyncio.Event()
            release_first = asyncio.Event()

            class _Retriever:
                def __init__(self):
                    self.active = 0
                    self.peak = 0
                    self.contents = []

                async def add_memory(self, content, metadata):
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.contents.append(content)
                    if content == "first":
                        first_started.set()
                        await release_first.wait()
                    self.active -= 1

            retriever_instance = _Retriever()

            class _Engine:
                _projection_lock = shared_lock
                v2_store = SimpleNamespace()
                retriever = retriever_instance

                @staticmethod
                def _build_memory_metadata(**kwargs):
                    return dict(kwargs)

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return []

                async def _execute_documents_write(self, sql, params=(), db_path=None):
                    return 0

            projector_a = MemoryIndexProjector(_Engine())
            projector_b = MemoryIndexProjector(_Engine())
            request_a = MemoryWriteRequest(source="test", kind="memory", session_id="s", content="first")
            request_b = MemoryWriteRequest(source="test", kind="memory", session_id="s", content="second")

            first_task = asyncio.create_task(projector_a.project("mem-a", request_a))
            await first_started.wait()
            second_task = asyncio.create_task(projector_b.project("mem-b", request_b))
            await asyncio.sleep(0)
            self.assertEqual(retriever_instance.contents, ["first"])
            release_first.set()
            await asyncio.gather(first_task, second_task)
            self.assertEqual(retriever_instance.contents, ["first", "second"])
            self.assertEqual(retriever_instance.peak, 1)

        asyncio.run(run())

    def test_rebuild_barrier_defers_projection_without_waiting_for_lock(self):
        async def run():
            calls = []

            class _Store:
                async def schedule_projection_retry(self, memory_id, reason, **kwargs):
                    calls.append((memory_id, reason))
                    return True

            class _Engine:
                _projection_rebuild_active = True
                v2_store = _Store()
                retriever = object()

            projector = MemoryIndexProjector(_Engine())
            request = MemoryWriteRequest(source="test", kind="memory", session_id="s", content="queued")
            result = await asyncio.wait_for(projector.project("mem-queued", request), timeout=0.2)

            self.assertFalse(result)
            self.assertEqual(calls, [("mem-queued", "projection_rebuild_in_progress")])
            self.assertEqual(projector.pending_reason("mem-queued"), "projection_rebuild_in_progress")

        asyncio.run(run())

    def test_candidate_projection_does_not_ack_outbox_before_publish(self):
        async def run():
            completed = []

            class _Store:
                async def complete_projection_retry(self, memory_id):
                    completed.append(memory_id)

            class _Engine:
                v2_store = _Store()
                _ack_projection_outbox = False
                _candidate_outbox_candidates = {"mem-candidate"}

            projector = MemoryIndexProjector(_Engine())
            await projector._mark_pending_persisted("mem-candidate", "projection_rebuild_in_progress")
            await projector._clear_pending_persisted("mem-candidate")

            self.assertEqual(completed, [])
            self.assertEqual(projector.candidate_outbox_ids(), {"mem-candidate"})
            await projector.confirm_projection_outbox(projector.candidate_outbox_ids())
            self.assertEqual(completed, ["mem-candidate"])

        asyncio.run(run())

    def test_active_projection_does_not_clear_newer_outbox(self):
        async def run():
            from astrmai.memory.services.v2_store import MemoryV2Store

            data_dir = Path(tempfile.mkdtemp(prefix="astrmai-outbox-active-"))
            store = MemoryV2Store(data_dir / "active.db", data_path=data_dir)

            class _Engine:
                v2_store = store

            projector = MemoryIndexProjector(_Engine())
            await store.schedule_projection_retry("mem-race", "old")
            old_revision = await store.projection_retry_revision("mem-race")
            await store.schedule_projection_retry("mem-race", "new")
            await projector._clear_pending_persisted("mem-race", old_revision)

            self.assertEqual(await store.projection_retry_snapshot(), {"mem-race": "new"})

        asyncio.run(run())

    def test_replacement_cancelled_after_cleanup_keeps_outbox(self):
        async def run():
            calls = []
            started = asyncio.Event()
            release = asyncio.Event()

            class _Store:
                async def projection_retry_revision(self, memory_id):
                    return 1

                async def complete_projection_retry_if_unchanged(self, memory_id, revision):
                    calls.append((memory_id, revision))
                    return True

                async def schedule_projection_retry(self, memory_id, reason, **kwargs):
                    return True

                async def get_by_id(self, memory_id, allow_stale=False):
                    return SimpleNamespace(
                        source="test", kind="memory", session_id="s", persona_id="",
                        content="new", summary="", tags=[], importance=0.5, confidence=0.8,
                        metadata={}, visibility="auto_and_tool",
                    )

            class _Retriever:
                async def add_memory(self, content, metadata):
                    started.set()
                    await release.wait()

            async def delete_document(key):
                return True

            class _Engine:
                v2_store = _Store()
                retriever = _Retriever()
                vec_retriever = SimpleNamespace(delete_document=delete_document)

                @staticmethod
                def _build_memory_metadata(**kwargs):
                    return dict(kwargs)

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [(1, "doc-1")]

                async def _execute_documents_write(self, sql, params=(), db_path=None):
                    return 1

            task = asyncio.create_task(
                MemoryIndexProjector(_Engine()).project(
                    "mem-cancel",
                    MemoryWriteRequest(source="test", kind="memory", session_id="s", content="new"),
                )
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(calls, [])

        asyncio.run(run())

    def test_failed_vector_delete_does_not_add_replacement(self):
        async def run():
            added = []

            async def fail_delete(key):
                raise RuntimeError("delete failed")

            class _Store:
                async def projection_retry_revision(self, memory_id):
                    return 1

                async def schedule_projection_retry(self, memory_id, reason, **kwargs):
                    return True

                async def get_by_id(self, memory_id, allow_stale=False):
                    return SimpleNamespace(
                        source="test", kind="memory", session_id="s", persona_id="",
                        content="new", summary="", tags=[], importance=0.5, confidence=0.8,
                        metadata={}, visibility="auto_and_tool",
                    )

            class _Retriever:
                async def add_memory(self, content, metadata):
                    added.append(content)

            class _Engine:
                v2_store = _Store()
                retriever = _Retriever()
                vec_retriever = SimpleNamespace(delete_document=fail_delete)

                @staticmethod
                def _build_memory_metadata(**kwargs):
                    return dict(kwargs)

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [(1, "doc-1")]

                async def _execute_documents_write(self, sql, params=(), db_path=None):
                    return 0

            result = await MemoryIndexProjector(_Engine()).project(
                "mem-failed-delete",
                MemoryWriteRequest(source="test", kind="memory", session_id="s", content="new"),
            )
            self.assertFalse(result)
            self.assertEqual(added, [])

        asyncio.run(run())

    def test_candidate_processed_outbox_is_not_missing_when_watermark_is_unchanged(self):
        async def run():
            class _Store:
                async def projection_retry_snapshot_with_revisions(self):
                    return {"mem-1": {"reason": "pending", "revision": 7}}

                async def list_projectable(self):
                    return [SimpleNamespace(id="mem-1")]

                async def get_canonical(self, memory_id, include_inactive=False):
                    return SimpleNamespace(status="active", visibility="auto_and_tool")

            class _Engine:
                v2_store = _Store()
                _ack_projection_outbox = False
                _candidate_outbox_candidates = {"mem-1"}
                _candidate_outbox_watermarks = {"mem-1": 7.0}
                faiss_db = None

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [(1, '{"canonical_id":"mem-1"}')]

            projector = MemoryIndexProjector(_Engine())
            projector._candidate_outbox_confirmations = {"mem-1": 7.0}
            report = await projector.check_consistency()
            self.assertEqual(report["missing_projection_ids"], [])
            self.assertEqual(report["pending_projection_reasons"], {})

        asyncio.run(run())

    def test_candidate_deferred_snapshot_is_not_truncated(self):
        async def run():
            count = 1001
            ids = [f"mem-{index}" for index in range(count)]

            class _Store:
                async def projection_retry_snapshot_with_revisions(self):
                    return {
                        memory_id: {"reason": "deferred", "revision": index + 1}
                        for index, memory_id in enumerate(ids)
                    }

                async def list_projectable(self):
                    return [SimpleNamespace(id=memory_id) for memory_id in ids]

                async def get_canonical(self, memory_id, include_inactive=False):
                    return SimpleNamespace(status="active", visibility="auto_and_tool")

            class _Engine:
                v2_store = _Store()
                _ack_projection_outbox = False

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [
                        (index + 1, '{"canonical_id":"%s"}' % memory_id)
                        for index, memory_id in enumerate(ids)
                    ]

            report = await MemoryIndexProjector(_Engine()).check_consistency()
            self.assertEqual(report["pending_projection_count"], count)
            self.assertEqual(len(report["deferred_projection_ids"]), count)
            self.assertEqual(report["missing_projection_ids"], [])

        asyncio.run(run())

    def test_candidate_processed_outbox_remains_deferred_after_same_id_update(self):
        async def run():
            class _Store:
                async def projection_retry_snapshot_with_revisions(self):
                    return {"mem-1": {"reason": "pending", "revision": 8}}

                async def list_projectable(self):
                    return [SimpleNamespace(id="mem-1")]

                async def get_canonical(self, memory_id, include_inactive=False):
                    return SimpleNamespace(status="active", visibility="auto_and_tool")

            class _Engine:
                v2_store = _Store()
                _ack_projection_outbox = False
                _candidate_outbox_candidates = {"mem-1"}
                _candidate_outbox_watermarks = {"mem-1": 7.0}
                faiss_db = None

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [(1, '{"canonical_id":"mem-1"}')]

            projector = MemoryIndexProjector(_Engine())
            projector._candidate_outbox_confirmations = {"mem-1": 7.0}
            report = await projector.check_consistency()
            self.assertEqual(report["missing_projection_ids"], [])
            self.assertEqual(report["deferred_projection_ids"], ["mem-1"])

        asyncio.run(run())

    def test_outbox_revision_prevents_same_timestamp_aba_delete(self):
        async def run():
            from astrmai.memory.services.v2_store import MemoryV2Store

            data_dir = Path(tempfile.mkdtemp(prefix="astrmai-outbox-"))
            store = MemoryV2Store(data_dir / "outbox-revision-aba.db", data_path=data_dir)
            store._now = lambda: 100.0
            await store.schedule_projection_retry("mem-aba", "first")
            first = await store.projection_retry_watermarks()
            await store.schedule_projection_retry("mem-aba", "second")
            second = await store.projection_retry_watermarks()

            self.assertEqual(first["mem-aba"], 1)
            self.assertEqual(second["mem-aba"], 2)
            self.assertFalse(
                await store.complete_projection_retry_if_unchanged("mem-aba", first["mem-aba"])
            )
            self.assertEqual(await store.projection_retry_snapshot(), {"mem-aba": "second"})
            self.assertTrue(
                await store.complete_projection_retry_if_unchanged("mem-aba", second["mem-aba"])
            )
            self.assertEqual(await store.projection_retry_snapshot(), {})

        asyncio.run(run())

    def test_outbox_revision_increments_for_concurrent_schedule_calls(self):
        async def run():
            from astrmai.memory.services.v2_store import MemoryV2Store

            data_dir = Path(tempfile.mkdtemp(prefix="astrmai-outbox-concurrent-"))
            store = MemoryV2Store(data_dir / "outbox-concurrent.db", data_path=data_dir)
            await asyncio.gather(
                *(store.schedule_projection_retry("mem-concurrent", f"reason-{index}") for index in range(20))
            )
            watermarks = await store.projection_retry_watermarks()
            self.assertEqual(watermarks["mem-concurrent"], 20)

        asyncio.run(run())

    def test_rebuild_lock_uses_separate_longer_wait_budget(self):
        async def run():
            shared_lock = asyncio.Lock()
            started = asyncio.Event()
            release = asyncio.Event()

            class _Config:
                class timing:
                    projection_rebuild_lock_timeout_sec = 0.2

            class _Store:
                async def list_projectable(self):
                    return []

            class _Engine:
                _projection_lock = shared_lock
                config = _Config()
                v2_store = _Store()

                async def _ensure_faiss_initialized(self):
                    started.set()
                    await release.wait()
                    return True

            projector = MemoryIndexProjector(_Engine())
            owner = await projector._acquire_projection_lock()
            self.assertTrue(owner)
            task = asyncio.create_task(projector.rebuild_all())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            release.set()
            shared_lock.release()
            await task

        asyncio.run(run())

    def test_id_mismatch_repair_triggers_full_rebuild(self):
        async def run():
            calls = []

            class _Engine:
                _projection_lock = asyncio.Lock()
                v2_store = SimpleNamespace()
                retriever = object()

            projector = MemoryIndexProjector(_Engine())

            async def rebuild_all():
                calls.append("rebuild")
                return 3

            async def check_consistency():
                return {"faiss_ids_missing_from_documents": [], "document_ids_missing_from_faiss": []}

            projector.rebuild_all = rebuild_all
            projector.check_consistency = check_consistency
            result = await projector.repair_consistency(
                {
                    "faiss_ids_missing_from_documents": [2],
                    "document_ids_missing_from_faiss": [3],
                }
            )

            self.assertEqual(calls, ["rebuild"])
            self.assertEqual(result["rebuilt_index"], 3)

        asyncio.run(run())

    def test_without_faiss_retains_rows_until_vector_delete_is_available(self):
        calls = []
        projector = self._projector(calls, with_faiss=False)

        deleted = asyncio.run(projector.cleanup_deleted(["mem-1"]))

        self.assertEqual(deleted, 0)
        self.assertEqual(calls, [])
        self.assertEqual(projector.pending_reason("mem-1"), "vector_delete_unavailable")

    def test_retry_worker_treats_budget_drain_rejection_as_shutdown(self):
        async def run():
            from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskQueueFull

            class _Budget:
                def status(self):
                    return {"draining": True}

                async def run(self, *_args, **_kwargs):
                    raise BackgroundTaskQueueFull("background task budget is draining")

            class _Store:
                async def list_due_projection_retries(self, *, limit=20):
                    return []

            class _Engine:
                background_task_budget = _Budget()
                v2_store = _Store()

            projector = MemoryIndexProjector(_Engine())
            await projector._retry_loop()
            status = projector.describe_status()
            self.assertEqual(status["retry_rejected_by_shutdown"], 1)
            self.assertEqual(status["retry_failure_count"], 0)
            self.assertFalse(status["retry_worker_alive"])

        asyncio.run(run())

    def test_failed_faiss_delete_retains_rows_for_retry(self):
        calls = []

        class _Faiss:
            async def delete(self, doc_id):
                calls.append(("faiss", doc_id))
                raise RuntimeError("faiss unavailable")

        class _Engine:
            faiss_db = _Faiss()

            async def _run_documents_query(self, sql, params, db_path=None):
                return [(7, "doc-abc")]

            async def _execute_documents_write(self, sql, params, db_path=None):
                calls.append(("sql_delete", params[0]))
                return 1

        projector = MemoryIndexProjector.__new__(MemoryIndexProjector)
        projector.engine = _Engine()
        projector._pending_projection_ids = set()
        projector._pending_projection_reasons = {}
        projector._pending_projection_scheduled = {}
        projector._delete_fts_rows = lambda ids: None
        projector._documents_db_path = lambda: None

        async def _fts(ids):
            calls.append(("fts", tuple(ids)))

        projector._delete_fts_rows = _fts

        deleted = asyncio.run(projector.cleanup_deleted(["mem-1"]))

        self.assertEqual(deleted, 0)
        self.assertEqual(calls, [("faiss", "doc-abc")])
        self.assertEqual(projector.pending_reason("mem-1"), "vector_delete_failed")

    def test_partial_vector_delete_cleans_successful_document_fts(self):
        async def run():
            calls = []

            class _Faiss:
                async def delete(self, doc_key):
                    calls.append(("faiss", doc_key))
                    if doc_key == "doc-b":
                        raise RuntimeError("transient delete failure")

            class _Engine:
                faiss_db = _Faiss()

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    if "memories_fts" in sql:
                        return []
                    return [(7, "doc-a"), (8, "doc-b")]

                async def _execute_documents_write(self, sql, params=(), db_path=None):
                    calls.append(("sql", sql.split()[0], params[0]))
                    return 1

            projector = MemoryIndexProjector.__new__(MemoryIndexProjector)
            projector.engine = _Engine()
            projector._pending_projection_ids = set()
            projector._pending_projection_reasons = {}
            projector._pending_projection_scheduled = {}
            projector._documents_db_path = lambda: None

            deleted = await projector.cleanup_deleted(["mem-partial"])

            self.assertEqual(deleted, 1)
            self.assertEqual(calls[0:2], [("faiss", "doc-a"), ("faiss", "doc-b")])
            self.assertIn(("sql", "DELETE", 7), calls)
            self.assertIn(("sql", "DELETE", 7), calls)
            self.assertEqual(projector.pending_reason("mem-partial"), "vector_delete_failed")

        asyncio.run(run())

    def test_fts_delete_failure_keeps_projection_pending(self):
        async def run():
            class _Faiss:
                async def delete(self, doc_key):
                    return True

            class _Engine:
                faiss_db = _Faiss()

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [] if "memories_fts" in sql else [(7, "doc-a")]

                async def _execute_documents_write(self, sql, params=(), db_path=None):
                    if "memories_fts" in sql:
                        raise RuntimeError("fts unavailable")
                    return 1

            projector = MemoryIndexProjector.__new__(MemoryIndexProjector)
            projector.engine = _Engine()
            projector._pending_projection_ids = set()
            projector._pending_projection_reasons = {}
            projector._pending_projection_scheduled = {}
            projector._documents_db_path = lambda: None

            deleted = await projector.cleanup_deleted(["mem-fts-failure"])

            self.assertEqual(deleted, 1)
            self.assertEqual(projector.pending_reason("mem-fts-failure"), "fts_delete_failed")

        asyncio.run(run())

    def test_missing_canonical_with_failed_projection_cleanup_returns_false(self):
        async def run():
            class _Store:
                async def get_by_id(self, memory_id, allow_stale=False):
                    return None

                async def schedule_projection_retry(self, memory_id, reason, **kwargs):
                    return True

            class _Faiss:
                async def delete(self, doc_key):
                    raise RuntimeError("delete unavailable")

            class _Engine:
                v2_store = _Store()
                retriever = object()
                faiss_db = _Faiss()

                async def _run_documents_query(self, sql, params=(), db_path=None):
                    return [(7, "doc-a")]

                async def _execute_documents_write(self, sql, params=(), db_path=None):
                    return 0

            projector = MemoryIndexProjector(_Engine())
            result = await projector.project("missing-canonical")

            self.assertFalse(result)
            self.assertEqual(projector.pending_reason("missing-canonical"), "vector_delete_failed")

        asyncio.run(run())

    def test_sql_cleanup_failure_is_scheduled_for_retry(self):
        calls = []

        class _Faiss:
            async def delete(self, doc_id):
                calls.append(("faiss", doc_id))

        class _Engine:
            faiss_db = _Faiss()
            v2_store = None

            async def _run_documents_query(self, sql, params, db_path=None):
                return [(7, "doc-abc")]

            async def _execute_documents_write(self, sql, params, db_path=None):
                calls.append(("sql_delete", params[0]))
                raise RuntimeError("database locked")

        projector = MemoryIndexProjector.__new__(MemoryIndexProjector)
        projector.engine = _Engine()
        projector._pending_projection_ids = set()
        projector._pending_projection_reasons = {}
        projector._pending_projection_scheduled = {}
        projector._delete_fts_rows = lambda ids: None
        projector._documents_db_path = lambda: None

        deleted = asyncio.run(projector.cleanup_deleted(["mem-1"]))

        self.assertEqual(deleted, 0)
        self.assertEqual(projector.pending_reason("mem-1"), "documents_delete_failed")


class MaintenanceScheduleTests(unittest.TestCase):
    """WU-04：调度接通、purge 分步、按日节流。"""

    def _task(self, *, schedule=True, purge=False, runs=None):
        runs = runs if runs is not None else []

        class _Maintenance:
            async def run_once(self, *, policy=None, now=None):
                runs.append(dict(policy or {}))
                return {"physically_deleted": 0, "projection_deleted": 0, "marked_stale": 0, "index_repair": {}, "errors": []}

        task = ProactiveTask.__new__(ProactiveTask)
        task.config = SimpleNamespace(
            memory=SimpleNamespace(
                maintenance_schedule_enabled=schedule,
                maintenance_purge_enabled=purge,
            )
        )
        task.memory_engine = SimpleNamespace(maintenance_service=_Maintenance())
        return task, runs

    def test_purge_disabled_uses_astronomical_grace(self):
        task, runs = self._task(purge=False)

        asyncio.run(task._run_memory_store_maintenance())

        self.assertEqual(len(runs), 1)
        self.assertGreater(runs[0]["stale_grace_seconds"], 1e11)
        self.assertGreater(runs[0]["rejected_jargon_grace_seconds"], 1e11)

    def test_purge_enabled_uses_default_policy(self):
        task, runs = self._task(purge=True)

        asyncio.run(task._run_memory_store_maintenance())

        self.assertEqual(runs, [{}])

    def test_daily_throttle_skips_second_run(self):
        task, runs = self._task()

        asyncio.run(task._run_memory_store_maintenance())
        asyncio.run(task._run_memory_store_maintenance())

        self.assertEqual(len(runs), 1)

    def test_schedule_disabled_never_runs(self):
        task, runs = self._task(schedule=False)

        asyncio.run(task._run_memory_store_maintenance())

        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
