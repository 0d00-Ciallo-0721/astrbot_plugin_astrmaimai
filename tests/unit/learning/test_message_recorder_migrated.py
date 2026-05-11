import unittest

from astrmai.learning.logging.message_recorder import MessageRecorder


class MessageRecorderMigratedTests(unittest.TestCase):
    def test_trigger_after_window_and_min_messages(self):
        recorder = MessageRecorder(window_seconds=60, min_messages=3, cooldown_seconds=60)
        self.assertFalse(recorder.record("group-a", timestamp=1000.0))
        self.assertFalse(recorder.record("group-a", timestamp=1010.0))
        self.assertTrue(recorder.record("group-a", timestamp=1020.0))

    def test_respects_cooldown(self):
        recorder = MessageRecorder(window_seconds=60, min_messages=2, cooldown_seconds=60)
        self.assertFalse(recorder.record("group-a", timestamp=1000.0))
        self.assertTrue(recorder.record("group-a", timestamp=1001.0))
        self.assertFalse(recorder.record("group-a", timestamp=1010.0))
        self.assertTrue(recorder.record("group-a", timestamp=1065.0))

    def test_clear_resets_window(self):
        recorder = MessageRecorder(window_seconds=60, min_messages=2, cooldown_seconds=0)
        self.assertFalse(recorder.record("group-a", timestamp=1000.0))
        recorder.clear("group-a")
        self.assertFalse(recorder.record("group-a", timestamp=1001.0))


__all__ = ["MessageRecorderMigratedTests"]
