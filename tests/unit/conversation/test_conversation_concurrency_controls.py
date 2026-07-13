import unittest
from types import SimpleNamespace

from astrmai.conversation.concurrency.controls import (
    ConversationConcurrencyFlags,
    record_conversation_concurrency_trace,
    resolve_conversation_concurrency_flags,
)


class _Event:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class ConversationConcurrencyControlsTests(unittest.TestCase):
    def test_defaults_keep_p0_guards_on_and_p1_gray_switch_off(self):
        flags = resolve_conversation_concurrency_flags(SimpleNamespace())

        self.assertEqual(
            flags,
            ConversationConcurrencyFlags(
                generation_enabled=True,
                send_claim_enabled=True,
                group_thread_wait_enabled=False,
                non_conversational_guard_enabled=True,
                debug_trace_enabled=False,
            ),
        )

    def test_flags_are_read_from_conversation_config(self):
        config = SimpleNamespace(
            conversation=SimpleNamespace(
                conversation_generation_enabled=False,
                reply_send_claim_enabled=False,
                group_thread_wait_enabled=True,
                non_conversational_guard_enabled=False,
                conversation_concurrency_debug_trace_enabled=True,
            )
        )

        flags = resolve_conversation_concurrency_flags(config)

        self.assertFalse(flags.generation_enabled)
        self.assertFalse(flags.send_claim_enabled)
        self.assertTrue(flags.group_thread_wait_enabled)
        self.assertFalse(flags.non_conversational_guard_enabled)
        self.assertTrue(flags.debug_trace_enabled)

    def test_summary_trace_drops_content_and_debug_factory_is_lazy(self):
        event = _Event()
        debug_factory_calls = []

        record_conversation_concurrency_trace(
            event,
            "reply_blocked",
            chat_id="chat-1",
            thread_id="thread-1",
            generation=2,
            blocked_reason="stale_generation",
            message_text="private user content",
            assistant_text="private assistant content",
            debug_enabled=False,
            debug_factory=lambda: debug_factory_calls.append(True) or {"raw_text": "private debug content"},
        )

        trace = event.get_extra("astrmai_conversation_concurrency_trace")
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["action"], "reply_blocked")
        self.assertNotIn("message_text", trace[0])
        self.assertNotIn("assistant_text", trace[0])
        self.assertNotIn("debug", trace[0])
        self.assertNotIn("private", str(trace[0]))
        self.assertEqual(debug_factory_calls, [])

    def test_debug_factory_runs_only_when_debug_trace_is_enabled(self):
        event = _Event()
        debug_factory_calls = []

        record_conversation_concurrency_trace(
            event,
            "turn_created",
            chat_id="chat-1",
            debug_enabled=True,
            debug_factory=lambda: debug_factory_calls.append(True) or {"message_ids": ["msg-1"]},
        )

        trace = event.get_extra("astrmai_conversation_concurrency_trace")
        self.assertEqual(debug_factory_calls, [True])
        self.assertEqual(trace[0]["debug"], {"message_ids": ["msg-1"]})


if __name__ == "__main__":
    unittest.main()
