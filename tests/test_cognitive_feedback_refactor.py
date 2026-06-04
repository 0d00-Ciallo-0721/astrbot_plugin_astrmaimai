import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeGateway:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.config = SimpleNamespace(memory=SimpleNamespace(recall_top_k=5), persona=SimpleNamespace(persona_id="persona-1"))

    async def call_data_process_task(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return self.responses.pop(0) if self.responses else {"action": "reply"}


class _FakeEvent:
    def __init__(self, text="please remember this detail"):
        self.message_str = text
        self.unified_msg_origin = "chat-1"
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"


class CognitiveFeedbackRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        event_mod = sys.modules["astrbot.api.event"]

        class MessageChain:
            def __init__(self):
                self.chain = []

            def message(self, text):
                self.chain.append(text)
                return self

        event_mod.MessageChain = MessageChain
        for name in [
            "astrmai.memory.services.memory_engine",
            "astrmai.conversation.planning.agency_runtime",
            "astrmai.conversation.planning.agency_feedback_bridge",
            "astrmai.conversation.planning.cognitive_loop",
            "astrmai.proactive.dream_scheduler",
            "astrmai.proactive.diary_service",
        ]:
            sys.modules.pop(name, None)
        self.memory_mod = importlib.reload(importlib.import_module("astrmai.memory.services.memory_engine"))
        self.runtime_mod = importlib.reload(importlib.import_module("astrmai.conversation.planning.agency_runtime"))
        self.bridge_mod = importlib.reload(importlib.import_module("astrmai.conversation.planning.agency_feedback_bridge"))
        self.loop_mod = importlib.reload(importlib.import_module("astrmai.conversation.planning.cognitive_loop"))
        self.dream_mod = importlib.reload(importlib.import_module("astrmai.proactive.dream_scheduler"))
        self.diary_mod = importlib.reload(importlib.import_module("astrmai.proactive.diary_service"))

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _memory_engine(self):
        return self.memory_mod.MemoryEngine(SimpleNamespace(), _FakeGateway(), embedding_models=[])

    def test_memory_engine_records_feedback_in_cache_and_filters_recall(self):
        engine = self._memory_engine()

        async def _offline():
            return False

        engine._ensure_faiss_initialized = _offline
        # mock initialize() 依赖 — 真实路径需要 v2_store / faiss 等重量依赖
        engine.write_service = SimpleNamespace()

        async def _mock_write(*args, **kwargs):
            return "mock-memory-id"

        engine.write_service.write = _mock_write
        engine.retrieval_service = SimpleNamespace()

        async def _mock_retrieve(*args, **kwargs):
            return [
                SimpleNamespace(content="[cognitive_feedback:agency]\nsummary: hidden\n"),
                SimpleNamespace(content="normal memory content"),
            ]

        engine.retrieval_service.retrieve = _mock_retrieve

        def _mock_render_recall(query, candidates):
            return " ".join(item.content for item in candidates)

        engine.retrieval_service.render_recall = _mock_render_recall

        async def _run():
            await engine.record_cognitive_feedback(
                session_id="chat-1",
                source="agency",
                summary="recently used meme twice",
                guidance="avoid repeated meme",
                tags=["meme"],
            )
            return await engine.get_cognitive_feedback("chat-1", limit=2)

        signals = asyncio.run(_run())

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "agency")
        self.assertEqual(signals[0].guidance, "avoid repeated meme")

        class _Item:
            def __init__(self, content):
                self.content = content
                self.score = 1.0

        async def _ready():
            return True

        async def _search(*args, **kwargs):
            return [
                _Item("[cognitive_feedback:agency]\nsummary: hidden\n"),
                _Item("normal memory content"),
            ]

        engine._ensure_faiss_initialized = _ready
        engine._search_memories = _search
        recalled = asyncio.run(engine.recall("anything", session_id="chat-1"))

        self.assertIn("normal memory content", recalled)
        self.assertNotIn("cognitive_feedback", recalled)

    def test_agency_reflection_bridge_flushes_after_threshold(self):
        runtime = self.runtime_mod.AgencyRuntimeStore()

        class _Memory:
            def __init__(self):
                self.calls = []

            async def record_cognitive_feedback(self, **kwargs):
                self.calls.append(kwargs)

        memory = _Memory()
        bridge = self.bridge_mod.AgencyReflectionBridge(memory)
        for index in range(6):
            runtime.record(
                chat_id="chat-1",
                reply_need="reply",
                social_intent="tease" if index < 4 else "answer",
                action_tier="chat",
                action_taken="reply",
                reply_preview="ok",
                cooldown_tags=["meme"] if index % 2 == 0 else ["like"],
            )

        flushed = asyncio.run(bridge.maybe_flush(runtime, "chat-1"))

        self.assertTrue(flushed)
        self.assertEqual(len(memory.calls), 1)
        self.assertEqual(memory.calls[0]["source"], "agency")
        self.assertIn("main_intent=tease", memory.calls[0]["summary"])
        self.assertIn("Avoid repeating", memory.calls[0]["guidance"])

    def test_cognitive_loop_reads_long_term_feedback_in_hidden_prompt(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "memory_policy": "light",
                    "social_intent": "answer",
                    "action_tier": "chat",
                }
            ]
        )
        loop = self.loop_mod.CognitiveLoop(gateway)
        event = _FakeEvent("please explain why this matters")
        event.set_extra("astrmai_memory_feedback_summary", "Long-term behavior and memory feedback:\n- agency: avoid repeated meme")

        decision = asyncio.run(loop.decide(event=event))

        self.assertIsNotNone(decision)
        self.assertIn("Long-term behavior/memory feedback", gateway.calls[0]["prompt"])
        self.assertIn("avoid repeated meme", gateway.calls[0]["prompt"])

    def test_dream_scheduler_builds_feedback_guidance_from_maintenance_tags(self):
        guidance = self.dream_mod.DreamScheduler._maintenance_guidance(["merge", "delete", "jargon_review"])

        self.assertIn("consolidated memory", guidance)
        self.assertIn("stale or noisy", guidance)
        self.assertIn("jargon", guidance)

    def test_dream_scheduler_writes_cognitive_feedback(self):
        class _Memory:
            def __init__(self):
                self.feedback_calls = []
                self.memory_calls = []

            async def record_cognitive_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)

            async def add_memory(self, **kwargs):
                self.memory_calls.append(kwargs)

        class _Agent:
            MIN_EVENTS_TO_DREAM = 5
            _last_session_id = "chat-1"

            async def run_dream_cycle(self, session_id=None):
                return "dream log"

        class _Generator:
            async def generate(self, **kwargs):
                return "dream text"

            def build_maintenance_result(self, dream_log, session_id="global"):
                return {"summary": "merged memory", "tags": ["merge", "delete"]}

        memory = _Memory()
        scheduler = self.dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=memory,
            config=SimpleNamespace(life=SimpleNamespace(dream_interval_min=1, min_memory_events_to_dream=5, dream_time_ranges=[]), persona=SimpleNamespace(name="Mai")),
            semaphore=asyncio.Semaphore(1),
            dream_visible=False,
        )
        scheduler.bind_dependencies(_Agent(), _Generator())

        asyncio.run(scheduler.run_once())

        self.assertEqual(len(memory.feedback_calls), 1)
        self.assertEqual(memory.feedback_calls[0]["source"], "dream")
        self.assertIn("consolidated memory", memory.feedback_calls[0]["guidance"])

    def test_diary_service_writes_cognitive_feedback(self):
        class _Memory:
            def __init__(self):
                self.feedback_calls = []
                self.memory_calls = []
                self.summarizer = None

            async def get_recent_memories(self, group_id, hours=24):
                return ["one recent memory"]

            async def record_cognitive_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)

            async def add_memory(self, **kwargs):
                self.memory_calls.append(kwargs)

        async def _call_background_lane(*args, **kwargs):
            return "quiet diary summary"

        class _FakePromptRegistry:
            def render_template(self, template_id, variables):
                return SimpleNamespace(prompt="diary prompt", system_prompt="diary system")

        memory = _Memory()
        service = self.diary_mod.DiaryService(
            persistence=SimpleNamespace(load_persona_cache=lambda: {"persona-1": {"summary": "core persona"}}),
            memory_engine=memory,
            config=SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1")),
            call_background_lane=_call_background_lane,
            semaphore=asyncio.Semaphore(1),
            prompt_registry=_FakePromptRegistry(),
        )

        asyncio.run(service.run_once([SimpleNamespace(chat_id="chat-1")]))

        self.assertEqual(len(memory.feedback_calls), 1)
        self.assertEqual(memory.feedback_calls[0]["source"], "diary")
        self.assertIn("quiet diary summary", memory.feedback_calls[0]["summary"])


if __name__ == "__main__":
    unittest.main()
