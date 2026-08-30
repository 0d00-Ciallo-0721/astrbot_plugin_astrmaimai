import asyncio
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class CrossInstanceReloadFenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self._temp_dir.name)

    def tearDown(self):
        self._temp_dir.cleanup()

    async def test_new_registration_waits_for_previous_termination(self):
        from astrmai.app.runtime_instance_coordinator import RuntimeInstanceCoordinator

        coordinator = RuntimeInstanceCoordinator()
        old_started = asyncio.Event()
        release_old = asyncio.Event()
        order = []

        class Facade:
            async def terminate(self):
                old_started.set()
                await release_old.wait()
                order.append("old_terminated")

        old = Facade()
        coordinator.register_facade(old)
        new = Facade()
        registration = coordinator.register_facade(new)
        await old_started.wait()
        waited = asyncio.create_task(
            coordinator.wait_for_previous_termination(registration.previous_termination, timeout_sec=1.0)
        )
        await asyncio.sleep(0)
        self.assertFalse(waited.done())
        release_old.set()
        ok, reason = await waited
        self.assertTrue(ok, reason)
        self.assertEqual(order, ["old_terminated"])

    async def test_timeout_does_not_claim_shared_resource_owner(self):
        from astrmai.app.lifecycle import PluginLifecycleManager
        from astrmai.app.runtime_instance_coordinator import RuntimeInstanceCoordinator

        coordinator = RuntimeInstanceCoordinator()
        release_old = asyncio.Event()

        class Facade:
            async def terminate(self):
                await release_old.wait()

        old = Facade()
        coordinator.register_facade(old)
        new = Facade()
        registration = coordinator.register_facade(new)
        runtime = SimpleNamespace(
            runtime_previous_termination=registration.previous_termination,
            runtime_generation=registration.generation,
            runtime_facade=new,
            status=SimpleNamespace(
                accepting_events=True,
                is_running=True,
                lifecycle_started=True,
                startup_blocked_reason="",
                startup_retry_at=0.0,
            ),
            set_boot_phase=lambda phase: setattr(runtime, "boot_phase", phase),
            mark_degraded=lambda component, reason: setattr(runtime, "degraded", (component, reason)),
        )
        manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
        manager.runtime = runtime
        manager._shutdown_timing = lambda _name, default: 0.01

        self.assertFalse(await manager._await_runtime_reload_fence())
        self.assertEqual(runtime.status.startup_blocked_reason, "previous_facade_termination_pending")
        self.assertFalse(coordinator.can_use_shared_resources(new, registration.generation))
        release_old.set()
        ok, _ = await coordinator.wait_for_previous_termination(registration.previous_termination, timeout_sec=1.0)
        self.assertTrue(ok)

    async def test_old_completion_cannot_replace_new_active_facade(self):
        from astrmai.app.runtime_instance_coordinator import RuntimeInstanceCoordinator

        coordinator = RuntimeInstanceCoordinator()
        release_old = asyncio.Event()

        class Facade:
            async def terminate(self):
                await release_old.wait()

        old = Facade()
        coordinator.register_facade(old)
        new = Facade()
        registration = coordinator.register_facade(new)
        release_old.set()
        ok, _ = await coordinator.wait_for_previous_termination(registration.previous_termination, timeout_sec=1.0)
        self.assertTrue(ok)
        self.assertIs(coordinator.active_facade, new)

    async def test_termination_exception_is_consumed_and_reported(self):
        from astrmai.app.runtime_instance_coordinator import RuntimeInstanceCoordinator

        coordinator = RuntimeInstanceCoordinator()

        class Facade:
            async def terminate(self):
                raise RuntimeError("boom")

        old = Facade()
        coordinator.register_facade(old)
        new = Facade()
        registration = coordinator.register_facade(new)
        ok, reason = await coordinator.wait_for_previous_termination(registration.previous_termination, timeout_sec=1.0)
        self.assertFalse(ok)
        self.assertIn("RuntimeError", reason)
        await asyncio.sleep(0)
        report = coordinator.describe()
        self.assertTrue(report["terminations"])
        self.assertIn("RuntimeError", report["terminations"][0]["error"])

    async def test_dialogue_snapshot_requires_resource_lease_when_bound(self):
        from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore

        store = GroupDialogueStore(snapshot_dir=self._temp_dir.name)
        store.runtime_generation = 2
        store.runtime_resource_guard = lambda: False
        self.assertFalse(await store.persist_snapshot())
        self.assertFalse((store.snapshot_path()).exists())

    async def test_three_reload_generations_terminate_in_order(self):
        from astrmai.app.runtime_instance_coordinator import RuntimeInstanceCoordinator

        coordinator = RuntimeInstanceCoordinator()
        order = []

        class Facade:
            def __init__(self, name):
                self.name = name

            async def terminate(self):
                order.append(f"{self.name}:start")
                await asyncio.sleep(0)
                order.append(f"{self.name}:done")

        first = Facade("g1")
        coordinator.register_facade(first)
        second = Facade("g2")
        reg2 = coordinator.register_facade(second)
        third = Facade("g3")
        reg3 = coordinator.register_facade(third)
        ok, reason = await coordinator.wait_for_previous_termination(reg3.previous_termination, timeout_sec=1.0)
        self.assertTrue(ok, reason)
        self.assertEqual(order, ["g1:start", "g1:done", "g2:start", "g2:done"])
        self.assertTrue(coordinator.claim_resource_owner(third, reg3.generation))
