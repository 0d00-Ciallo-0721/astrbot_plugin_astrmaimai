import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import install_attention_stubs as _install_attention_stubs
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeSensors:
    def __init__(self, wakeup=False):
        self._wakeup = wakeup

    def is_wakeup_signal(self, event, self_id):
        return self._wakeup

    async def is_command(self, msg_str):
        return False

    async def should_process_message(self, event):
        return True


class _FakeStateEngine:
    def __init__(self, config):
        self.config = config
        self.persistence = SimpleNamespace(add_last_message_meta=self._noop)

    async def get_state(self, chat_id):
        return SimpleNamespace(energy=1.0, mood=0.0)

    async def _noop(self, *args, **kwargs):
        return None


class _FakeEvent:
    def __init__(self, text="", extras=None, group_id="group-1"):
        self.message_str = text
        self.unified_msg_origin = f"default:{'GroupMessage' if group_id else 'FriendMessage'}:{group_id or 'user-1'}"
        self.message_obj = None
        self.timestamp = 123.0
        self._group_id = group_id
        self._extra = dict(extras or {})

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"

    def get_self_id(self):
        return "bot-1"

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class AttentionImageGatingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_attention_stubs()
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        self.attention_mod = importlib.import_module("astrmai.conversation.attention.gate")
        self.attention_mod = importlib.reload(self.attention_mod)
        self.config = SimpleNamespace(
            attention=SimpleNamespace(
                max_message_length=100,
                throttle_min_entropy=2,
                throttle_probability=1.0,
                repeater_threshold=3,
                debounce_window=0.1,
                focus_thread_enabled=True,
                focus_thread_core_max_messages=4,
                focus_thread_related_max_messages=3,
                ambient_background_max_messages=2,
                thread_same_speaker_followup_sec=8,
                thread_reply_priority_enabled=True,
            ),
            system1=SimpleNamespace(wakeup_words=[], nicknames=[]),
            global_settings=SimpleNamespace(debug_mode=False),
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_passive_group_image_share_is_ignored(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=False),
            system2_callback=None,
        )
        event = _FakeEvent(
            text="",
            extras={"extracted_image_urls": ["https://example.com/passive.jpg"]},
            group_id="group-1",
        )

        result = asyncio.run(gate.process_event(event))
        self.assertEqual(result, "IGNORED_IMAGE")

    def test_direct_vision_request_is_not_treated_as_passive_share(self):
        gate = self.attention_mod.AttentionGate(
            state_engine=_FakeStateEngine(self.config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(wakeup=True),
            system2_callback=None,
        )
        event = _FakeEvent(
            text="@亚托莉 看这个",
            extras={
                "extracted_image_urls": ["https://example.com/direct.jpg"],
                "direct_vision_urls": ["https://example.com/direct.jpg"],
            },
            group_id="group-1",
        )

        result = asyncio.run(gate.process_event(event))
        self.assertEqual(result, "ENGAGED")


if __name__ == "__main__":
    unittest.main()
