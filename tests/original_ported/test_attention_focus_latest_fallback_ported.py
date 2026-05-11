import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_attention_stubs as _install_attention_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeSensors:
    def is_wakeup_signal(self, event, self_id):
        return False

    async def is_command(self, msg_str):
        return False

    async def should_process_message(self, event):
        return True


class _FakeEvent:
    def __init__(self, sender_id, sender_name, text):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = SimpleNamespace(message=[], message_id=f"{sender_id}:{text}")
        self.timestamp = 123
        self._extra = {}
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return "bot-1"

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class AttentionFocusLatestFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_attention_stubs()
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        self.attention_mod = importlib.import_module("astrmai.conversation.attention.gate")
        self.attention_mod = importlib.reload(self.attention_mod)
        config = SimpleNamespace(
            attention=SimpleNamespace(max_message_length=100),
            system1=SimpleNamespace(wakeup_words=[], nicknames=["亚托莉"]),
        )
        self.gate = self.attention_mod.AttentionGate(
            state_engine=SimpleNamespace(config=config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(),
            system2_callback=None,
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_falls_back_to_latest_user_message_without_explicit_signal(self):
        first_event = _FakeEvent("user-1", "Alice", "先聊前面的事情")
        second_event = _FakeEvent("user-2", "Bob", "我插一句")
        latest_event = _FakeEvent("user-3", "Carol", "现在请回我这句")

        focus_event, background_events, reason = self.gate._select_focus_event(
            [first_event, second_event, latest_event],
            self_id="bot-1",
        )

        self.assertIs(focus_event, latest_event)
        self.assertEqual(reason, "latest_user_message")
        self.assertEqual(background_events, [first_event, second_event])


if __name__ == "__main__":
    unittest.main()
