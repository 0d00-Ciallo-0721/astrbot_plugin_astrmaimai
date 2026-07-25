import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.execution.reply_freshness import (
    is_stale_reply_reason,
    resolve_reply_max_age_seconds,
)
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.infrastructure.runtime.runtime_contracts import FreshnessState


class ReplyFreshnessBudgetTests(unittest.TestCase):
    def test_auto_reply_budget_scales_past_old_ninety_second_cap(self):
        config = SimpleNamespace(
            timing=SimpleNamespace(
                reply_max_age_sec=0.0,
                model_request_timeout_sec=240.0,
                agent_execution_timeout_sec=600,
            )
        )

        self.assertEqual(resolve_reply_max_age_seconds(config), 750.0)

    def test_reply_age_exceeded_is_classified_as_stale(self):
        self.assertTrue(is_stale_reply_reason("reply_age_exceeded:94.6s>90.0s"))
        self.assertFalse(is_stale_reply_reason("transport failed"))

    def test_evaluate_reply_freshness_returns_stale_and_expired(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            await coordinator.mark_activity("chat-1", 15.0, sender_id="u2", sender_name="Bob", preview="later", thread_signature="sig-new")
            stale_state = await coordinator.evaluate_reply_freshness(
                "chat-1",
                10.0,
                max_age_seconds=30.0,
                thread_signature="sig-old",
                salvage_window_seconds=6.0,
            )
            fresh_same_thread = await coordinator.evaluate_reply_freshness(
                "chat-1",
                10.0,
                max_age_seconds=30.0,
                thread_signature="sig-new",
                salvage_window_seconds=6.0,
            )
            await coordinator.mark_activity("chat-2", 25.0, sender_id="u3", sender_name="Carol", preview="much later", thread_signature="sig-new")
            expired_state = await coordinator.evaluate_reply_freshness(
                "chat-2",
                10.0,
                max_age_seconds=30.0,
                thread_signature="sig-old",
                salvage_window_seconds=6.0,
            )
            parallel_thread_state = await coordinator.evaluate_reply_freshness(
                "chat-2",
                10.0,
                max_age_seconds=30.0,
                thread_signature="sig-old",
                salvage_window_seconds=6.0,
                allow_parallel_threads=True,
            )
            return stale_state, fresh_same_thread, expired_state, parallel_thread_state

        stale_state, fresh_same_thread, expired_state, parallel_thread_state = asyncio.run(_run())
        self.assertEqual(stale_state[0], FreshnessState.STALE_BUT_SALVAGEABLE)
        self.assertEqual(fresh_same_thread[0], FreshnessState.FRESH)
        self.assertEqual(expired_state[0], FreshnessState.EXPIRED)
        self.assertEqual(parallel_thread_state[0], FreshnessState.FRESH)
        self.assertEqual(parallel_thread_state[1], "newer_activity_other_thread_ignored")


if __name__ == "__main__":
    unittest.main()
