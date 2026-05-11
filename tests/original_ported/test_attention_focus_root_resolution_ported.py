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
        self.timestamp = 100.0
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


class AttentionFocusRootResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_attention_stubs()
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        self.attention_mod = importlib.import_module("astrmai.conversation.attention.gate")
        self.attention_mod = importlib.reload(self.attention_mod)
        config = SimpleNamespace(
            attention=SimpleNamespace(
                max_message_length=100,
                focus_thread_enabled=True,
                focus_thread_core_max_messages=4,
                focus_thread_related_max_messages=3,
                ambient_background_max_messages=2,
                thread_same_speaker_followup_sec=8,
                thread_reply_priority_enabled=True,
            ),
            system1=SimpleNamespace(wakeup_words=[], nicknames=["亚托莉"]),
            global_settings=SimpleNamespace(debug_mode=False),
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

    def test_reply_to_bot_uses_replied_event_as_thread_root(self):
        bot_event = _FakeEvent("bot-1", "亚托莉", "不可以")
        focus_event = _FakeEvent(
            "user-1",
            "Alice",
            "为什么不可以",
            components=[Reply(sender_id="bot-1", sender_nickname="亚托莉")],
        )
        normalized = self.gate._build_normalized_events([bot_event, focus_event], self_id="bot-1")
        focus_candidate = normalized[-1]

        root_candidate, reason = self.gate._resolve_thread_root(focus_candidate, normalized)

        self.assertIsNotNone(root_candidate)
        self.assertIs(root_candidate.event, bot_event)
        self.assertEqual(reason, "explicit_reply_target")

    def test_same_sender_followup_uses_previous_message_as_root(self):
        first = _FakeEvent("user-1", "Alice", "等下", extras={"astrmai_timestamp": 10.0})
        second = _FakeEvent("user-1", "Alice", "我真正想补一句说明", extras={"astrmai_timestamp": 15.0})
        normalized = self.gate._build_normalized_events([first, second], self_id="bot-1")
        focus_candidate = normalized[-1]

        root_candidate, reason = self.gate._resolve_thread_root(focus_candidate, normalized)

        self.assertIsNotNone(root_candidate)
        self.assertIs(root_candidate.event, first)
        self.assertEqual(reason, "same_sender_chain")


if __name__ == "__main__":
    unittest.main()
