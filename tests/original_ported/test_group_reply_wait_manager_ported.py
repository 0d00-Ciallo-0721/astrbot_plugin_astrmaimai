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


class GroupReplyWaitManagerPortedTests(unittest.TestCase):
    def test_register_direct_wakeup_and_resume_on_target_message(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=5)
        reply_event = _FakeEvent(sender_id="user-42", sender_name="Target")
        reply_event.set_extra("astrmai_group_direct_wakeup", True)

        self.assertTrue(manager.register_from_reply_event(reply_event))

        resumed_event = _FakeEvent(sender_id="user-42", sender_name="Target")
        result = manager.handle_incoming_message(resumed_event)

        self.assertEqual(result, "RESUME")
        self.assertTrue(resumed_event.get_extra("astrmai_force_engage"))
        self.assertEqual(resumed_event.get_extra("astrmai_group_wait_target_id"), "user-42")
        self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1"))

    def test_non_target_messages_consume_message_budget(self):
        manager = GroupReplyWaitManager(timeout_sec=30, message_budget=2)
        reply_event = _FakeEvent(sender_id="user-42", sender_name="Target")
        reply_event.set_extra("astrmai_group_direct_wakeup", True)
        manager.register_from_reply_event(reply_event)

        result1 = manager.handle_incoming_message(_FakeEvent(sender_id="user-2", sender_name="Other-1"))
        result2 = manager.handle_incoming_message(_FakeEvent(sender_id="user-3", sender_name="Other-2"))

        self.assertEqual(result1, "OBSERVED")
        self.assertEqual(result2, "EXPIRED")
        self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1"))


if __name__ == "__main__":
    unittest.main()
