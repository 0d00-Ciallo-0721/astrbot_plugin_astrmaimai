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
        return "AstrMai: no, that is not allowed\nUser: why not?"


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

    async def build_prompt(self, **kwargs):
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
        }
        return system_prompt, "final prompt"


class _FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, event, system_prompt, prompt, tools=None, direct_vision_urls=None):
        self.calls.append((system_prompt, prompt))
        return "ok"


class _FakeEvent:
    def __init__(self, text="why not?"):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = None
        self._extra = {"retrieve_keys": []}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return "group-1"


class PlannerIncludesLastAssistantTurnPortedTests(unittest.TestCase):
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

    def test_planner_carries_last_assistant_turn_into_prompt_envelope(self):
        prompt_refiner = _FakePromptRefiner()
        planner = self.planner_mod.Planner(
            context=SimpleNamespace(),
            gateway=_FakeGateway(),
            context_engine=_FakeContextEngine(),
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
        event = _FakeEvent()

        asyncio.run(planner.plan_and_execute(event, [event]))

        envelope = prompt_refiner.call["prompt_envelope"]
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.last_assistant_reply, "no, that is not allowed")
        self.assertEqual(envelope.focus_message_text, "Alice: why not?")
        self.assertEqual(event.get_extra("astrmai_raw_user_text"), "Alice: why not?")
        self.assertEqual(
            event.get_extra("astrmai_recent_transcript"),
            "AstrMai: no, that is not allowed\nUser: why not?",
        )


if __name__ == "__main__":
    unittest.main()
