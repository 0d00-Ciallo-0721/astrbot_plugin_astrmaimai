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
    def __init__(self, prompt_envelope):
        self.message_str = "why not?"
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._extras = {
            "retrieve_keys": [],
            "astrmai_use_lane_history": True,
            "astrmai_prompt_envelope": prompt_envelope,
        }

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def get_sender_name(self):
        return "Alice"


class _FakeReactRetriever:
    async def retrieve(self, **kwargs):
        return ""


class _FakeMemoryEngine:
    async def recall(self, query, session_id=""):
        return ""


class PromptEnvelopeRenderingPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        install_planner_stubs()
        sys.modules.pop("astrmai.infrastructure.runtime.runtime_contracts", None)
        sys.modules.pop("astrmai.conversation.planning.prompt_refiner", None)
        self.contracts_mod = importlib.import_module("astrmai.infrastructure.runtime.runtime_contracts")
        self.contracts_mod = importlib.reload(self.contracts_mod)
        self.prompt_refiner_mod = importlib.import_module("astrmai.conversation.planning.prompt_refiner")
        self.prompt_refiner_mod = importlib.reload(self.prompt_refiner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_refiner_prefers_prompt_envelope_when_available(self):
        envelope = self.contracts_mod.PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="AstrMai: no, that is not allowed\nUser: why not?",
            last_assistant_reply="no, that is not allowed",
            focus_message_text="Alice: why not?",
            direct_context_text="Current focus",
            ambient_background_text="Bob: stay on topic",
            focus_reason="reply_to_bot",
            focus_thread_reason="recent_assistant_turn",
            near_context_priority=False,
        )
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=_FakeReactRetriever(),
        )
        event = _FakeEvent(envelope)

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="old prompt",
                context={"disable_rag_injection": False},
            )

        final_system_prompt, final_prompt = asyncio.run(_run())
        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertIn("现在是 ", final_prompt)
        self.assertIn("---眼前正在对我说的---\nAlice: why not?", final_prompt)
        self.assertIn("---前因---\nCurrent focus", final_prompt)
        self.assertIn("---旁边在聊的---\nBob: stay on topic", final_prompt)
        self.assertNotIn("old prompt", final_prompt)


if __name__ == "__main__":
    unittest.main()
