"""Regression tests for executor_lock cancellation leak fix (R5).

Verifies that try_acquire_executor() properly decrements executor_pending
when CancelledError is raised during lock acquisition, preventing permanent
chat blockage.
"""

import asyncio
import importlib
import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.background_task_owner_registry import (
    BackgroundTaskOwnerRegistry,
)


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

    def test_lease_is_traceable_and_release_is_idempotent(self):
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coord_mod.ChatRuntimeCoordinator()

        async def _run():
            lease = await coordinator.try_acquire_executor(
                "lease-chat", thread_id="thread-1", turn_id="turn-1", generation=7
            )
            self.assertIsNotNone(lease)
            self.assertTrue(lease.token)
            self.assertEqual(lease.chat_id, "lease-chat")
            self.assertEqual(lease.generation, 7)
            snapshot = await coordinator.get_activity_snapshot("lease-chat")
            self.assertEqual(snapshot["executor_pending"], 1)
            self.assertEqual(snapshot["executor_leases"][0]["state"], "acquired")
            self.assertTrue(await coordinator.release_executor("lease-chat", lease=lease))
            self.assertFalse(await coordinator.release_executor("lease-chat", lease=lease))
            snapshot = await coordinator.get_activity_snapshot("lease-chat")
            self.assertEqual(snapshot["executor_pending"], 0)

        asyncio.run(_run())

    def test_generation_or_turn_mismatch_cannot_settle_lease(self):
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coord_mod.ChatRuntimeCoordinator()

        async def _run():
            lease = await coordinator.try_acquire_executor(
                "identity-chat", thread_id="thread-1", turn_id="turn-1", generation=3
            )
            self.assertFalse(
                await coordinator.release_executor("identity-chat", lease=lease, generation=4)
            )
            self.assertFalse(
                await coordinator.release_executor("identity-chat", lease=lease, turn_id="turn-2")
            )
            self.assertEqual(
                (await coordinator.get_activity_snapshot("identity-chat"))["executor_pending"], 1
            )
            self.assertTrue(await coordinator.release_executor("identity-chat", lease=lease, generation=3, turn_id="turn-1"))

        asyncio.run(_run())

    def test_owner_task_reconciliation_settles_orphaned_lease(self):
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        coordinator = coord_mod.ChatRuntimeCoordinator()

        async def _run():
            lease = await coordinator.try_acquire_executor("reconcile-chat")
            self.assertIsNotNone(lease)
            lease.owner_task = None
            settled = await coordinator.reconcile_executor_leases("reconcile-chat")
            self.assertEqual(settled, 1)
            self.assertEqual(
                (await coordinator.get_activity_snapshot("reconcile-chat"))["executor_pending"], 0
            )

        asyncio.run(_run())

    def test_release_settlement_survives_cancellation_while_coordinator_locked(self):
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        coordinator = coord_mod.ChatRuntimeCoordinator()
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.runtime_coordinator = coordinator
        executor._executor_release_tasks = set()

        async def _run():
            lease = await coordinator.try_acquire_executor("cancel-release-chat")
            await coordinator._lock.acquire()
            release_task = asyncio.create_task(
                executor._release_chat_execution_lock(
                    "cancel-release-chat", True, lease, None
                )
            )
            await asyncio.sleep(0)
            release_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await release_task
            coordinator._lock.release()
            if executor._executor_release_tasks:
                await asyncio.gather(*list(executor._executor_release_tasks), return_exceptions=True)
            snapshot = await coordinator.get_activity_snapshot("cancel-release-chat")
            self.assertEqual(snapshot["executor_pending"], 0)

        asyncio.run(_run())

    def test_release_settlement_is_registered_with_owner_registry(self):
        coord_mod = importlib.import_module(
            "astrmai.infrastructure.runtime.chat_runtime_coordinator"
        )
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        coordinator = coord_mod.ChatRuntimeCoordinator()
        registry = BackgroundTaskOwnerRegistry(generation=8)
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.runtime_coordinator = coordinator
        executor.owner_registry = registry
        executor._executor_release_tasks = set()

        async def _run():
            lease = await coordinator.try_acquire_executor(
                "owner-release-chat", turn_id="turn-1", generation=8
            )
            await executor._release_chat_execution_lock("owner-release-chat", True, lease, None)
            await asyncio.sleep(0)
            records = registry.describe()["tasks"]
            release = next(item for item in records if item["task_family"] == "executor.release")
            self.assertEqual(release["scope_id"], "owner-release-chat")
            self.assertEqual(release["generation"], 0)
            self.assertEqual(release["status"], "succeeded")

        asyncio.run(_run())

    def test_release_settlement_failure_is_recorded(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        registry = BackgroundTaskOwnerRegistry()
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.owner_registry = registry
        executor._executor_release_tasks = set()

        class _Coordinator:
            async def release_executor(self, *_args, **_kwargs):
                raise RuntimeError("release failed")

        executor.runtime_coordinator = _Coordinator()

        async def _run():
            with self.assertRaises(RuntimeError):
                await executor._release_chat_execution_lock("failed-release-chat", True, object(), None)
            await asyncio.sleep(0)
            release = next(item for item in registry.describe()["tasks"] if item["task_family"] == "executor.release")
            self.assertEqual(release["status"], "failed")
            self.assertEqual(release["error_type"], "RuntimeError")

        asyncio.run(_run())

    def test_release_settlement_retries_explicit_coordinator_rejection(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.owner_registry = None
        executor._executor_release_tasks = set()

        class _Coordinator:
            def __init__(self):
                self.attempts = 0

            async def release_executor(self, *_args, **_kwargs):
                self.attempts += 1
                return self.attempts >= 2

        coordinator = _Coordinator()
        executor.runtime_coordinator = coordinator

        async def _run():
            lease = SimpleNamespace(token="release-retry-token")
            await executor._release_chat_execution_lock("retry-release-chat", True, lease, None)
            self.assertEqual(coordinator.attempts, 2)

        asyncio.run(_run())

    def test_release_exhaustion_triggers_reconciliation(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.owner_registry = None
        executor._executor_release_tasks = set()

        class _Coordinator:
            def __init__(self):
                self.reconciled = []

            async def release_executor(self, *_args, **_kwargs):
                return False

            async def reconcile_executor_leases(self, chat_id=None):
                self.reconciled.append(chat_id)

        coordinator = _Coordinator()
        executor.runtime_coordinator = coordinator

        async def _run():
            with self.assertRaises(RuntimeError):
                await executor._release_chat_execution_lock(
                    "exhausted-release-chat", True, SimpleNamespace(token="token"), None
                )
            self.assertEqual(coordinator.reconciled, ["exhausted-release-chat"])

        asyncio.run(_run())

    def test_release_false_after_reconciliation_is_idempotent(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.owner_registry = None
        executor._executor_release_tasks = set()

        class _Coordinator:
            async def release_executor(self, *_args, **_kwargs):
                return False

            async def get_activity_snapshot(self, _chat_id):
                return {"executor_leases": []}

        executor.runtime_coordinator = _Coordinator()

        async def _run():
            await executor._release_chat_execution_lock(
                "already-reconciled-chat", True, SimpleNamespace(token="settled-token"), None
            )

        asyncio.run(_run())

    def test_release_snapshot_must_be_explicit_and_well_formed(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")

        async def run_with_snapshot(snapshot):
            executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
            executor.owner_registry = None
            executor._executor_release_tasks = set()

            class _Coordinator:
                async def release_executor(self, *_args, **_kwargs):
                    return False

                async def get_activity_snapshot(self, _chat_id):
                    if isinstance(snapshot, BaseException):
                        raise snapshot
                    return snapshot

                async def reconcile_executor_leases(self, chat_id=None):
                    return 0

            executor.runtime_coordinator = _Coordinator()
            with self.assertRaises(RuntimeError):
                await executor._release_chat_execution_lock(
                    "snapshot-chat", True, SimpleNamespace(token="lease-token"), None
                )

        for snapshot in ({}, {"other": []}, {"executor_leases": None}, {"executor_leases": [{}]}, RuntimeError("broken")):
            asyncio.run(run_with_snapshot(snapshot))

        async def valid_empty():
            executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
            executor.owner_registry = None
            executor._executor_release_tasks = set()

            class _Coordinator:
                async def release_executor(self, *_args, **_kwargs):
                    return False

                async def get_activity_snapshot(self, _chat_id):
                    return {"executor_leases": []}

            executor.runtime_coordinator = _Coordinator()
            await executor._release_chat_execution_lock(
                "snapshot-chat", True, SimpleNamespace(token="lease-token"), None
            )

        asyncio.run(valid_empty())

    def test_coordinator_internal_type_error_is_not_treated_as_legacy_signature(self):
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        executor = executor_mod.ConcurrentExecutor.__new__(executor_mod.ConcurrentExecutor)
        executor.runtime_coordinator = SimpleNamespace()
        executor.config = SimpleNamespace(
            timing=SimpleNamespace(executor_lock_wait_timeout_sec=0.1)
        )

        async def _broken(chat_id, max_pending=2):
            raise TypeError("internal coordinator failure")

        executor.runtime_coordinator.try_acquire_executor = _broken

        class _Event:
            def get_extra(self, _key, default=None):
                return default

        with self.assertRaises(TypeError):
            asyncio.run(executor._acquire_chat_execution_lock("type-error-chat", _Event()))

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
