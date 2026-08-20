import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Coordinator:
    def __init__(self):
        self.calls = []

    async def advance_generation(self, chat_id, thread_id):
        self.calls.append((chat_id, thread_id))
        return len(self.calls)


class _Event:
    def __init__(
        self,
        *,
        chat_id="default:GroupMessage:group-1",
        extras=None,
        group_id="group-1",
        sender_id="user-1",
        sender_name="Alice",
    ):
        self.unified_msg_origin = chat_id
        self.message_obj = SimpleNamespace(message=[], message_id="msg-1")
        self._extra = dict(extras or {})
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name


class PluginFacadeTurnPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.app.plugin_facade", None)
        self.facade_mod = importlib.import_module("astrmai.app.plugin_facade")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _facade(self, coordinator, *, group_thread_wait_enabled=True, generation_enabled=True):
        facade = self.facade_mod.PluginFacade.__new__(self.facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(
            runtime_coordinator=coordinator,
            config=SimpleNamespace(
                conversation=SimpleNamespace(
                    conversation_generation_enabled=generation_enabled,
                    reply_send_claim_enabled=True,
                    group_thread_wait_enabled=group_thread_wait_enabled,
                    non_conversational_guard_enabled=True,
                    conversation_concurrency_debug_trace_enabled=False,
                )
            ),
        )
        return facade

    def test_system2_entry_rebuilds_missing_runner_and_uses_bounded_implementation(self):
        calls = []

        class _Runner:
            async def run(self, event, events):
                calls.append((event, events))
                return "done"

        runner = _Runner()
        facade = self.facade_mod.PluginFacade.__new__(self.facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(system2_runner=None)
        event = _Event()

        with patch(
            "astrmai.conversation.execution.system2_runner.System2Runner",
            return_value=runner,
        ):
            result = asyncio.run(facade._system2_entry(event, [event]))

        self.assertEqual(result, "done")
        self.assertIs(facade.runtime.system2_runner, runner)
        self.assertEqual(calls, [(event, [event])])

    def test_group_turn_uses_thread_signature(self):
        coordinator = _Coordinator()
        facade = self._facade(coordinator)
        event = _Event(extras={"astrmai_thread_signature": "thread-a"})
        scope = SimpleNamespace(chat_id=event.unified_msg_origin, is_private_chat=False, sender_id="user-1")

        asyncio.run(facade.prepare_conversation_turn(event, scope))

        turn = event.get_extra("astrmai_turn_identity")
        self.assertEqual(turn.thread_id, "thread-a")
        self.assertEqual(turn.generation, 1)
        self.assertEqual(event.get_extra("astrmai_group_thread_source"), "thread_signature")
        self.assertEqual(coordinator.calls, [(event.unified_msg_origin, "thread-a")])

    def test_private_turn_keeps_private_thread_id(self):
        coordinator = _Coordinator()
        facade = self._facade(coordinator)
        event = _Event(chat_id="default:FriendMessage:user-1", group_id="")
        scope = SimpleNamespace(chat_id=event.unified_msg_origin, is_private_chat=True, sender_id="user-1")

        asyncio.run(facade.prepare_conversation_turn(event, scope))

        turn = event.get_extra("astrmai_turn_identity")
        self.assertEqual(turn.thread_id, "private:default:FriendMessage:user-1")
        self.assertEqual(coordinator.calls, [("default:FriendMessage:user-1", "private:default:FriendMessage:user-1")])

    def test_group_thread_gray_switch_off_falls_back_to_chat_scope(self):
        coordinator = _Coordinator()
        facade = self._facade(coordinator, group_thread_wait_enabled=False)
        event = _Event(extras={"astrmai_thread_signature": "thread-a"})
        scope = SimpleNamespace(chat_id=event.unified_msg_origin, is_private_chat=False, sender_id="user-1")

        asyncio.run(facade.prepare_conversation_turn(event, scope))

        turn = event.get_extra("astrmai_turn_identity")
        self.assertEqual(turn.thread_id, event.unified_msg_origin)
        self.assertEqual(event.get_extra("astrmai_group_thread_source"), "gray_switch_disabled")

    def test_group_wait_info_uses_thread_scope_when_thread_wait_is_enabled(self):
        class _Manager:
            def __init__(self):
                self.calls = []

            def handle_incoming_message(self, _event):
                return "OBSERVED"

            def get_wait_info(self, chat_id, thread_id=""):
                self.calls.append((chat_id, thread_id))
                return None

        class _Kernel:
            async def record_concurrency_event(self, _name):
                return None

        manager = _Manager()
        facade = self._facade(_Coordinator(), group_thread_wait_enabled=True)
        facade.runtime.group_reply_wait_manager = manager
        facade.runtime.chat_loop_kernel = _Kernel()
        event = _Event(extras={"astrmai_turn_thread_id": "thread-a"})
        scope = SimpleNamespace(
            chat_id=event.unified_msg_origin,
            is_private_chat=False,
            sender_id="user-1",
        )

        asyncio.run(facade.handle_group_reply_wait(event, scope))

        self.assertEqual(manager.calls, [(event.unified_msg_origin, "thread-a")])

    def test_group_wait_info_uses_chat_scope_when_thread_wait_is_disabled(self):
        class _Manager:
            def __init__(self):
                self.calls = []

            def handle_incoming_message(self, _event):
                return "OBSERVED"

            def get_wait_info(self, chat_id, thread_id=""):
                self.calls.append((chat_id, thread_id))
                return None

        class _Kernel:
            async def record_concurrency_event(self, _name):
                return None

        manager = _Manager()
        facade = self._facade(_Coordinator(), group_thread_wait_enabled=False)
        facade.runtime.group_reply_wait_manager = manager
        facade.runtime.chat_loop_kernel = _Kernel()
        event = _Event(extras={"astrmai_turn_thread_id": "thread-a"})
        scope = SimpleNamespace(
            chat_id=event.unified_msg_origin,
            is_private_chat=False,
            sender_id="user-1",
        )

        asyncio.run(facade.handle_group_reply_wait(event, scope))

        self.assertEqual(manager.calls, [(event.unified_msg_origin, "")])

    def test_generation_gray_switch_off_preserves_legacy_event_without_turn(self):
        coordinator = _Coordinator()
        facade = self._facade(coordinator, generation_enabled=False)
        event = _Event()
        scope = SimpleNamespace(chat_id=event.unified_msg_origin, is_private_chat=False, sender_id="user-1")

        asyncio.run(facade.prepare_conversation_turn(event, scope))

        self.assertIsNone(event.get_extra("astrmai_turn_identity"))
        self.assertEqual(coordinator.calls, [])

    def test_flush_deferred_turn_trace_uses_latest_pending_snapshot(self):
        recorded = []

        class _Planner:
            async def record_turn_trace(self, chat_id, event, *, status, reply_text=None):
                recorded.append((chat_id, status, reply_text, event.get_extra("astrmai_defer_turn_trace_persist")))

        facade = self.facade_mod.PluginFacade.__new__(self.facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(system2_planner=_Planner())
        event = _Event(
            extras={
                "astrmai_defer_turn_trace_persist": True,
                "astrmai_deferred_turn_trace": {
                    "chat_id": "chat-final",
                    "status": "executed",
                    "reply_text": "reply",
                },
            }
        )

        flushed = asyncio.run(facade.flush_deferred_turn_trace(event, fallback_status="fallback"))

        self.assertTrue(flushed)
        self.assertEqual(recorded, [("chat-final", "executed", "reply", False)])
        self.assertFalse(event.get_extra("astrmai_defer_turn_trace_persist"))
        self.assertIsNone(event.get_extra("astrmai_deferred_turn_trace"))

    def test_group_wait_interrupt_cancels_only_current_turn_thread(self):
        from astrmai.state.group_wait.group_reply_wait_manager import GroupReplyWaitManager

        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=3, threaded_enabled=True)
        first = _Event(extras={
            "astrmai_turn_thread_id": "thread-a",
            "astrmai_group_direct_wakeup": True,
        })
        first.set_extra("astrmai_wait_targets", ["user-a"])
        second = _Event(extras={
            "astrmai_turn_thread_id": "thread-b",
            "astrmai_group_direct_wakeup": True,
        })
        second.set_extra("astrmai_wait_targets", ["user-b"])
        self.assertTrue(manager.register_from_reply_event(first))
        self.assertTrue(manager.register_from_reply_event(second))

        facade = self.facade_mod.PluginFacade.__new__(self.facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(group_reply_wait_manager=manager)
        interrupted = _Event(extras={"astrmai_turn_thread_id": "thread-a"})

        facade.cancel_group_wait_if_interrupted(interrupted, "OBSERVED", "ENGAGED")

        self.assertIsNone(manager.get_wait_info(interrupted.unified_msg_origin, thread_id="thread-a"))
        self.assertIsNotNone(manager.get_wait_info(interrupted.unified_msg_origin, thread_id="thread-b"))


if __name__ == "__main__":
    unittest.main()
