import asyncio
import importlib
import time
import unittest
from types import SimpleNamespace


class ChatRuntimeCoordinatorRefactorTests(unittest.TestCase):
    def test_sys2_lock_is_reused_per_chat(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()

        async def _run():
            first = await coordinator.get_sys2_lock("chat-1")
            second = await coordinator.get_sys2_lock("chat-1")
            other = await coordinator.get_sys2_lock("chat-2")
            return first, second, other

        first, second, other = asyncio.run(_run())
        self.assertIs(first, second)
        self.assertIsNot(first, other)

    def test_sys2_lock_is_rejected_after_shutdown(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()

        async def _run():
            await coordinator.shutdown()
            return await coordinator.get_sys2_lock("chat-1", "thread-1")

        self.assertIsNone(asyncio.run(_run()))

    def test_active_thread_locks_are_not_evicted_at_capacity(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()
        coordinator.MAX_THREAD_GENERATIONS_PER_CHAT = 2

        async def _run():
            first = await coordinator.get_sys2_lock("chat-1", "thread-1")
            second = await coordinator.get_sys2_lock("chat-1", "thread-2")
            await first.acquire()
            await second.acquire()
            third = await coordinator.get_sys2_lock("chat-1", "thread-3")
            first_again = await coordinator.get_sys2_lock("chat-1", "thread-1")
            first.release()
            second.release()
            return first, first_again, third

        first, first_again, third = asyncio.run(_run())
        self.assertIs(first, first_again)
        self.assertIsNot(first, third)

    def test_clear_runtime_state_keeps_state_until_active_lock_is_released(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()

        async def _run():
            lock = await coordinator.get_sys2_lock("chat-1")
            await lock.acquire()
            self.assertTrue(await coordinator.clear_runtime_state("chat-1"))
            retained = await coordinator.get_sys2_lock("chat-1")
            lock.release()
            return lock, retained

        lock, retained = asyncio.run(_run())
        self.assertIs(lock, retained)

    def test_wait_targets_are_de_duplicated(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()

        async def _run():
            await coordinator.update_wait_targets("chat-1", ["u1", "u2", "u1"], "Alice")
            return await coordinator.get_wait_targets("chat-1"), await coordinator.get_wait_target_name("chat-1")

        targets, target_name = asyncio.run(_run())
        self.assertEqual(targets, ["u1", "u2"])
        self.assertEqual(target_name, "Alice")

    def test_active_chat_listing_and_snapshot_use_recent_activity(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()
        now = time.time()

        async def _run():
            await coordinator.mark_activity("old-chat", now - 2000, "u-old", "Old", "old preview")
            await coordinator.mark_activity("chat-1", now - 40, "u1", "Alice", "first")
            await coordinator.mark_activity("chat-1", now - 20, "u1", "Alice", "second", "thread-a")
            active = await coordinator.list_active_chats(max_age_seconds=1800)
            snapshot = await coordinator.get_activity_snapshot("chat-1")
            return active, snapshot

        active, snapshot = asyncio.run(_run())

        self.assertEqual(active, ["chat-1"])
        self.assertEqual(snapshot["latest_activity_sender_name"], "Alice")
        self.assertEqual(snapshot["latest_activity_preview"], "second")
        self.assertEqual(snapshot["latest_activity_thread_signature"], "thread-a")
        self.assertEqual(snapshot["recent_activity_count"], 2)

    def test_shutdown_cancels_active_turns_and_rejects_late_results(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()

        async def _run():
            generation = await coordinator.advance_generation("chat-1", "thread-1")
            turn = SimpleNamespace(
                chat_id="chat-1",
                thread_id="thread-1",
                generation=generation,
            )
            started = asyncio.Event()

            async def _worker():
                started.set()
                await asyncio.sleep(60)

            task = asyncio.create_task(_worker())
            self.assertTrue(await coordinator.register_turn_task(turn, task))
            await started.wait()
            cancelled_count = await coordinator.shutdown()
            return cancelled_count, task, await coordinator.is_current_turn(turn)

        cancelled_count, task, is_current = asyncio.run(_run())

        self.assertEqual(cancelled_count, 1)
        self.assertTrue(task.cancelled())
        self.assertFalse(is_current)
        self.assertEqual(coordinator._states, {})

    def test_active_turn_task_count_tracks_register_and_unregister(self):
        coordinator_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coordinator_mod.ChatRuntimeCoordinator()

        async def _run():
            generation = await coordinator.advance_generation("chat-1", "thread-1")
            turn = SimpleNamespace(chat_id="chat-1", thread_id="thread-1", generation=generation)
            task = asyncio.create_task(asyncio.sleep(60))
            self.assertTrue(await coordinator.register_turn_task(turn, task))
            active = coordinator.active_turn_task_count_sync()
            await coordinator.unregister_turn_task(turn, task)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return active, coordinator.active_turn_task_count_sync()

        active, remaining = asyncio.run(_run())
        self.assertEqual(active, 1)
        self.assertEqual(remaining, 0)


if __name__ == "__main__":
    unittest.main()
