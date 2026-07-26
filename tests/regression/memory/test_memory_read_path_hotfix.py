"""OPT-06 回归测试：记忆读取链恢复（ML-05 / ML-02(含 RT-12) / ML-06）。

守护不变式：
1. ML-05: think1 门在关键词未命中时按语义意图（identity/preference/location…）放行，
   "我叫什么名字"这类问句必须能触发检索；配置可关闭回退旧行为。
2. ML-02: 深检索 rerank/guidance 的 LLM 调用受 turn 预算钳制（预算耗尽直接跳过）、
   候选数不超过 top_k 时跳过整次 rerank；react 循环受总预算约束且逐步带超时。
3. ML-06: 发送后内联 claim 抽取只做规则（allow_llm=False），不再同步等 LLM。
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from astrmai.infrastructure.runtime.turn_call_ledger import (
    configure_turn_budget,
    turn_telemetry_scope,
)
from astrmai.memory.contracts.memory_query import MemoryQuery
from astrmai.memory.retrieval.react_retriever import ReActRetriever
from astrmai.memory.services.memory_claim_service import MemoryClaimExtractor
from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService


class _Event:
    def __init__(self, created_at_offset_sec: float = 0.0):
        self._extras = {}
        if created_at_offset_sec:
            self._extras["astrmai_turn_created_at"] = time.time() + created_at_offset_sec

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class Think1SemanticGateTests(unittest.TestCase):
    """ML-05：身份/偏好问句在 think1 放行。"""

    def _refiner(self, semantic_enabled=True):
        refiner = PromptRefiner.__new__(PromptRefiner)
        refiner.config = SimpleNamespace(
            memory=SimpleNamespace(think1_semantic_intent_enabled=semantic_enabled)
        )
        return refiner

    def test_identity_question_passes_without_keyword(self):
        self.assertTrue(self._refiner()._think1_memory_gate_passes("我叫什么名字"))

    def test_preference_question_passes(self):
        self.assertTrue(self._refiner()._think1_memory_gate_passes("我喜欢吃什么来着"))

    def test_smalltalk_still_blocked(self):
        self.assertFalse(self._refiner()._think1_memory_gate_passes("呜呜呜"))

    def test_keyword_path_unchanged(self):
        self.assertTrue(self._refiner(semantic_enabled=False)._think1_memory_gate_passes("你还记得那件事吗"))

    def test_config_off_restores_keyword_only(self):
        self.assertFalse(self._refiner(semantic_enabled=False)._think1_memory_gate_passes("我叫什么名字"))


class InlineClaimExtractionTests(unittest.TestCase):
    """ML-06：内联抽取 allow_llm=False 不得触碰网关。"""

    def test_allow_llm_false_skips_gateway(self):
        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                raise AssertionError("allow_llm=False 时不得调用 LLM")

        extractor = MemoryClaimExtractor(_Gateway())
        extractor._rule_extract = lambda **kwargs: []

        claims = asyncio.run(
            extractor.extract(user_text="随便聊聊今天的天气", allow_llm=False)
        )

        self.assertEqual(claims, [])

    def test_rule_claims_still_returned(self):
        extractor = MemoryClaimExtractor(None)
        sentinel = [SimpleNamespace(entity="e", attribute="a", value="v")]
        extractor._rule_extract = lambda **kwargs: sentinel

        claims = asyncio.run(extractor.extract(user_text="规则命中", allow_llm=False))

        self.assertEqual(claims, sentinel)


class DeepRetrievalBudgetTests(unittest.TestCase):
    """ML-02：deep json 受预算钳制；小候选集跳过 rerank。"""

    def _service_with_gateway(self, gateway):
        service = MemoryRetrievalService.__new__(MemoryRetrievalService)
        service.engine = SimpleNamespace(gateway=gateway)
        return service

    def test_deep_json_skipped_when_budget_exhausted(self):
        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                raise AssertionError("预算耗尽时不得调用 LLM")

        service = self._service_with_gateway(_Gateway())
        event = _Event(created_at_offset_sec=-400.0)

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                return await service._call_deep_json("prompt", scope_id="chat-1")

        self.assertEqual(asyncio.run(_run()), {})

    def test_deep_json_carries_timeout_and_lane(self):
        captured = {}

        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                captured.update(kwargs)
                return {"ids": []}

        service = self._service_with_gateway(_Gateway())

        result = asyncio.run(service._call_deep_json("prompt", scope_id="chat-1"))

        self.assertEqual(result, {"ids": []})
        self.assertAlmostEqual(captured.get("timeout_override"), 12.0, places=3)
        self.assertIsNotNone(captured.get("lane_key"))
        self.assertEqual(captured.get("max_retries_override"), 0)

    def test_small_candidate_set_skips_rerank(self):
        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                raise AssertionError("候选数<=top_k 时不得调用 rerank LLM")

        service = self._service_with_gateway(_Gateway())
        candidates = [
            SimpleNamespace(id=f"m{i}", kind="topic", summary="s", content="c", importance=0.5,
                            confidence=0.5, status="active", relevance_score=0.5, metadata={})
            for i in range(3)
        ]
        query = MemoryQuery(query="q", session_id="chat-1", top_k=5)

        result = asyncio.run(service._rerank_candidates(query, candidates))

        self.assertEqual(result, candidates)


class ReactBudgetTests(unittest.TestCase):
    """ML-02(RT-12)：react 循环受总预算约束、逐步带超时。"""

    def test_react_skips_when_budget_exhausted(self):
        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                raise AssertionError("预算耗尽时 react 不得起步")

        retriever = ReActRetriever(gateway=_Gateway())
        event = _Event(created_at_offset_sec=-400.0)

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                return await retriever.retrieve("问题", "chat-1", retrieve_keys=["person"])

        self.assertEqual(asyncio.run(_run()), "")

    def test_react_step_carries_timeout_override(self):
        captured = {}

        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                captured.update(kwargs)
                return '{"thinking":"done","tool":"found_answer","args":{"answer":"42"}}'

        retriever = ReActRetriever(gateway=_Gateway())

        async def _noop_trace(**kwargs):
            return None

        retriever._save_trace = _noop_trace

        answer = asyncio.run(retriever.retrieve("问题", "chat-1", retrieve_keys=["person"]))

        self.assertIn("42", answer)
        self.assertIn("timeout_override", captured)
        self.assertLessEqual(captured["timeout_override"], 8.0)


if __name__ == "__main__":
    unittest.main()
