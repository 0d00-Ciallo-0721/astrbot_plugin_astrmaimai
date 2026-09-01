import asyncio
import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.background_task_owner_registry import (
    BackgroundTaskOwnerRegistry,
)
from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline
from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
from astrmai.conversation.ingress.external_result_dispatcher import ExternalResultDispatcher
from astrmai.infrastructure.runtime.event_bus import EventBus
from astrmai.multimodal.visual_cortex import VisualCortex
from astrmai.learning.review.expression_governance_runner import ExpressionGovernanceRunner
from astrmai.memory.persona.persona_summarizer import PersonaSummarizer


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

    async def test_event_bus_workers_and_callbacks_are_registered(self):
        bus = EventBus()
        await bus.stop(timeout_sec=0.1)
        registry = BackgroundTaskOwnerRegistry(generation=4)
        bus.bind_owner_registry(registry)
        seen = asyncio.Event()

        async def callback(_payload):
            seen.set()

        bus.subscribe(bus.TOPIC_KNOWLEDGE_UPDATED, callback)
        bus.trigger_knowledge_update()
        await asyncio.wait_for(seen.wait(), timeout=1.0)
        await asyncio.sleep(0)
        families = {item["task_family"] for item in registry.describe()["tasks"]}
        self.assertIn("eventbus.knowledge_update", families)
        self.assertIn("eventbus.worker", families)
        await bus.stop(timeout_sec=0.2)
        await asyncio.sleep(0)
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)

    async def test_visual_cortex_worker_and_analysis_are_registered(self):
        registry = BackgroundTaskOwnerRegistry(generation=2)
        cortex = VisualCortex(SimpleNamespace(), None, owner_registry=registry)
        cortex.start()
        await asyncio.sleep(0)
        self.assertTrue(any(item["task_family"] == "vision.worker" for item in registry.describe()["tasks"]))
        worker = cortex._worker_task
        cortex.stop()
        if worker is not None:
            await worker
        await asyncio.sleep(0)
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)

    async def test_governance_scheduler_is_registered_and_stopped(self):
        registry = BackgroundTaskOwnerRegistry(generation=1)
        runner = ExpressionGovernanceRunner(
            state_engine=SimpleNamespace(),
            interval_seconds=15,
            owner_registry=registry,
        )
        await runner.start()
        await asyncio.sleep(0)
        record = next(item for item in registry.describe()["tasks"] if item["task_family"] == "governance.scheduler")
        self.assertEqual(record["scope_id"], "GLOBAL")
        self.assertTrue(record["run_id"])
        await runner.stop()
        await asyncio.sleep(0)
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)

    async def test_persona_background_tasks_are_registered(self):
        registry = BackgroundTaskOwnerRegistry(generation=5)
        persistence = SimpleNamespace(load_persona_cache=lambda: {})
        gateway = SimpleNamespace(config=SimpleNamespace())
        summarizer = PersonaSummarizer(persistence, gateway, owner_registry=registry)

        async def fake_enrichment(*_args, **_kwargs):
            await asyncio.sleep(10)

        summarizer._run_enrichment_until_complete = fake_enrichment
        task = summarizer._start_shard_task("prompt", "session")
        self.assertIsNotNone(task)
        await asyncio.sleep(0)
        record = next(item for item in registry.describe()["tasks"] if item["task_family"] == "persona.shards")
        self.assertEqual(record["scope_id"], "session")
        await summarizer.stop()
        await asyncio.sleep(0)
        self.assertEqual(registry.describe(include_terminal=False)["active"], 0)


if __name__ == "__main__":
    unittest.main()
