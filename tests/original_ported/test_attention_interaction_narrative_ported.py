import asyncio
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
    def __init__(self):
        self.message_str = "(Interaction: 哈基亚(3874287208) -> 恸(516779421))"
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_obj = SimpleNamespace(message=[], message_id="poke-1")
        self.timestamp = 123.0
        self._extra = {
            "is_virtual_poke": True,
            "astrmai_interaction_kind": "poke",
            "astrmai_interaction_actor_id": "3874287208",
            "astrmai_interaction_actor_name": "哈基亚",
            "astrmai_interaction_target_id": "516779421",
            "astrmai_interaction_target_name": "恸",
            "astrmai_interaction_target_is_bot": False,
        }

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return "3874287208"

    def get_sender_name(self):
        return "3874287208"

    def get_self_id(self):
        return "3927550965"

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class AttentionInteractionNarrativeTests(unittest.TestCase):
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

    def test_poke_narrative_uses_explicit_actor_and_target_labels(self):
        filtered = asyncio.run(self.gate._format_and_filter_messages([_FakeEvent()]))

        self.assertEqual(len(filtered), 1)
        event = filtered[0]
        self.assertEqual(event.get_extra("astrmai_interaction_kind"), "poke")
        self.assertEqual(event.get_extra("astrmai_interaction_actor_id"), "3874287208")
        self.assertEqual(event.get_extra("astrmai_interaction_target_id"), "516779421")


if __name__ == "__main__":
    unittest.main()
