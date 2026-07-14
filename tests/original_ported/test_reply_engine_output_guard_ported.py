import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.original_ported.helpers import _install_astrbot_stubs
from tests.helpers.reply_engine_stubs import install_reply_engine_stubs


class _CaptureContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        parts = []
        for comp in chain.chain:
            text = getattr(comp, "text", None)
            if text is not None:
                parts.append(text)
        self.sent.append((umo, "".join(parts) if parts else chain))


class _DummyLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeStateEngine:
    def __init__(self):
        self.gateway = SimpleNamespace(context=_CaptureContext())
        self.config = SimpleNamespace(
            reply=SimpleNamespace(segment_min_len=4, no_segment_max_len=200, meme_probability=0, emotion_mapping={}, fallback_text="fallback", typing_speed_factor=0.0),
            infra=SimpleNamespace(api_timeout=15),
            attention=SimpleNamespace(bg_pool_size=20),
            global_settings=SimpleNamespace(debug_mode=False),
        )

    async def get_state(self, chat_id):
        return SimpleNamespace()

    async def atomic_update_mood(self, chat_id, delta=0.0):
        return 0.0

    def _get_user_lock(self, user_id):
        return _DummyLock()

    async def calculate_and_update_affection(self, **kwargs):
        return None


class _FakeEvent:
    def __init__(self, sender_id="user-1", sender_name="Alice", text="test message"):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = {}

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return "bot-1"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class ReplyEngineOutputGuardPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_reply_engine_stubs()
        sys.modules.pop("astrmai.conversation.execution.reply_service", None)
        self.reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.reply_mod = importlib.reload(self.reply_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_provider_failure_payload_is_replaced_with_fallback(self):
        state_engine = _FakeStateEngine()
        service = self.reply_mod.ReplyService(state_engine=state_engine, mood_manager=SimpleNamespace())
        event = _FakeEvent(text="why not?")
        dirty_reply = 'JSON response: {"candidates": [{"finishReason": "SAFETY"}]}\nHTTP status code: 200\n(request_id: 20260410000000)'
        asyncio.run(service.handle_reply(event, dirty_reply, event.unified_msg_origin))
        self.assertEqual(state_engine.gateway.context.sent[0][1], "fallback")

    def test_prompt_scaffold_lines_are_filtered_but_natural_reply_is_preserved(self):
        state_engine = _FakeStateEngine()
        service = self.reply_mod.ReplyService(state_engine=state_engine, mood_manager=SimpleNamespace())
        event = _FakeEvent(text="hello")
        dirty_reply = "user: please comfort me\nassistant: hmm...\n[RollingSummary]\nDo not be sad, I am here."
        asyncio.run(service.handle_reply(event, dirty_reply, event.unified_msg_origin))
        sent_text = "\n".join(text for _, text in state_engine.gateway.context.sent)
        self.assertNotIn("user:", sent_text)
        self.assertNotIn("assistant:", sent_text)
        self.assertNotIn("[RollingSummary]", sent_text)
        self.assertIn("hmm", sent_text)
        self.assertIn("Do not be sad", sent_text)

    def test_optional_meme_failure_does_not_change_successful_text_reply(self):
        async def _raise_meme_error(**_kwargs):
            raise RuntimeError("meme adapter unavailable")

        state_engine = _FakeStateEngine()
        service = self.reply_mod.ReplyService(state_engine=state_engine, mood_manager=SimpleNamespace())
        event = _FakeEvent(text="hello")

        with patch("astrmai.conversation.execution.reply_post_send.send_meme", new=_raise_meme_error):
            artifact = asyncio.run(
                service.handle_reply(
                    event,
                    "A normal successful reply.",
                    event.unified_msg_origin,
                    bypassed_tag="happy",
                )
            )

        self.assertEqual(artifact.metadata["send_status"], "sent")
        self.assertEqual(len(state_engine.gateway.context.sent), 1)
        self.assertTrue(event.get_extra("astrmai_meme_send_degraded"))


if __name__ == "__main__":
    unittest.main()
