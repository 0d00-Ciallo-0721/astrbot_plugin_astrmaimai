import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.executor_stubs import install_executor_stubs
from tests.helpers.planner_stubs import install_planner_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeEvent:
    def __init__(self):
        self.message_str = "why not?"
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._extras = {
            "retrieve_keys": [],
            "astrmai_use_lane_history": True,
            "astrmai_recent_transcript": "AstrMai: no, that is not allowed\nUser: why not?",
            "astrmai_raw_user_text": "Alice: why not?",
            "astrmai_focus_message_text": "Alice: why not?",
            "astrmai_direct_context_text": "Focus block",
            "astrmai_related_context_text": "Related\nAstrMai: no, that is not allowed",
            "astrmai_ambient_background_text": "Bob: stay on topic\nCarol: I am reading too",
            "astrmai_focus_reason": "reply_to_bot",
            "astrmai_focus_thread_reason": "reply_to_bot",
        }

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def get_sender_name(self):
        return "Alice"


class _FakeReactRetriever:
    async def retrieve(self, **kwargs):
        return "related memory"


class _FakeMemoryEngine:
    async def recall(self, query, session_id=""):
        return "should not hit recall fallback"


class PromptRefinerFocusLayoutPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.planning.prompt_refiner", None)
        self.prompt_refiner_mod = importlib.import_module("astrmai.conversation.planning.prompt_refiner")
        self.prompt_refiner_mod = importlib.reload(self.prompt_refiner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_focus_sections_precede_ambient_background(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(),
        )
        event = _FakeEvent()

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": False},
            )

        final_system_prompt, final_prompt = asyncio.run(_run())
        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertLess(final_prompt.index("---前因---\nFocus block"), final_prompt.index("---旁边在聊的---\nBob: stay on topic"))
        self.assertIn("---眼前正在对我说的---\nAlice: why not?", final_prompt)
        self.assertIn("---补充---\nRelated\nAstrMai: no, that is not allowed", final_prompt)
        self.assertIn("Carol: I am reading too", final_prompt)

    def test_transcript_dedup_keeps_semantic_context_sections(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_recent_transcript"] = (
            "Alice: why not?\n"
            "Bob: ok\n"
            "Carol: longer duplicated clue\n"
            "Dave: separate transcript line"
        )
        event._extras["astrmai_direct_context_text"] = "Carol: longer duplicated clue"
        event._extras["astrmai_related_context_text"] = ""
        event._extras["astrmai_focus_message_text"] = "Alice: why not?"

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        transcript_block = final_prompt.split("---对话记录---\n", 1)[1].split("\n\n---眼前正在对我说的---", 1)[0]

        self.assertNotIn("Alice: why not?", transcript_block)
        self.assertNotIn("Carol: longer duplicated clue", transcript_block)
        self.assertIn("Bob: ok", transcript_block)
        self.assertIn("Dave: separate transcript line", transcript_block)
        self.assertIn("---前因---\nCarol: longer duplicated clue", final_prompt)


if __name__ == "__main__":
    unittest.main()
