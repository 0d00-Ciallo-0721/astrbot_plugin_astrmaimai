import asyncio
import threading
import unittest

from astrmai.state.group_wait.group_reply_wait_manager import GroupReplyWaitManager


class _FakeEvent:
    def __init__(self, chat_id="default:GroupMessage:group-1", sender_id="user-1", sender_name="Alice"):
        self.unified_msg_origin = chat_id
        self.message_str = ""
        self.message_obj = None
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = {}

    def get_group_id(self):
        return self.unified_msg_origin.split(":")[-1]

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _BlockingTask:
    def __init__(self, entered: threading.Event, release: threading.Event):
        self._entered = entered
        self._release = release

    def done(self):
        return False

    def cancel(self):
        self._entered.set()
        self._release.wait(timeout=2)


class GroupReplyWaitManagerConcurrencyMigratedTests(unittest.TestCase):
    def test_concurrent_reregistration_keeps_latest_wait_state(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=5)
        chat_id = "default:GroupMessage:group-1"
        entered = threading.Event()
        release = threading.Event()
        manager._states[chat_id] = object()
        manager._timeout_tasks[chat_id] = _BlockingTask(entered, release)
        results = {}

        def _register(name: str, sender_id: str, sender_name: str):
            event = _FakeEvent(chat_id=chat_id, sender_id=sender_id, sender_name=sender_name)
            event.set_extra("astrmai_group_direct_wakeup", True)
            results[name] = manager.register_from_reply_event(event)

        first_thread = threading.Thread(target=_register, args=("first", "user-a", "First"))
        first_thread.start()
        self.assertTrue(entered.wait(timeout=2), "first registration did not block on old timeout cancellation")

        second_thread = threading.Thread(target=_register, args=("second", "user-b", "Second"))
        second_thread.start()
        second_thread.join(timeout=2)
        self.assertFalse(second_thread.is_alive(), "second registration should finish while first is blocked")

        release.set()
        first_thread.join(timeout=2)
        self.assertFalse(first_thread.is_alive(), "first registration should finish after release")

        self.assertEqual(results, {"first": True, "second": True})
        info = manager.get_wait_info(chat_id)
        self.assertIsNotNone(info)
        self.assertEqual(info["target_user_id"], "user-b")
        self.assertEqual(info["target_name"], "Second")

    def test_reregistered_wait_survives_old_timeout_task(self):
        async def _run():
            manager = GroupReplyWaitManager(timeout_sec=1.0, message_budget=5)
            first = _FakeEvent(sender_id="user-a", sender_name="First")
            first.set_extra("astrmai_group_direct_wakeup", True)
            self.assertTrue(manager.register_from_reply_event(first))

            await asyncio.sleep(0.4)

            second = _FakeEvent(sender_id="user-b", sender_name="Second")
            second.set_extra("astrmai_group_direct_wakeup", True)
            self.assertTrue(manager.register_from_reply_event(second))

            await asyncio.sleep(0.75)

            info = manager.get_wait_info("default:GroupMessage:group-1")
            self.assertIsNotNone(info)
            self.assertEqual(info["target_user_id"], "user-b")
            self.assertEqual(info["target_name"], "Second")

            self.assertTrue(manager.cancel_wait("default:GroupMessage:group-1", reason="cleanup"))
            await asyncio.sleep(0)
            self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1"))

        asyncio.run(_run())
