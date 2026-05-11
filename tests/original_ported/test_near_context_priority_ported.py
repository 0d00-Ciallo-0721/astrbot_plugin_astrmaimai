import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.planning.context_engine import ContextEngine
from astrmai.conversation.planning.prompt_refiner import PromptRefiner


class _FakeDB:
    def get_chat_state(self, chat_id):
        return SimpleNamespace(mood=0.0, energy=0.9)

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
                memory=SimpleNamespace(auto_recall_probability=1.0),
            ),
            context=SimpleNamespace(),
        )

    async def get_summary(self, original_prompt="", persona_id="", session_id=""):
        return {
            "summary": "natural chat persona",
            "style": "brief and natural",
            "shards": {},
            "raw": "raw persona",
            "is_full_ready": True,
        }


class NearContextPriorityPortedTests(unittest.TestCase):
    def test_context_engine_drops_far_context_blocks_when_near_context_priority(self):
        engine = ContextEngine(db=_FakeDB(), persona_summarizer=_FakeSummarizer())

        async def _run():
            return await engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                slang_patterns="SLANG_BLOCK_SHOULD_DROP",
                expression_habits="EXPRESSION_BLOCK_SHOULD_DROP",
                jargon_explanation="JARGON_BLOCK_SHOULD_DROP",
                near_context_priority=True,
            )

        system_prompt, style_variant, proactive_recall = asyncio.run(_run())

        self.assertNotIn("SLANG_BLOCK_SHOULD_DROP", system_prompt)
        self.assertNotIn("EXPRESSION_BLOCK_SHOULD_DROP", system_prompt)
        self.assertNotIn("JARGON_BLOCK_SHOULD_DROP", system_prompt)
        self.assertIsInstance(style_variant, str)
        self.assertNotIn("SLANG_BLOCK_SHOULD_DROP", style_variant)
        self.assertEqual(proactive_recall, "")

    def test_refiner_memory_injection_is_disabled_when_near_context_priority(self):
        refiner = PromptRefiner(
            memory_engine=SimpleNamespace(recall=None),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=SimpleNamespace(retrieve=None),
        )

        class _FakeEvent:
            message_str = "why not?"
            unified_msg_origin = "default:GroupMessage:group-1"

            def get_extra(self, key, default=None):
                if key == "astrmai_near_context_priority":
                    return True
                return default

            def get_sender_name(self):
                return "Alice"

        result = asyncio.run(
            refiner._build_memory_injection(
                event=_FakeEvent(),
                prompt="why not?",
                disable_rag=False,
                is_fast_mode=False,
                retrieve_keys=[],
            )
        )

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
