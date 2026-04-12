import importlib
import sys
import tempfile
import unittest

from tests.test_group_reply_wait_manager import _FakeEvent
from tests.test_persistence_regressions import _install_astrbot_stubs


class _ResumeFakeEvent(_FakeEvent):
    def __init__(self, chat_id="default:GroupMessage:group-1", sender_id="user-1", sender_name="Alice", message_str=""):
        super().__init__(chat_id=chat_id, sender_id=sender_id, sender_name=sender_name)
        self.message_str = message_str
        self.message_obj = None


class GroupWaitThreadSignatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.Heart.group_reply_wait_manager", None)
        self.manager_mod = importlib.import_module("astrmai.Heart.group_reply_wait_manager")
        self.manager_mod = importlib.reload(self.manager_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_target_message_without_thread_resume_signal_keeps_waiting(self):
        manager = self.manager_mod.GroupReplyWaitManager(timeout_sec=30, message_budget=3)
        reply_event = _ResumeFakeEvent(sender_id="user-42", sender_name="Target")
        reply_event.set_extra("astrmai_group_direct_wakeup", True)
        reply_event.set_extra("astrmai_thread_signature", "thread-1")
        reply_event.set_extra("astrmai_reply_mode", "casual_followup")
        self.assertTrue(manager.register_from_reply_event(reply_event))

        unrelated_followup = _ResumeFakeEvent(sender_id="user-42", sender_name="Target", message_str="换话题了")
        result = manager.handle_incoming_message(unrelated_followup)

        self.assertEqual(result, "OBSERVED")
        info = manager.get_wait_info("default:GroupMessage:group-1")
        self.assertIsNotNone(info)
        self.assertEqual(info["thread_signature"], "thread-1")

    def test_target_message_with_resume_signal_restores_thread_extras(self):
        manager = self.manager_mod.GroupReplyWaitManager(timeout_sec=30, message_budget=3)
        reply_event = _ResumeFakeEvent(sender_id="user-42", sender_name="Target")
        reply_event.set_extra("astrmai_group_direct_wakeup", True)
        reply_event.set_extra("astrmai_thread_signature", "thread-1")
        reply_event.set_extra("astrmai_reply_mode", "casual_followup")
        manager.register_from_reply_event(reply_event)

        resumed_event = _ResumeFakeEvent(sender_id="user-42", sender_name="Target", message_str="@亚托莉 继续")
        result = manager.handle_incoming_message(resumed_event)

        self.assertEqual(result, "RESUME")
        self.assertEqual(resumed_event.get_extra("astrmai_thread_signature"), "thread-1")
        self.assertEqual(resumed_event.get_extra("astrmai_reply_mode"), "casual_followup")


if __name__ == "__main__":
    unittest.main()
