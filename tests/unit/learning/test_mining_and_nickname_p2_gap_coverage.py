import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class MiningAndNicknameP2GapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_expression_miner_normalizes_messages_before_extracting(self):
        from astrmai.learning.mining.expression_miner import ExpressionMiner

        config = SimpleNamespace(evolution=SimpleNamespace(expression_min_count=1, min_mining_context=2))
        miner = ExpressionMiner(SimpleNamespace(config=config), config=config)
        captured = {}

        async def _extract(group_id, messages, existing_patterns=None):
            captured["messages"] = list(messages)
            captured["existing"] = existing_patterns
            return [{"expression": "ok"}]

        async def _enrich(group_id, candidates):
            return [{"expression": candidates[0]["expression"], "style": "soft"}]

        miner.candidate_extractor.extract = _extract
        miner.enricher.enrich = _enrich
        messages = [
            None,
            SimpleNamespace(sender_name="SELF", content="mine"),
            SimpleNamespace(sender_name="Alice", content="[image]"),
            SimpleNamespace(sender_name="Bob", content="  hello  "),
            SimpleNamespace(sender_name="Cici", content="world"),
        ]

        result = asyncio.run(miner.mine("group-1", messages))

        self.assertEqual(result, [{"expression": "ok", "style": "soft"}])
        self.assertEqual([item.sender_name for item in captured["messages"]], ["Bob", "Cici"])

    def test_expression_miner_loads_existing_patterns_and_degrades_on_failure(self):
        from astrmai.learning.mining.expression_miner import ExpressionMiner

        class _PatternService:
            def normalize_text(self, value):
                return value.lower()

            async def list_patterns(self, group_id, **kwargs):
                return [SimpleNamespace(expression="Hello")]

        config = SimpleNamespace(evolution=SimpleNamespace(expression_min_count=1, min_mining_context=1))
        miner = ExpressionMiner(
            SimpleNamespace(config=config),
            config=config,
            memory_engine=SimpleNamespace(expression_pattern_service=_PatternService()),
        )

        self.assertEqual(asyncio.run(miner._existing_patterns("group-1")), {"hello"})

        async def _broken(*args, **kwargs):
            raise RuntimeError("db locked")

        miner.memory_engine.expression_pattern_service.list_patterns = _broken
        self.assertEqual(asyncio.run(miner._existing_patterns("group-1")), set())

    def test_nickname_generator_payload_parse_and_fallback_choice(self):
        from astrmai.learning.profiling.nickname_generator import NicknameGenerator

        generator = NicknameGenerator()
        profile = SimpleNamespace(name="Alice", persona_analysis="A" * 260, tags=["careful", 7])

        payload = generator.build_template_payload(profile, persona_summary="calm persona")
        nickname, reason = generator.parse_result('prefix {"nickname": "小A", "reason": "亲切"} suffix')
        bad_nickname, bad_reason = generator.parse_result("not-json")

        self.assertEqual(payload["persona_summary"], "calm persona")
        self.assertEqual(payload["tags_text"], "careful, 7")
        self.assertEqual(len(payload["analysis"]), 200)
        self.assertEqual((nickname, reason), ("小A", "亲切"))
        self.assertEqual((bad_nickname, bad_reason), ("", ""))
        self.assertEqual(generator.choose("Display", ""), "Display")
        self.assertEqual(generator.choose("", ""), "未知用户")


if __name__ == "__main__":
    unittest.main()
