import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.test_persistence_regressions import _install_astrbot_stubs


def _install_gateway_stub():
    gateway_mod = types.ModuleType("astrmai.infra.gateway")
    gateway_mod.GlobalModelGateway = type("GlobalModelGateway", (), {})
    sys.modules["astrmai.infra.gateway"] = gateway_mod


def _install_heart_package_stubs():
    attention_mod = types.ModuleType("astrmai.Heart.attention")
    attention_mod.AttentionGate = type("AttentionGate", (), {})
    sys.modules["astrmai.Heart.attention"] = attention_mod

    judge_mod = types.ModuleType("astrmai.Heart.judge")
    judge_mod.Judge = type("Judge", (), {})
    sys.modules["astrmai.Heart.judge"] = judge_mod

    sensors_mod = types.ModuleType("astrmai.Heart.sensors")
    sensors_mod.PreFilters = type("PreFilters", (), {})
    sys.modules["astrmai.Heart.sensors"] = sensors_mod


class StateEngineMoodTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_gateway_stub()
        _install_heart_package_stubs()
        sys.modules.pop("astrmai.Heart", None)
        sys.modules.pop("astrmai.Heart.mood_manager", None)
        sys.modules.pop("astrmai.Heart.state_engine", None)
        self.mood_mod = importlib.import_module("astrmai.Heart.mood_manager")
        self.mood_mod = importlib.reload(self.mood_mod)
        self.state_mod = importlib.import_module("astrmai.Heart.state_engine")
        self.state_mod = importlib.reload(self.state_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_analyze_text_mood_alias_parses_markdown_wrapped_json(self):
        async def _call_mood_task(prompt, system_prompt=None):
            return "```json\n{\"mood_tag\": \"sad\", \"mood_value\": -0.2}\n```"

        gateway = SimpleNamespace(
            config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping=[])),
            call_mood_task=_call_mood_task,
        )
        manager = self.mood_mod.MoodManager(gateway)

        tag, mood_value = asyncio.run(manager.analyze_text_mood("hello", 0.1))

        self.assertEqual(tag, "sad")
        self.assertAlmostEqual(mood_value, -0.2)

    def test_update_mood_delegates_to_analyze_mood(self):
        gateway = SimpleNamespace(config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping=[])))
        engine = self.state_mod.StateEngine(SimpleNamespace(), gateway)

        observed = {}

        async def _get_state(chat_id):
            return SimpleNamespace(mood=0.2)

        async def _analyze_mood(text, current_mood, user_affection=0.0):
            observed["text"] = text
            observed["current_mood"] = current_mood
            return "happy", 0.6

        async def _atomic_update(chat_id, delta=0.0):
            observed["delta"] = delta
            return 0.6

        engine.get_state = _get_state
        engine.mood_manager.analyze_mood = _analyze_mood
        engine.atomic_update_mood = _atomic_update

        tag, final_mood = asyncio.run(engine.update_mood("chat-1", "hello"))

        self.assertEqual(tag, "happy")
        self.assertEqual(final_mood, 0.6)
        self.assertEqual(observed["text"], "hello")
        self.assertAlmostEqual(observed["current_mood"], 0.2)
        self.assertAlmostEqual(observed["delta"], 0.4)


if __name__ == "__main__":
    unittest.main()
