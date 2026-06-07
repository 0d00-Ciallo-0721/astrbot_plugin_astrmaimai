import asyncio
import unittest

from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator


class ChatRuntimeCoordinatorMigratedTests(unittest.TestCase):
    def test_sys2_lock_is_reused_per_chat(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            first = await coordinator.get_sys2_lock("chat-1")
            second = await coordinator.get_sys2_lock("chat-1")
            other = await coordinator.get_sys2_lock("chat-2")
            return first, second, other

        first, second, other = asyncio.run(_run())
        self.assertIs(first, second)
        self.assertIsNot(first, other)

    def test_executor_pending_limit_and_release(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            first = await coordinator.try_acquire_executor("chat-1", max_pending=2)
            second_task = asyncio.create_task(coordinator.try_acquire_executor("chat-1", max_pending=2))
            await asyncio.sleep(0)
            third = await coordinator.try_acquire_executor("chat-1", max_pending=2)
            await coordinator.release_executor("chat-1")
            second = await second_task
            await coordinator.release_executor("chat-1")
            fourth = await coordinator.try_acquire_executor("chat-1", max_pending=2)
            return first, second, third, fourth

        first, second, third, fourth = asyncio.run(_run())
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertIsNotNone(fourth)
        asyncio.run(coordinator.release_executor("chat-1"))

    def test_wait_targets_are_de_duplicated(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            await coordinator.update_wait_targets("chat-1", ["u1", "u2", "u1"], "Alice")
            return await coordinator.get_wait_targets("chat-1"), await coordinator.get_wait_target_name("chat-1")

        targets, target_name = asyncio.run(_run())
        self.assertEqual(targets, ["u1", "u2"])
        self.assertEqual(target_name, "Alice")

    def test_latest_activity_keeps_newest_timestamp(self):
        coordinator = ChatRuntimeCoordinator()

        async def _run():
            await coordinator.mark_activity("chat-1", 20.0, sender_id="u2", sender_name="Bob", preview="new")
            await coordinator.mark_activity("chat-1", 10.0, sender_id="u1", sender_name="Alice", preview="old")
            return await coordinator.get_latest_activity("chat-1")

        latest_ts, sender_id, sender_name, preview = asyncio.run(_run())
        self.assertEqual(latest_ts, 20.0)
        self.assertEqual(sender_id, "u2")
        self.assertEqual(sender_name, "Bob")
        self.assertEqual(preview, "new")


__all__ = ["ChatRuntimeCoordinatorMigratedTests"]
