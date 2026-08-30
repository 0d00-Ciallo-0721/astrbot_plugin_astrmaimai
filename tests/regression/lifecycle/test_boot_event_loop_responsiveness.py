import asyncio
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class BootEventLoopResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self._temp_dir.name)

    def tearDown(self):
        self._temp_dir.cleanup()

    async def test_startup_checkpoint_yields_control(self):
        from astrmai.memory.services.memory_engine import MemoryEngine

        engine = MemoryEngine.__new__(MemoryEngine)
        engine._startup_last_yield = time.monotonic()
        engine._startup_yield_count = 0
        marker = []

        async def observer():
            await asyncio.sleep(0)
            marker.append("observed")

        task = asyncio.create_task(observer())
        await engine._startup_checkpoint(force=True)
        await task
        self.assertEqual(marker, ["observed"])
        self.assertEqual(engine._startup_yield_count, 1)

    async def test_vector_bootstrap_is_delayed_until_after_basic_boot(self):
        from astrmai.memory.services.memory_engine import MemoryEngine

        engine = MemoryEngine.__new__(MemoryEngine)
        engine._vector_bootstrap_delay_task = None
        engine._accepting_vector_work = True
        called = asyncio.Event()
        engine._schedule_vector_bootstrap = called.set
        engine.schedule_vector_bootstrap_after_startup(delay_sec=0.01)
        self.assertFalse(called.is_set())
        await asyncio.sleep(0.02)
        self.assertTrue(called.is_set())

    async def test_vector_bootstrap_delay_can_be_cancelled_before_start(self):
        from astrmai.memory.services.memory_engine import MemoryEngine

        engine = MemoryEngine.__new__(MemoryEngine)
        engine._vector_bootstrap_delay_task = None
        engine._accepting_vector_work = True
        called = asyncio.Event()
        engine._schedule_vector_bootstrap = called.set
        engine.schedule_vector_bootstrap_after_startup(delay_sec=1.0)
        task = engine._vector_bootstrap_delay_task
        self.assertIsNotNone(task)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(called.is_set())

    async def test_startup_checkpoint_propagates_cancellation(self):
        from astrmai.memory.services.memory_engine import MemoryEngine

        engine = MemoryEngine.__new__(MemoryEngine)
        engine.STARTUP_YIELD_SEC = 1.0
        engine._startup_last_yield = time.monotonic()
        engine._startup_yield_count = 0
        task = asyncio.create_task(engine._startup_checkpoint(force=True))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_projector_rebuild_yields_between_batches(self):
        from astrmai.memory.services.memory_index_projector import MemoryIndexProjector

        yielded = asyncio.Event()

        class Store:
            async def list_projectable(self):
                return [SimpleNamespace(id=str(i)) for i in range(4)]

        class Engine:
            STARTUP_BATCH_SIZE = 2
            v2_store = Store()
            _projection_rebuild_active = False
            _is_ready = True
            _vector_state = "ready"

            async def _ensure_faiss_initialized(self):
                return True

        projector = MemoryIndexProjector(Engine())
        async def clear():
            return None
        async def project(_memory_id):
            return True
        projector._clear_projected_documents = clear
        projector._project_locked = project

        original_sleep = asyncio.sleep
        async def tracking_sleep(delay):
            if delay == 0.001:
                yielded.set()
            await original_sleep(delay)

        asyncio.sleep = tracking_sleep
        try:
            self.assertEqual(await projector.rebuild_all(), 4)
        finally:
            asyncio.sleep = original_sleep
        self.assertTrue(yielded.is_set())

    async def test_projector_faiss_id_scan_runs_off_event_loop(self):
        from astrmai.memory.services.memory_index_projector import MemoryIndexProjector

        projector = MemoryIndexProjector(SimpleNamespace())
        calls = []

        def sync_scan():
            calls.append("scan")
            return {1, 2}

        projector._faiss_id_set_sync = sync_scan
        original_to_thread = asyncio.to_thread

        async def tracking_to_thread(func, *args, **kwargs):
            calls.append("thread")
            return await original_to_thread(func, *args, **kwargs)

        asyncio.to_thread = tracking_to_thread
        try:
            result = await projector._faiss_id_set()
        finally:
            asyncio.to_thread = original_to_thread
        self.assertEqual(result, {1, 2})
        self.assertEqual(calls, ["thread", "scan"])
