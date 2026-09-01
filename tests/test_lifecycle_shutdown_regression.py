import asyncio
import tempfile
import threading
import time
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
                _vector_registry_lock=threading.RLock(),
                memory_pipeline=SimpleNamespace(
                    stop=_AsyncRecorder(calls, "memory_pipeline.stop", fail=failing_head),
                    describe_runtime_status=lambda: {},
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

    def test_begin_shutdown_fences_memory_pipeline_immediately(self):
        calls = []
        runtime = self._build_runtime(calls)
        runtime.memory_engine.memory_pipeline.begin_shutdown = lambda: calls.append("memory_pipeline.begin_shutdown")
        from astrmai.app.lifecycle import PluginLifecycleManager

        manager = PluginLifecycleManager(runtime)
        manager.begin_shutdown()
        self.assertIn("memory_pipeline.begin_shutdown", calls)
        self.assertFalse(runtime.status.accepting_events)

    def test_shutdown_stage_task_is_registered_with_terminal_status(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            runtime = self._build_runtime([])
            runtime.runtime_generation = 17
            manager = PluginLifecycleManager(runtime)

            completed = await manager._run_bounded_shutdown_stage(
                "owner-test",
                lambda: asyncio.sleep(0),
                deadline=time.monotonic() + 1.0,
            )
            await asyncio.sleep(0)

            self.assertTrue(completed)
            record = next(
                item
                for item in runtime.owner_registry.describe()["tasks"]
                if item["task_family"] == "lifecycle.shutdown.stage"
            )
            self.assertEqual(record["scope_id"], "owner-test")
            self.assertEqual(record["generation"], 17)
            self.assertTrue(record["run_id"])
            self.assertEqual(record["status"], "succeeded")

        asyncio.run(_run())

    def test_begin_shutdown_fences_memory_index_projector_immediately(self):
        calls = []
        runtime = self._build_runtime(calls)
        runtime.memory_engine.index_projector = SimpleNamespace(
            begin_shutdown=lambda: calls.append("index_projector.begin_shutdown")
        )
        from astrmai.app.lifecycle import PluginLifecycleManager

        manager = PluginLifecycleManager(runtime)
        manager.begin_shutdown()

        self.assertIn("index_projector.begin_shutdown", calls)

    def test_shutdown_pending_report_includes_vector_close_owner(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            close_owner = asyncio.create_task(asyncio.Event().wait(), name="vector-close-owner")
            runtime.memory_engine._vector_close_tasks = {1: (object(), close_owner)}
            manager = PluginLifecycleManager(runtime)
            report = manager._shutdown_pending_report(None)
            close_owner.cancel()
            await asyncio.gather(close_owner, return_exceptions=True)
            return report

        report = asyncio.run(_run())

        self.assertEqual(report["vector_close_owner_count"], 1)
        self.assertEqual(report["remaining_by_kind"]["memory.vector_close_owner"], 1)
        self.assertEqual(report["remaining_by_category_total"], report["remaining_total"])
        self.assertIn("vector-close-owner", report["owner_task_names"])

    def test_legacy_vector_owner_snapshot_without_registry_lock_fails_closed(self):
        from astrmai.app.lifecycle import PluginLifecycleManager

        runtime = self._build_runtime([])
        del runtime.memory_engine._vector_registry_lock
        runtime.memory_engine._vector_close_tasks = {}
        manager = PluginLifecycleManager(runtime)

        report = manager._shutdown_pending_report(None)

        self.assertGreaterEqual(report["remaining"], 1)
        self.assertIn("memory.vector_owners", report["unknown_components"])
        self.assertEqual(
            report["remaining_by_kind"]["memory.vector_owners.state_unknown"],
            1,
        )

    def test_shutdown_pending_report_includes_vector_physical_futures(self):
        from concurrent.futures import Future
        from astrmai.app.lifecycle import PluginLifecycleManager

        calls = []
        runtime = self._build_runtime(calls)
        candidate_future = Future()
        retirement_future = Future()
        runtime.memory_engine._vector_candidate_futures = {candidate_future}
        runtime.memory_engine._vector_sync_retirement_futures = {retirement_future}
        manager = PluginLifecycleManager(runtime)

        report = manager._shutdown_pending_report(None)

        self.assertEqual(report["vector_candidate_physical_count"], 1)
        self.assertEqual(report["vector_sync_retirement_count"], 1)
        self.assertEqual(report["remaining_by_kind"]["memory.vector_candidate_physical"], 1)
        self.assertEqual(report["remaining_by_kind"]["memory.vector_sync_retirement"], 1)
        self.assertIn("astrmai-vector-candidate-physical", report["owner_task_names"])
        self.assertIn("astrmai-vector-sync-retirement", report["owner_task_names"])

    def test_shutdown_pending_report_includes_candidate_paths_and_isolated_tasks(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            runtime = self._build_runtime([])
            runtime.memory_engine._vector_candidate_paths = {"candidate.index"}
            manager = PluginLifecycleManager(runtime)
            isolated = asyncio.create_task(asyncio.Event().wait(), name="isolated-owner")
            manager._isolated_shutdown_tasks.add(isolated)
            report = manager._shutdown_pending_report(None)
            isolated.cancel()
            await asyncio.gather(isolated, return_exceptions=True)
            return report

        report = asyncio.run(_run())

        self.assertEqual(report["vector_candidate_path_count"], 1)
        self.assertEqual(report["isolated_shutdown_task_count"], 1)
        self.assertEqual(report["remaining_by_kind"]["memory.vector_candidate_path"], 1)
        self.assertEqual(report["remaining_by_kind"]["shutdown.isolated_task"], 1)
        self.assertEqual(report["remaining_max_by_category"], report["remaining"])
        self.assertEqual(report["category_count_sum"], report["category_sum"])
        self.assertEqual(report["unique_owner_count_estimate"], report["unique_remaining_owner_count"])

    def test_shutdown_pending_report_distinguishes_category_overlap_from_owner_estimate(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            runtime = self._build_runtime([])
            manager = PluginLifecycleManager(runtime)
            shared_owner = asyncio.create_task(asyncio.Event().wait(), name="shared-close-owner")
            runtime.memory_engine._vector_close_tasks = {1: (object(), shared_owner)}
            manager._isolated_shutdown_tasks.add(shared_owner)
            report = manager._shutdown_pending_report(None)
            shared_owner.cancel()
            await asyncio.gather(shared_owner, return_exceptions=True)
            return report

        report = asyncio.run(_run())

        self.assertEqual(report["remaining_max_by_category"], 1)
        self.assertEqual(report["category_count_sum"], 2)
        self.assertEqual(report["unique_owner_count_estimate"], 1)

    def test_shutdown_pending_report_isolates_budget_status_failure(self):
        from astrmai.app.lifecycle import PluginLifecycleManager

        calls = []
        runtime = self._build_runtime(calls)
        manager = PluginLifecycleManager(runtime)

        class _Budget:
            def status(self):
                raise RuntimeError("status unavailable")

        report = manager._shutdown_pending_report(_Budget())

        self.assertIn("RuntimeError: status unavailable", report["diagnostics_error"])
        self.assertTrue(report["budget_state_unknown"])
        self.assertEqual(report["remaining_by_kind"]["background_budget.state_unknown"], 1)
        self.assertGreaterEqual(report["remaining"], 1)
        self.assertEqual(report["category_sum"], report["remaining_by_category_total"])
        self.assertLessEqual(report["unique_remaining_owner_count"], report["category_sum"])

    def test_shutdown_pending_report_treats_missing_budget_status_as_unknown(self):
        from astrmai.app.lifecycle import PluginLifecycleManager

        runtime = self._build_runtime([])
        manager = PluginLifecycleManager(runtime)

        report = manager._shutdown_pending_report(SimpleNamespace())

        self.assertTrue(report["budget_state_unknown"])
        self.assertGreaterEqual(report["remaining"], 1)
        self.assertIn("background_budget.state_unknown", report["remaining_by_kind"])

    def test_shutdown_pending_report_fails_closed_for_component_diagnostics(self):
        from astrmai.app.lifecycle import PluginLifecycleManager

        runtime = self._build_runtime([])

        def _raise():
            raise RuntimeError("diagnostics unavailable")

        runtime.interaction.attention_gate = SimpleNamespace(describe_status=_raise)
        runtime.memory_engine.index_projector = SimpleNamespace(describe_status=_raise)
        runtime.memory_engine.memory_pipeline = SimpleNamespace(
            describe_runtime_status=_raise,
        )
        runtime.memory_engine.vec_retriever = SimpleNamespace(describe_status=_raise)
        manager = PluginLifecycleManager(runtime)

        report = manager._shutdown_pending_report(None)

        self.assertGreaterEqual(report["remaining"], 1)
        self.assertEqual(
            set(report["unknown_components"]),
            {"attention", "memory.pipeline", "memory.projector", "memory.vector"},
        )
        for component in report["unknown_components"]:
            self.assertIn(f"{component}.state_unknown", report["remaining_by_kind"])

    def test_shutdown_pending_report_survives_malformed_diagnostic_payload(self):
        from astrmai.app.lifecycle import PluginLifecycleManager

        runtime = self._build_runtime([])
        runtime.background_task_budget = SimpleNamespace(
            status=lambda: {
                "active": "n/a",
                "queued": object(),
                "active_by_kind": {"memory.worker": "bad"},
                "queued_by_kind": ["invalid"],
                "owner_task_names": 123,
            }
        )
        runtime.interaction.attention_gate = SimpleNamespace(
            describe_status=lambda: {"worker_count": "bad"}
        )
        manager = PluginLifecycleManager(runtime)

        report = manager._shutdown_pending_report(runtime.background_task_budget)

        self.assertGreaterEqual(report["remaining"], 1)
        self.assertTrue(report["budget_state_unknown"])
        self.assertIn("background_budget", report["unknown_components"])
        self.assertIn("attention", report["unknown_components"])
        self.assertTrue(report["diagnostics_errors"])

    def test_shutdown_pending_report_fails_closed_when_vector_owner_snapshot_fails(self):
        from astrmai.app.lifecycle import PluginLifecycleManager

        runtime = self._build_runtime([])

        def _raise():
            raise RuntimeError("vector owner snapshot unavailable")

        runtime.memory_engine.describe_shutdown_owners = _raise
        manager = PluginLifecycleManager(runtime)

        report = manager._shutdown_pending_report(None)

        self.assertGreaterEqual(report["remaining"], 1)
        self.assertIn("memory.vector_owners", report["unknown_components"])
        self.assertEqual(
            report["remaining_by_kind"]["memory.vector_owners.state_unknown"],
            1,
        )

    def test_budget_status_failure_keeps_dependencies_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _Budget:
                def begin_drain(self):
                    calls.append("budget.begin_drain")

                async def drain(self, timeout_sec):
                    calls.append("budget.drain")
                    return {}

                async def wait_until_idle(self, timeout_sec):
                    return False

                def status(self):
                    raise RuntimeError("budget ledger unavailable")

            runtime.background_task_budget = _Budget()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.05)
            )
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertFalse(manager._termination_complete)
            self.assertNotEqual(runtime.status.boot_phase, "shutdown.complete")
            self.assertNotIn("memory.resources.close", calls)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_budget_drain_exception_keeps_dependencies_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _Budget:
                def begin_drain(self):
                    calls.append("budget.begin_drain")

                async def drain(self, timeout_sec):
                    calls.append("budget.drain")
                    raise RuntimeError("drain failed")

                async def wait_until_idle(self, timeout_sec):
                    return False

                def status(self):
                    return {"active": 0, "queued": 0, "deferred": 0, "physical": 0}

            runtime.background_task_budget = _Budget()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.2)
            )
            manager = PluginLifecycleManager(runtime)

            await manager._terminate_impl()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            stage = runtime.status.shutdown_stage_stats["budget_drain"]
            self.assertEqual(stage["status"], "pending_drain")
            self.assertIn("RuntimeError: drain failed", stage["budget_drain_error"])
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_malformed_budget_drain_report_keeps_dependencies_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _Budget:
                def begin_drain(self):
                    calls.append("budget.begin_drain")

                async def drain(self, timeout_sec):
                    calls.append("budget.drain")
                    return {
                        "remaining": "not-a-count",
                        "active_by_kind": ["invalid"],
                    }

                async def wait_until_idle(self, timeout_sec):
                    return False

                def status(self):
                    return {"active": 0, "queued": 0, "deferred": 0, "physical": 0}

            runtime.background_task_budget = _Budget()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.2)
            )
            manager = PluginLifecycleManager(runtime)

            await manager._terminate_impl()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            stage = runtime.status.shutdown_stage_stats["budget_drain"]
            self.assertIn("invalid drain report", stage["budget_drain_error"])
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_malformed_nested_budget_drain_count_keeps_dependencies_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _Budget:
                def begin_drain(self):
                    calls.append("budget.begin_drain")

                async def drain(self, timeout_sec):
                    calls.append("budget.drain")
                    return {
                        "observed": 0,
                        "remaining": 0,
                        "active_by_kind": {"memory.worker": "bad"},
                    }

                async def wait_until_idle(self, timeout_sec):
                    return False

                def status(self):
                    return {"active": 0, "queued": 0, "deferred": 0, "physical": 0}

            runtime.background_task_budget = _Budget()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.2)
            )
            manager = PluginLifecycleManager(runtime)

            await manager._terminate_impl()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            stage = runtime.status.shutdown_stage_stats["budget_drain"]
            self.assertIn("active_by_kind.memory.worker", stage["budget_drain_error"])
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_budget_begin_drain_exception_keeps_dependencies_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _Budget:
                def begin_drain(self):
                    calls.append("budget.begin_drain")
                    raise RuntimeError("admission fence failed")

                async def drain(self, timeout_sec):
                    calls.append("budget.drain")
                    return {"observed": 0, "remaining": 0}

                async def wait_until_idle(self, timeout_sec):
                    return False

                def status(self):
                    return {"active": 0, "queued": 0, "deferred": 0, "physical": 0}

            runtime.background_task_budget = _Budget()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.05)
            )
            manager = PluginLifecycleManager(runtime)

            await manager._terminate_impl()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            self.assertIn(
                "background_budget.begin_drain",
                runtime.status.shutdown_stage_stats["budget_drain"]["shutdown_fence_errors"],
            )
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_event_bus_pending_stop_task_keeps_persistence_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _EventBus:
                async def stop(self):
                    calls.append("event_bus.stop")

                def describe_status(self):
                    return {"background_task_count": 0, "pending_stop_task_count": 1}

            runtime.core.event_bus = _EventBus()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.05)
            )
            manager = PluginLifecycleManager(runtime)

            await manager._terminate_impl()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertNotIn("persistence.dispose", calls)
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_blocking_persistence_dispose_is_bounded_and_remains_pending(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            release = threading.Event()

            def _dispose():
                calls.append("persistence.dispose")
                release.wait(timeout=1.0)

            runtime.core.persistence = SimpleNamespace(dispose=_dispose)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.05)
            )
            manager = PluginLifecycleManager(runtime)
            manager.SHUTDOWN_TASK_TIMEOUT = 0.05
            started = time.monotonic()

            await manager._terminate_impl()

            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(manager._shutdown_pending_drain)
            self.assertIn("persistence.dispose", calls)
            release.set()
            dispose_task = manager._persistence_dispose_task
            if dispose_task is not None:
                await asyncio.wait_for(asyncio.shield(dispose_task), timeout=1.0)
            cleanup = manager._late_shutdown_cleanup_task
            if cleanup is not None:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_late_memory_close_false_retries_until_success(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            attempts = 0

            async def close_resources():
                nonlocal attempts
                attempts += 1
                return attempts >= 2

            class _Budget:
                async def wait_until_idle(self, timeout_sec):
                    return True

                def status(self):
                    return {"active": 0, "queued": 0, "deferred": 0, "physical": 0}

            runtime.memory_engine.close_background_resources = close_resources
            runtime.background_task_budget = _Budget()
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.2)
            )
            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = time.monotonic()
            manager._shutdown_pending_drain = True
            manager._schedule_late_shutdown_cleanup(runtime.background_task_budget)

            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=1.0)

            self.assertEqual(attempts, 2)
            self.assertTrue(manager._termination_complete)
            self.assertEqual(runtime.status.boot_phase, "shutdown.complete")

        asyncio.run(_run())

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

                def status(self):
                    return {"active": 0, "queued": 0, "deferred": 0, "physical": 0}

                def resume(self):
                    return True

            runtime.background_task_budget = _Budget()
            manager = PluginLifecycleManager(runtime)
            await manager._terminate_impl()

            self.assertLess(calls.index("budget.begin_drain"), calls.index("memory.producers.stop"))
            self.assertLess(calls.index("memory.producers.stop"), calls.index("budget.drain"))
            self.assertLess(calls.index("budget.drain"), calls.index("memory.resources.close"))

        asyncio.run(_run())

    def test_memory_close_exception_keeps_event_bus_and_persistence_open(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            async def close_resources():
                calls.append("memory.resources.close")
                raise RuntimeError("vector close failed")

            runtime.memory_engine.close_background_resources = close_resources
            manager = PluginLifecycleManager(runtime)
            await manager._terminate_impl()

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertTrue(runtime.status.shutdown_pending_drain)
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)

        asyncio.run(_run())

    def test_reinitialize_does_not_resume_dispatcher_when_budget_resume_fails(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []

            class _Budget:
                def can_resume(self):
                    calls.append("budget.can_resume")
                    return True

                def resume_if_idle(self):
                    calls.append("budget.resume")
                    return False

            runtime = SimpleNamespace(
                background_task_budget=_Budget(),
                reread_action_dispatcher=SimpleNamespace(
                    resume=lambda: calls.append("dispatcher.resume") or True,
                ),
            )
            manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
            manager.runtime = runtime
            manager._shutdown_dependency_close_errors = []
            manager._shutdown_pending_drain = False
            manager._late_shutdown_cleanup_task = None

            result = await manager._prepare_reinitialize()
            return result, calls

        result, calls = asyncio.run(_run())

        self.assertFalse(result)
        self.assertEqual(calls, ["budget.can_resume", "budget.resume"])

    def test_reinitialize_rolls_budget_back_when_dispatcher_resume_fails(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []

            class _Budget:
                def can_resume(self):
                    return True

                def resume_if_idle(self):
                    calls.append("budget.resume")
                    return True

                def begin_drain(self):
                    calls.append("budget.begin_drain")

            runtime = SimpleNamespace(
                background_task_budget=_Budget(),
                reread_action_dispatcher=SimpleNamespace(
                    resume=lambda: calls.append("dispatcher.resume") or False,
                ),
            )
            manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
            manager.runtime = runtime
            manager._shutdown_dependency_close_errors = []
            manager._shutdown_pending_drain = False
            manager._late_shutdown_cleanup_task = None
            return await manager._prepare_reinitialize(), calls

        result, calls = asyncio.run(_run())

        self.assertFalse(result)
        self.assertEqual(
            calls,
            ["budget.resume", "dispatcher.resume", "budget.begin_drain"],
        )

    def test_reinitialize_rolls_back_each_service_opened_before_late_failure(self):
        async def _run(failed_stage):
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []

            class _Budget:
                def can_resume(self):
                    return True

                def resume_if_idle(self):
                    calls.append("budget.resume")
                    return True

                def begin_drain(self):
                    calls.append("budget.begin_drain")

            class _Coordinator:
                async def reopen(self):
                    calls.append("coordinator.reopen")
                    if failed_stage == "coordinator":
                        raise RuntimeError("coordinator failed")

                async def shutdown(self, *, timeout_sec=1.0):
                    calls.append("coordinator.shutdown")

            class _Persona:
                def reopen(self):
                    calls.append("persona.reopen")
                    if failed_stage == "persona":
                        raise RuntimeError("persona failed")

                async def stop(self):
                    calls.append("persona.stop")

            class _Cron:
                def start(self):
                    calls.append("cron.start")
                    if failed_stage == "cron":
                        raise RuntimeError("cron failed")

                def stop(self):
                    calls.append("cron.stop")

            status = SimpleNamespace(
                accepting_events=True,
                startup_blocked_reason="",
                shutdown_stage_stats={},
            )
            runtime = SimpleNamespace(
                config=SimpleNamespace(),
                status=status,
                background_task_budget=_Budget(),
                reread_action_dispatcher=SimpleNamespace(
                    resume=lambda: calls.append("dispatcher.resume") or True,
                    shutdown=lambda **_kwargs: calls.append("dispatcher.shutdown"),
                ),
                event_bus=SimpleNamespace(
                    reset_abort=lambda: calls.append("event_bus.reset_abort"),
                    trigger_abort=lambda: calls.append("event_bus.trigger_abort"),
                ),
                runtime_coordinator=_Coordinator(),
                persona_summarizer=_Persona(),
                cron_guard=_Cron(),
                mark_degraded=lambda component, reason: calls.append(
                    f"degraded:{component}:{reason}"
                ),
            )
            manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
            manager.runtime = runtime
            manager._shutdown_dependency_close_errors = []
            manager._shutdown_pending_drain = False
            manager._late_shutdown_cleanup_task = None

            result = await manager._prepare_reinitialize()
            return result, calls, status

        for failed_stage in ("coordinator", "persona", "cron"):
            with self.subTest(failed_stage=failed_stage):
                result, calls, status = asyncio.run(_run(failed_stage))
                self.assertFalse(result)
                self.assertIn("budget.begin_drain", calls)
                self.assertIn("dispatcher.shutdown", calls)
                self.assertIn("event_bus.trigger_abort", calls)
                self.assertFalse(status.accepting_events)
                self.assertEqual(
                    status.shutdown_stage_stats["reinitialize"]["status"],
                    "rolled_back",
                )
                self.assertIn("coordinator.shutdown", calls)
                if failed_stage in {"persona", "cron"}:
                    self.assertIn("persona.stop", calls)
                if failed_stage == "cron":
                    self.assertIn("cron.stop", calls)

    def test_reinitialize_rolls_back_partial_learning_collaboration_bind(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []

            class _Budget:
                def can_resume(self):
                    return True

                def resume_if_idle(self):
                    calls.append("budget.resume")
                    return True

                def begin_drain(self):
                    calls.append("budget.begin_drain")

            class _EventBus:
                TOPIC_LEARNING_MESSAGE_RECORDED = "message"
                TOPIC_LEARNING_BOT_REPLY_RECORDED = "reply"
                TOPIC_LEARNING_MINING_COMPLETED = "mining"

                def reset_abort(self):
                    calls.append("event_bus.reset_abort")

                def trigger_abort(self):
                    calls.append("event_bus.trigger_abort")

                def subscribe(self, topic, _callback):
                    calls.append(f"subscribe:{topic}")
                    if topic == "reply":
                        raise RuntimeError("subscribe failed")

                def unsubscribe(self, topic, _callback):
                    calls.append(f"unsubscribe:{topic}")

            status = SimpleNamespace(
                accepting_events=True,
                startup_blocked_reason="",
                shutdown_stage_stats={},
            )
            runtime = SimpleNamespace(
                config=SimpleNamespace(),
                status=status,
                background_task_budget=_Budget(),
                reread_action_dispatcher=SimpleNamespace(
                    resume=lambda: calls.append("dispatcher.resume") or True,
                    shutdown=lambda **_kwargs: calls.append("dispatcher.shutdown"),
                ),
                event_bus=_EventBus(),
                state_engine=SimpleNamespace(
                    on_learning_message_recorded=lambda *_args: None,
                ),
                memory_engine=SimpleNamespace(
                    on_learning_bot_reply_recorded=lambda *_args: None,
                    on_learning_mining_completed=lambda *_args: None,
                ),
                mark_degraded=lambda component, reason: calls.append(
                    f"degraded:{component}:{reason}"
                ),
            )
            manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
            manager.runtime = runtime
            manager._shutdown_dependency_close_errors = []
            manager._shutdown_pending_drain = False
            manager._late_shutdown_cleanup_task = None

            result = await manager._prepare_reinitialize()
            return result, calls, status

        result, calls, status = asyncio.run(_run())
        self.assertFalse(result)
        self.assertIn("subscribe:message", calls)
        self.assertIn("unsubscribe:message", calls)
        self.assertIn("budget.begin_drain", calls)
        self.assertFalse(status.accepting_events)
        self.assertEqual(status.shutdown_stage_stats["reinitialize"]["status"], "rolled_back")

    def test_late_shutdown_watcher_retries_after_diagnostic_exception(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.01)
            )

            class _Budget:
                def __init__(self):
                    self.wait_calls = 0
                    self.active = 1

                def begin_drain(self):
                    return None

                async def wait_until_idle(self, timeout_sec):
                    self.wait_calls += 1
                    if self.wait_calls == 1:
                        await asyncio.sleep(0.02)
                        return False
                    if self.wait_calls == 2:
                        raise RuntimeError("diagnostics temporarily unavailable")
                    self.active = 0
                    return True

                def status(self):
                    return {
                        "active": self.active,
                        "queued": 0,
                        "deferred": 0,
                        "physical": 0,
                    }

            budget = _Budget()
            runtime.background_task_budget = budget
            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = time.monotonic()
            manager._shutdown_pending_drain = True
            manager._schedule_late_shutdown_cleanup(budget)

            initial_cleanup = manager._late_shutdown_cleanup_task
            await initial_cleanup
            watcher = manager._late_shutdown_cleanup_task
            self.assertIsNot(watcher, initial_cleanup)
            await asyncio.wait_for(asyncio.shield(watcher), timeout=1.0)
            return manager, runtime, calls, budget

        manager, runtime, calls, budget = asyncio.run(_run())

        self.assertGreaterEqual(budget.wait_calls, 3)
        self.assertIn("event_bus.stop", calls)
        self.assertIn("persistence.dispose", calls)
        self.assertTrue(manager._termination_complete)
        self.assertEqual(runtime.status.shutdown_final_status, "complete")
        self.assertEqual(
            runtime.status.shutdown_stage_stats["late_cleanup_watcher"]["status"],
            "retrying_after_error",
        )

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

    def test_two_physical_owners_are_tracked_and_watcher_finishes_after_deadline(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.infrastructure.runtime.background_task_budget import (
                BackgroundTaskBudget,
                BackgroundTaskExecutionTimeout,
                BackgroundTaskQueueFull,
            )

            calls = []
            runtime = self._build_runtime(calls)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.01)
            )
            budget = BackgroundTaskBudget(2, execution_timeout_sec=0.01)
            runtime.background_task_budget = budget
            releases = [asyncio.Event(), asyncio.Event()]
            entered = [threading.Event(), threading.Event()]

            async def physical(index):
                def blocking():
                    entered[index].set()
                    while not releases[index].is_set():
                        time.sleep(0.005)

                await asyncio.to_thread(blocking)

            for index, task_name in enumerate(("memory_projection", "proactive.profile")):
                with self.assertRaises(BackgroundTaskExecutionTimeout):
                    await budget.run(
                        lambda index=index: physical(index),
                        task_name=task_name,
                        defer_release_on_timeout=True,
                    )
                self.assertTrue(entered[index].wait(0.5))

            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = asyncio.get_running_loop().time()
            manager._shutdown_pending_drain = True
            budget.begin_drain()
            with self.assertRaises(BackgroundTaskQueueFull):
                await budget.run(lambda: asyncio.sleep(0), task_name="memory_projection")
            manager._schedule_late_shutdown_cleanup(budget)
            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await cleanup

            status = budget.status()
            self.assertEqual(status["physical_owner_count"], 2)
            self.assertEqual(
                status["deferred_by_kind"],
                {"memory_projection": 1, "proactive.profile": 1},
            )
            self.assertEqual(status["shutdown_rejected_by_kind"], {"memory_projection": 1})
            self.assertEqual(runtime.status.shutdown_final_status, "degraded")
            self.assertEqual(runtime.status.shutdown_late_cleanup_task_count, 1)
            self.assertTrue(runtime.status.shutdown_stage_stats["late_cleanup"]["remaining_by_kind"])

            for release in releases:
                release.set()
            watcher = manager._late_shutdown_cleanup_task
            await asyncio.wait_for(watcher, timeout=1.5)
            self.assertEqual(runtime.status.shutdown_final_status, "complete")
            self.assertEqual(runtime.status.shutdown_late_cleanup_task_count, 0)
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)

        asyncio.run(_run())

    def test_late_cleanup_cancellation_records_degraded_terminal_state(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.infrastructure.runtime.background_task_budget import (
                BackgroundTaskBudget,
                BackgroundTaskExecutionTimeout,
            )

            calls = []
            runtime = self._build_runtime(calls)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=1.0)
            )
            release = threading.Event()
            entered = threading.Event()
            budget = BackgroundTaskBudget(1, execution_timeout_sec=0.01)
            runtime.background_task_budget = budget

            async def physical():
                def blocking():
                    entered.set()
                    while not release.is_set():
                        time.sleep(0.005)

                await asyncio.to_thread(blocking)

            manager = PluginLifecycleManager(runtime)
            with self.assertRaises(BackgroundTaskExecutionTimeout):
                await budget.run(
                    physical,
                    task_name="memory_projection",
                    defer_release_on_timeout=True,
                )
            self.assertTrue(entered.wait(0.5))
            manager._shutdown_started_monotonic = asyncio.get_running_loop().time()
            manager._shutdown_pending_drain = True
            manager._schedule_late_shutdown_cleanup(budget)
            cleanup = manager._late_shutdown_cleanup_task
            await asyncio.sleep(0)
            cleanup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await cleanup
            self.assertEqual(runtime.status.shutdown_final_status, "degraded")
            self.assertTrue(runtime.status.shutdown_forced_termination_risk)
            self.assertTrue(runtime.status.shutdown_pending_drain)
            self.assertIn("shutdown.cleanup_cancelled", runtime.status.degraded_components)
            self.assertEqual(runtime.status.boot_phase, "shutdown.degraded")
            release.set()
            await asyncio.wait_for(budget.wait_until_idle(1.0), timeout=1.5)

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

    def test_runtime_coordinator_internal_type_error_is_not_retried(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls)

            class _Coordinator:
                def __init__(self):
                    self.calls = 0
                    self._states = {}

                async def shutdown(self, *, timeout_sec=1.0):
                    self.calls += 1
                    raise TypeError("internal coordinator failure")

            coordinator = _Coordinator()
            runtime.runtime_coordinator = coordinator
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()

            self.assertEqual(coordinator.calls, 1)
            self.assertEqual(
                runtime.status.shutdown_stage_stats["runtime_coordinator"]["status"],
                "failed",
            )

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
            self.assertEqual(runtime.status.boot_phase, "shutdown.degraded")
            self.assertEqual(runtime.status.shutdown_final_status, "degraded")
            self.assertTrue(runtime.status.shutdown_forced_termination_risk)
            self.assertIn("shutdown.dependency_close", runtime.status.degraded_components)
            self.assertFalse(await manager._prepare_reinitialize())
            self.assertFalse(runtime.status.lifecycle_started)

        asyncio.run(_run())

    def test_late_cleanup_dependency_failure_stays_degraded(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager
            from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget

            calls = []
            runtime = self._build_runtime(calls, failing_tail=True)
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.1)
            )
            runtime.background_task_budget = BackgroundTaskBudget(1)
            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = asyncio.get_running_loop().time()
            manager._shutdown_pending_drain = True
            manager._schedule_late_shutdown_cleanup(runtime.background_task_budget)

            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await cleanup

            self.assertEqual(runtime.status.boot_phase, "shutdown.degraded")
            self.assertEqual(runtime.status.shutdown_final_status, "degraded")
            self.assertTrue(runtime.status.shutdown_forced_termination_risk)
            self.assertFalse(manager._termination_complete)
            self.assertIn("shutdown.dependency_close", runtime.status.degraded_components)

        asyncio.run(_run())

    def test_unrelated_isolated_task_cancellation_does_not_mark_late_cleanup_cancelled(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            runtime = self._build_runtime([])
            manager = PluginLifecycleManager(runtime)
            late_cleanup = asyncio.create_task(asyncio.sleep(60), name="late-cleanup")
            unrelated = asyncio.create_task(asyncio.sleep(60), name="unrelated-cleanup")
            manager._late_shutdown_cleanup_task = late_cleanup
            manager._isolated_shutdown_tasks.update({late_cleanup, unrelated})
            runtime.status.shutdown_late_cleanup_task_count = 1
            unrelated.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await unrelated
            manager._consume_isolated_shutdown_task(unrelated)

            self.assertEqual(runtime.status.shutdown_late_cleanup_task_count, 1)
            self.assertNotIn("shutdown.cleanup_cancelled", runtime.status.degraded_components)
            late_cleanup.cancel()
            await asyncio.gather(late_cleanup, return_exceptions=True)

        asyncio.run(_run())

    def test_terminate_continues_when_head_shutdown_component_fails(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            calls = []
            runtime = self._build_runtime(calls, failing_head=True)
            manager = PluginLifecycleManager(runtime)

            await manager.terminate()

            self.assertEqual(calls[0], "proactive.stop")
            self.assertLess(calls.index("proactive.stop"), calls.index("memory_pipeline.stop"))
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
            await asyncio.sleep(0)
            self.assertTrue(manager._shutdown_pending_drain)
            self.assertFalse(manager._termination_complete)
            self.assertNotEqual(runtime.status.boot_phase, "shutdown.complete")
            self.assertNotIn("event_bus.stop", calls)
            self.assertNotIn("persistence.dispose", calls)
            release.set()
            cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(cleanup)
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=1.0)
            self.assertIn("event_bus.stop", calls)
            self.assertIn("persistence.dispose", calls)
            self.assertEqual(runtime.status.boot_phase, "shutdown.complete")
            self.assertTrue(manager._termination_complete)

        asyncio.run(_run())

    def test_late_reread_cleanup_does_not_override_budget_pending_state(self):
        async def _run():
            from astrmai.app.lifecycle import PluginLifecycleManager

            runtime = self._build_runtime([])
            runtime.config = SimpleNamespace(
                timing=SimpleNamespace(shutdown_late_physical_drain_budget_sec=0.05)
            )
            reread_release = asyncio.Event()

            class _Dispatcher:
                async def force_shutdown(self):
                    return False

                def describe_status(self):
                    return {"pending_dispatch_shutdown": True}

                async def shutdown(self):
                    await reread_release.wait()

            class _Budget:
                def begin_drain(self):
                    return None

                async def wait_until_idle(self, timeout_sec):
                    return False

                def status(self):
                    return {"active": 1, "active_by_kind": {"memory.worker": 1}}

            runtime.interaction.reread_action_dispatcher = _Dispatcher()
            runtime.background_task_budget = _Budget()
            manager = PluginLifecycleManager(runtime)
            manager._shutdown_started_monotonic = time.monotonic()

            await manager._force_shutdown_tail_async()
            reread_cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(reread_cleanup)
            reread_release.set()
            await reread_cleanup
            await asyncio.sleep(0)

            self.assertTrue(manager._shutdown_pending_drain)
            self.assertFalse(manager._termination_complete)
            self.assertNotEqual(runtime.status.boot_phase, "shutdown.complete")
            budget_cleanup = manager._late_shutdown_cleanup_task
            self.assertIsNotNone(budget_cleanup)
            self.assertIsNot(budget_cleanup, reread_cleanup)
            budget_cleanup.cancel()
            await asyncio.gather(budget_cleanup, return_exceptions=True)

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
