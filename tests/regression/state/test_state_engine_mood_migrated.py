import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


class StateEngineMoodMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for module_name in [
            'astrmai.state.mood.mood_manager',
            'astrmai.state.chat_state_service',
        ]:
            sys.modules.pop(module_name, None)
        self.mood_mod = importlib.import_module('astrmai.state.mood.mood_manager')
        self.mood_mod = importlib.reload(self.mood_mod)
        self.state_mod = importlib.import_module('astrmai.state.chat_state_service')
        self.state_mod = importlib.reload(self.state_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_analyze_text_mood_alias_parses_markdown_wrapped_json(self):
        async def _call_mood_task(prompt, system_prompt=None):
            return """```json
{"mood_tag": "sad", "mood_value": -0.2}
```"""

        gateway = SimpleNamespace(
            config=SimpleNamespace(reply=SimpleNamespace(emotion_mapping=[])),
            call_mood_task=_call_mood_task,
        )
        manager = self.mood_mod.MoodManager(gateway)

        tag, mood_value = asyncio.run(manager.analyze_text_mood('hello', 0.1))

        self.assertEqual(tag, 'sad')
        self.assertAlmostEqual(mood_value, -0.2)

    def test_update_mood_delegates_to_analyze_mood(self):
        config = SimpleNamespace(
            reply=SimpleNamespace(emotion_mapping=[]),
            energy=SimpleNamespace(recovery_silence_min=60, daily_recovery=0.1, cost_per_reply=0.1, min_reply_threshold=0.2),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
        )
        gateway = SimpleNamespace(config=config)
        engine = self.state_mod.StateEngine(SimpleNamespace(), gateway, config=config)

        observed = {}

        class _MockState:
            mood = 0.2
            energy = 0.5
            last_reply_time = 0
            last_passive_decay_time = 0
            is_dirty = False

        async def _get_state_inner(chat_id):
            return _MockState()

        async def _save_chat_state(chat_id, state):
            observed['saved_mood'] = state.mood

        async def _analyze_mood(text, current_mood, user_affection=0.0, chat_id=None):
            observed['text'] = text
            observed['current_mood'] = current_mood
            observed['chat_id'] = chat_id
            return 'happy', 0.6

        engine.chat_state_service._get_state_inner = _get_state_inner
        engine.chat_state_service.persistence.save_chat_state = _save_chat_state
        engine.mood_manager.analyze_mood = _analyze_mood

        tag, final_mood = asyncio.run(engine.update_mood('chat-1', 'hello'))

        self.assertEqual(tag, 'happy')
        self.assertEqual(final_mood, 0.6)
        self.assertEqual(observed['text'], 'hello')
        self.assertEqual(observed['chat_id'], 'chat-1')
        self.assertAlmostEqual(observed['current_mood'], 0.2)
        self.assertAlmostEqual(observed['saved_mood'], 0.6)

    def test_update_mood_falls_back_for_legacy_mood_manager_signature(self):
        config = SimpleNamespace(
            reply=SimpleNamespace(emotion_mapping=[]),
            energy=SimpleNamespace(recovery_silence_min=60, daily_recovery=0.1, cost_per_reply=0.1, min_reply_threshold=0.2),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
        )
        gateway = SimpleNamespace(config=config)
        engine = self.state_mod.StateEngine(SimpleNamespace(), gateway, config=config)

        observed = {}

        class _MockState:
            mood = -0.1
            energy = 0.5
            last_reply_time = 0
            last_passive_decay_time = 0
            is_dirty = False

        async def _get_state_inner(chat_id):
            return _MockState()

        async def _save_chat_state(chat_id, state):
            observed['saved_mood'] = state.mood

        async def _legacy_analyze_mood(text, current_mood, user_affection=0.0):
            observed['text'] = text
            observed['current_mood'] = current_mood
            return 'neutral', 0.0

        engine.chat_state_service._get_state_inner = _get_state_inner
        engine.chat_state_service.persistence.save_chat_state = _save_chat_state
        engine.mood_manager.analyze_mood = _legacy_analyze_mood

        tag, final_mood = asyncio.run(engine.update_mood('chat-legacy', 'legacy hello'))

        self.assertEqual(tag, 'neutral')
        self.assertEqual(final_mood, 0.0)
        self.assertEqual(observed['text'], 'legacy hello')
        self.assertAlmostEqual(observed['current_mood'], -0.1)
        self.assertAlmostEqual(observed['saved_mood'], 0.0)


__all__ = ['StateEngineMoodMigratedTests']
