import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


class MemoryConflictResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.memory.services.memory_"):
                sys.modules.pop(name, None)
        self.claim_mod = importlib.import_module("astrmai.memory.services.memory_claim_service")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_explicit_correction_extracts_correction_claim(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            claims = await extractor.extract(
                user_text="I said it wrong, before 2 servers, now 3 servers",
                subject_id="zlj",
                turn_id="turn-1",
            )
            resolver = self.claim_mod.MemoryConflictResolver()
            decision = resolver.resolve(claims)
            return claims, decision

        claims, decision = asyncio.run(run())
        self.assertTrue(claims)
        self.assertTrue(claims[0].is_correction)
        self.assertEqual(claims[0].attribute, "server_count")
        self.assertEqual(decision.action, "authority_override")

    def test_short_term_state_does_not_override_authority(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            claims = await extractor.extract(
                user_text="today I feel anxious",
                subject_id="zlj",
                turn_id="turn-2",
            )
            resolver = self.claim_mod.MemoryConflictResolver()
            return claims, resolver.resolve(claims)

        claims, decision = asyncio.run(run())
        self.assertTrue(claims)
        self.assertEqual(claims[0].fact_scope, "short_term")
        self.assertEqual(decision.action, "volatile_state_write")
        self.assertTrue(decision.metadata["volatile_state"])

    def test_uncertain_correction_degrades_to_plain_memory(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            claims = await extractor.extract(
                user_text="recently maybe not that many servers",
                subject_id="zlj",
                turn_id="turn-3",
            )
            resolver = self.claim_mod.MemoryConflictResolver()
            return claims, resolver.resolve(claims)

        claims, decision = asyncio.run(run())
        if claims:
            self.assertNotEqual(decision.action, "authority_override")
        else:
            self.assertEqual(decision.action, "plain_memory_write")

    def test_llm_claim_extraction_failure_returns_empty_claims(self):
        class _Templates:
            def render_template(self, *_args, **_kwargs):
                return SimpleNamespace(prompt="extract", system_prompt="system")

        class _Gateway:
            context_economy = SimpleNamespace(templates=_Templates())

            async def call_data_process_task(self, **_kwargs):
                raise RuntimeError("provider failed")

        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor(_Gateway())
            return await extractor.extract(
                user_text="plain chat with no rule claim",
                subject_id="zlj",
                turn_id="turn-4",
            )

        self.assertEqual(asyncio.run(run()), [])

    def test_llm_claim_extraction_accepts_naked_members(self):
        class _Templates:
            def render_template(self, *_args, **_kwargs):
                return SimpleNamespace(prompt="extract", system_prompt="system")

        class _Gateway:
            context_economy = SimpleNamespace(templates=_Templates())

            async def call_data_process_task(self, **_kwargs):
                return (
                    '"claims":[{"subject_id":"u1","entity":"profile",'
                    '"attribute":"name","value":"Alice","certainty":0.9}]'
                )

        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor(_Gateway())
            return await extractor.extract(
                user_text="plain chat with no rule claim",
                subject_id="u1",
                turn_id="turn-naked",
            )

        claims = asyncio.run(run())
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0].value, "Alice")

    def test_llm_claim_extraction_uses_explicit_chat_lane(self):
        captured = {}

        class _Templates:
            def render_template(self, *_args, **_kwargs):
                return SimpleNamespace(prompt="extract", system_prompt="system")

        class _Gateway:
            context_economy = SimpleNamespace(templates=_Templates())

            async def call_data_process_task(self, **kwargs):
                captured.update(kwargs)
                return {"claims": []}

        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor(_Gateway())
            return await extractor.extract(
                user_text="plain group conversation",
                turn_id="turn-group-1",
                lane_scope_id="ff:GroupMessage:123",
                lane_scope_kind="chat",
            )

        self.assertEqual(asyncio.run(run()), [])
        lane_key = captured["lane_key"]
        self.assertEqual(lane_key.scope_id, "ff:GroupMessage:123")
        self.assertEqual(lane_key.scope_kind, "chat")

    def test_llm_claim_extraction_falls_back_to_subject_lane(self):
        captured = {}

        class _Templates:
            def render_template(self, *_args, **_kwargs):
                return SimpleNamespace(prompt="extract", system_prompt="system")

        class _Gateway:
            context_economy = SimpleNamespace(templates=_Templates())

            async def call_data_process_task(self, **kwargs):
                captured.update(kwargs)
                return {"claims": []}

        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor(_Gateway())
            return await extractor.extract(
                user_text="plain private conversation",
                subject_id="user-42",
                turn_id="turn-private-1",
            )

        self.assertEqual(asyncio.run(run()), [])
        lane_key = captured["lane_key"]
        self.assertEqual(lane_key.scope_id, "subject:user-42")
        self.assertEqual(lane_key.scope_kind, "user")


if __name__ == "__main__":
    unittest.main()
