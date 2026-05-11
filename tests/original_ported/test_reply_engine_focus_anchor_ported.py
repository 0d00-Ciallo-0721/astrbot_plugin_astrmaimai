import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

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


class ReplyEngineFocusAnchorPortedTests(unittest.TestCase):
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

    def test_reply_prefers_thread_root_over_focus_and_legacy_anchor(self):
        service = self.reply_mod.ReplyService(_FakeStateEngine(), mood_manager=SimpleNamespace())
        event = _FakeEvent("user-1", "Alice", "main message")
        old_anchor = _FakeEvent("user-2", "Bob", "old anchor")
        focus_event = _FakeEvent("user-3", "Carol", "focus message")
        thread_root_event = _FakeEvent("user-4", "Dora", "thread root message")
        event.set_extra("astrmai_anchor_event", old_anchor)
        event.set_extra("astrmai_focus_event", focus_event)
        event.set_extra("astrmai_focus_thread_root_event", thread_root_event)
        event.set_extra("astrmai_window_events", [old_anchor, focus_event])
        captured = {}

        async def _fake_fetch_history(chat_id, anchor_text, anchor_event=None):
            captured["anchor_text"] = anchor_text
            captured["anchor_event"] = anchor_event
            return []

        service._fetch_history = _fake_fetch_history
        asyncio.run(service.handle_reply(event, "ok", event.unified_msg_origin))
        self.assertEqual(captured["anchor_text"], "thread root message")
        self.assertIs(captured["anchor_event"], thread_root_event)


if __name__ == "__main__":
    unittest.main()
