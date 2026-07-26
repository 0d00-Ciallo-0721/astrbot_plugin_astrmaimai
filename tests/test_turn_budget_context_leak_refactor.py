"""OPT-02 回归测试：后台任务 contextvar 预算泄漏（RT-01，吸收 ML-01/PL-08）。

守护不变式：
1. asyncio.create_task 会复制 turn_telemetry_scope 设置的 contextvar（机制文档化）；
   detach_turn_telemetry() 必须能在后台任务内斩断继承。
2. EventBus 懒启动的常驻 worker 不得被创建时刻旧 turn 的 deadline 钳制。
3. MemoryTurnPipeline 的 per-chat worker 同上（线上实证 instant backfill 17/17 全败）。
4. rebind_turn_telemetry 可把长驻 worker 的 contextvar 重绑到新批次事件。
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.event_bus import EventBus
from astrmai.infrastructure.runtime.turn_call_ledger import (
    configure_turn_budget,
    detach_turn_telemetry,
    rebind_turn_telemetry,
    remaining_turn_budget,
    turn_telemetry_scope,
)
from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline


class _Event:
    def __init__(self, created_at_offset_sec: float = 0.0):
        self._extras = {}
        if created_at_offset_sec:
            self._extras["astrmai_turn_created_at"] = time.time() + created_at_offset_sec

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _exhausted_event() -> _Event:
    """构造一个预算早已耗尽的事件：created_at 在 400s 前、总预算 1s。"""
    event = _Event(created_at_offset_sec=-400.0)
    return event


class ContextVarInheritanceTests(unittest.TestCase):
    def test_create_task_inherits_scope_and_detach_clears(self):
        event = _exhausted_event()
        results = {}

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                self.assertEqual(remaining_turn_budget(None), 0.0)

                async def _inheriting_worker():
                    results["inherited"] = remaining_turn_budget(None)
                    detach_turn_telemetry()
                    results["after_detach"] = remaining_turn_budget(None)

                await asyncio.create_task(_inheriting_worker())
            results["outside_scope"] = remaining_turn_budget(None)

        asyncio.run(_run())

        # create_task 复制 contextvar：worker 天然继承陈旧 deadline（这就是泄漏机制）
        self.assertEqual(results["inherited"], 0.0)
        # detach 后 worker 不再受任何 turn 预算约束
        self.assertIsNone(results["after_detach"])
        # scope 外主任务不受影响
        self.assertIsNone(results["outside_scope"])

    def test_rebind_switches_context_to_new_event(self):
        stale = _exhausted_event()
        fresh = _Event()
        results = {}

        async def _run():
            with turn_telemetry_scope(stale):
                configure_turn_budget(stale, total_budget_sec=1.0, main_reply_reserve_sec=0.0)

                async def _worker():
                    results["before"] = remaining_turn_budget(None)
                    rebind_turn_telemetry(fresh)
                    configure_turn_budget(fresh, total_budget_sec=360.0, main_reply_reserve_sec=0.0)
                    results["after"] = remaining_turn_budget(None)

                await asyncio.create_task(_worker())

        asyncio.run(_run())

        self.assertEqual(results["before"], 0.0)
        self.assertGreater(results["after"], 300.0)


class EventBusWorkerDetachTests(unittest.TestCase):
    def setUp(self):
        EventBus._instance = None

    def tearDown(self):
        EventBus._instance = None

    def test_event_bus_worker_not_clamped_by_stale_turn_scope(self):
        event = _exhausted_event()
        results = {}
        done = asyncio.Event()

        async def _probe(data):
            results["remaining"] = remaining_turn_budget(None)
            done.set()

        async def _run():
            bus = EventBus()
            bus.subscribe("opt02_probe", _probe)
            try:
                with turn_telemetry_scope(event):
                    configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                    self.assertEqual(remaining_turn_budget(None), 0.0)
                    # 首次 publish 会在当前（预算耗尽的）turn 上下文里懒启动常驻 worker
                    await bus.publish("opt02_probe", {})
                    await asyncio.wait_for(done.wait(), timeout=3.0)
            finally:
                shutdown = getattr(bus, "shutdown", None)
                if callable(shutdown):
                    await shutdown()

        asyncio.run(_run())

        # worker 入口 detach 后，订阅回调不再看到陈旧 deadline
        self.assertIsNone(results["remaining"])


class MemoryChatWorkerDetachTests(unittest.TestCase):
    def test_chat_worker_detaches_inherited_scope(self):
        event = _exhausted_event()
        results = {}
        done = asyncio.Event()

        pipeline = MemoryTurnPipeline.__new__(MemoryTurnPipeline)
        pipeline._running = True

        async def _probe_observe(turn, *args, **kwargs):
            results.setdefault("remaining", remaining_turn_budget(None))
            done.set()

        pipeline._observe_turn = _probe_observe

        async def _run():
            queue: asyncio.Queue = asyncio.Queue()
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                self.assertEqual(remaining_turn_budget(None), 0.0)
                worker = asyncio.create_task(pipeline._chat_worker("chat-x", queue))
            await queue.put(SimpleNamespace(instant_gate_hit=True, chat_id="chat-x"))
            try:
                await asyncio.wait_for(done.wait(), timeout=3.0)
            finally:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

        asyncio.run(_run())

        # per-chat worker 入口 detach 后，backfill 等调用不再被陈旧预算钳死
        self.assertIsNone(results["remaining"])


if __name__ == "__main__":
    unittest.main()
