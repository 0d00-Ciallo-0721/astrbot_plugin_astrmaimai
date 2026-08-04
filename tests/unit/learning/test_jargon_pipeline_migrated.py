import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


class _Message:
    def __init__(self, content):
        self.content = content


class _Gateway:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else {"items": []}
        self.error = error
        self.calls = []
        self.config = SimpleNamespace()

    async def call_data_process_task(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class JargonPipelineMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.learning.mining") or name.startswith("astrmai.memory.services"):
                sys.modules.pop(name, None)
        self.extractor_mod = importlib.import_module("astrmai.learning.mining.jargon_candidate_extractor")
        self.enricher_mod = importlib.import_module("astrmai.learning.mining.jargon_enricher")
        self.policy_mod = importlib.import_module("astrmai.memory.services.jargon_retrieval_policy")
        self.store_mod = importlib.import_module("astrmai.memory.services.v2_store")
        self.write_mod = importlib.import_module("astrmai.memory.services.memory_write_service")
        self.contracts = importlib.import_module("astrmai.memory.contracts.memory_query")
        self.db_path = os.path.join(self.temp_dir.name, "docs.db")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_candidate_extractor_filters_noise_and_keeps_high_frequency_term(self):
        async def run():
            extractor = self.extractor_mod.JargonCandidateExtractor(min_count=2)
            messages = [
                _Message("bigbird is coming"),
                _Message("I saw bigbird again"),
                _Message("哈哈 好的"),
                _Message("bigbirdzzz appears once"),
            ]
            candidates = await extractor.extract("group-1", messages)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["content"], "bigbird")
            self.assertEqual(candidates[0]["count"], 2)

        asyncio.run(run())

    def test_enricher_fails_closed_when_llm_fails(self):
        async def run():
            gateway = _Gateway(error=RuntimeError("llm offline"))
            enricher = self.enricher_mod.JargonEnricher(gateway)
            result = await enricher.enrich(
                "group-1",
                [
                    {
                        "content": "bigbird",
                        "raw_content": "bigbird is coming",
                        "count": 3,
                        "activation_score": 0.7,
                        "examples": ["bigbird is coming"],
                    }
                ],
            )
            self.assertEqual(result.status, "provider_failure")
            self.assertEqual(result.items, [])
            self.assertTrue(result.retryable)
            self.assertFalse(result.terminal)
            self.assertEqual(result.error_type, "RuntimeError")

        asyncio.run(run())

    def test_enricher_never_returns_active_review_status(self):
        async def run():
            gateway = _Gateway(
                response={
                    "items": [
                        {
                            "index": 1,
                            "meaning": "raid boss nickname",
                            "scene": "raid call",
                            "confidence": 0.9,
                            "is_jargon": True,
                            "review_status": "active",
                        }
                    ]
                }
            )
            enricher = self.enricher_mod.JargonEnricher(gateway)
            result = await enricher.enrich(
                "group-1",
                [
                    {
                        "content": "bigbird",
                        "raw_content": "bigbird is coming",
                        "count": 3,
                        "activation_score": 0.7,
                        "examples": ["bigbird is coming"],
                    }
                ],
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.items[0]["review_status"], "review_pending")

        asyncio.run(run())

    def test_enricher_distinguishes_valid_all_rejected_from_failure(self):
        async def run():
            gateway = _Gateway(
                response={
                    "items": [
                        {
                            "index": 1,
                            "meaning": "",
                            "scene": "ordinary chat",
                            "confidence": 0.2,
                            "is_jargon": False,
                            "review_status": "rejected",
                        }
                    ]
                }
            )
            result = await self.enricher_mod.JargonEnricher(gateway).enrich(
                "group-1",
                [{"content": "hello", "raw_content": "hello", "count": 2, "examples": ["hello"]}],
            )

            self.assertEqual(result.status, "all_rejected")
            self.assertEqual(result.items, [])
            self.assertTrue(result.terminal)
            self.assertFalse(result.retryable)
            self.assertEqual(result.rejected_count, 1)

        asyncio.run(run())

    def test_enricher_keeps_valid_partial_items_and_reports_missing_indexes(self):
        async def run():
            gateway = _Gateway(
                response={
                    "items": [
                        {
                            "index": 1,
                            "meaning": "raid boss nickname",
                            "scene": "raid call",
                            "confidence": 0.9,
                            "is_jargon": True,
                            "review_status": "review_pending",
                        }
                    ]
                }
            )
            candidates = [
                {"content": "bigbird", "raw_content": "bigbird", "count": 3, "examples": ["bigbird"]},
                {"content": "smallbird", "raw_content": "smallbird", "count": 3, "examples": ["smallbird"]},
            ]
            result = await self.enricher_mod.JargonEnricher(gateway).enrich("group-1", candidates)

            self.assertEqual(result.status, "partial")
            self.assertEqual([item["content"] for item in result.items], ["bigbird"])
            self.assertEqual(result.missing_indexes, [2])
            self.assertTrue(result.terminal)

        asyncio.run(run())

    def test_enricher_reports_invalid_json_as_retryable(self):
        async def run():
            result = await self.enricher_mod.JargonEnricher(_Gateway(response="{broken")).enrich(
                "group-1",
                [{"content": "bigbird", "raw_content": "bigbird", "count": 3, "examples": ["bigbird"]}],
            )

            self.assertEqual(result.status, "invalid_json")
            self.assertEqual(result.items, [])
            self.assertTrue(result.retryable)
            self.assertFalse(result.terminal)

        asyncio.run(run())

    def test_jargon_retrieval_policy_matches_scene_and_examples(self):
        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            writer = self.write_mod.MemoryWriteService(store)
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="group-1",
                    content="bigbird",
                    summary="raid boss nickname",
                    confidence=0.9,
                    metadata={
                        "meaning": "raid boss nickname",
                        "scene": "raid call",
                        "examples": ["bigbird is here"],
                        "review_status": "active",
                    },
                    dedup_key="jargon:group-1:bigbird",
                    status="active",
                    visibility="auto_and_tool",
                )
            )
            policy = self.policy_mod.JargonRetrievalPolicy(store)
            by_scene = await policy.search(query="raid call", session_id="another-group", top_k=3)
            by_example = await policy.search(query="bigbird is here", session_id="another-group", top_k=3)
            excluded = await policy.search(
                query="bigbird",
                session_id="group-1",
                top_k=3,
                exclude_ids=[by_scene[0].id],
            )
            self.assertEqual(by_scene[0].content, "bigbird")
            self.assertEqual(by_example[0].content, "bigbird")
            self.assertEqual(excluded, [])

        asyncio.run(run())

    def test_jargon_retrieval_policy_exact_route_does_not_fuzzy_inject(self):
        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            writer = self.write_mod.MemoryWriteService(store)
            await writer.write(
                self.contracts.MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id="__global_jargon__",
                    content="bigbird",
                    summary="raid boss nickname",
                    confidence=0.9,
                    metadata={
                        "meaning": "raid boss nickname",
                        "scene": "raid call",
                        "aliases": ["大鸟"],
                        "review_status": "approved",
                    },
                    dedup_key="jargon:global:bigbird",
                    status="active",
                    visibility="auto_and_tool",
                )
            )
            policy = self.policy_mod.JargonRetrievalPolicy(store)

            fuzzy = await policy.search(query="raid call", top_k=3)
            exact_miss = await policy.search(query="raid call", top_k=3, exact_only=True)
            exact_alias = await policy.search(query="今天大鸟又来了", top_k=3, exact_only=True)

            self.assertEqual(fuzzy[0].content, "bigbird")
            self.assertEqual(exact_miss, [])
            self.assertEqual(exact_alias[0].content, "bigbird")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
