import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.planner_stubs import install_planner_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeLaneManager:
    async def get_recent_transcript(self, lane_key, base_origin, max_turns=4, max_age_seconds=None):
        return "AstrMai: that phrasing is odd\nUser: what part is odd?"


class _FakeGateway:
    def __init__(self):
        self.config = SimpleNamespace(
            system1=SimpleNamespace(nicknames=["AstrMai"]),
            global_settings=SimpleNamespace(debug_mode=False),
            provider=SimpleNamespace(),
            reply=SimpleNamespace(follow_up_probability=0.0, emotion_mapping={}),
            memory=SimpleNamespace(),
            agent=SimpleNamespace(),
        )
        self.lane_manager = _FakeLaneManager()


class _FakeContextEngine:
    def __init__(self):
        self.db = SimpleNamespace()
        self.context = SimpleNamespace(shared_dict={})
        self.calls = []

    async def build_prompt(self, **kwargs):
        self.calls.append(kwargs)
        return ("system prompt only", "自然简短", "")

    def get_last_prefix_hash(self, chat_id):
        return "hash-1"


class _FakePromptRefiner:
    def __init__(self):
        self.call = None

    async def refine_prompt(
        self,
        event,
        system_prompt,
        prompt="",
        context=None,
        *,
        prompt_envelope=None,
        style_variant="",
        proactive_recall="",
    ):
        self.call = {
            "prompt_envelope": prompt_envelope,
            "style_variant": style_variant,
            "proactive_recall": proactive_recall,
        }
        return system_prompt, "final prompt"


class _FakeExecutor:
    async def execute(self, event, system_prompt, prompt, tools=None, direct_vision_urls=None):
        return "ok"


class _FakeEvent:
    def __init__(self, sender_id, sender_name, text):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = None
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = {"retrieve_keys": []}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return "group-1"


class PlannerFocusMessagePriorityPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.planning.planner", None)
        self.planner_mod = importlib.import_module("astrmai.conversation.planning.planner")
        self.planner_mod = importlib.reload(self.planner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_planner_promotes_focus_thread_and_demotes_ambient_background(self):
        prompt_refiner = _FakePromptRefiner()
        context_engine = _FakeContextEngine()
        planner = self.planner_mod.Planner(
            context=SimpleNamespace(),
            gateway=_FakeGateway(),
            context_engine=context_engine,
            reply_engine=SimpleNamespace(config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping={}))),
            memory_engine=SimpleNamespace(),
            evolution_manager=SimpleNamespace(get_active_patterns=lambda chat_id: ""),
            state_engine=None,
            prompt_refiner=prompt_refiner,
            sys3_router=None,
        )
        planner.executor = _FakeExecutor()

        async def _no_follow(*args, **kwargs):
            return None

        planner._should_follow_up = _no_follow

        root_event = _FakeEvent("user-1", "Alice", "that setting feels odd")
        related_event = _FakeEvent("user-2", "Bob", "yeah, same here")
        ambient_event = _FakeEvent("user-3", "Carol", "I am getting water")
        focus_event = _FakeEvent("user-4", "Dora", "@AstrMai what part feels odd?")
        focus_event.set_extra("astrmai_focus_event", focus_event)
        focus_event.set_extra("astrmai_focus_reason", "at_bot")
        focus_event.set_extra("astrmai_focus_message_text", "Dora: @AstrMai what part feels odd?")
        focus_event.set_extra("astrmai_background_events", [root_event, related_event, ambient_event])
        focus_event.set_extra("astrmai_focus_thread_root_event", root_event)
        focus_event.set_extra("astrmai_focus_thread_core_events", [focus_event, root_event])
        focus_event.set_extra("astrmai_focus_thread_related_events", [related_event])
        focus_event.set_extra("astrmai_focus_thread_ambient_events", [ambient_event])
        focus_event.set_extra("astrmai_focus_thread_reason", "at_bot")

        asyncio.run(planner.plan_and_execute(focus_event, [root_event, related_event, ambient_event, focus_event]))

        envelope = prompt_refiner.call["prompt_envelope"]
        self.assertEqual(focus_event.get_extra("astrmai_raw_user_text"), "Dora: @AstrMai what part feels odd?")
        self.assertEqual(focus_event.get_extra("astrmai_ambient_background_text"), "Carol: I am getting water")
        self.assertIn("Alice: that setting feels odd", envelope.direct_context_text)
        self.assertIn("Bob: yeah, same here", envelope.related_context_text)
        self.assertIn("Carol: I am getting water", envelope.ambient_background_text)
        self.assertEqual(context_engine.calls[0]["event_messages"][-1], focus_event)


if __name__ == "__main__":
    unittest.main()
