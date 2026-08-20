import asyncio
import importlib
import unittest
from types import SimpleNamespace


class _DummyLock:
    def __init__(self):
        self._locked = False

    async def acquire(self):
        self._locked = True
        return True

    def release(self):
        self._locked = False

    def locked(self):
        return self._locked


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
        self.timeout_sec = 30.0

    async def wait_for_new_message(self, sender_id, chat_id=""):
        self.calls.append(sender_id)
        return True


class _FakeKernel:
    DEFAULT_FOLLOWUP_COOLDOWN_SEC = 8.0

    def __init__(self):
        self.wait_target_syncs = []
        self.group_waits = []
        self.private_waits = []
        self.cooldowns = []
        self.expired_waits = []

    async def sync_runtime_wait_targets(self, chat_id, targets, target_name):
        self.wait_target_syncs.append((chat_id, list(targets), target_name))

    async def arm_group_wait(self, chat_id, payload):
        self.group_waits.append((chat_id, dict(payload or {})))

    async def arm_private_wait(self, chat_id, payload):
        self.private_waits.append((chat_id, dict(payload or {})))

    async def set_cooldown(self, chat_id, action, until_ts, reason=""):
        self.cooldowns.append((chat_id, action, reason))

    async def expire_wait(self, chat_id, reason):
        self.expired_waits.append((chat_id, reason))


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
    def test_runner_preserves_cancellation_while_waiting_for_chat_lock(self):
        runner_mod = importlib.import_module("astrmai.conversation.execution.system2_runner")

        class _BlockingLock:
            def __init__(self):
                self.started = asyncio.Event()
                self._locked = False

            async def acquire(self):
                self.started.set()
                await asyncio.Event().wait()

            def release(self):
                self._locked = False

            def locked(self):
                return self._locked

        lock = _BlockingLock()
        runtime = SimpleNamespace(
            runtime_coordinator=SimpleNamespace(get_sys2_lock=lambda chat_id: asyncio.sleep(0, result=lock)),
        )
        runner = runner_mod.System2Runner(runtime)
        event = _FakeEvent()

        async def _run():
            task = asyncio.create_task(runner.run(event))
            await lock.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(_run())
        stages = event.get_extra("astrmai_stage_ledger", [])
        self.assertEqual(stages[-1]["status"], "cancelled")

    def test_runner_times_out_while_waiting_for_chat_lock(self):
        runner_mod = importlib.import_module("astrmai.conversation.execution.system2_runner")
        lock = asyncio.Lock()
        runtime = SimpleNamespace(
            runtime_coordinator=SimpleNamespace(get_sys2_lock=lambda chat_id: asyncio.sleep(0, result=lock)),
            config=SimpleNamespace(timing=SimpleNamespace(sys2_lock_wait_timeout_sec=0.1)),
        )
        runner = runner_mod.System2Runner(runtime)
        event = _FakeEvent()

        async def _run():
            await lock.acquire()
            try:
                return await runner.run(event)
            finally:
                lock.release()

        self.assertFalse(asyncio.run(_run()))
        self.assertEqual(event.get_extra("astrmai_execution_status"), "queue_timeout")
        self.assertEqual(event.get_extra("astrmai_queue_timeout_stage"), "system2.chat_lock_wait")
        stages = event.get_extra("astrmai_stage_ledger", [])
        self.assertEqual(stages[-1]["status"], "timeout")

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
                "chat_loop_kernel": _FakeKernel(),
                "config": SimpleNamespace(attention=SimpleNamespace(thread_same_speaker_followup_sec=8)),
            },
        )()
        runner = runner_mod.System2Runner(runtime)
        event = _FakeEvent()
        event.set_extra("astrmai_turn_thread_id", "thread-a")

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
        self.assertEqual(
            runtime.chat_loop_kernel.wait_target_syncs,
            [(event.unified_msg_origin, ["user-2"], "Bob")],
        )
        self.assertEqual(runtime.chat_loop_kernel.cooldowns[0][1:], ("followup", "followup_dispatch"))
        self.assertEqual(runtime.group_reply_wait_manager.events, [event])
        stages = event.get_extra("astrmai_stage_ledger", [])
        self.assertEqual(stages[-2]["stage"], "system2.chat_lock_wait")
        self.assertEqual(stages[-2]["status"], "success")
        self.assertEqual(stages[-2]["metadata"]["thread_id"], "thread-a")
        self.assertEqual(stages[-2]["metadata"]["lock_scope"], "chat")
        self.assertEqual(stages[-1]["stage"], "system2.lane_prepare")
        self.assertEqual(stages[-1]["status"], "success")

    def test_runner_times_out_while_preparing_system2_lane(self):
        runner_mod = importlib.import_module("astrmai.conversation.execution.system2_runner")

        class _BlockingLaneManager:
            async def ensure_lane(self, lane_key, base_origin):
                await asyncio.Event().wait()

        runtime = type(
            "Runtime",
            (),
            {
                "runtime_coordinator": _FakeCoordinator(),
                "state_engine": _FakeStateEngine(),
                "lane_manager": _BlockingLaneManager(),
                "system2_planner": _FakePlanner(),
                "config": SimpleNamespace(
                    timing=SimpleNamespace(lane_prepare_timeout_sec=0.1),
                ),
            },
        )()
        runner = runner_mod.System2Runner(runtime)
        event = _FakeEvent()

        self.assertFalse(asyncio.run(runner.run(event)))
        self.assertEqual(event.get_extra("astrmai_execution_status"), "queue_timeout")
        self.assertEqual(event.get_extra("astrmai_queue_timeout_stage"), "system2.lane_prepare")
        stages = event.get_extra("astrmai_stage_ledger", [])
        self.assertEqual(stages[-1]["stage"], "system2.lane_prepare")
        self.assertEqual(stages[-1]["status"], "timeout")

    def test_runner_private_followup_does_not_block_and_expires_wait_in_background(self):
        runner_mod = importlib.import_module("astrmai.conversation.execution.system2_runner")

        class _PrivatePlanner(_FakePlanner):
            async def plan_and_execute(self, event, queue_events):
                await super().plan_and_execute(event, queue_events)
                event.set_extra("is_private_chat", True)
                event.set_extra("astrmai_wait_targets", [])
                event.set_extra("astrmai_wait_target_name", "")

        class _BlockingPrivateChatManager(_FakePrivateChatManager):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def wait_for_new_message(self, sender_id, chat_id=""):
                self.calls.append((sender_id, chat_id))
                self.started.set()
                await self.release.wait()
                return False

        class _PrivateEvent(_FakeEvent):
            def get_group_id(self):
                return ""

            def get_sender_name(self):
                return "Alice"

        private_manager = _BlockingPrivateChatManager()
        kernel = _FakeKernel()
        runtime = type(
            "Runtime",
            (),
            {
                "runtime_coordinator": _FakeCoordinator(),
                "state_engine": _FakeStateEngine(),
                "lane_manager": _FakeLaneManager(),
                "system2_planner": _PrivatePlanner(),
                "private_chat_manager": private_manager,
                "group_reply_wait_manager": None,
                "chat_loop_kernel": kernel,
                "config": SimpleNamespace(attention=SimpleNamespace(thread_same_speaker_followup_sec=8)),
            },
        )()
        runner = runner_mod.System2Runner(runtime)
        event = _PrivateEvent()

        async def _run():
            reply_sent = await runner.run(event)
            await private_manager.started.wait()
            before_release = list(kernel.expired_waits)
            private_manager.release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return reply_sent, before_release, list(kernel.expired_waits)

        reply_sent, before_release, after_release = asyncio.run(_run())

        self.assertTrue(reply_sent)
        self.assertEqual(before_release, [])
        self.assertEqual(kernel.private_waits[0][0], event.unified_msg_origin)
        self.assertEqual(after_release, [(event.unified_msg_origin, "private_wait_timeout")])

    def test_private_followup_reschedule_keeps_new_task_registered(self):
        followup_mod = importlib.import_module("astrmai.conversation.execution.followup_manager")

        class _ReschedulablePrivateChatManager(_FakePrivateChatManager):
            def __init__(self):
                super().__init__()
                self.first_started = asyncio.Event()
                self.second_started = asyncio.Event()
                self.first_release = asyncio.Event()
                self.second_release = asyncio.Event()

            async def wait_for_new_message(self, sender_id, chat_id=""):
                call_index = len(self.calls)
                self.calls.append((sender_id, chat_id))
                if call_index == 0:
                    self.first_started.set()
                    await self.first_release.wait()
                    return True
                self.second_started.set()
                await self.second_release.wait()
                return False

        class _PrivateEvent(_FakeEvent):
            def __init__(self):
                super().__init__()
                self._extra["is_private_chat"] = True

            def get_group_id(self):
                return ""

            def get_sender_name(self):
                return "Alice"

        async def _run():
            private_manager = _ReschedulablePrivateChatManager()
            kernel = _FakeKernel()
            runtime = SimpleNamespace(
                runtime_coordinator=_FakeCoordinator(),
                private_chat_manager=private_manager,
                group_reply_wait_manager=None,
                chat_loop_kernel=kernel,
                config=SimpleNamespace(attention=SimpleNamespace(thread_same_speaker_followup_sec=8)),
            )
            manager = followup_mod.FollowupManager(runtime)
            event = _PrivateEvent()

            await manager.finalize_after_reply(event.unified_msg_origin, event, True)
            await private_manager.first_started.wait()
            first_task = manager._private_wait_tasks[event.unified_msg_origin]

            await manager.finalize_after_reply(event.unified_msg_origin, event, True)
            await private_manager.second_started.wait()
            second_task = manager._private_wait_tasks[event.unified_msg_origin]

            private_manager.first_release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            current_task = manager._private_wait_tasks.get(event.unified_msg_origin)

            private_manager.second_release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return first_task, second_task, current_task

        first_task, second_task, current_task = asyncio.run(_run())

        self.assertIsNot(first_task, second_task)
        self.assertIs(current_task, second_task)


if __name__ == "__main__":
    unittest.main()
