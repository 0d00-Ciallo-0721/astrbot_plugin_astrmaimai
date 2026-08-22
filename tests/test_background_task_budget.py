import asyncio
import threading
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from astrmai.app.runtime_context import PluginRuntimeContext
from astrmai.infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskExecutionTimeout,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from astrmai.conversation.attention.context_compaction import ContextCompactionEngine
from astrmai.memory.persona.persona_summarizer import PersonaSummarizer


class BackgroundTaskBudgetTests(unittest.TestCase):
    def test_persona_background_model_uses_shared_budget(self):
        async def run():
            budget = BackgroundTaskBudget(1)
            summarizer = object.__new__(PersonaSummarizer)
            summarizer.background_task_budget = budget
            release = asyncio.Event()
            second_started = asyncio.Event()

            async def first():
                await release.wait()
                return "first"

            async def second():
                second_started.set()
                return "second"

            first_task = asyncio.create_task(summarizer._run_background_model(first))
            await asyncio.sleep(0)
            second_task = asyncio.create_task(summarizer._run_background_model(second))
            await asyncio.sleep(0)
            self.assertFalse(second_started.is_set())
            self.assertEqual(budget.status()["queued"], 1)
            release.set()
            self.assertEqual(await first_task, "first")
            self.assertEqual(await second_task, "second")

        asyncio.run(run())

    def test_compaction_provider_uses_shared_budget(self):
        async def run():
            budget = BackgroundTaskBudget(1)
            engine = ContextCompactionEngine(None, background_task_budget=budget)
            release = asyncio.Event()
            second_started = asyncio.Event()

            async def first():
                await release.wait()
                return "first"

            async def second():
                second_started.set()
                return "second"

            first_task = asyncio.create_task(engine._run_compaction_model(first))
            await asyncio.sleep(0)
            second_task = asyncio.create_task(engine._run_compaction_model(second))
            await asyncio.sleep(0)
            self.assertFalse(second_started.is_set())
            self.assertEqual(budget.status()["queued"], 1)
            release.set()
            self.assertEqual(await first_task, "first")
            self.assertEqual(await second_task, "second")

        asyncio.run(run())

    def test_compaction_provider_uses_gateway_concurrency_slot(self):
        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()
            stages = []

            class Gateway:
                @asynccontextmanager
                async def _concurrency_slot(self, critical_path, *, event, stage):
                    self.critical_path = critical_path
                    self.event = event
                    stages.append(stage)
                    entered.set()
                    await release.wait()
                    yield

            engine = ContextCompactionEngine(None, gateway=Gateway())

            async def work():
                return "summary"

            task = asyncio.create_task(engine._run_compaction_model(work))
            await entered.wait()
            self.assertEqual(stages, ["gateway.compaction_semaphore_wait"])
            self.assertFalse(task.done())
            release.set()
            self.assertEqual(await task, "summary")

        asyncio.run(run())

    def test_concurrency_never_exceeds_limit(self):
        async def run():
            budget = BackgroundTaskBudget(2)
            active = 0
            peak = 0
            release = asyncio.Event()

            async def work(index):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await release.wait()
                active -= 1
                return index

            tasks = [asyncio.create_task(budget.run(lambda index=index: work(index))) for index in range(4)]
            await asyncio.sleep(0)
            self.assertEqual(
                budget.status(),
                {
                    "limit": 2,
                    "max_queue": 64,
                    "wait_timeout_sec": 120.0,
                    "active": 2,
                    "available_slots": 0,
                    "queued": 2,
                    "peak_queued": 2,
                    "rejected": 0,
                    "timed_out": 0,
                },
            )
            release.set()
            self.assertEqual(await asyncio.gather(*tasks), [0, 1, 2, 3])
            self.assertEqual(peak, 2)
            self.assertEqual(
                budget.status(),
                {
                    "limit": 2,
                    "max_queue": 64,
                    "wait_timeout_sec": 120.0,
                    "active": 0,
                    "available_slots": 2,
                    "queued": 0,
                    "peak_queued": 2,
                    "rejected": 0,
                    "timed_out": 0,
                },
            )

        asyncio.run(run())

    def test_exception_and_running_task_cancellation_release_slots(self):
        async def run():
            budget = BackgroundTaskBudget(1)

            async def fail():
                raise RuntimeError("failed")

            with self.assertRaisesRegex(RuntimeError, "failed"):
                await budget.run(fail)
            self.assertEqual(budget.status()["available_slots"], 1)

            started = asyncio.Event()

            async def wait_forever():
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(budget.run(wait_forever))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(budget.status()["active"], 0)
            self.assertEqual(budget.status()["available_slots"], 1)

        asyncio.run(run())

    def test_waiting_task_cancellation_does_not_create_awaitable(self):
        async def run():
            budget = BackgroundTaskBudget(1)
            release = asyncio.Event()
            factory_called = False

            async def first():
                await release.wait()

            async def second():
                nonlocal factory_called
                factory_called = True

            running = asyncio.create_task(budget.run(first))
            await asyncio.sleep(0)
            waiting = asyncio.create_task(budget.run(second))
            await asyncio.sleep(0)
            self.assertEqual(budget.status()["queued"], 1)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertFalse(factory_called)
            self.assertEqual(budget.status()["queued"], 0)
            release.set()
            await running

        asyncio.run(run())

    def test_refresh_limit_wakes_waiters_and_applies_decrease(self):
        async def run():
            budget = BackgroundTaskBudget(1)
            first_release = asyncio.Event()
            second_started = asyncio.Event()
            second_release = asyncio.Event()

            async def first():
                await first_release.wait()

            async def second():
                second_started.set()
                await second_release.wait()

            first_task = asyncio.create_task(budget.run(first))
            second_task = asyncio.create_task(budget.run(second))
            await asyncio.sleep(0)
            budget.refresh_limit(2)
            await second_started.wait()
            self.assertEqual(budget.status()["active"], 2)

            budget.refresh_limit(1)
            third_started = asyncio.Event()

            async def third():
                third_started.set()

            third_task = asyncio.create_task(budget.run(third))
            await asyncio.sleep(0)
            first_release.set()
            await first_task
            await asyncio.sleep(0)
            self.assertFalse(third_started.is_set())
            second_release.set()
            await second_task
            await third_task
            self.assertTrue(third_started.is_set())
            self.assertEqual(budget.status()["limit"], 1)

        asyncio.run(run())

    def test_high_pressure_background_queue_keeps_active_work_bounded(self):
        async def run():
            budget = BackgroundTaskBudget(3)
            active = 0
            peak = 0
            completed = 0
            release = asyncio.Event()

            async def work():
                nonlocal active, peak, completed
                active += 1
                peak = max(peak, active)
                await release.wait()
                active -= 1
                completed += 1

            tasks = [asyncio.create_task(budget.run(work)) for _ in range(64)]
            await asyncio.sleep(0)
            self.assertEqual(budget.status()["active"], 3)
            self.assertEqual(budget.status()["queued"], 61)
            release.set()
            await asyncio.gather(*tasks)
            self.assertEqual(completed, 64)
            self.assertEqual(peak, 3)
            self.assertEqual(budget.status()["queued"], 0)

        asyncio.run(run())

    def test_queue_limit_rejects_without_calling_factory(self):
        async def run():
            budget = BackgroundTaskBudget(1, max_queue=1)
            release = asyncio.Event()
            called = []

            async def first():
                await release.wait()

            async def second():
                called.append("second")

            async def third():
                called.append("third")

            running = asyncio.create_task(budget.run(first))
            await asyncio.sleep(0)
            waiting = asyncio.create_task(budget.run(second))
            await asyncio.sleep(0)
            with self.assertRaises(BackgroundTaskQueueFull):
                await budget.run(third)
            self.assertEqual(called, [])
            release.set()
            await running
            await waiting
            self.assertEqual(budget.status()["rejected"], 1)

        asyncio.run(run())

    def test_scope_diagnostics_separate_attention_groups(self):
        async def run():
            budget = BackgroundTaskBudget(1, max_queue=1)
            release = asyncio.Event()

            async def first():
                await release.wait()

            async def second():
                return "second"

            running = asyncio.create_task(
                budget.run(
                    first,
                    task_name="attention.system2",
                    scope_id="group-a",
                )
            )
            await asyncio.sleep(0)
            waiting = asyncio.create_task(
                budget.run(
                    second,
                    task_name="attention.system2",
                    scope_id="group-b",
                )
            )
            await asyncio.sleep(0)
            status = budget.status()
            self.assertEqual(
                status["scope_stats"]["attention.system2|group-a"]["active"],
                1,
            )
            self.assertEqual(
                status["scope_stats"]["attention.system2|group-b"]["queued"],
                1,
            )
            release.set()
            await running
            self.assertEqual(await waiting, "second")
            final_status = budget.status()
            self.assertEqual(
                final_status["scope_stats"]["attention.system2|group-a"]["completed"],
                1,
            )
            self.assertEqual(
                final_status["scope_stats"]["attention.system2|group-b"]["completed"],
                1,
            )

        asyncio.run(run())

    def test_scope_diagnostics_are_bounded(self):
        async def run():
            budget = BackgroundTaskBudget(1)

            async def work():
                return None

            for index in range(300):
                await budget.run(
                    work,
                    task_name="attention.system2",
                    scope_id=f"group-{index}",
                )
            scope_stats = budget.status()["scope_stats"]
            self.assertEqual(len(scope_stats), 256)
            self.assertNotIn("attention.system2|group-0", scope_stats)
            self.assertIn("attention.system2|group-299", scope_stats)

        asyncio.run(run())

    def test_queue_wait_timeout_releases_waiter(self):
        async def run():
            budget = BackgroundTaskBudget(1, max_queue=1, wait_timeout_sec=0.01)
            release = asyncio.Event()

            async def first():
                await release.wait()

            async def second():
                raise AssertionError("timed out task must not start")

            running = asyncio.create_task(budget.run(first))
            await asyncio.sleep(0)
            with self.assertRaises(BackgroundTaskQueueTimeout):
                await budget.run(second)
            self.assertEqual(budget.status()["queued"], 0)
            self.assertEqual(budget.status()["timed_out"], 1)
            release.set()
            await running

        asyncio.run(run())

    def test_runtime_rebuild_refreshes_budget_and_diagnostics(self):
        config = SimpleNamespace(infra=SimpleNamespace(background_task_concurrency=3))
        budget = BackgroundTaskBudget(1)
        runtime = PluginRuntimeContext(
            host_context=SimpleNamespace(),
            raw_config={},
            config=config,
            runtime_coordinator=SimpleNamespace(),
            host_bridge=SimpleNamespace(),
            background_task_budget=budget,
        )

        with patch("astrmai.app.runtime_context.build_infrastructure_settings", return_value=runtime.infrastructure_settings):
            runtime.rebuild_infrastructure_settings()

        self.assertEqual(budget.status()["limit"], 3)
        self.assertEqual(
            runtime.build_diagnostics()["infrastructure"]["background_task_budget"],
            {
                "limit": 3,
                "max_queue": 64,
                "wait_timeout_sec": 120.0,
                "active": 0,
                "available_slots": 3,
                "queued": 0,
                "peak_queued": 0,
                "rejected": 0,
                "timed_out": 0,
            },
        )

    def test_runtime_diagnostics_expose_vector_retrieval_status(self):
        runtime = PluginRuntimeContext(
            host_context=SimpleNamespace(),
            raw_config={},
            config=SimpleNamespace(),
            runtime_coordinator=SimpleNamespace(),
            host_bridge=SimpleNamespace(),
        )
        runtime.core.memory_engine = SimpleNamespace(
            vec_retriever=SimpleNamespace(
                describe_status=lambda: {
                    "active_queries": 1,
                    "degraded_ratio": 0.25,
                }
            )
        )

        status = runtime.build_diagnostics()["memory"]["vector_retrieval"]

        self.assertEqual(status["active_queries"], 1)
        self.assertEqual(status["degraded_ratio"], 0.25)

    def test_task_kind_records_duration_failure_and_execution_timeout(self):
        async def run():
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.1)

            async def slow_task():
                await asyncio.sleep(0.2)

            with self.assertRaises(BackgroundTaskExecutionTimeout):
                await budget.run(slow_task, task_name="embedding")

            status = budget.status()
            self.assertEqual(status["execution_timed_out_by_kind"], {"embedding": 1})
            self.assertEqual(status["completed_by_kind"], {"embedding": 1})
            self.assertIn("embedding", status["duration_ms_by_kind"])

        asyncio.run(run())

    def test_nested_budget_reuses_outer_lease(self):
        async def run():
            budget = BackgroundTaskBudget(1, wait_timeout_sec=0.01)
            started = asyncio.Event()

            async def inner():
                started.set()
                return "ok"

            async def outer():
                return await budget.run(inner, task_name="compaction")

            self.assertEqual(
                await budget.run(outer, task_name="attention.compaction"),
                "ok",
            )
            self.assertEqual(budget.status()["timed_out"], 0)

        asyncio.run(run())

    def test_deferred_timeout_keeps_physical_slot_until_work_finishes(self):
        async def run():
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)
            release = asyncio.Event()

            async def slow():
                await release.wait()

            task = asyncio.create_task(
                budget.run(
                    slow,
                    task_name="memory.vector_bootstrap",
                    defer_release_on_timeout=True,
                )
            )
            with self.assertRaises(BackgroundTaskExecutionTimeout):
                await task
            self.assertEqual(budget.status()["active"], 1)
            self.assertEqual(budget.status()["timed_out_but_running"], 1)
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(budget.status()["active"], 0)
            self.assertEqual(budget.status().get("timed_out_but_running", 0), 0)
            self.assertEqual(
                budget.status()["late_completed_by_kind"],
                {"memory.vector_bootstrap": 1},
            )

        asyncio.run(run())

    def test_factory_failure_and_future_do_not_leak_slot(self):
        async def run():
            budget = BackgroundTaskBudget(1)

            def fail_synchronously():
                raise RuntimeError("factory failed")

            with self.assertRaisesRegex(RuntimeError, "factory failed"):
                await budget.run(fail_synchronously, task_name="learning.triggered")
            self.assertEqual(budget.status()["active"], 0)
            self.assertEqual(budget.status()["failed_by_kind"], {"learning.triggered": 1})

            future = asyncio.get_running_loop().create_future()
            future.set_result("future")
            self.assertEqual(
                await budget.run(
                    lambda: future,
                    task_name="learning.triggered",
                    defer_release_on_timeout=True,
                ),
                "future",
            )
            self.assertEqual(budget.status()["active"], 0)

        asyncio.run(run())

    def test_child_task_cannot_reuse_released_lease(self):
        async def run():
            budget = BackgroundTaskBudget(1, max_queue=1, wait_timeout_sec=0.01)
            child_started = False
            child_result = None

            async def child_work():
                nonlocal child_started
                child_started = True

            async def child_runner():
                await asyncio.sleep(0.02)
                nonlocal child_result
                try:
                    await budget.run(child_work, task_name="child")
                    child_result = "ran"
                except BackgroundTaskQueueTimeout:
                    child_result = "queued_timeout"

            async def outer():
                return asyncio.create_task(child_runner())

            child_task = await budget.run(outer, task_name="outer")
            release = asyncio.Event()
            blocker = asyncio.create_task(budget.run(lambda: release.wait(), task_name="blocker"))
            await child_task
            self.assertEqual(child_result, "queued_timeout")
            self.assertFalse(child_started)
            release.set()
            await blocker

        asyncio.run(run())

    def test_drain_cancels_deferred_work_and_releases_slot(self):
        async def run():
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)
            release = asyncio.Event()

            async def slow():
                await release.wait()

            with self.assertRaises(BackgroundTaskExecutionTimeout):
                await budget.run(
                    slow,
                    task_name="memory.vector_bootstrap",
                    defer_release_on_timeout=True,
                )
            report = await budget.drain(0.01)
            self.assertEqual(report["observed"], 1)
            self.assertEqual(report["remaining"], 1)
            self.assertEqual(budget.status()["active"], 1)
            self.assertFalse(budget.resume())
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(budget.status()["active"], 0)
            self.assertEqual(budget.status().get("deferred_tasks", 0), 0)
            self.assertTrue(budget.resume())

        asyncio.run(run())

    def test_deferred_work_keeps_lease_for_late_nested_budget_call(self):
        async def run():
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)
            nested_started = asyncio.Event()
            release = asyncio.Event()

            async def nested():
                nested_started.set()
                return "nested"

            async def slow_outer():
                await asyncio.sleep(0.03)
                self.assertEqual(await budget.run(nested, task_name="nested"), "nested")
                await release.wait()

            with self.assertRaises(BackgroundTaskExecutionTimeout):
                await budget.run(
                    slow_outer,
                    task_name="outer",
                    defer_release_on_timeout=True,
                )
            await asyncio.wait_for(nested_started.wait(), timeout=0.2)
            self.assertEqual(budget.status()["timed_out"], 0)
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(budget.status()["active"], 0)

        asyncio.run(run())

    def test_begin_drain_then_producer_cancel_is_observed_by_drain(self):
        async def run():
            budget = BackgroundTaskBudget(1, execution_timeout_sec=1.0)
            release = asyncio.Event()

            async def work():
                await release.wait()

            wrapper = asyncio.create_task(
                budget.run(
                    work,
                    task_name="producer",
                    defer_release_on_timeout=True,
                )
            )
            await asyncio.sleep(0)
            budget.begin_drain()
            wrapper.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await wrapper
            report = await budget.drain(0.01)
            self.assertEqual(report["observed"], 1)
            self.assertEqual(report["remaining"], 1)
            self.assertEqual(budget.status()["active"], 1)
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertEqual(budget.status()["active"], 0)

        asyncio.run(run())

    def test_drain_does_not_release_slot_for_running_to_thread(self):
        async def run():
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)
            entered = threading.Event()
            release = threading.Event()

            def blocking_work():
                entered.set()
                release.wait(1.0)

            async def work():
                await asyncio.to_thread(blocking_work)

            with self.assertRaises(BackgroundTaskExecutionTimeout):
                await budget.run(
                    work,
                    task_name="threaded",
                    defer_release_on_timeout=True,
                )
            self.assertTrue(entered.wait(0.2))
            report = await budget.drain(0.01)
            self.assertEqual(report["remaining"], 1)
            self.assertEqual(budget.status()["active"], 1)
            self.assertFalse(budget.resume())
            release.set()
            for _ in range(20):
                if budget.status()["active"] == 0:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(budget.status()["active"], 0)
            self.assertTrue(budget.resume())

        asyncio.run(run())

    def test_drain_observes_active_owner_and_rejects_waiter(self):
        async def run():
            budget = BackgroundTaskBudget(1, max_queue=1)
            release = asyncio.Event()
            waiter_called = False

            async def running():
                await release.wait()

            async def queued():
                nonlocal waiter_called
                waiter_called = True

            running_task = asyncio.create_task(budget.run(running, task_name="running"))
            await asyncio.sleep(0)
            queued_task = asyncio.create_task(budget.run(queued, task_name="queued"))
            await asyncio.sleep(0)
            budget.begin_drain()
            report = await budget.drain(0.01)
            self.assertGreaterEqual(report["observed"], 1)
            self.assertEqual(report["remaining"], 1)
            self.assertEqual(budget.status()["active"], 1)
            with self.assertRaises(asyncio.CancelledError):
                await queued_task
            self.assertFalse(waiter_called)
            release.set()
            await running_task
            final_report = await budget.drain(0.01)
            self.assertEqual(final_report["remaining"], 0)

        asyncio.run(run())

    def test_drain_observed_resets_after_successful_resume(self):
        async def run():
            budget = BackgroundTaskBudget(1, max_queue=1)
            release = asyncio.Event()

            running_task = asyncio.create_task(
                budget.run(lambda: release.wait(), task_name="running")
            )
            await asyncio.sleep(0)
            queued_task = asyncio.create_task(
                budget.run(lambda: asyncio.sleep(0), task_name="queued")
            )
            await asyncio.sleep(0)

            budget.begin_drain()
            release.set()
            await running_task
            with self.assertRaises(asyncio.CancelledError):
                await queued_task
            first = await budget.drain(0.01)
            self.assertEqual(first["observed"], 1)
            self.assertEqual(first["remaining"], 0)
            self.assertTrue(budget.resume())

            second = await budget.drain(0.01)
            self.assertEqual(second, {"observed": 0, "remaining": 0})

        asyncio.run(run())

    def test_duration_summary_uses_conservative_small_sample_p95(self):
        self.assertEqual(BackgroundTaskBudget._duration_summary([0.0, 141.0])["p95"], 141.0)


if __name__ == "__main__":
    unittest.main()
