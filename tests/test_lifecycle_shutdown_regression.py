import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _AsyncRecorder:
    def __init__(self, calls, name, *, fail=False):
        self.calls = calls
        self.name = name
        self.fail = fail

    async def __call__(self):
        self.calls.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


class _SyncRecorder:
    def __init__(self, calls, name, *, fail=False):
        self.calls = calls
        self.name = name
        self.fail = fail

    def __call__(self):
        self.calls.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} failed")


class PluginLifecycleShutdownRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _build_runtime(self, calls, *, failing_head=False, failing_tail=False):
        from astrmai.app.runtime_context import (
            CoreServices,
            InteractionServices,
            LifecycleServices,
            PluginRuntimeContext,
            WorkModeServices,
        )

        runtime = PluginRuntimeContext(
            host_context=None,
            raw_config={},
            config=SimpleNamespace(),
            runtime_coordinator=SimpleNamespace(_states={"chat": object()}),
            host_bridge=None,
        )
        runtime.status.is_running = True
        runtime.status.lifecycle_started = True
        runtime.status.bootstrap_completed = True
        runtime.status.boot_logged = True
        runtime.status.work_mode_enabled = True
        runtime.status.memory_initialized = True
        runtime.status.proactive_started = True
        runtime.status.visual_started = True
        runtime.status.cron_guard_started = True
        runtime.status.foreign_commands_loaded = True

        runtime.core = CoreServices(
            memory_engine=SimpleNamespace(
                memory_pipeline=SimpleNamespace(
                    stop=_AsyncRecorder(calls, "memory_pipeline.stop", fail=failing_head)
                )
            ),
            event_bus=SimpleNamespace(stop=_AsyncRecorder(calls, "event_bus.stop", fail=failing_tail)),
            persistence=SimpleNamespace(dispose=_SyncRecorder(calls, "persistence.dispose", fail=failing_tail)),
            visual_cortex=SimpleNamespace(stop=_SyncRecorder(calls, "visual_cortex.stop")),
        )
        runtime.interaction = InteractionServices(
            private_chat_manager=SimpleNamespace(
                _persist_pending_sessions=_AsyncRecorder(calls, "private_chat.persist")
            )
        )
        runtime.lifecycle = LifecycleServices(
            proactive_task=SimpleNamespace(stop=_AsyncRecorder(calls, "proactive.stop")),
            expression_governance_runner=SimpleNamespace(stop=_AsyncRecorder(calls, "expression.stop")),
        )
        runtime.workmode = WorkModeServices(
            cron_guard=SimpleNamespace(stop=_SyncRecorder(calls, "cron_guard.stop"))
        )
        return runtime

    def test_terminate_runs_shutdown_order_and_resets_runtime_flags(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()
            await manager.terminate()

            self.assertEqual(
                calls[:8],
                [
                    "memory_pipeline.stop",
                    "private_chat.persist",
                    "proactive.stop",
                    "expression.stop",
                    "cron_guard.stop",
                    "visual_cortex.stop",
                    "event_bus.stop",
                    "persistence.dispose",
                ],
            )
            self.assertEqual(runtime.status.boot_phase, "shutdown.complete")
            self.assertFalse(runtime.status.is_running)
            self.assertFalse(runtime.status.lifecycle_started)
            self.assertFalse(runtime.status.bootstrap_completed)
            self.assertFalse(runtime.status.boot_logged)
            self.assertFalse(runtime.status.work_mode_enabled)
            self.assertFalse(runtime.status.memory_initialized)
            self.assertFalse(runtime.status.proactive_started)
            self.assertFalse(runtime.status.visual_started)
            self.assertFalse(runtime.status.cron_guard_started)
            self.assertFalse(runtime.status.foreign_commands_loaded)
            self.assertEqual(runtime.runtime_coordinator._states, {})

            asyncio.run(_run())

    def test_persona_core_initialization_retries_before_becoming_ready(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.app.runtime_context import RuntimeStatus

            attempts = []

            class _Summarizer:
                REQUIRED_SHARDS = ("logic_style",)

                def __init__(self):
                    self.pending_tasks = {}
                    self.cache = {}

                @staticmethod
                def _cache_key(persona_id, _session_id):
                    return persona_id

                async def ensure_core_ready(self, *_args, **_kwargs):
                    attempts.append("attempt")
                    if len(attempts) == 1:
                        raise RuntimeError("temporary persona timeout")
                    return {
                        "core_ready": True,
                        "is_full_ready": True,
                        "self_lore_ready": True,
                        "shard_status": {"logic_style": "completed"},
                    }

            status = RuntimeStatus()
            runtime = SimpleNamespace(
                background_tasks=set(),
                lifecycle=SimpleNamespace(manager=None),
                status=status,
                config=SimpleNamespace(
                    persona=SimpleNamespace(retry_interval_sec=1, retry_max_interval_sec=1)
                ),
                context_engine=SimpleNamespace(resolve_active_persona=lambda: ("persona", "raw prompt")),
                persona_summarizer=_Summarizer(),
                set_boot_phase=lambda phase: status.set_phase(phase),
                mark_degraded=lambda component, reason: status.mark_degraded(component, reason),
            )
            manager = PluginLifecycleManager(runtime)
            manager._persona_retry_bounds = lambda: (0.0, 0.0)

            await manager._initialize_persona_core_until_ready()

            self.assertEqual(len(attempts), 2)
            self.assertTrue(status.persona_persisted)
            self.assertEqual(status.persona_state, "full_ready")
            self.assertEqual(status.persona_completed_shards, 1)

        asyncio.run(_run())

    def test_persona_startup_timeout_marks_not_ready_without_opening_ingress(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.app.runtime_context import RuntimeStatus

            status = RuntimeStatus()

            class _Summarizer:
                REQUIRED_SHARDS = ()
                pending_tasks = {}

                @staticmethod
                def _cache_key(persona_id, _session_id):
                    return persona_id

                async def ensure_core_ready(self, *_args, **_kwargs):
                    raise RuntimeError("provider unavailable")

            runtime = SimpleNamespace(
                background_tasks=set(),
                lifecycle=SimpleNamespace(manager=None),
                status=status,
                config=SimpleNamespace(
                    persona=SimpleNamespace(
                        retry_interval_sec=0.005,
                        retry_max_interval_sec=0.005,
                        startup_timeout_sec=0.02,
                    )
                ),
                context_engine=SimpleNamespace(resolve_active_persona=lambda: ("persona", "raw prompt")),
                persona_summarizer=_Summarizer(),
                set_boot_phase=status.set_phase,
                mark_degraded=status.mark_degraded,
            )
            manager = PluginLifecycleManager(runtime)
            manager._persona_retry_bounds = lambda: (0.005, 0.005)

            ready = await manager._initialize_persona_core_until_ready()
            return ready, status

        ready, status = asyncio.run(_run())
        self.assertFalse(ready)
        self.assertFalse(status.accepting_events)
        self.assertEqual(status.persona_state, "core_failed")
        self.assertEqual(status.startup_blocked_reason, "persona_startup_timeout")
        self.assertEqual(status.boot_phase, "lifecycle.persona_timeout")

    def test_persona_single_hanging_call_is_hard_timed_out(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.app.runtime_context import RuntimeStatus

            status = RuntimeStatus()
            cancelled = []

            class _Summarizer:
                REQUIRED_SHARDS = ()
                pending_tasks = {}

                @staticmethod
                def _cache_key(persona_id, _session_id):
                    return persona_id

                async def ensure_core_ready(self, *_args, **_kwargs):
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        cancelled.append(True)
                        raise

            runtime = SimpleNamespace(
                background_tasks=set(),
                lifecycle=SimpleNamespace(manager=None),
                status=status,
                config=SimpleNamespace(
                    persona=SimpleNamespace(
                        retry_interval_sec=0.01,
                        retry_max_interval_sec=0.01,
                        startup_timeout_sec=0.02,
                    )
                ),
                context_engine=SimpleNamespace(resolve_active_persona=lambda: ("persona", "raw prompt")),
                persona_summarizer=_Summarizer(),
                set_boot_phase=status.set_phase,
                mark_degraded=status.mark_degraded,
            )
            manager = PluginLifecycleManager(runtime)
            manager._persona_retry_bounds = lambda: (0.01, 0.01)
            started = asyncio.get_running_loop().time()
            ready = await manager._initialize_persona_core_until_ready()
            elapsed = asyncio.get_running_loop().time() - started
            return ready, elapsed, cancelled, status

        ready, elapsed, cancelled, status = asyncio.run(_run())
        self.assertFalse(ready)
        self.assertLess(elapsed, 0.2)
        self.assertTrue(cancelled)
        self.assertEqual(status.startup_blocked_reason, "persona_startup_timeout")
        self.assertFalse(status.accepting_events)

    def test_persona_retry_stops_when_shutdown_is_requested(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.app.runtime_context import RuntimeStatus

            attempts = []
            status = RuntimeStatus()

            class _Summarizer:
                REQUIRED_SHARDS = ()
                pending_tasks = {}

                @staticmethod
                def _cache_key(persona_id, _session_id):
                    return persona_id

                async def ensure_core_ready(self, *_args, **_kwargs):
                    attempts.append(True)
                    raise RuntimeError("provider unavailable")

            runtime = SimpleNamespace(
                background_tasks=set(),
                lifecycle=SimpleNamespace(manager=None),
                status=status,
                config=SimpleNamespace(persona=SimpleNamespace(retry_interval_sec=0.01, retry_max_interval_sec=0.01, startup_timeout_sec=0)),
                context_engine=SimpleNamespace(resolve_active_persona=lambda: ("persona", "raw prompt")),
                persona_summarizer=_Summarizer(),
                set_boot_phase=status.set_phase,
                mark_degraded=status.mark_degraded,
            )
            manager = PluginLifecycleManager(runtime)
            manager._persona_retry_bounds = lambda: (0.01, 0.01)
            task = asyncio.create_task(manager._initialize_persona_core_until_ready())
            await asyncio.sleep(0.015)
            manager._shutdown_requested = True
            result = await asyncio.wait_for(task, timeout=0.2)
            return result, len(attempts)

        result, attempts = asyncio.run(_run())
        self.assertFalse(result)
        self.assertEqual(attempts, 1)

    def test_budget_drain_starts_after_producers_stop_and_before_memory_close(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            async def stop_producers():
                calls.append("memory.producers.stop")

            async def close_resources():
                calls.append("memory.resources.close")

            runtime.memory_engine.stop_background_producers = stop_producers
            runtime.memory_engine.close_background_resources = close_resources

            class _Budget:
                def begin_drain(self):
                    calls.append("budget.begin_drain")

                async def drain(self, timeout_sec):
                    calls.append("budget.drain")
                    return {"observed": 0, "remaining": 0}

                def resume(self):
                    return True

            runtime.background_task_budget = _Budget()
            manager = PluginLifecycleManager(runtime)
            await manager._terminate_impl()

            self.assertLess(calls.index("budget.begin_drain"), calls.index("memory.producers.stop"))
            self.assertLess(calls.index("memory.producers.stop"), calls.index("budget.drain"))
            self.assertLess(calls.index("budget.drain"), calls.index("memory.resources.close"))

        asyncio.run(_run())

    def test_pending_budget_drain_defers_dependency_close_until_late_work_finishes(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget

            calls = []
            runtime = self._build_runtime(calls)
            release = asyncio.Event()
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)

            async def slow_work():
                await release.wait()

            async def close_resources():
                calls.append("memory.resources.close")

            runtime.memory_engine.close_background_resources = close_resources
            runtime.background_task_budget = budget
            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = asyncio.get_running_loop().time()

            with self.assertRaises(TimeoutError):
                await budget.run(
                    slow_work,
                    task_name="memory.vector_bootstrap",
                    defer_release_on_timeout=True,
                )

            await manager._terminate_impl()
            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            self.assertNotIn("memory.resources.close", calls)

            release.set()
            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await cleanup
            self.assertIn("memory.resources.close", calls)
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)
            self.assertFalse(manager._shutdown_pending_drain)
            self.assertTrue(manager._termination_complete)

        asyncio.run(_run())

    def test_forced_shutdown_tail_defers_dependencies_when_budget_is_active(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget

            calls = []
            runtime = self._build_runtime(calls)
            release = asyncio.Event()
            budget = BackgroundTaskBudget(1)
            runtime.background_task_budget = budget

            async def slow_work():
                await release.wait()

            task = asyncio.create_task(budget.run(slow_work, task_name="slow"))
            await asyncio.sleep(0)
            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = asyncio.get_running_loop().time()
            manager._force_shutdown_tail()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            release.set()
            await task
            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await cleanup
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)

        asyncio.run(_run())

    def test_reinitialize_waits_for_lifecycle_late_cleanup(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = SimpleNamespace(
                background_task_budget=SimpleNamespace(
                    resume=lambda: calls.append("resume") or True,
                ),
                event_bus=SimpleNamespace(reset_abort=lambda: calls.append("reset_abort")),
                runtime_coordinator=SimpleNamespace(reopen=lambda: calls.append("reopen")),
                persona_summarizer=SimpleNamespace(reopen=lambda: calls.append("persona")),
                cron_guard=SimpleNamespace(start=lambda: calls.append("cron")),
                state_engine=SimpleNamespace(),
                memory_engine=SimpleNamespace(),
            )
            manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
            manager.runtime = runtime
            manager._shutdown_pending_drain = False
            manager._late_shutdown_cleanup_task = asyncio.create_task(asyncio.sleep(60))

            self.assertFalse(await manager._prepare_reinitialize())
            self.assertEqual(calls, [])
            manager._late_shutdown_cleanup_task.cancel()
            await asyncio.gather(manager._late_shutdown_cleanup_task, return_exceptions=True)

        asyncio.run(_run())

    def test_late_physical_drain_budget_enters_explicit_degraded_state(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget

            calls = []
            runtime = self._build_runtime(calls)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.01)
            )
            release = asyncio.Event()
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)
            runtime.background_task_budget = budget

            async def slow_work():
                await release.wait()

            manager = PluginLifecycleManager(runtime)
            with self.assertRaises(TimeoutError):
                await budget.run(
                    slow_work,
                    task_name="memory.vector_delete",
                    defer_release_on_timeout=True,
                )

            manager._shutdown_started_monotonic = asyncio.get_running_loop().time()
            manager._shutdown_pending_drain = True
            manager._schedule_late_shutdown_cleanup(budget)
            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await cleanup

            self.assertEqual(runtime.status.boot_phase, "shutdown.degraded")
            self.assertEqual(runtime.status.shutdown_final_status, "degraded")
            self.assertTrue(runtime.status.shutdown_pending_drain)
            self.assertTrue(runtime.status.shutdown_forced_termination_risk)
            self.assertIn("shutdown.late_cleanup", runtime.status.degraded_components)
            self.assertNotIn("event_bus.stop", calls)
            release.set()
            await budget.wait_until_idle(0.5)

        asyncio.run(_run())

    def test_startup_is_idempotent_after_runtime_becomes_ready(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.app.runtime_context import RuntimeStatus

            status = RuntimeStatus()
            runtime = SimpleNamespace(
                background_tasks=set(),
                lifecycle=SimpleNamespace(manager=None),
                status=status,
                set_boot_phase=lambda phase: status.set_phase(phase),
            )
            manager = PluginLifecycleManager(runtime)
            calls = []

            async def _complete_startup():
                calls.append("startup")
                status.is_running = True
                status.lifecycle_started = True

            manager._complete_startup = _complete_startup

            await manager.on_program_start()
            await manager._startup_task
            await manager.on_program_start()

            self.assertEqual(calls, ["startup"])

        asyncio.run(_run())

    def test_startup_is_idempotent_while_initialization_is_in_progress(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.app.runtime_context import RuntimeStatus

            status = RuntimeStatus()
            runtime = SimpleNamespace(
                background_tasks=set(),
                lifecycle=SimpleNamespace(manager=None),
                status=status,
                set_boot_phase=lambda phase: status.set_phase(phase),
            )
            manager = PluginLifecycleManager(runtime)
            release = asyncio.Event()
            calls = []

            async def _complete_startup():
                calls.append("startup")
                await release.wait()

            manager._complete_startup = _complete_startup

            await manager.on_program_start()
            first_task = manager._startup_task
            await manager.on_program_start()
            await asyncio.sleep(0)

            self.assertIs(manager._startup_task, first_task)
            self.assertEqual(calls, ["startup"])

            release.set()
            await first_task

        asyncio.run(_run())

    def test_terminated_lifecycle_cannot_be_restarted_by_late_hook(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()
            await manager.on_program_start()

            self.assertIsNone(manager._startup_task)
            self.assertFalse(runtime.status.lifecycle_started)

        asyncio.run(_run())

    def test_terminate_cancels_tracked_background_tasks(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            manager = PluginLifecycleManager(runtime)
            manager.SHUTDOWN_TASK_TIMEOUT = 0.1

            async def _sleep_forever():
                await asyncio.sleep(60)

            task = asyncio.create_task(_sleep_forever())
            runtime.background_tasks.add(task)

            await manager._terminate_impl()

            self.assertTrue(task.cancelled())
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)

        asyncio.run(_run())

    def test_terminate_continues_when_tail_shutdown_components_fail(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls, failing_tail=True)
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()

            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)
            self.assertEqual(runtime.status.boot_phase, "shutdown.complete")
            self.assertFalse(runtime.status.lifecycle_started)

        asyncio.run(_run())

    def test_terminate_continues_when_head_shutdown_component_fails(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls, failing_head=True)
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()

            self.assertEqual(calls[0], "memory_pipeline.stop")
            self.assertIn("private_chat.persist", calls)
            self.assertIn("proactive.stop", calls)
            self.assertIn("expression.stop", calls)
            self.assertIn("cron_guard.stop", calls)
            self.assertIn("visual_cortex.stop", calls)
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)
            self.assertEqual(runtime.status.boot_phase, "shutdown.complete")
            self.assertFalse(runtime.status.lifecycle_started)

        asyncio.run(_run())

    def test_terminate_is_bounded_when_component_ignores_cancellation(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(
                    hot_reload_shutdown_budget_sec=0.05,
                    shutdown_component_timeout_sec=0.02,
                    shutdown_cancel_grace_sec=0.01,
                    shutdown_snapshot_timeout_sec=0.01,
                )
            )
            release = asyncio.Event()

            async def _stubborn_stop():
                calls.append("memory_pipeline.stop")
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    await release.wait()

            runtime.memory_engine.memory_pipeline.stop = _stubborn_stop
            manager = PluginLifecycleManager(runtime)

            started = asyncio.get_running_loop().time()
            await manager.terminate()
            elapsed = asyncio.get_running_loop().time() - started

            self.assertLess(elapsed, 0.3)
            self.assertGreaterEqual(runtime.status.shutdown_isolated_tasks, 1)
            self.assertEqual(
                runtime.status.shutdown_stage_stats["shutdown_sequence"]["status"],
                "isolated_timeout",
            )
            self.assertEqual(
                runtime.status.shutdown_stage_stats["forced_tail"]["status"],
                "completed",
            )
            await asyncio.sleep(0)
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)
            release.set()
            await asyncio.sleep(0.05)

        asyncio.run(_run())

    def test_shutdown_task_collection_includes_attention_session_workers(self):
        from astrmai.shared.helpers.plugin_helpers import collect_background_tasks

        background = object()
        session_worker = object()
        owner = SimpleNamespace(
            _background_tasks={background},
            _session_tasks={session_worker},
        )

        collected = collect_background_tasks(owner)

        self.assertEqual(set(collected), {background, session_worker})

    def test_workmode_heartbeat_starts_when_initial_reload_fails(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _CronGuard:
                async def reload_all_lost_jobs(self):
                    calls.append("reload")
                    raise RuntimeError("temporary db outage")

                async def run_heartbeat(self):
                    calls.append("heartbeat")

            runtime.workmode.cron_guard = _CronGuard()
            manager = PluginLifecycleManager(runtime)
            tracked = []

            def _track(coro):
                tracked.append(coro)
                coro.close()
                return SimpleNamespace()

            manager.track_task = _track
            await manager.start_workmode_guard()

            self.assertEqual(calls, ["reload"])
            self.assertEqual(len(tracked), 1)
            self.assertTrue(runtime.status.cron_guard_started)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
