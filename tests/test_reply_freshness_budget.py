import asyncio
import unittest

from astrmai.infra.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.infra.runtime_contracts import FreshnessState


class ReplyFreshnessBudgetTests(unittest.TestCase):
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
            return stale_state, fresh_same_thread, expired_state

        stale_state, fresh_same_thread, expired_state = asyncio.run(_run())
        self.assertEqual(stale_state[0], FreshnessState.STALE_BUT_SALVAGEABLE)
        self.assertEqual(fresh_same_thread[0], FreshnessState.FRESH)
        self.assertEqual(expired_state[0], FreshnessState.EXPIRED)


if __name__ == "__main__":
    unittest.main()
