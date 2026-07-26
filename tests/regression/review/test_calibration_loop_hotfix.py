"""OPT-04 回归测试：人工校准与审核闭环（WU-01/02/03/05/07/08/10）。

守护不变式：
1. WU-02: UI 传入的绝对权重按"相对当前权重"换算 delta（预填值原样保存不得漂移）。
2. WU-01: 编辑通过/编辑驳回时人工修订文本必须随 approve/reject 落库。
3. WU-03: 人工待审队列必须包含 pending_human（auto-check 口径不得复用为人工口径）。
4. WU-05: 表达审批通过/驳回后同步召回索引投影。
5. WU-07: 黑话驳回走软墓碑（status=rejected），不再物理删除。
6. WU-08: /learning/cooldowns 读取真实属性 _recent_pattern_keys 并序列化。
7. WU-10: 黑话关键字搜索先过滤后分页，total 为过滤后计数。
"""

import asyncio
import unittest
from types import SimpleNamespace

from astrmai.learning.review.review_service import ExpressionReviewService
from astrmai.webui.backend.services.admin_ui_service import AdminUiService
from astrmai.webui.backend.services.learningservice import LearningService
from astrmai.webui.backend.services.memory_ui_service import MemoryUiService
from astrmai.webui.backend.services.review_ui_service import ReviewUiService


class _RecordingPatternService:
    def __init__(self, weight=2.0):
        self._weight = weight
        self.update_review_calls = []
        self.adjust_weight_calls = []

    async def get_pattern(self, pattern_id):
        return SimpleNamespace(
            id=pattern_id,
            weight=self._weight,
            group_id="g1",
            situation="",
            source="mining",
            expression="旧表达",
            status="review_pending",
            review_status="pending",
            create_time=0.0,
            last_active_time=0.0,
            metadata={},
        )

    async def update_review(self, pattern_id, **kwargs):
        self.update_review_calls.append({"pattern_id": pattern_id, **kwargs})
        return await self.get_pattern(pattern_id)

    async def adjust_weight(self, pattern_id, delta):
        self.adjust_weight_calls.append((pattern_id, delta))


class _RecordingProjector:
    def __init__(self):
        self.projected = []
        self.cleaned = []

    async def project(self, memory_id):
        self.projected.append(str(memory_id))

    async def cleanup_deleted(self, ids):
        self.cleaned.extend(str(i) for i in ids)


class _StubPluginApi:
    def __init__(self, pattern_service, projector):
        self._pattern_service = pattern_service
        self._projector = projector
        self.submit_review_calls = []

    def get_expression_pattern_service(self):
        return self._pattern_service

    def get_index_projector(self):
        return self._projector

    async def submit_review(self, **kwargs):
        self.submit_review_calls.append(kwargs)
        return {"id": kwargs.get("pattern_id", "")}


class AbsoluteWeightTests(unittest.TestCase):
    """WU-02：current=2.0 提交 2.0，delta 必须为 0（旧代码得 +1.0 → 漂移到 3.0）。"""

    def test_weight_delta_relative_to_current_weight(self):
        pattern_service = _RecordingPatternService(weight=2.0)
        api = _StubPluginApi(pattern_service, _RecordingProjector())
        service = ReviewUiService(api, db_factory=None)

        result = asyncio.run(service.submit_review("p1", "approve", weight=2.0))

        self.assertEqual(result.get("status"), "ok")
        self.assertEqual(len(api.submit_review_calls), 1)
        self.assertAlmostEqual(api.submit_review_calls[0]["weight_delta"], 0.0, places=6)

    def test_weight_delta_moves_toward_requested_absolute(self):
        pattern_service = _RecordingPatternService(weight=0.5)
        api = _StubPluginApi(pattern_service, _RecordingProjector())
        service = ReviewUiService(api, db_factory=None)

        asyncio.run(service.submit_review("p1", "approve", weight=1.5))

        self.assertAlmostEqual(api.submit_review_calls[0]["weight_delta"], 1.0, places=6)


class ReplacementPersistedTests(unittest.TestCase):
    """WU-01：approve/reject 携带人工修订文本。"""

    def _service_with_stub(self):
        pattern_service = _RecordingPatternService()
        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=pattern_service))
        return ExpressionReviewService(db), pattern_service

    def test_approve_with_replacement_persists_edit(self):
        service, pattern_service = self._service_with_stub()

        asyncio.run(
            service.submit_review(
                "p1",
                "approved",
                "webui",
                replacement_expression="人工修订后的表达",
            )
        )

        self.assertEqual(len(pattern_service.update_review_calls), 1)
        call = pattern_service.update_review_calls[0]
        self.assertEqual(call.get("replacement_expression"), "人工修订后的表达")
        self.assertTrue(call.get("apply_replacement"))
        self.assertEqual(call.get("review_status"), "approved")

    def test_reject_with_replacement_persists_edit(self):
        service, pattern_service = self._service_with_stub()

        asyncio.run(
            service.submit_review(
                "p1",
                "rejected",
                "webui",
                replacement_expression="记录驳回理由的修订稿",
            )
        )

        call = pattern_service.update_review_calls[0]
        self.assertEqual(call.get("replacement_expression"), "记录驳回理由的修订稿")
        self.assertTrue(call.get("apply_replacement"))

    def test_plain_approve_without_replacement_unchanged(self):
        service, pattern_service = self._service_with_stub()

        asyncio.run(service.submit_review("p1", "approved", "webui"))

        call = pattern_service.update_review_calls[0]
        self.assertNotIn("replacement_expression", call)


class PendingHumanQueueTests(unittest.TestCase):
    """WU-03：人工待审队列包含 pending_human 且不复用 auto-check 口径。"""

    def test_pending_human_visible_in_queue(self):
        records = [
            SimpleNamespace(id="a", review_status="pending", expression="e1", group_id="g", status="review_pending"),
            SimpleNamespace(id="b", review_status="pending_human", expression="e2", group_id="g", status="review_pending"),
            SimpleNamespace(id="c", review_status="approved", expression="e3", group_id="g", status="active"),
        ]

        class _PatternService:
            async def list_patterns(self, group_id, **kwargs):
                return records

            async def list_reviewable_patterns(self, **kwargs):
                raise AssertionError("人工队列不得复用 auto-check 口径 list_reviewable_patterns")

        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=_PatternService()))
        service = ExpressionReviewService(db)
        service._serialize_pattern = lambda p: {"id": p.id, "review_status": p.review_status}

        rows = asyncio.run(service.list_pending_reviews())

        ids = {row["id"] for row in rows}
        self.assertIn("a", ids)
        self.assertIn("b", ids)
        self.assertNotIn("c", ids)


class ProjectionSyncTests(unittest.TestCase):
    """WU-05：审批后同步召回索引投影。"""

    def test_approve_projects_and_reject_cleans(self):
        pattern_service = _RecordingPatternService()
        projector = _RecordingProjector()
        api = _StubPluginApi(pattern_service, projector)
        service = ReviewUiService(api, db_factory=None)

        asyncio.run(service.submit_review("p1", "approve"))
        asyncio.run(service.submit_review("p2", "reject"))

        self.assertIn("p1", projector.projected)
        self.assertIn("p2", projector.cleaned)


class JargonRejectTombstoneTests(unittest.TestCase):
    """WU-07：驳回走软墓碑而非物理删除。"""

    def test_reject_uses_status_update_not_hard_delete(self):
        service = MemoryUiService.__new__(MemoryUiService)
        service.plugin_api = SimpleNamespace(get_v2_store=lambda: object())
        captured = {}

        async def _fake_update(jargon_id, payload):
            captured["id"] = jargon_id
            captured["payload"] = dict(payload)
            return {"status": "ok", "changed": True}

        async def _fail_delete(jargon_id):
            raise AssertionError("reject 不得再走物理删除 delete_jargon")

        service.update_jargon = _fake_update
        service.delete_jargon = _fail_delete

        result = asyncio.run(service.reject_jargon("j1"))

        self.assertEqual(captured["id"], "j1")
        self.assertEqual(captured["payload"].get("status"), "rejected")
        self.assertTrue(result.get("tombstone"))
        self.assertEqual(result.get("action"), "reject")


class CooldownEndpointTests(unittest.TestCase):
    """WU-08：冷却面板读取真实属性并序列化。"""

    def _planner_stub(self):
        selector = SimpleNamespace()
        selector._recent_pattern_keys = {"chat-1": [("situation-a", "expression-a")]}
        return SimpleNamespace(expression_selector=selector)

    def test_admin_cooldowns_return_real_keys(self):
        api = SimpleNamespace(get_planner=lambda: self._planner_stub())
        service = AdminUiService.__new__(AdminUiService)
        service.plugin_api = api

        result = asyncio.run(service.expression_cooldowns())

        recent = result["data"]["recent_patterns"]
        self.assertEqual(recent, {"chat-1": [["situation-a", "expression-a"]]})
        self.assertTrue(result["runtime_bound"])

    def test_learning_cooldowns_return_real_keys(self):
        api = SimpleNamespace(get_planner=lambda: self._planner_stub())
        service = LearningService.__new__(LearningService)
        service.plugin_api = api

        result = asyncio.run(service.expression_cooldowns())

        self.assertEqual(
            result["data"]["recent_patterns"],
            {"chat-1": [["situation-a", "expression-a"]]},
        )


class JargonSearchPaginationTests(unittest.TestCase):
    """WU-10：关键字先过滤后分页，total 为过滤后计数。"""

    def _store_with_rows(self, total_rows=30, hit_index=27):
        rows = []
        for index in range(total_rows):
            content = "目标黑话" if index == hit_index else f"普通词{index}"
            rows.append({"content": content, "meaning": "", "scene": "", "examples": []})

        class _Store:
            async def list_canonical(self, *, kind, status, session_id, limit, offset):
                page = rows[offset : offset + limit]
                return {"items": page, "total": len(rows)}

        return _Store()

    def _service(self, store):
        service = MemoryUiService.__new__(MemoryUiService)
        service.db_factory = None
        service.plugin_api = SimpleNamespace(
            get_v2_store=lambda: store,
            get_memory_engine=lambda: None,
        )
        service._canonical_jargon_view = lambda item: item
        return service

    def test_cross_page_hit_found_with_filtered_total(self):
        # 命中项在第 28 行（默认页大小 25 时落在第 2 页），搜索第 1 页必须能召回
        service = self._service(self._store_with_rows(total_rows=30, hit_index=27))

        result = asyncio.run(service.list_jargon(query="目标黑话", limit=25, offset=0))

        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["content"], "目标黑话")
        self.assertFalse(result.get("search_scan_capped"))

    def test_no_query_keeps_store_total(self):
        service = self._service(self._store_with_rows(total_rows=30))

        result = asyncio.run(service.list_jargon(query="", limit=25, offset=0))

        self.assertEqual(result["total"], 30)
        self.assertEqual(len(result["items"]), 25)


if __name__ == "__main__":
    unittest.main()
