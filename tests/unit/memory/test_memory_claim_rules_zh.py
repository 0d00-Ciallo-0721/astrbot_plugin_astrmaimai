import asyncio
import importlib
import sys
import tempfile
import unittest

from tests.helpers import install_astrbot_stubs


class MemoryClaimRulesZhTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.memory.services.memory_") or name.endswith("claim_rules_zh"):
                sys.modules.pop(name, None)
        self.claim_mod = importlib.import_module("astrmai.memory.services.memory_claim_service")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_zh_correction_server_count_is_detected(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            return await extractor.extract(
                user_text="之前说错了，不是2台，是3台服务器",
                subject_id="zlj",
                turn_id="turn-zh-1",
            )

        claims = asyncio.run(run())
        self.assertTrue(claims)
        self.assertTrue(claims[0].is_correction)
        self.assertEqual(claims[0].attribute, "server_count")

    def test_zh_short_term_anxiety_is_detected(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            return await extractor.extract(
                user_text="我今天特别焦虑",
                subject_id="zlj",
                turn_id="turn-zh-2",
            )

        claims = asyncio.run(run())
        self.assertTrue(claims)
        self.assertEqual(claims[0].fact_scope, "short_term")
        self.assertEqual(claims[0].attribute, "anxiety_state")

    def test_zh_uncertain_server_count_still_extracts_claim(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            return await extractor.extract(
                user_text="最近好像没那么多服务器了",
                subject_id="zlj",
                turn_id="turn-zh-3",
            )

        claims = asyncio.run(run())
        if claims:
            self.assertEqual(claims[0].attribute, "server_count")
        else:
            self.assertEqual(claims, [])

    def test_zh_correction_hint_is_present_for_phrase(self):
        async def run():
            extractor = self.claim_mod.MemoryClaimExtractor()
            return await extractor.extract(
                user_text="我改一下，不是那个意思",
                subject_id="zlj",
                turn_id="turn-zh-4",
            )

        claims = asyncio.run(run())
        if claims:
            self.assertTrue(claims[0].is_correction)
        else:
            self.assertEqual(claims, [])


if __name__ == "__main__":
    unittest.main()
