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


def _thread_event(thread_signature: str, *, sender_id: str = "user-1", sender_name: str = "Alice") -> _FakeEvent:
    event = _FakeEvent(sender_id=sender_id, sender_name=sender_name)
    event.set_extra("astrmai_group_direct_wakeup", True)
    event.set_extra("astrmai_thread_signature", thread_signature)
    return event


def _turn_thread_event(turn_thread_id: str, *, sender_id: str = "user-1", sender_name: str = "Alice") -> _FakeEvent:
    event = _FakeEvent(sender_id=sender_id, sender_name=sender_name)
    event.set_extra("astrmai_group_direct_wakeup", True)
    event.set_extra("astrmai_turn_thread_id", turn_thread_id)
    return event


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

    def test_same_group_waits_are_isolated_by_thread_signature(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=5, threaded_enabled=True)
        first = _thread_event("thread-a", sender_id="user-a", sender_name="Alpha")
        second = _thread_event("thread-b", sender_id="user-b", sender_name="Beta")

        self.assertTrue(manager.register_from_reply_event(first))
        self.assertTrue(manager.register_from_reply_event(second))

        first_info = manager.get_wait_info("default:GroupMessage:group-1", thread_id="thread-a")
        second_info = manager.get_wait_info("default:GroupMessage:group-1", thread_id="thread-b")
        default_info = manager.get_wait_info("default:GroupMessage:group-1")

        self.assertEqual(first_info["target_user_id"], "user-a")
        self.assertEqual(second_info["target_user_id"], "user-b")
        self.assertEqual(first_info["thread_id"], "thread-a")
        self.assertEqual(second_info["thread_id"], "thread-b")
        self.assertIn(default_info["target_user_id"], {"user-a", "user-b"})

    def test_target_plain_message_on_new_topic_does_not_resume_thread_wait(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=3, threaded_enabled=True)
        wait_event = _thread_event("thread-a", sender_id="user-a", sender_name="Alpha")
        self.assertTrue(manager.register_from_reply_event(wait_event))

        new_topic = _FakeEvent(sender_id="user-a", sender_name="Alpha")
        new_topic.set_extra("astrmai_thread_signature", "thread-b")

        result = manager.handle_incoming_message(new_topic)

        self.assertEqual(result, "NONE")
        self.assertFalse(new_topic.get_extra("astrmai_group_wait_resume", False))
        self.assertIsNotNone(manager.get_wait_info("default:GroupMessage:group-1", thread_id="thread-a"))

    def test_turn_thread_id_is_used_for_wait_registration_and_resume(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=3, threaded_enabled=True)
        wait_event = _turn_thread_event("reply:msg-1", sender_id="user-a", sender_name="Alpha")

        self.assertTrue(manager.register_from_reply_event(wait_event))
        self.assertIsNotNone(manager.get_wait_info("default:GroupMessage:group-1", thread_id="reply:msg-1"))

        unrelated = _FakeEvent(sender_id="user-a", sender_name="Alpha")
        unrelated.set_extra("astrmai_turn_thread_id", "reply:msg-2")
        self.assertEqual(manager.handle_incoming_message(unrelated), "NONE")
        self.assertIsNotNone(manager.get_wait_info("default:GroupMessage:group-1", thread_id="reply:msg-1"))

        plain_same_thread = _FakeEvent(sender_id="user-a", sender_name="Alpha")
        plain_same_thread.set_extra("astrmai_turn_thread_id", "reply:msg-1")
        self.assertEqual(manager.handle_incoming_message(plain_same_thread), "RESUME")
        self.assertTrue(plain_same_thread.get_extra("astrmai_group_wait_resume", False))
        self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1", thread_id="reply:msg-1"))

    def test_turn_thread_id_takes_priority_over_late_thread_signature(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=3, threaded_enabled=True)
        wait_event = _turn_thread_event("sender:user-a", sender_id="user-a", sender_name="Alpha")
        wait_event.set_extra("astrmai_thread_signature", "late-focus-signature")

        self.assertTrue(manager.register_from_reply_event(wait_event))

        self.assertIsNotNone(
            manager.get_wait_info("default:GroupMessage:group-1", thread_id="sender:user-a")
        )
        self.assertIsNone(
            manager.get_wait_info("default:GroupMessage:group-1", thread_id="late-focus-signature")
        )

    def test_unique_target_plain_reply_resumes_wait_without_explicit_thread(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=3, threaded_enabled=True)
        wait_event = _turn_thread_event("reply:msg-1", sender_id="user-a", sender_name="Alpha")
        self.assertTrue(manager.register_from_reply_event(wait_event))

        reply = _FakeEvent(sender_id="user-a", sender_name="Alpha")

        self.assertEqual(manager.handle_incoming_message(reply), "RESUME")
        self.assertTrue(reply.get_extra("astrmai_group_wait_resume", False))

    def test_threaded_waits_are_capped_per_chat(self):
        manager = GroupReplyWaitManager(
            timeout_sec=30,
            message_budget=3,
            threaded_enabled=True,
            max_active_waits_per_chat=3,
        )

        for index in range(5):
            self.assertTrue(
                manager.register_from_reply_event(
                    _thread_event(f"thread-{index}", sender_id=f"user-{index}")
                )
            )

        waits = manager.list_waits("default:GroupMessage:group-1")
        self.assertEqual([item["thread_id"] for item in waits], ["thread-2", "thread-3", "thread-4"])
