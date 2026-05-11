import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_state_stubs():
    gateway_mod = types.ModuleType("astrmai.infra.gateway")
    gateway_mod.GlobalModelGateway = type("GlobalModelGateway", (), {})
    sys.modules["astrmai.infra.gateway"] = gateway_mod


class _FakePersistence:
    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        return None

    async def load_user_profile(self, user_id):
        return None

    async def save_user_profile(self, user_id, profile):
        return None


class StateRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_state_stubs()
        sys.modules.pop("astrmai.state", None)
        sys.modules.pop("astrmai.state.chat_state_service", None)
        self.state_mod = importlib.import_module("astrmai.state.chat_state_service")
        self.state_mod = importlib.reload(self.state_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_update_mood_keeps_delta_contract(self):
        config = SimpleNamespace(
            energy=SimpleNamespace(cost_per_reply=0.1, min_reply_threshold=0.1, daily_recovery=0.1, recovery_silence_min=60),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
            reply=SimpleNamespace(emotion_mapping=[]),
        )
        engine = self.state_mod.StateEngine(_FakePersistence(), SimpleNamespace(config=config), config=config)

        observed = {}

        async def _get_state(chat_id):
            return SimpleNamespace(mood=0.2)

        async def _analyze(text, current_mood, user_affection=0.0, chat_id=None):
            observed["text"] = text
            observed["chat_id"] = chat_id
            observed["current_mood"] = current_mood
            return "happy", 0.6

        async def _atomic(chat_id, delta=0.0, absolute_val=None):
            observed["delta"] = delta
            return 0.6

        engine.get_state = _get_state
        engine.mood_manager.analyze_mood = _analyze
        engine.atomic_update_mood = _atomic

        tag, final_mood = asyncio.run(engine.update_mood("chat-1", "hello"))
        self.assertEqual(tag, "happy")
        self.assertEqual(final_mood, 0.6)
        self.assertEqual(observed["chat_id"], "chat-1")
        self.assertAlmostEqual(observed["delta"], 0.4)


if __name__ == "__main__":
    unittest.main()
