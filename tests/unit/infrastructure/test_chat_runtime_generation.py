import asyncio
import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator


class ChatRuntimeGenerationTests(unittest.TestCase):
    def test_advance_generation_increments_per_thread(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            first = await coordinator.advance_generation("chat-1", "thread-a")
            second = await coordinator.advance_generation("chat-1", "thread-a")
            other_thread = await coordinator.advance_generation("chat-1", "thread-b")
            other_chat = await coordinator.advance_generation("chat-2", "thread-a")
            return first, second, other_thread, other_chat

        self.assertEqual(asyncio.run(_run()), (1, 2, 1, 1))

    def test_current_generation_returns_zero_without_state(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            return await coordinator.current_generation("missing", "thread-a")

        self.assertEqual(asyncio.run(_run()), 0)

    def test_is_current_turn_is_compatible_with_missing_turn(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            return await coordinator.is_current_turn(None), await coordinator.is_current_turn(SimpleNamespace(chat_id="chat-1"))

        self.assertEqual(asyncio.run(_run()), (True, True))

    def test_is_current_turn_detects_stale_generation(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            generation = await coordinator.advance_generation("chat-1", "thread-a")
            await coordinator.advance_generation("chat-1", "thread-a")
            current = SimpleNamespace(chat_id="chat-1", thread_id="thread-a", generation=generation)
            stale = SimpleNamespace(chat_id="chat-1", thread_id="thread-a", generation=generation)
            current.generation = await coordinator.current_generation("chat-1", "thread-a")
            return await coordinator.is_current_turn(current), await coordinator.is_current_turn(stale)

        self.assertEqual(asyncio.run(_run()), (True, False))

    def test_send_claim_is_exactly_once_until_cleared_with_chat_state(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            first = await coordinator.claim_send("chat-1", "send-key")
            duplicate = await coordinator.claim_send("chat-1", "send-key")
            await coordinator.commit_send("chat-1", "send-key", ["msg-1", "msg-1", "msg-2"])
            claim = await coordinator.get_send_claim("chat-1", "send-key")
            snapshot = await coordinator.get_activity_snapshot("chat-1")
            await coordinator.clear_runtime_state("chat-1")
            after_clear = await coordinator.claim_send("chat-1", "send-key")
            return first, duplicate, claim, snapshot["send_claim_count"], after_clear

        first, duplicate, claim, claim_count, after_clear = asyncio.run(_run())
        self.assertEqual((first, duplicate, claim_count, after_clear), (True, False, 1, True))
        self.assertEqual(claim["status"], "committed")
        self.assertEqual(claim["outbound_message_ids"], ["msg-1", "msg-2"])

    def test_mark_send_failed_allows_same_turn_retry(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            first = await coordinator.claim_send("chat-1", "send-key")
            await coordinator.mark_send_failed("chat-1", "send-key", "x" * 400)
            claim = await coordinator.get_send_claim("chat-1", "send-key")
            duplicate = await coordinator.claim_send("chat-1", "send-key")
            return first, claim, duplicate

        first, claim, duplicate = asyncio.run(_run())
        self.assertTrue(first)
        self.assertEqual(claim["status"], "failed")
        self.assertEqual(len(claim["error"]), 300)
        self.assertTrue(duplicate)
        retried = asyncio.run(coordinator.get_send_claim("chat-1", "send-key"))
        self.assertEqual(retried["status"], "claimed")
        self.assertEqual(retried["error"], "")

    def test_empty_send_key_is_not_claimed(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            return await coordinator.claim_send("chat-1", ""), await coordinator.get_send_claim("chat-1", "")

        self.assertEqual(asyncio.run(_run()), (False, None))

    def test_generation_and_send_claim_state_are_bounded(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            for index in range(coordinator.MAX_THREAD_GENERATIONS_PER_CHAT + 20):
                await coordinator.advance_generation("chat-1", f"thread-{index}")
            for index in range(coordinator.MAX_SEND_CLAIMS_PER_CHAT + 20):
                await coordinator.claim_send("chat-1", f"send-{index}")
            return await coordinator.get_activity_snapshot("chat-1")

        snapshot = asyncio.run(_run())

        self.assertLessEqual(
            len(snapshot["turn_generations"]),
            coordinator.MAX_THREAD_GENERATIONS_PER_CHAT,
        )
        self.assertLessEqual(
            snapshot["send_claim_count"],
            coordinator.MAX_SEND_CLAIMS_PER_CHAT,
        )

    def test_concurrency_metrics_count_generation_and_duplicate_claims(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            await coordinator.advance_generation("chat-1", "thread-a")
            await coordinator.claim_send("chat-1", "send-a")
            await coordinator.claim_send("chat-1", "send-a")
            await coordinator.record_concurrency_event("stale_generation")
            return await coordinator.get_concurrency_metrics()

        metrics = asyncio.run(_run())

        self.assertEqual(metrics["generation_advanced"], 1)
        self.assertEqual(metrics["send_claimed"], 1)
        self.assertEqual(metrics["send_claim_exists"], 1)
        self.assertEqual(metrics["stale_generation"], 1)

    def test_advancing_generation_cancels_registered_task_for_same_thread(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            generation = await coordinator.advance_generation("chat-1", "thread-a")
            started = asyncio.Event()

            async def _work():
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(_work())
            turn = SimpleNamespace(chat_id="chat-1", thread_id="thread-a", generation=generation)
            self.assertTrue(await coordinator.register_turn_task(turn, task))
            await started.wait()
            await coordinator.advance_generation("chat-1", "thread-a")
            await asyncio.sleep(0)
            metrics = await coordinator.get_concurrency_metrics()
            return task.cancelled(), metrics

        cancelled, metrics = asyncio.run(_run())

        self.assertTrue(cancelled)
        self.assertEqual(metrics["stale_turn_cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
