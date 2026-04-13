import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.test_persistence_regressions import _install_astrbot_stubs


def _install_attention_stubs():
    state_mod = types.ModuleType("astrmai.Heart.state_engine")
    state_mod.StateEngine = type("StateEngine", (), {})
    sys.modules["astrmai.Heart.state_engine"] = state_mod

    judge_mod = types.ModuleType("astrmai.Heart.judge")
    judge_mod.Judge = type("Judge", (), {})
    sys.modules["astrmai.Heart.judge"] = judge_mod

    sensors_mod = types.ModuleType("astrmai.Heart.sensors")
    sensors_mod.PreFilters = type("PreFilters", (), {})
    sys.modules["astrmai.Heart.sensors"] = sensors_mod

    message_components_mod = types.ModuleType("astrbot.api.message_components")
    for name in ["Image", "Plain", "At", "Face"]:
        setattr(message_components_mod, name, type(name, (), {}))
    sys.modules["astrbot.api.message_components"] = message_components_mod


class _FakeSensors:
    def is_wakeup_signal(self, event, self_id):
        return False

    async def is_command(self, msg_str):
        return False

    async def should_process_message(self, event):
        return False


class _FakeEvent:
    def __init__(self):
        self.message_str = "hello"
        self.unified_msg_origin = "default:FriendMessage:user-1"
        self.message_obj = None
        self.timestamp = 123
        self._extra = {}

    def get_group_id(self):
        return None

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


class _FakePrivateChatManager:
    def __init__(self):
        self.calls = []

    async def signal_new_message(self, user_id, message_str):
        self.calls.append((user_id, message_str))


class _FakeRuntimeCoordinator:
    def __init__(self):
        self.calls = []

    async def mark_activity(self, chat_id, timestamp, sender_id, sender_name, preview, thread_signature=None):
        self.calls.append((chat_id, timestamp, sender_id, sender_name, preview, thread_signature))


class _FakeGroupWakeupEvent(_FakeEvent):
    def __init__(self):
        super().__init__()
        self.message_str = "抱抱"
        self.unified_msg_origin = "default:GroupMessage:group-1"

    def get_group_id(self):
        return "group-1"


class AttentionPrivateChatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_attention_stubs()
        sys.modules.pop("astrmai.Heart.attention", None)
        self.attention_mod = importlib.import_module("astrmai.Heart.attention")
        self.attention_mod = importlib.reload(self.attention_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_constructor_stores_private_chat_manager(self):
        config = SimpleNamespace(
            attention=SimpleNamespace(max_message_length=100),
            system1=SimpleNamespace(wakeup_words=[]),
        )
        manager = _FakePrivateChatManager()
        gate = self.attention_mod.AttentionGate(
            state_engine=SimpleNamespace(config=config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(),
            system2_callback=None,
            private_chat_manager=manager,
        )

        self.assertIs(gate.private_chat_manager, manager)

    def test_private_chat_message_signals_wait_manager(self):
        config = SimpleNamespace(
            attention=SimpleNamespace(max_message_length=100),
            system1=SimpleNamespace(wakeup_words=[]),
        )
        manager = _FakePrivateChatManager()
        gate = self.attention_mod.AttentionGate(
            state_engine=SimpleNamespace(config=config),
            judge=SimpleNamespace(),
            sensors=_FakeSensors(),
            system2_callback=None,
            private_chat_manager=manager,
        )
        self.attention_mod.AttentionGate._global_msg_cache = []

        async def _run():
            result = await gate.process_event(_FakeEvent())
            await asyncio.sleep(0)
            return result

        result = asyncio.run(_run())

        self.assertEqual(result, "IGNORE")
        self.assertEqual(manager.calls, [("user-1", "hello")])

    def test_fast_wakeup_path_marks_runtime_activity(self):
        config = SimpleNamespace(
            attention=SimpleNamespace(max_message_length=100),
            system1=SimpleNamespace(wakeup_words=[]),
        )
        runtime_coordinator = _FakeRuntimeCoordinator()
        sys2_calls = []

        async def _sys2_callback(event, events):
            sys2_calls.append((event, events))

        gate = self.attention_mod.AttentionGate(
            state_engine=SimpleNamespace(config=config),
            judge=SimpleNamespace(),
            sensors=SimpleNamespace(
                is_wakeup_signal=lambda event, self_id: True,
                is_command=lambda msg_str: asyncio.sleep(0, result=False),
                should_process_message=lambda event: asyncio.sleep(0, result=False),
            ),
            system2_callback=_sys2_callback,
            runtime_coordinator=runtime_coordinator,
        )
        self.attention_mod.AttentionGate._global_msg_cache = []

        async def _run():
            event = _FakeGroupWakeupEvent()
            result = await gate.process_event(event)
            await asyncio.sleep(0)
            return result, event

        result, event = asyncio.run(_run())

        self.assertEqual(result, "ENGAGED")
        self.assertGreater(event.get_extra("astrmai_timestamp", 0.0), 0.0)
        self.assertEqual(len(runtime_coordinator.calls), 1)
        chat_id, timestamp, sender_id, sender_name, preview, thread_signature = runtime_coordinator.calls[0]
        self.assertEqual(chat_id, "default:GroupMessage:group-1")
        self.assertEqual(sender_id, "user-1")
        self.assertEqual(sender_name, "Alice")
        self.assertEqual(preview, "抱抱")
        self.assertIsNone(thread_signature)
        self.assertEqual(timestamp, event.get_extra("astrmai_timestamp"))
        self.assertEqual(len(sys2_calls), 1)


if __name__ == "__main__":
    unittest.main()
