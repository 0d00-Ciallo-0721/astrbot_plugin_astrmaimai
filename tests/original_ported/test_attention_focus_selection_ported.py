import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_attention_stubs as _install_attention_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class Reply:
    def __init__(self, sender_id="", sender_nickname=""):
        self.sender_id = sender_id
        self.sender_nickname = sender_nickname


class _FakeSensors:
    def is_wakeup_signal(self, event, self_id):
        return event.get_extra("wakeup", False)

    async def is_command(self, msg_str):
        return False

    async def should_process_message(self, event):
        return True


class _FakeEvent:
    def __init__(self, sender_id, sender_name, text, extras=None, components=None):
        self.message_str = text
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = SimpleNamespace(message=components or [], message_id=f"{sender_id}:{text}")
        self.timestamp = 123
        self._extra = dict(extras or {})
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


class AttentionFocusSelectionTests(unittest.TestCase):
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

    def test_reply_to_bot_has_highest_focus_priority(self):
        older_plain = _FakeEvent("user-1", "Alice", "前面那句先别管")
        reply_to_bot = _FakeEvent(
            "user-2",
            "Bob",
            "为什么不可以",
            components=[Reply(sender_id="bot-1", sender_nickname="亚托莉")],
        )
        later_plain = _FakeEvent("user-3", "Carol", "我也在看")

        focus_event, background_events, reason = self.gate._select_focus_event(
            [older_plain, reply_to_bot, later_plain],
            self_id="bot-1",
        )

        self.assertIs(focus_event, reply_to_bot)
        self.assertEqual(reason, "reply_to_bot")
        self.assertEqual(background_events, [older_plain, later_plain])

    def test_direct_wakeup_beats_later_plain_message(self):
        earlier_plain = _FakeEvent("user-1", "Alice", "旧话题")
        wakeup_event = _FakeEvent("user-2", "Bob", "@亚托莉 回我一下", extras={"wakeup": True})
        later_plain = _FakeEvent("user-3", "Carol", "路过插一句")

        focus_event, background_events, reason = self.gate._select_focus_event(
            [earlier_plain, wakeup_event, later_plain],
            self_id="bot-1",
        )

        self.assertIs(focus_event, wakeup_event)
        self.assertEqual(reason, "direct_wakeup")
        self.assertEqual(background_events, [earlier_plain, later_plain])


if __name__ == "__main__":
    unittest.main()
