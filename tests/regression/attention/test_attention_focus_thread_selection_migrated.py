import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs, install_attention_stubs


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
        self.timestamp = 123.0
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


class AttentionFocusThreadSelectionMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_attention_stubs()
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
            system1=SimpleNamespace(wakeup_words=[], nicknames=["AstrMai"]),
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

    def test_reply_to_bot_builds_focus_thread_and_keeps_unrelated_message_ambient(self):
        bot_event = _FakeEvent("bot-1", "AstrMai", "legacy-bot-message")
        focus_event = _FakeEvent(
            "user-1",
            "Alice",
            "涓轰粈涔堜笉鍙互",
            components=[Reply(sender_id="bot-1", sender_nickname="AstrMai")],
        )
        unrelated = _FakeEvent("user-2", "Bob", "鎴戝幓鍚冮キ")

        normalized = self.gate._build_normalized_events([bot_event, focus_event, unrelated], self_id="bot-1")
        focus_candidate = normalized[1]
        root_candidate, _ = self.gate._resolve_thread_root(focus_candidate, normalized)
        focus_thread = self.gate._build_focus_thread(focus_candidate, root_candidate, normalized)

        self.assertEqual(focus_thread["core_events"], [bot_event, focus_event])
        self.assertEqual(focus_thread["ambient_events"], [unrelated])


__all__ = ["AttentionFocusThreadSelectionMigratedTests"]
