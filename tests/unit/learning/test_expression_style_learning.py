import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace

from astrmai.learning.mining.expression_candidate_extractor import ExpressionCandidateExtractor
from astrmai.learning.mining.expression_pattern_enricher import ExpressionPatternEnricher
from astrmai.conversation.planning.expression_policy import ExpressionSelector
from astrmai.memory.contracts.memory_query import MemoryCandidate
from astrmai.memory.services.expression_pattern_retrieval_policy import ExpressionPatternRetrievalPolicy
from astrmai.memory.services.expression_pattern_service import ExpressionPatternService


class ExpressionStyleLearningTests(unittest.TestCase):
    def test_candidates_are_scoped_per_group_and_topic_words_are_not_expression(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=1, sender_id="alice", sender_name="Alice", content="唉嘿嘿～"),
            SimpleNamespace(id=2, sender_id="alice", sender_name="Alice", content="唉嘿嘿～"),
            SimpleNamespace(id=3, sender_id="alice", sender_name="Alice", content="好呀"),
            SimpleNamespace(id=4, sender_id="bob", sender_name="Bob", content="充电宝"),
            SimpleNamespace(id=5, sender_id="bob", sender_name="Bob", content="充电宝"),
            SimpleNamespace(id=6, sender_id="bob", sender_name="Bob", content="充电宝"),
            SimpleNamespace(id=7, sender_id="bob", sender_name="Bob", content="OpenAI"),
            SimpleNamespace(id=8, sender_id="bob", sender_name="Bob", content="OpenAI"),
            SimpleNamespace(id=9, sender_id="bob", sender_name="Bob", content="OpenAI"),
            SimpleNamespace(id=10, sender_id="bot", sender_name="SELF", content="唉嘿嘿～", is_bot=True),
        ]

        result = asyncio.run(extractor.extract("group-1", messages))

        matches = [item for item in result if item.get("expression") == "唉嘿嘿～"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["shared_scope"], "group-1")
        self.assertEqual(matches[0]["scope_kind"], "group")
        self.assertEqual(matches[0]["distinct_turn_count"], 2)
        self.assertEqual(matches[0]["content_kind"], "expression")
        self.assertNotIn("speaker_id", matches[0])
        self.assertNotIn("speaker_name", matches[0])
        self.assertFalse(any("充电宝" in str(item.get("expression")) for item in result))
        self.assertFalse(any("openai" in str(item.get("expression", "")).lower() for item in result))
        self.assertEqual(matches[0]["distinct_contributor_count"], 1)

    def test_same_habit_from_two_speakers_merges_at_group_scope(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=1, sender_id="alice", content="嘿嘿"),
            SimpleNamespace(id=2, sender_id="alice", content="嘿嘿"),
            SimpleNamespace(id=3, sender_id="bob", content="嘿嘿"),
            SimpleNamespace(id=4, sender_id="bob", content="嘿嘿"),
        ]

        result = asyncio.run(extractor.extract("group-1", messages))
        matches = [item for item in result if item.get("expression") == "嘿嘿" and item.get("candidate_type") == "exact"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["shared_scope"], "group-1")
        self.assertEqual(matches[0]["distinct_contributor_count"], 2)
        self.assertEqual(matches[0]["count"], 4)

    def test_unknown_sender_uses_group_compatibility_scope_and_unix_days_are_real_days(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        day_one = datetime(2026, 7, 1, 12, 0).timestamp()
        day_two = datetime(2026, 7, 2, 12, 0).timestamp()
        messages = [
            SimpleNamespace(id=1, sender_name="同名", content="嘿嘿", timestamp=day_one),
            SimpleNamespace(id=2, sender_name="同名", content="嘿嘿", timestamp=day_two),
        ]

        result = asyncio.run(extractor.extract("group-1", messages))
        candidate = next(item for item in result if item.get("candidate_type") == "exact")

        self.assertEqual(candidate["shared_scope"], "group-1")
        self.assertEqual(candidate["distinct_day_count"], 2)

    def test_existing_pattern_suppresses_group_wide_reextraction(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=1, sender_id="alice", content="嘿嘿"),
            SimpleNamespace(id=2, sender_id="alice", content="嘿嘿"),
            SimpleNamespace(id=3, sender_id="bob", content="嘿嘿"),
            SimpleNamespace(id=4, sender_id="bob", content="嘿嘿"),
        ]

        result = asyncio.run(
            extractor.extract(
                "group-1",
                messages,
                existing_patterns={("group-1", "嘿嘿")},
            )
        )

        matches = [item for item in result if item.get("expression") == "嘿嘿"]
        self.assertEqual(matches, [])

    def test_ending_symbol_and_reply_rhythm_are_distinct_habit_types(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=1, sender_id="alice", content="好呀", timestamp=100.0),
            SimpleNamespace(id=2, sender_id="alice", content="知道呀", timestamp=102.0),
            SimpleNamespace(id=3, sender_id="alice", content="来啦", timestamp=104.0),
            SimpleNamespace(id=4, sender_id="alice", content="(≧ω≦)♡", timestamp=106.0),
            SimpleNamespace(id=5, sender_id="alice", content="(≧ω≦)♡", timestamp=108.0),
        ]

        result = asyncio.run(extractor.extract("group-1", messages))

        self.assertTrue(any(item["expression"] == "呀" and item["habit_type"] == "ending" for item in result))
        self.assertTrue(any(item["expression"] == "(≧ω≦)♡" and item["habit_type"] == "symbol" for item in result))
        self.assertTrue(any(item["candidate_type"] == "rhythm" and item["habit_type"] == "rhythm" for item in result))

    def test_enricher_keeps_original_expression_and_rejects_topic_content(self):
        class Gateway:
            def __init__(self):
                self.prompt = ""

            async def call_data_process_task(self, **kwargs):
                self.prompt = str(kwargs.get("prompt") or "")
                return {
                    "items": [
                        {
                            "candidate_id": "expr-1",
                            "decision": "keep",
                            "content_kind": "expression",
                            "habit_type": "particle",
                            "expression": "模型不应替换我",
                            "summary": "句末带轻微撒娇语气",
                            "review_status": "pending_human",
                            "confidence": 0.9,
                        },
                        {
                            "candidate_id": "expr-2",
                            "decision": "keep",
                            "content_kind": "topic_content",
                            "review_status": "pending_human",
                        },
                    ]
                }

        gateway = Gateway()
        candidates = [
            {
                "candidate_id": "expr-1",
                "candidate_type": "phrase",
                "expression": "嘿嘿",
                "content_kind": "expression",
                "habit_type": "catchphrase",
                "content_samples": ["嘿嘿"],
                "evidence_message_ids": ["1", "2"],
                "count": 2,
            },
            {
                "candidate_id": "expr-2",
                "candidate_type": "exact",
                "expression": "充电宝",
                "content_kind": "topic_content",
                "content_samples": ["充电宝"],
                "evidence_message_ids": ["3", "4"],
                "count": 2,
            },
        ]

        result = asyncio.run(ExpressionPatternEnricher(gateway).enrich("group-1", candidates))

        self.assertEqual([item["expression"] for item in result.items], ["嘿嘿"])
        self.assertIn("领域词", gateway.prompt)
        self.assertIn("habit_type", gateway.prompt)

    def test_retrieval_uses_only_current_group_scope(self):
        class Store:
            async def list_candidates(self, **kwargs):
                return [
                    MemoryCandidate(
                        id="alice",
                        kind="expression_pattern",
                        source="test",
                        summary="alice",
                        content="嘿嘿",
                        session_id="group-1",
                        metadata={"shared_scope": "group-1:user:alice", "review_status": "approved", "count": 3},
                    ),
                    MemoryCandidate(
                        id="bob",
                        kind="expression_pattern",
                        source="test",
                        summary="bob",
                        content="嘿嘿",
                        session_id="group-1",
                        metadata={"shared_scope": "group-1:user:bob", "review_status": "approved", "count": 3},
                    ),
                    MemoryCandidate(
                        id="legacy-group",
                        kind="expression_pattern",
                        source="test",
                        summary="legacy",
                        content="嘿嘿",
                        session_id="group-1",
                        metadata={"shared_scope": "group-1", "review_status": "approved", "count": 3},
                    ),
                    MemoryCandidate(
                        id="unscoped",
                        kind="expression_pattern",
                        source="test",
                        summary="unscoped",
                        content="嘿嘿",
                        session_id="group-1",
                        metadata={"shared_scope": "", "review_status": "approved", "count": 3},
                    ),
                ]

        result = asyncio.run(
            ExpressionPatternRetrievalPolicy(Store()).search(
                query="嘿嘿",
                session_id="group-1",
                shared_scope="group-1",
            )
        )
        self.assertEqual({item.id for item in result}, {"legacy-group"})

    def test_pattern_service_scope_filter_matches_retrieval_policy(self):
        class Store:
            async def list_candidates(self, **kwargs):
                return [
                    MemoryCandidate(
                        id="alice",
                        kind="expression_pattern",
                        source="test",
                        content="嘿嘿",
                        summary="alice",
                        session_id="group-1",
                        status="active",
                        metadata={"shared_scope": "group-1:user:alice", "review_status": "approved"},
                    ),
                    MemoryCandidate(
                        id="bob",
                        kind="expression_pattern",
                        source="test",
                        content="嘿嘿",
                        summary="bob",
                        session_id="group-1",
                        status="active",
                        metadata={"shared_scope": "group-1:user:bob", "review_status": "approved"},
                    ),
                    MemoryCandidate(
                        id="unscoped",
                        kind="expression_pattern",
                        source="test",
                        content="嘿嘿",
                        summary="unscoped",
                        session_id="group-1",
                        status="active",
                        metadata={"shared_scope": "", "review_status": "approved"},
                    ),
                ]

        service = ExpressionPatternService(Store(), write_service=SimpleNamespace())
        result = asyncio.run(
            service.list_patterns(
                "group-1",
                shared_scope="group-1",
                review_status="approved",
            )
        )
        self.assertEqual([item.id for item in result], ["unscoped"])

    def test_prompt_formats_habit_type_as_style_not_fixed_line(self):
        text = ExpressionSelector._format_habits(
            [
                SimpleNamespace(
                    situation="日常回应",
                    expression="偏好短句连发，常在数秒内连续补充",
                    style="短句连发",
                    metadata={"habit_type": "rhythm"},
                )
            ]
        )
        self.assertIn("回复节奏可参考（特征：回复节奏；短句连发）", text)
        self.assertIn("只模仿节奏", text)
        self.assertIn("不要原句复读", text)


if __name__ == "__main__":
    unittest.main()
