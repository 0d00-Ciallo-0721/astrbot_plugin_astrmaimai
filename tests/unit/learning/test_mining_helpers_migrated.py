import asyncio
import unittest
from types import SimpleNamespace

from astrmai.learning.mining.jargon_miner import JargonMiner
from astrmai.learning.mining.expression_candidate_extractor import ExpressionCandidateExtractor
from astrmai.learning.mining.expression_pattern_enricher import ExpressionPatternEnricher
from astrmai.learning.mining.social_relation_miner import SocialRelationMiner


class _FakeExpressionMiner:
    def __init__(self):
        from config import AstrMaiConfig

        self.gateway = None
        self.config = AstrMaiConfig(evolution={"jargon_min_count": 1})


class _FakeExtractor:
    def __init__(self):
        self.calls = []

    async def extract(self, group_id, messages, *, existing_terms=None):
        self.calls.append((group_id, list(messages), set(existing_terms or set())))
        return [{"content": "ok"}]


class _FakeEnricher:
    def __init__(self):
        self.calls = []

    async def enrich(self, group_id, candidates):
        self.calls.append((group_id, list(candidates)))
        return ["ok"]


class _FakeStateEngine:
    def __init__(self):
        self.calls = []

    async def update_social_score_from_fact(self, user_id, impact_score):
        self.calls.append((user_id, impact_score))


class MiningHelpersMigratedTests(unittest.TestCase):
    def test_expression_candidate_extractor_uses_deterministic_frequency_and_dedup(self):
        extractor = ExpressionCandidateExtractor(min_count=2)

        async def _run():
            return await extractor.extract(
                "group-1",
                [
                    SimpleNamespace(content="ship it softly"),
                    SimpleNamespace(content="ok"),
                    SimpleNamespace(content="ship it softly"),
                    SimpleNamespace(content="ship it softly!"),
                    SimpleNamespace(content="x" * 80),
                ],
                existing_patterns={"already approved phrase"},
            )

        result = asyncio.run(_run())
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["expression"], "ship it softly")
        self.assertEqual(result[0]["count"], 2)

    def test_expression_pattern_enricher_degrades_to_candidate_payload(self):
        class _Gateway:
            async def call_data_process_task(self, *args, **kwargs):
                raise RuntimeError("offline")

        enricher = ExpressionPatternEnricher(_Gateway())

        async def _run():
            return await enricher.enrich(
                "group-1",
                [
                    {
                        "expression": "ship it softly",
                        "situation": "daily",
                        "style": "plain",
                        "content_samples": ["ship it softly"],
                        "activation_score": 0.72,
                    }
                ],
            )

        result = asyncio.run(_run())
        self.assertEqual(result[0]["summary"], "ship it softly")
        self.assertEqual(result[0]["review_status"], "pending")
        self.assertAlmostEqual(result[0]["confidence"], 0.72)

    def test_jargon_miner_filters_blank_messages_before_delegating(self):
        miner = _FakeExpressionMiner()
        jargon_miner = JargonMiner(miner, min_messages=1)
        extractor = _FakeExtractor()
        jargon_miner.candidate_extractor = extractor
        jargon_miner.enricher = _FakeEnricher()

        async def _run():
            return await jargon_miner.mine(
                "group-1",
                [
                    SimpleNamespace(content="hello"),
                    SimpleNamespace(content="   "),
                    SimpleNamespace(content="world"),
                ],
            )

        result = asyncio.run(_run())
        self.assertEqual(result, ["ok"])
        self.assertEqual([msg.content for msg in extractor.calls[0][1]], ["hello", "world"])

    def test_jargon_miner_reads_jargon_min_count_from_real_config(self):
        miner = _FakeExpressionMiner()
        jargon_miner = JargonMiner(miner, min_messages=1)

        self.assertEqual(jargon_miner.candidate_extractor.min_count, 1)

    def test_social_relation_miner_normalizes_score_and_ignores_empty_input(self):
        state_engine = _FakeStateEngine()
        miner = SocialRelationMiner(state_engine)

        async def _run():
            await miner.record_affection_fact("", 1)
            await miner.record_affection_fact("user-1", 0)
            await miner.record_affection_fact("user-1", "1.5")

        asyncio.run(_run())
        self.assertEqual(state_engine.calls, [("user-1", 1.5)])


if __name__ == "__main__":
    unittest.main()
