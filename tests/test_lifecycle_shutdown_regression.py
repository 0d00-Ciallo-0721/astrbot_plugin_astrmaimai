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


if __name__ == "__main__":
    unittest.main()
