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
import unittest
from types import SimpleNamespace

from astrmai.memory.services.instant_memory_gate import InstantMemoryGate
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

    def _projector(self, calls, with_faiss=True):
        class _Faiss:
            async def delete(self, doc_id):
                calls.append(("faiss", doc_id))

        class _Engine:
            faiss_db = _Faiss() if with_faiss else None

            async def _run_documents_query(self, sql, params, db_path=None):
                return [(7, "doc-abc"), (8, "doc-def")]

            async def _execute_documents_write(self, sql, params, db_path=None):
                calls.append(("sql_delete", params[0]))
                return 2

        projector = MemoryIndexProjector.__new__(MemoryIndexProjector)
        projector.engine = _Engine()

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

    def test_without_faiss_falls_back_to_sql_count(self):
        calls = []
        projector = self._projector(calls, with_faiss=False)

        deleted = asyncio.run(projector.cleanup_deleted(["mem-1"]))

        self.assertEqual(deleted, 2)
        self.assertEqual(calls[0], ("sql_delete", "mem-1"))

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
        self.assertEqual(projector.pending_reason("mem-1"), "cleanup_error:RuntimeError")


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
