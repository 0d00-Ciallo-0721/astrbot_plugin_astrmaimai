"""Regression tests for executor_lock cancellation leak fix (R5).

Verifies that try_acquire_executor() properly decrements executor_pending
when CancelledError is raised during lock acquisition, preventing permanent
chat blockage.
"""

import asyncio
import importlib
import unittest
from types import SimpleNamespace


class ExecutorLockRegressionTests(unittest.TestCase):
    """Regression tests for R5: executor_lock cancellation leak."""

    def test_cancelled_error_decrements_executor_pending(self):
        """After CancelledError during try_acquire_executor, executor_pending
        must be 0 so subsequent calls can succeed."""
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coord_mod.ChatRuntimeCoordinator()

        async def _run():
            chat_id = "test-chat-cancel"

            # Acquire the lock so the next acquire will block
            lock1 = await coordinator.try_acquire_executor(chat_id)
            self.assertIsNotNone(lock1, "First acquire must succeed")

            # Start a task that will block on the second acquire, then cancel it
            async def blocker():
                return await coordinator.try_acquire_executor(chat_id)

            task = asyncio.create_task(blocker())
            await asyncio.sleep(0)  # let it start and block

            # Cancel the blocked task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected

            # Release the first lock
            await coordinator.release_executor(chat_id)

            # Now try to acquire again — must succeed (pending was decremented)
            lock2 = await coordinator.try_acquire_executor(chat_id)
            self.assertIsNotNone(
                lock2,
                "Third acquire must succeed after cancellation — executor_pending was not decremented",
            )
            await coordinator.release_executor(chat_id)

            # Verify pending counter is 0
            state = coordinator._states.get(chat_id)
            self.assertIsNotNone(state)
            self.assertEqual(
                state.executor_pending, 0,
                f"executor_pending should be 0 after releases, got {state.executor_pending}",
            )

        asyncio.run(_run())

    def test_normal_acquire_release_does_not_leak(self):
        """Normal acquire/release cycle keeps executor_pending at 0."""
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coord_mod.ChatRuntimeCoordinator()

        async def _run():
            chat_id = "test-chat-normal"
            for _ in range(5):
                lock = await coordinator.try_acquire_executor(chat_id)
                self.assertIsNotNone(lock)
                await coordinator.release_executor(chat_id)

            state = coordinator._states.get(chat_id)
            if state is not None:
                self.assertEqual(state.executor_pending, 0)

        asyncio.run(_run())

    def test_local_executor_timeout_rolls_back_pending_counter(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.runtime_coordinator = None
        executor.config = SimpleNamespace(
            timing=SimpleNamespace(executor_lock_wait_timeout_sec=0.1)
        )
        executor._global_lock = asyncio.Lock()
        executor._chat_locks = {}
        executor._chat_pending_count = {}

        class _Event:
            def __init__(self):
                self.extras = {}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        async def _run():
            event = _Event()
            first_lock, using_coordinator, outcome = await executor._acquire_chat_execution_lock("chat", event)
            self.assertIsNotNone(first_lock)
            self.assertFalse(using_coordinator)
            self.assertEqual(outcome, "")

            second_lock, _, second_outcome = await executor._acquire_chat_execution_lock("chat", event)
            self.assertIsNone(second_lock)
            self.assertEqual(second_outcome, "queue_timeout")
            self.assertEqual(executor._chat_pending_count["chat"], 1)

            await executor._release_chat_execution_lock("chat", False, first_lock)
            self.assertNotIn("chat", executor._chat_pending_count)
            self.assertNotIn("chat", executor._chat_locks)

        asyncio.run(_run())

    def test_cancelled_error_has_try_except_in_source(self):
        """Verify the source code of try_acquire_executor contains
        'except asyncio.CancelledError' to confirm the fix is in place."""
        import inspect
        source = inspect.getsource(
            importlib.import_module(
                "astrmai.infrastructure.runtime.chat_runtime_coordinator"
            ).ChatRuntimeCoordinator.try_acquire_executor
        )
        self.assertIn(
            "CancelledError",
            source,
            "try_acquire_executor source must contain CancelledError handler",
        )


if __name__ == "__main__":
    unittest.main()
