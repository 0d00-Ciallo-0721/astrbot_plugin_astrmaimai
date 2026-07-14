import unittest
from types import SimpleNamespace

from astrmai.conversation.ingress import dedupe


class _Event:
    def __init__(self, *, message_id: str, text: str = "same text"):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id=message_id, message=text)
        self.unified_msg_origin = "default:GroupMessage:group-1"

    def get_sender_id(self):
        return "user-1"


class FinalReviewFollowupTests(unittest.TestCase):
    def setUp(self):
        dedupe._debounce_cache.clear()

    def tearDown(self):
        dedupe._debounce_cache.clear()

    def test_message_dedup_prefers_message_id_over_equal_content(self):
        first = dedupe.check_message_dedup(_Event(message_id="msg-1"))
        second = dedupe.check_message_dedup(_Event(message_id="msg-2"))
        duplicate = dedupe.check_message_dedup(_Event(message_id="msg-1"))

        self.assertFalse(first.should_stop)
        self.assertFalse(second.should_stop)
        self.assertTrue(duplicate.should_stop)

    def test_message_dedup_retains_content_fallback_without_message_id(self):
        first = dedupe.check_message_dedup(_Event(message_id="", text="fallback"))
        duplicate = dedupe.check_message_dedup(_Event(message_id="", text="fallback"))

        self.assertFalse(first.should_stop)
        self.assertTrue(duplicate.should_stop)


if __name__ == "__main__":
    unittest.main()
