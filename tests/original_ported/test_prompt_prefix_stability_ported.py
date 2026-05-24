import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.executor_stubs import install_executor_stubs
from tests.helpers.planner_stubs import install_planner_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeDB:
    def get_chat_state(self, chat_id):
        return SimpleNamespace(mood=0.1, energy=0.8)

    def get_session(self):
        class _Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get=lambda *args, **kwargs: None)

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _Ctx()


class _FakeSummarizer:
    def __init__(self):
        self.gateway = SimpleNamespace(
            config=SimpleNamespace(
                persona=SimpleNamespace(persona_id="p1", prompt=""),
                memory=SimpleNamespace(auto_recall_probability=0.0),
            ),
            context=SimpleNamespace(),
        )

    async def get_summary(self, original_prompt="", persona_id="", session_id=""):
        return {
            "summary": "You are a natural conversational character.",
            "style": "Natural and concise.",
            "shards": {},
            "raw": "Raw persona",
            "is_full_ready": True,
        }


class PromptPrefixStabilityPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.planning.context_engine", None)
        self.context_engine_mod = importlib.import_module("astrmai.conversation.planning.context_engine")
        self.context_engine_mod = importlib.reload(self.context_engine_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_same_inputs_produce_same_prefix_hash(self):
        engine = self.context_engine_mod.ContextEngine(
            db=_FakeDB(),
            persona_summarizer=_FakeSummarizer(),
        )

        async def _build():
            prompt1 = await engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                situational_style_cues="fixed slang",
                stable_expression_habits="fixed habit",
                stable_jargon_explanation="fixed jargon",
            )
            hash1 = engine.get_last_prefix_hash("default:GroupMessage:group-1")
            prompt2 = await engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                situational_style_cues="fixed slang",
                stable_expression_habits="fixed habit",
                stable_jargon_explanation="fixed jargon",
            )
            hash2 = engine.get_last_prefix_hash("default:GroupMessage:group-1")
            return prompt1, prompt2, hash1, hash2

        prompt1, prompt2, hash1, hash2 = asyncio.run(_build())
        system_prompt1, style_variant1, proactive_recall1 = prompt1
        system_prompt2, style_variant2, proactive_recall2 = prompt2

        self.assertEqual(hash1, hash2)
        self.assertTrue(hash1)
        self.assertEqual(system_prompt1, system_prompt2)
        self.assertEqual(style_variant1, style_variant2)
        self.assertEqual(proactive_recall1, proactive_recall2)
        self.assertNotIn("<CHAT_HISTORY>", system_prompt1)
        self.assertNotIn("[Tools]", system_prompt1)
        self.assertNotIn("fixed slang", system_prompt1)


if __name__ == "__main__":
    unittest.main()
