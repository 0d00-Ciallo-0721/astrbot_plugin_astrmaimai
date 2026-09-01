import asyncio
import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.background_task_owner_registry import (
    BackgroundTaskOwnerRegistry,
)
from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline
from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
from astrmai.conversation.ingress.external_result_dispatcher import ExternalResultDispatcher


class BackgroundTaskOwnerRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_records_metadata_and_success_terminal(self):
        registry = BackgroundTaskOwnerRegistry(generation=7)

        async def work():
            await asyncio.sleep(0)
            return "ok"

        task = asyncio.create_task(work(), name="owner-success")
        task_id = registry.register(
            task,
            task_family="learning.backlog",
            scope_id="group-1",
            run_id="run-1",
            owner="EvolutionManager",
        )
        await task
        await asyncio.sleep(0)

        record = registry.describe()["tasks"][0]
        self.assertEqual(record["task_id"], task_id)
        self.assertEqual(record["task_family"], "learning.backlog")
        self.assertEqual(record["scope_id"], "group-1")
        self.assertEqual(record["run_id"], "run-1")
        self.assertEqual(record["generation"], 7)
        self.assertEqual(record["status"], "succeeded")
        self.assertGreater(record["started_at"], 0)
        self.assertGreater(record["finished_at"], 0)

    async def test_pre_start_cancel_is_settled_from_task_callback(self):
        registry = BackgroundTaskOwnerRegistry()
        started = False

        async def work():
            nonlocal started
            started = True
            await asyncio.sleep(10)

        task = asyncio.create_task(work(), name="owner-cancel")
        task_id = registry.register(
            task,
            task_family="memory.replay",
            scope_id="GLOBAL",
            run_id="run-cancel",
            cancel_status="cancelled",
        )
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        record = next(item for item in registry.describe()["tasks"] if item["task_id"] == task_id)
        self.assertFalse(started)
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["error_type"], "CancelledError")

    async def test_failed_task_and_cancel_all_are_visible(self):
        registry = BackgroundTaskOwnerRegistry()

        async def fail():
            raise ValueError("boom")

        failed = asyncio.create_task(fail(), name="owner-fail")
        registry.register(
            failed,
            task_family="governance.audit",
            scope_id="group-2",
            run_id="run-fail",
        )
        with self.assertRaises(ValueError):
            await failed
        await asyncio.sleep(0)

        async def long_running():
            await asyncio.sleep(10)

        pending = registry.track(
            long_running(),
            task_family="profile.scan",
            scope_id="user-1",
            run_id="run-pending",
        )
        self.assertEqual(len(registry._background_tasks), 1)
        remaining = await registry.cancel_all(timeout_sec=0.5)
        self.assertEqual(remaining, 0)
        with self.assertRaises(asyncio.CancelledError):
            await pending

        statuses = {item["status"] for item in registry.describe()["tasks"]}
        self.assertIn("failed", statuses)
        self.assertIn("cancelled", statuses)

    async def test_memory_pipeline_sweep_is_registered_and_stopped(self):
        registry = BackgroundTaskOwnerRegistry(generation=3)
        pipeline = MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace())),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace()),
            owner_registry=registry,
        )
        await pipeline.start()
        self.assertTrue(
            any(
                item["task_family"] == "memory.maintenance.sweep"
                and item["scope_id"] == "GLOBAL"
                for item in registry.describe()["tasks"]
            )
        )
        await pipeline.stop()
        await asyncio.sleep(0)
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)

    async def test_memory_projector_retry_is_registered(self):
        registry = BackgroundTaskOwnerRegistry()
        engine = SimpleNamespace(
            owner_registry=registry,
            v2_store=SimpleNamespace(),
            background_task_budget=None,
        )
        projector = MemoryIndexProjector(engine)
        await projector.start()
        self.assertTrue(
            any(
                item["task_family"] == "memory.projection.retry"
                and item["scope_id"] == "GLOBAL"
                for item in registry.describe()["tasks"]
            )
        )
        await projector.stop()
        await asyncio.sleep(0)
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)

    async def test_external_result_worker_uses_owner_registry_without_lifecycle_manager(self):
        registry = BackgroundTaskOwnerRegistry()
        runtime = SimpleNamespace(owner_registry=registry, background_tasks=set())
        dispatcher = ExternalResultDispatcher(runtime)

        async def empty_worker():
            await asyncio.sleep(0)

        dispatcher._run = empty_worker
        dispatcher._ensure_worker()
        await dispatcher._worker_task
        await asyncio.sleep(0)
        records = registry.describe()["tasks"]
        self.assertTrue(any(item["task_family"] == "external_result.dispatch" for item in records))
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)


if __name__ == "__main__":
    unittest.main()
