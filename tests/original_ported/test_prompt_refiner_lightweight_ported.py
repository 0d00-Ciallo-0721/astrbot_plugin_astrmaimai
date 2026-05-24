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

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"


class _FakeRetrievalService:
    def __init__(self, contracts_mod, result="memory v2 reminder"):
        self.contracts_mod = contracts_mod
        self.result = result
        self.calls = []

    async def retrieve(self, query):
        self.calls.append(query)
        if not self.result:
            return []
        return [
            self.contracts_mod.MemoryCandidate(
                id="mem-1",
                kind="memory",
                source="canonical",
                summary=self.result,
                content=self.result,
                session_id=query.session_id,
                relevance_score=0.9,
                recency_score=0.8,
                created_at=1.0,
                updated_at=1.0,
            )
        ]


class PromptRefinerLightweightPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        install_planner_stubs()
        for name in [
            "astrmai.memory.contracts.memory_query",
            "astrmai.memory.services.memory_injection_service",
            "astrmai.conversation.planning.prompt_refiner",
        ]:
            sys.modules.pop(name, None)
        self.contracts_mod = importlib.import_module("astrmai.memory.contracts.memory_query")
        self.injection_mod = importlib.import_module("astrmai.memory.services.memory_injection_service")
        self.prompt_refiner_mod = importlib.import_module("astrmai.conversation.planning.prompt_refiner")
        self.prompt_refiner_mod = importlib.reload(self.prompt_refiner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _config(self):
        return SimpleNamespace(
            memory=SimpleNamespace(enable_react_agent=True, recall_top_k=5),
            persona=SimpleNamespace(persona_id="persona-1"),
        )

    def _make_memory_engine(self, result="memory v2 reminder"):
        retrieval = _FakeRetrievalService(self.contracts_mod, result=result)
        injection = self.injection_mod.MemoryInjectionService(retrieval, config=self._config())
        return SimpleNamespace(
            injection_service=injection,
            retrieval_service=retrieval,
            retrieval_calls=retrieval.calls,
        )

    def test_refiner_builds_user_prompt_sections_and_memory_block(self):
        memory_engine = self._make_memory_engine("earlier lore reminder")
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
        )
        event = _FakeEvent()
        prompt_envelope = self.prompt_refiner_mod.PromptEnvelope()

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": False},
                prompt_envelope=prompt_envelope,
                proactive_recall="proactive memory fragment",
            )

        final_system_prompt, final_prompt = asyncio.run(_run())

        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertEqual(len(memory_engine.retrieval_calls), 1)
        self.assertIn("---记忆闪回", final_prompt)
        self.assertIn("earlier lore reminder", final_prompt)
        self.assertIn("proactive memory fragment", final_prompt)
        self.assertIn("proactive memory fragment", prompt_envelope.background_memory_block)
        self.assertIn("earlier lore reminder", prompt_envelope.background_memory_block)
        self.assertEqual(prompt_envelope.memory_block, prompt_envelope.background_memory_block)
        self.assertIn("proactive_recall", prompt_envelope.background_memory_sections)
        self.assertIn("memory_injection", prompt_envelope.background_memory_sections)
        self.assertEqual(prompt_envelope.background_memory_sections["proactive_recall"], "proactive memory fragment")
        self.assertIn("earlier lore reminder", prompt_envelope.background_memory_sections["memory_injection"])
        self.assertGreater(prompt_envelope.background_memory_rendered_chars, 0)

        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertTrue(memory_decision.injected)
        self.assertEqual(memory_decision.source, "proactive_recall+memory_v2")
        self.assertIn("earlier lore reminder", memory_decision.summary_preview)

    def test_refiner_records_no_result_memory_decision(self):
        memory_engine = self._make_memory_engine(result="")
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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

        self.assertNotIn("memory v2 reminder", final_prompt)
        self.assertEqual(len(memory_engine.retrieval_calls), 1)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertFalse(memory_decision.injected)
        self.assertEqual(memory_decision.skip_reason, "no_result")

    def test_refiner_records_disable_and_fast_skip_reasons(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=self._make_memory_engine(),
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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
            memory_engine=self._make_memory_engine(),
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertFalse(memory_decision.injected)
        self.assertEqual(memory_decision.skip_reason, "lightweight_event")

    def test_refiner_skips_memory_for_think_level_zero(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=self._make_memory_engine(),
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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
        memory_engine = self._make_memory_engine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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
        self.assertEqual(memory_engine.retrieval_calls, [])

    def test_refiner_level_one_memory_intent_uses_memory_v2(self):
        memory_engine = self._make_memory_engine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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
        self.assertEqual(memory_decision.source, "memory_v2")
        self.assertEqual(len(memory_engine.retrieval_calls), 1)

    def test_refiner_level_two_uses_memory_v2_by_default(self):
        memory_engine = self._make_memory_engine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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

        self.assertEqual(len(memory_engine.retrieval_calls), 1)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.source, "memory_v2")

    def test_refiner_level_two_deep_memory_intent_still_uses_memory_v2(self):
        memory_engine = self._make_memory_engine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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

        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.policy, "deep")
        self.assertEqual(memory_decision.source, "memory_v2")

    def test_refiner_allows_memory_v2_at_think_level_three(self):
        memory_engine = self._make_memory_engine()
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=memory_engine,
            config=self._config(),
            react_retriever=SimpleNamespace(retrieve=None),
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

        self.assertEqual(len(memory_engine.retrieval_calls), 1)
        memory_decision = event.get_extra("astrmai_turn_context").memory
        self.assertEqual(memory_decision.source, "memory_v2")


if __name__ == "__main__":
    unittest.main()
