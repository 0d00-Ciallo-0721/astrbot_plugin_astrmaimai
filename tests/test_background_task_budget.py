import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrmai.app.runtime_context import PluginRuntimeContext
from astrmai.infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
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


if __name__ == "__main__":
    unittest.main()
