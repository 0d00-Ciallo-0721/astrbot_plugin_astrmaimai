from __future__ import annotations

import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Event:
    def __init__(self, text="", **extras):
        self.message_str = text
        self.unified_msg_origin = "chat-1"
        self._extras = dict(extras)

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"


class _Store:
    def __init__(self, candidates=None):
        self.candidates = list(candidates or [])
        self.calls = []

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return list(self.candidates)

    async def batch_get_by_ids(self, _ids, allow_stale=False):
        return {}


class MemoryQueryOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(cls.temp_dir.name)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    @staticmethod
    def _build_query(builder, event, text, envelope=None, top_k=5):
        return builder.build(
            event=event,
            raw_query=text,
            prompt_envelope=envelope,
            session_id="chat-1",
            persona_id="persona-1",
            sender_id="user-1",
            top_k=top_k,
            policy="light",
            think_level=2,
            retrieve_keys=[],
            allow_stale=False,
            metadata={"visibility_mode": "auto"},
        )

    def test_builder_off_preserves_legacy_query_and_top_k(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        builder = MemoryQueryBuilder()
        event = _Event(astrmai_memory_query_builder_enabled=False)
        query = self._build_query(builder, event, "  我喜欢吃什么  ", top_k=7)

        self.assertEqual(query.query, "  我喜欢吃什么  ")
        self.assertEqual(query.top_k, 7)
        self.assertNotIn("primary_intent", query.metadata)
        self.assertNotIn("candidate_limit", query.metadata)

    def test_builder_off_reaches_store_with_legacy_query(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        store = _Store()
        query = self._build_query(
            MemoryQueryBuilder(),
            _Event(astrmai_memory_query_builder_enabled=False),
            "  原始查询  ",
            top_k=7,
        )

        asyncio.run(MemoryRetrievalService(store).retrieve(query))

        self.assertEqual(store.calls[0][0], "  原始查询  ")
        self.assertEqual(store.calls[0][1]["top_k"], 7)
        self.assertIsNone(store.calls[0][1]["candidate_limit"])

    def test_normal_query_keeps_main_query_and_does_not_expand_recall(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        query = self._build_query(MemoryQueryBuilder(), _Event(), "我喜欢吃什么")

        self.assertEqual(query.query, "我喜欢吃什么")
        self.assertEqual(query.metadata["primary_intent"], "food_preference")
        self.assertIn("吃", query.metadata["expansion_terms"])
        self.assertNotIn("candidate_limit", query.metadata)
        self.assertFalse(query.metadata["intent_rerank_enabled"])

    def test_common_queries_keep_normalized_query_as_retrieval_query(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        builder = MemoryQueryBuilder()
        for text in ("我喜欢吃什么", "我叫什么", "我住在哪里", "我讨厌什么"):
            with self.subTest(text=text):
                query = self._build_query(builder, _Event(), text)
                self.assertEqual(query.query, text)
                self.assertEqual(query.metadata["retrieval_query"], text)
                self.assertNotIn("candidate_limit", query.metadata)
                self.assertEqual(query.top_k, 5)

    def test_multi_intent_and_short_entity_detection(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        builder = MemoryQueryBuilder()
        composite = self._build_query(builder, _Event(), "我上次说我不喜欢吃什么")
        short_entity = self._build_query(builder, _Event(), "火锅呢")
        general = self._build_query(builder, _Event(), "我喜欢什么")

        self.assertEqual(composite.metadata["primary_intent"], "food_preference")
        self.assertEqual(
            set(composite.metadata["intents"]),
            {"recent_reference", "dislike", "food_preference"},
        )
        self.assertFalse(short_entity.metadata["is_low_information"])
        self.assertEqual(general.metadata["primary_intent"], "preference_general")
        self.assertNotIn("food_preference", general.metadata["intents"])

    def test_recent_context_uses_previous_context_but_not_current_query(self):
        from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        envelope = PromptEnvelope(
            raw_user_text="那个呢",
            focus_message_text="那个呢",
            direct_context_text="我们刚才聊到用户喜欢吃火锅",
        )
        query = self._build_query(MemoryQueryBuilder(), _Event(), "那个呢", envelope)

        self.assertTrue(query.metadata["is_low_information"])
        self.assertIn("火锅", query.metadata["context_terms"])
        self.assertEqual(query.query.count("那个呢"), 1)
        self.assertIn("火锅", query.query)

    def test_recent_context_uses_compact_terms_not_full_assistant_text(self):
        from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        assistant_text = "我们刚才详细聊了很久，你提到自己偏爱甜食，口味上尤其喜欢芒果做的点心"
        envelope = PromptEnvelope(
            raw_user_text="那个呢",
            focus_message_text="那个呢",
            direct_context_text=assistant_text,
        )

        query = self._build_query(MemoryQueryBuilder(), _Event(), "那个呢", envelope)

        self.assertNotIn(assistant_text, query.query)
        self.assertTrue({"口味", "芒果"} & set(query.metadata["context_terms"]))
        self.assertTrue(all(len(term) <= 8 for term in query.metadata["context_terms"]))

    def test_flag_combinations_keep_query_and_candidate_controls_separate(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        builder = MemoryQueryBuilder()
        rerank_only = self._build_query(
            builder,
            _Event(astrmai_intent_rerank_enabled=True),
            "我喜欢吃什么",
        )
        identity_full = self._build_query(
            builder,
            _Event(
                astrmai_intent_rerank_enabled=True,
                astrmai_adaptive_top_k_enabled=True,
            ),
            "我叫什么",
        )
        preference_full = self._build_query(
            builder,
            _Event(
                astrmai_intent_rerank_enabled=True,
                astrmai_adaptive_top_k_enabled=True,
            ),
            "我喜欢什么",
        )

        self.assertEqual(rerank_only.query, "我喜欢吃什么")
        self.assertEqual(rerank_only.top_k, 5)
        self.assertNotIn("candidate_limit", rerank_only.metadata)
        self.assertTrue(rerank_only.metadata["intent_rerank_enabled"])
        self.assertEqual(identity_full.top_k, 3)
        self.assertLessEqual(identity_full.metadata["candidate_limit"], 12)
        self.assertEqual(preference_full.top_k, 8)
        self.assertEqual(preference_full.metadata["candidate_limit"], 24)

    def test_explicit_candidate_limit_is_forwarded_without_second_multiplier(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        store = _Store()
        service = MemoryRetrievalService(store)
        query = MemoryQuery(
            query="名字",
            session_id="chat-1",
            top_k=6,
            metadata={"candidate_limit": 24},
        )

        result = asyncio.run(service.retrieve(query))

        self.assertEqual(result, [])
        self.assertEqual(store.calls[0][1]["top_k"], 6)
        self.assertEqual(store.calls[0][1]["candidate_limit"], 24)

    def test_builder_flags_drive_actual_store_limits(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        builder = MemoryQueryBuilder()
        rerank_only = self._build_query(
            builder,
            _Event(astrmai_intent_rerank_enabled=True),
            "我喜欢吃什么",
        )
        full = self._build_query(
            builder,
            _Event(
                astrmai_intent_rerank_enabled=True,
                astrmai_adaptive_top_k_enabled=True,
            ),
            "我喜欢什么",
        )
        rerank_store = _Store()
        full_store = _Store()

        asyncio.run(MemoryRetrievalService(rerank_store).retrieve(rerank_only))
        asyncio.run(MemoryRetrievalService(full_store).retrieve(full))

        self.assertEqual(rerank_store.calls[0][1]["top_k"], 5)
        self.assertIsNone(rerank_store.calls[0][1]["candidate_limit"])
        self.assertEqual(full_store.calls[0][1]["top_k"], 8)
        self.assertEqual(full_store.calls[0][1]["candidate_limit"], 24)

    def test_v2_store_candidate_limit_is_backward_compatible(self):
        from astrmai.memory.services.v2_store import MemoryV2Store

        self.assertEqual(MemoryV2Store._resolve_search_limit(5, None), 40)
        self.assertEqual(MemoryV2Store._resolve_search_limit(6, 24), 24)
        self.assertEqual(MemoryV2Store._resolve_search_limit(6, 3), 6)

    def test_builder_on_rerank_off_keeps_existing_candidate_order(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        first = MemoryCandidate(
            id="color",
            kind="preference",
            source="canonical",
            summary="用户喜欢蓝色",
            content="用户喜欢蓝色",
            relevance_score=0.9,
        )
        second = MemoryCandidate(
            id="food",
            kind="preference",
            source="canonical",
            summary="用户喜欢吃火锅",
            content="用户喜欢吃火锅",
            relevance_score=0.8,
        )
        service = MemoryRetrievalService(_Store())
        query = MemoryQuery(
            query="我喜欢吃什么",
            top_k=2,
            metadata={"intent_rerank_enabled": False},
        )

        result = service._finalize_candidates(query, [first, second])

        self.assertEqual([item.id for item in result], ["color", "food"])
        self.assertNotIn("_final_relevance_score", result[0].metadata)

    def test_intent_rerank_prefers_food_and_preserves_score_breakdown(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidates = [
            MemoryCandidate(
                id="color",
                kind="preference",
                source="canonical",
                summary="用户喜欢蓝色",
                content="用户喜欢蓝色",
                relevance_score=0.9,
                metadata={"matched_by": ["canonical_fts"]},
            ),
            MemoryCandidate(
                id="food",
                kind="preference",
                source="canonical",
                summary="用户喜欢吃火锅",
                content="用户喜欢吃火锅",
                relevance_score=0.85,
                metadata={"matched_by": ["canonical_fts", "faiss"]},
            ),
            MemoryCandidate(
                id="sport",
                kind="preference",
                source="canonical",
                summary="用户讨厌跑步",
                content="用户讨厌跑步",
                relevance_score=0.2,
            ),
        ]
        service = MemoryRetrievalService(_Store())
        query = MemoryQuery(
            query="我喜欢吃什么",
            top_k=3,
            metadata={
                "intent_rerank_enabled": True,
                "intents": ["food_preference"],
            },
        )

        result = service._finalize_candidates(query, candidates)

        self.assertEqual(result[0].id, "food")
        self.assertIn("_base_relevance_score", result[0].metadata)
        self.assertIn("_final_relevance_score", result[0].metadata)
        self.assertIn("intent", result[0].metadata["_score_breakdown"])
        self.assertEqual(candidates[1].metadata, {"matched_by": ["canonical_fts", "faiss"]})

    def test_general_preference_does_not_force_food_and_low_base_cannot_jump_to_first(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        service = MemoryRetrievalService(_Store())
        candidates = [
            MemoryCandidate(
                id="color",
                kind="preference",
                source="canonical",
                summary="用户喜欢蓝色",
                content="用户喜欢蓝色",
                relevance_score=1.0,
            ),
            MemoryCandidate(
                id="food",
                kind="preference",
                source="canonical",
                summary="用户喜欢吃火锅",
                content="用户喜欢吃火锅",
                relevance_score=0.4,
            ),
        ]
        general_query = MemoryQuery(
            query="我喜欢什么",
            top_k=2,
            metadata={"intent_rerank_enabled": True, "intents": ["preference_general"]},
        )
        food_query = MemoryQuery(
            query="我喜欢吃什么",
            top_k=2,
            metadata={"intent_rerank_enabled": True, "intents": ["food_preference"]},
        )

        general_result = service._finalize_candidates(general_query, candidates)
        food_result = service._finalize_candidates(food_query, candidates)

        self.assertEqual(general_result[0].id, "color")
        self.assertEqual(food_result[0].id, "color")
        self.assertLess(
            food_result[1].metadata["_final_relevance_score"],
            food_result[0].metadata["_final_relevance_score"],
        )

    def test_rerank_deduplicates_canonical_id_and_merges_sources(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidates = [
            MemoryCandidate(
                id="legacy-1",
                kind="preference",
                source="hybrid",
                summary="用户喜欢吃火锅",
                content="用户喜欢吃火锅",
                relevance_score=0.7,
                metadata={"canonical_id": "canonical-1", "matched_by": "faiss"},
            ),
            MemoryCandidate(
                id="canonical-1",
                kind="preference",
                source="canonical",
                summary="用户喜欢吃火锅",
                content="用户喜欢吃火锅",
                relevance_score=0.8,
                metadata={"canonical_id": "canonical-1", "matched_by": ["canonical_fts"]},
            ),
        ]
        query = MemoryQuery(
            query="我喜欢吃什么",
            top_k=5,
            metadata={"intent_rerank_enabled": True, "intents": ["food_preference"]},
        )

        result = MemoryRetrievalService(_Store())._finalize_candidates(query, candidates)

        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].metadata["matched_by"]), {"faiss", "canonical_fts"})

    def test_rerank_near_deduplicates_short_chinese_preference_phrases(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidates = [
            MemoryCandidate(
                id=f"hotpot-{index}",
                kind="preference",
                source="canonical",
                summary=text,
                content=text,
                relevance_score=0.9 - index * 0.05,
            )
            for index, text in enumerate(
                ("用户喜欢吃火锅", "用户很喜欢火锅", "用户爱吃火锅")
            )
        ]
        query = MemoryQuery(
            query="我喜欢吃什么",
            top_k=5,
            metadata={"intent_rerank_enabled": True, "intents": ["food_preference"]},
        )

        result = MemoryRetrievalService(_Store())._finalize_candidates(query, candidates)

        self.assertEqual(len(result), 1)

    def test_adaptive_top_k_has_neutral_confidence_fallback(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidates = [
            MemoryCandidate(
                id=f"item-{index}",
                kind="memory",
                source="canonical",
                summary=f"memory {index}",
                content=f"memory {index}",
                relevance_score=0.5 - index * 0.05,
                confidence=0.0,
            )
            for index in range(5)
        ]
        query = MemoryQuery(
            query="之前那个",
            top_k=5,
            metadata={"adaptive_top_k_enabled": True},
        )

        result = MemoryRetrievalService(_Store())._finalize_candidates(query, candidates)

        self.assertEqual(len(result), 3)

    def test_summary_trace_omits_query_and_memory_content(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidate = MemoryCandidate(
            id="secret-id",
            kind="identity",
            source="canonical",
            summary="隐私正文",
            content="隐私正文",
            relevance_score=0.8,
        )
        query = MemoryQuery(query="我的名字", top_k=1, metadata={})

        MemoryRetrievalService(_Store())._finalize_candidates(query, [candidate])

        summary = query.metadata["_trace"]["retrieval"]
        self.assertNotIn("隐私正文", str(summary))
        self.assertNotIn("我的名字", str(summary))
        self.assertEqual(summary["selected_ids"], ["secret-id"])
        self.assertNotIn("retrieval_debug", query.metadata["_trace"])

    def test_persisted_summary_sanitizes_rewritten_queries_and_search_text(self):
        from astrmai.memory.contracts.memory_query import MemoryInjectionTrace, MemoryQuery
        from astrmai.memory.services.memory_injection_service import MemoryInjectionService

        query = MemoryQuery(query="隐私查询")
        trace = MemoryInjectionTrace(
            trace_id="trace-1",
            selected_ids=["memory-1"],
            selected_count=1,
        )
        summary = MemoryInjectionService._build_trace_summary(
            query,
            {
                "rewritten_queries": ["隐私改写"],
                "search_steps": [{"query": "隐私查询", "matched_terms": ["term"]}],
            },
            trace,
            "",
        )

        self.assertEqual(summary["rewritten_queries"], [])
        self.assertNotIn("隐私查询", str(summary))
        self.assertNotIn("隐私改写", str(summary))
        self.assertEqual(summary["search_steps"], [{"matched_terms": ["term"]}])

    def test_debug_trace_includes_diagnostics_only_when_enabled(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidate = MemoryCandidate(
            id="debug-id",
            kind="identity",
            source="canonical",
            summary="调试正文",
            content="调试正文",
            relevance_score=0.8,
        )
        query = MemoryQuery(
            query="调试查询",
            top_k=1,
            metadata={"memory_retrieval_debug_trace_enabled": True},
        )

        MemoryRetrievalService(_Store())._finalize_candidates(query, [candidate])

        debug = query.metadata["_trace"]["retrieval_debug"]
        self.assertEqual(debug["query"], "调试查询")
        self.assertIn("调试正文", str(debug))

    def test_query_builder_debug_trace_has_query_layers_only_when_enabled(self):
        from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        envelope = PromptEnvelope(direct_context_text="用户喜欢芒果")
        normal = self._build_query(MemoryQueryBuilder(), _Event(), "那个呢", envelope)
        debug = self._build_query(
            MemoryQueryBuilder(),
            _Event(astrmai_memory_retrieval_debug_trace_enabled=True),
            "那个呢",
            envelope,
        )

        self.assertNotIn("query_builder_debug", normal.metadata["_trace"])
        payload = debug.metadata["_trace"]["query_builder_debug"]
        self.assertEqual(payload["raw_query"], "那个呢")
        self.assertEqual(payload["normalized_query"], "那个呢")
        self.assertIn("芒果", payload["context_terms"])
        self.assertIn("芒果", payload["retrieval_query"])

    def test_adaptive_injection_uses_query_limit_and_default_trace_has_no_content(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate
        from astrmai.memory.services.memory_injection_service import MemoryInjectionService

        candidates = [
            MemoryCandidate(
                id=f"memory-{index}",
                kind="preference",
                source="canonical",
                summary=f"偏好 {index}",
                content=f"偏好 {index}",
                relevance_score=1.0 - index * 0.05,
            )
            for index in range(8)
        ]

        class _Retrieval:
            engine = None

            async def retrieve(self, query):
                self.query = query
                return candidates

        retrieval = _Retrieval()
        config = SimpleNamespace(
            memory=SimpleNamespace(
                recall_top_k=5,
                memory_query_builder_enabled=True,
                intent_rerank_enabled=True,
                adaptive_top_k_enabled=True,
                memory_retrieval_debug_trace_enabled=False,
            ),
            persona=SimpleNamespace(persona_id=""),
        )
        service = MemoryInjectionService(retrieval, config=config)
        event = _Event("我喜欢什么", astrmai_think_level=2)

        bundle = asyncio.run(service.build_bundle(event=event))

        self.assertEqual(retrieval.query.top_k, 8)
        self.assertEqual(len(bundle.items), 8)
        self.assertEqual(bundle.trace.summary_preview, "")
        self.assertNotIn("偏好 0", str(retrieval.query.metadata["_trace"]))

    def test_rerank_failure_falls_back_to_existing_order(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        candidates = [
            MemoryCandidate(
                id=item_id,
                kind="memory",
                source="canonical",
                summary=item_id,
                content=item_id,
                relevance_score=score,
            )
            for item_id, score in (("first", 0.9), ("second", 0.8))
        ]
        service = MemoryRetrievalService(_Store())
        service._intent_rerank = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("rerank failed"))
        query = MemoryQuery(
            query="test",
            top_k=2,
            metadata={"intent_rerank_enabled": True},
        )

        result = service._finalize_candidates(query, candidates)

        self.assertEqual([item.id for item in result], ["first", "second"])
        self.assertIn("intent_rerank", query.metadata["_trace"]["degraded_components"])

    def test_hybrid_failure_degrades_to_canonical_results(self):
        from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        canonical = MemoryCandidate(
            id="canonical",
            kind="memory",
            source="canonical",
            summary="canonical memory",
            content="canonical memory",
            relevance_score=0.8,
        )

        class _Engine:
            async def search_memories(self, *_args, **_kwargs):
                raise RuntimeError("faiss unavailable")

        query = MemoryQuery(query="canonical", top_k=1, metadata={})
        result = asyncio.run(MemoryRetrievalService(_Store([canonical]), engine=_Engine()).retrieve(query))

        self.assertEqual([item.id for item in result], ["canonical"])
        self.assertIn("hybrid", query.metadata["_trace"]["degraded_components"])

    def test_query_builder_failure_returns_legacy_query(self):
        from astrmai.memory.services.memory_query_builder import MemoryQueryBuilder

        builder = MemoryQueryBuilder()
        builder.normalizer.normalize = lambda _text: (_ for _ in ()).throw(RuntimeError("builder failed"))

        query = self._build_query(builder, _Event(), "  原始查询  ", top_k=7)

        self.assertEqual(query.query, "  原始查询  ")
        self.assertEqual(query.top_k, 7)
        self.assertEqual(query.metadata, {"visibility_mode": "auto"})

    def test_flags_can_be_read_from_config_debug_channel(self):
        from astrmai.memory.services.memory_query_builder import MemoryRetrievalFlags

        config = SimpleNamespace(
            debug_flags={
                "memory_query_builder_enabled": False,
                "intent_rerank_enabled": True,
                "adaptive_top_k_enabled": True,
            }
        )

        flags = MemoryRetrievalFlags.from_sources(config)

        self.assertFalse(flags.query_builder)
        self.assertTrue(flags.intent_rerank)
        self.assertTrue(flags.adaptive_top_k)

    def test_flags_can_be_read_from_real_config_model(self):
        from config import AstrMaiConfig
        from astrmai.memory.services.memory_query_builder import MemoryRetrievalFlags

        config = AstrMaiConfig(
            memory={
                "memory_query_builder_enabled": False,
                "intent_rerank_enabled": True,
                "adaptive_top_k_enabled": True,
                "memory_retrieval_debug_trace_enabled": True,
            }
        )

        flags = MemoryRetrievalFlags.from_sources(config)

        self.assertFalse(flags.query_builder)
        self.assertTrue(flags.intent_rerank)
        self.assertTrue(flags.adaptive_top_k)
        self.assertTrue(flags.debug_trace)


if __name__ == "__main__":
    unittest.main()
