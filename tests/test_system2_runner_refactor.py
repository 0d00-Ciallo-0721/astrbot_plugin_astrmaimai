import asyncio
import importlib
import unittest


class _DummyLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeCoordinator:
    def __init__(self):
        self.wait_updates = []

    async def get_sys2_lock(self, chat_id):
        return _DummyLock()

    async def update_wait_targets(self, chat_id, targets, target_name):
        self.wait_updates.append((chat_id, targets, target_name))


class _FakeStateEngine:
    def __init__(self):
        self.calls = []

    async def consume_energy(self, chat_id):
        self.calls.append(chat_id)


class _FakeLaneManager:
    def __init__(self):
        self.calls = []

    async def ensure_lane(self, lane_key, base_origin):
        self.calls.append((lane_key.subsystem, lane_key.task_family, lane_key.scope_id, base_origin))


class _FakePlanner:
    def __init__(self):
        self.calls = []

    async def plan_and_execute(self, event, queue_events):
        self.calls.append((event, list(queue_events)))
        event.set_extra("astrmai_reply_sent", True)
        event.set_extra("astrmai_wait_targets", ["user-2"])
        event.set_extra("astrmai_wait_target_name", "Bob")


class _FakeGroupReplyWaitManager:
    def __init__(self):
        self.events = []

    def register_from_reply_event(self, event):
        self.events.append(event)


class _FakePrivateChatManager:
    def __init__(self):
        self.calls = []

    async def wait_for_new_message(self, sender_id):
        self.calls.append(sender_id)
        return True


class _FakeEvent:
    def __init__(self):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._extra = {}

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def get_sender_id(self):
        return "user-1"

    def get_group_id(self):
        return "group-1"


class RefactoredSystem2RunnerTests(unittest.TestCase):
    def test_runner_handles_queue_energy_wait_targets_and_group_followup(self):
        runner_mod = importlib.import_module("astrmai.conversation.execution.system2_runner")
        runtime = type(
            "Runtime",
            (),
            {
                "runtime_coordinator": _FakeCoordinator(),
                "state_engine": _FakeStateEngine(),
                "lane_manager": _FakeLaneManager(),
                "system2_planner": _FakePlanner(),
                "private_chat_manager": _FakePrivateChatManager(),
                "group_reply_wait_manager": _FakeGroupReplyWaitManager(),
            },
        )()
        runner = runner_mod.System2Runner(runtime)
        event = _FakeEvent()

        reply_sent = asyncio.run(runner.run(event))

        self.assertTrue(reply_sent)
        self.assertEqual(runtime.state_engine.calls, [event.unified_msg_origin])
        self.assertEqual(
            runtime.lane_manager.calls,
            [("sys2", "dialog", event.unified_msg_origin, event.unified_msg_origin)],
        )
        self.assertEqual(
            runtime.runtime_coordinator.wait_updates,
            [(event.unified_msg_origin, ["user-2"], "Bob")],
        )
        self.assertEqual(runtime.group_reply_wait_manager.events, [event])


if __name__ == "__main__":
    unittest.main()
