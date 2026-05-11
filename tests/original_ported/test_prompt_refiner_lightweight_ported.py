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
            "astrmai_ambient_background_text": "Bob: stay on topic",
            "astrmai_focus_reason": "latest_user_message",
            "astrmai_focus_thread_reason": "latest_user_message",
        }

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_name(self):
        return "Alice"


class _FakeReactRetriever:
    def __init__(self, result="earlier lore reminder"):
        self.result = result
        self.calls = []

    async def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeMemoryEngine:
    def __init__(self, result="should not hit recall fallback"):
        self.calls = []
        self.result = result

    async def recall(self, query, session_id=""):
        self.calls.append((query, session_id))
        return self.result


class PromptRefinerLightweightPortedTests(unittest.TestCase):
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

    def test_refiner_builds_user_prompt_sections_and_memory_block(self):
        memory_engine = _FakeMemoryEngine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
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
                proactive_recall="主动记忆闪回：\n旧记忆片段",
            )

        final_system_prompt, final_prompt = asyncio.run(_run())

        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertEqual(memory_engine.calls, [])
        self.assertTrue(final_prompt.startswith("现在是 "))
        self.assertIn("---对话记录---\nUser: why not?", final_prompt)
        self.assertIn("---眼前正在对我说的---\nAlice: why not?", final_prompt)
        self.assertIn("---前因---\nFocus block", final_prompt)
        self.assertIn("---补充---\nRelated\nAstrMai: no, that is not allowed", final_prompt)
        self.assertIn("---旁边在聊的---\nBob: stay on topic", final_prompt)
        self.assertIn("---记忆闪回（仅供内心参考，不要出现在回复正文中）---", final_prompt)
        self.assertIn("主动记忆闪回：\n旧记忆片段", final_prompt)
        self.assertIn("earlier lore reminder", final_prompt)
        self.assertIn("内心浮现的印象，仅供我自己判断当下", final_prompt)
        self.assertIn("原文不要逐字出现在回复里", final_prompt)
        self.assertIn("绝不照搬原文", final_prompt)
        self.assertNotIn("不要照抄", final_prompt)

        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertTrue(memory_decision.injected)
        self.assertEqual(memory_decision.source, "proactive_recall+react")
        self.assertEqual(memory_decision.policy, "light")
        self.assertEqual(memory_decision.retrieve_keys, [])
        self.assertIn("earlier lore reminder", memory_decision.summary_preview)

    def test_refiner_skips_memory_and_slims_background_when_near_context_priority(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(),
        )
        event = _FakeEvent()
        event._extras["astrmai_near_context_priority"] = True
        event._extras["astrmai_ambient_background_text"] = "Bob: stay on topic\nCarol: I am reading too"

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="why not?",
                context={"disable_rag_injection": False},
            )

        final_system_prompt, final_prompt = asyncio.run(_run())
        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertNotIn("earlier lore reminder", final_prompt)
        self.assertNotIn("Bob: stay on topic", final_prompt)
        self.assertIn("Carol: I am reading too", final_prompt)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertFalse(memory_decision.injected)
        self.assertEqual(memory_decision.skip_reason, "near_context_priority")

    def test_refiner_records_fallback_recall_memory_decision(self):
        memory_engine = _FakeMemoryEngine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(result=""),
        )
        event = _FakeEvent()

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="why not?",
                context={"disable_rag_injection": False},
            )

        _final_system_prompt, final_prompt = asyncio.run(_run())

        self.assertIn("should not hit recall fallback", final_prompt)
        self.assertEqual(len(memory_engine.calls), 1)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertTrue(memory_decision.injected)
        self.assertEqual(memory_decision.source, "fallback_recall")
        self.assertIn("should not hit recall fallback", memory_decision.summary_preview)

    def test_refiner_records_disable_and_fast_skip_reasons(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(),
        )
        disabled_event = _FakeEvent()

        async def _disabled():
            return await refiner.refine_prompt(
                event=disabled_event,
                system_prompt="system prompt only",
                prompt="why not?",
                context={"disable_rag_injection": True},
            )

        asyncio.run(_disabled())
        disabled_decision = disabled_event.get_extra("astrmai_turn_context").memory
        self.assertFalse(disabled_decision.injected)
        self.assertEqual(disabled_decision.skip_reason, "disable_rag_injection")

        fast_event = _FakeEvent()
        fast_event._extras["retrieve_keys"] = ["CORE_ONLY"]

        async def _fast():
            return await refiner.refine_prompt(
                event=fast_event,
                system_prompt="system prompt only",
                prompt="why not?",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_fast())
        fast_decision = fast_event.get_extra("astrmai_turn_context").memory
        self.assertFalse(fast_decision.injected)
        self.assertEqual(fast_decision.skip_reason, "fast_mode")

    def test_refiner_lightweight_event_suppresses_memory_and_proactive_recall(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(),
        )
        event = _FakeEvent()
        event._extras["astrmai_lightweight_event"] = True

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="poke",
                context={"disable_rag_injection": False},
                proactive_recall="old proactive memory should not appear",
            )

        _final_system_prompt, final_prompt = asyncio.run(_run())

        self.assertNotIn("old proactive memory should not appear", final_prompt)
        self.assertNotIn("---璁板繂闂洖", final_prompt)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertFalse(memory_decision.injected)
        self.assertEqual(memory_decision.skip_reason, "lightweight_event")

    def test_refiner_skips_memory_for_think_level_zero(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(),
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 0

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="why not?",
                context={"disable_rag_injection": False},
                proactive_recall="proactive memory should not appear",
            )

        _system, final_prompt = asyncio.run(_run())

        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertFalse(memory_decision.injected)
        self.assertEqual(memory_decision.policy, "none")
        self.assertEqual(memory_decision.skip_reason, "think_level_0")
        self.assertNotIn("proactive memory should not appear", final_prompt)

    def test_refiner_skips_level_one_without_memory_intent(self):
        memory_engine = _FakeMemoryEngine()
        react = _FakeReactRetriever()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=react,
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 1

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="how are you?",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_run())

        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertFalse(memory_decision.injected)
        self.assertEqual(memory_decision.skip_reason, "think_level_1_no_memory_intent")
        self.assertEqual(react.calls, [])
        self.assertEqual(memory_engine.calls, [])

    def test_refiner_level_one_memory_intent_uses_fallback_only(self):
        memory_engine = _FakeMemoryEngine()
        react = _FakeReactRetriever()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=react,
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 1

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="你还记得刚才那件事吗",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_run())

        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertTrue(memory_decision.injected)
        self.assertEqual(memory_decision.source, "fallback_recall")
        self.assertEqual(react.calls, [])
        self.assertEqual(len(memory_engine.calls), 1)

    def test_refiner_level_two_uses_fallback_without_react_by_default(self):
        memory_engine = _FakeMemoryEngine()
        react = _FakeReactRetriever()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=react,
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 2

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="刚才为什么是那样？",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_run())

        self.assertEqual(react.calls, [])
        self.assertEqual(len(memory_engine.calls), 1)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.source, "fallback_recall")

    def test_refiner_level_two_deep_memory_intent_allows_react(self):
        memory_engine = _FakeMemoryEngine()
        react = _FakeReactRetriever()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=react,
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 2
        event._extras["astrmai_cognitive_memory_policy"] = "deep"

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="你还记得之前说的那个计划吗",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_run())

        self.assertEqual(len(react.calls), 1)
        self.assertEqual(memory_engine.calls, [])
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.policy, "deep")
        self.assertEqual(memory_decision.source, "react")

    def test_refiner_allows_react_at_think_level_three(self):
        memory_engine = _FakeMemoryEngine()
        react = _FakeReactRetriever()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=react,
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 3

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="你还记得我上次说什么吗？",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_run())

        self.assertEqual(len(react.calls), 1)
        self.assertEqual(memory_engine.calls, [])
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.source, "react")

    def test_refiner_level_three_falls_back_when_react_has_no_result(self):
        memory_engine = _FakeMemoryEngine()
        react = _FakeReactRetriever(result="")
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=react,
        )
        event = _FakeEvent()
        event._extras["astrmai_think_level"] = 3

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="你还记得我上次说什么吗？",
                context={"disable_rag_injection": False},
            )

        asyncio.run(_run())

        self.assertEqual(len(react.calls), 1)
        self.assertEqual(len(memory_engine.calls), 1)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.source, "fallback_recall")


if __name__ == "__main__":
    unittest.main()
