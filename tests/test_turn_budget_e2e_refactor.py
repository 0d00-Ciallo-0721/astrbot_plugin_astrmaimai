"""OPT-07 回归测试：延迟预算统一（RT-04 / RT-05 / TG-03）。

守护不变式：
1. TG-03①: message_entry 预算接线来自 config.timing；接线异常必须以默认预算兜底
   （旧代码静默吞掉 → clamp 全变 no-op 且无人知晓）。
2. RT-04: gateway.tool（主回复/工具环）受 turn 预算约束；预算不足时以
   main_reply_reserve 兜底而非直接饿死。
3. RT-05: executor 视觉旁路的单图超时 = min(配置, turn 剩余预算)，预算耗尽即跳过；
   coordinator 暴露 vision_total_budget_sec 供 gate 合并循环持久化 burst deadline。
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator
from astrmai.conversation.execution.executor import ConcurrentExecutor
from astrmai.infrastructure.gateway.gateway_lane import GatewayLaneMixin
from astrmai.infrastructure.runtime.lane_storage import LaneStorageMixin
from astrmai.infrastructure.runtime.turn_call_ledger import (
    configure_turn_budget,
    turn_telemetry_scope,
    turn_telemetry_snapshot,
)
from astrmai.presentation.events.message_entry import _configure_turn_budget
from astrmai.state.mood.mood_manager import MoodManager


class _Event:
    def __init__(self, created_at_offset_sec: float = 0.0):
        self._extras = {}
        if created_at_offset_sec:
            self._extras["astrmai_turn_created_at"] = time.time() + created_at_offset_sec

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class MessageEntryBudgetWiringTests(unittest.TestCase):
    """TG-03①：预算接线与失败兜底。"""

    def test_budget_configured_from_config_timing(self):
        facade = SimpleNamespace(
            get_runtime_config=lambda: SimpleNamespace(
                timing=SimpleNamespace(turn_total_budget_sec=123.0, main_reply_reserve_sec=45.0)
            )
        )
        event = _Event()

        _configure_turn_budget(facade, event)

        budget = turn_telemetry_snapshot(event)["budget"]
        self.assertEqual(budget["total_budget_sec"], 123.0)
        self.assertEqual(budget["main_reply_reserve_sec"], 45.0)

    def test_wiring_failure_falls_back_to_defaults(self):
        def _boom():
            raise RuntimeError("config backend down")

        facade = SimpleNamespace(get_runtime_config=_boom)
        event = _Event()

        _configure_turn_budget(facade, event)

        budget = turn_telemetry_snapshot(event)["budget"]
        self.assertEqual(budget["total_budget_sec"], 360.0)
        self.assertEqual(budget["main_reply_reserve_sec"], 90.0)


class ToolLoopBudgetTests(unittest.TestCase):
    """RT-04：工具环总超时受预算约束 + 保留额兜底。"""

    def _lane(self, api_timeout=60.0):
        lane = GatewayLaneMixin.__new__(GatewayLaneMixin)
        lane._api_timeout = lambda: api_timeout
        return lane

    def test_no_turn_scope_keeps_base_timeout(self):
        self.assertAlmostEqual(self._lane()._tool_loop_total_timeout(90.0, 5), 90.0, places=3)

    def test_remaining_budget_caps_timeout(self):
        lane = self._lane()
        event = _Event(created_at_offset_sec=-160.0)

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=360.0, main_reply_reserve_sec=90.0)
                return lane._tool_loop_total_timeout(300.0, 5)

        effective = asyncio.run(_run())
        self.assertLess(effective, 210.0)
        self.assertGreater(effective, 190.0)

    def test_exhausted_budget_floors_at_reserve(self):
        lane = self._lane()
        event = _Event(created_at_offset_sec=-400.0)

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=100.0, main_reply_reserve_sec=90.0)
                return lane._tool_loop_total_timeout(300.0, 5)

        self.assertAlmostEqual(asyncio.run(_run()), 90.0, places=1)

    def test_exhausted_budget_without_reserve_fails_fast(self):
        lane = self._lane()
        event = _Event(created_at_offset_sec=-400.0)

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                return lane._tool_loop_total_timeout(300.0, 5)

        self.assertAlmostEqual(asyncio.run(_run()), 0.1, places=3)


class VisionSidePathBudgetTests(unittest.TestCase):
    """RT-05：视觉旁路超时接入预算；coordinator 暴露 burst 总额。"""

    def _executor(self):
        executor = ConcurrentExecutor.__new__(ConcurrentExecutor)
        executor.config = SimpleNamespace(timing=SimpleNamespace(image_analysis_timeout_sec=25.0))
        return executor

    def test_vision_timeout_uses_configured_without_scope(self):
        self.assertAlmostEqual(self._executor()._vision_side_path_timeout_override(), 25.0, places=3)

    def test_vision_timeout_uses_slow_model_default_without_config(self):
        executor = ConcurrentExecutor.__new__(ConcurrentExecutor)
        executor.config = SimpleNamespace(timing=SimpleNamespace())

        self.assertAlmostEqual(executor._vision_side_path_timeout_override(), 90.0, places=3)

    def test_vision_timeout_zero_when_budget_exhausted(self):
        executor = self._executor()
        event = _Event(created_at_offset_sec=-400.0)

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=1.0, main_reply_reserve_sec=0.0)
                return executor._vision_side_path_timeout_override()

        self.assertLessEqual(asyncio.run(_run()), 0.0)

    def test_coordinator_exposes_burst_budget(self):
        coordinator = PrivateTurnCoordinator.__new__(PrivateTurnCoordinator)
        coordinator._vision_total_timeout = lambda: 180.0

        self.assertAlmostEqual(coordinator.vision_total_budget_sec(), 180.0, places=3)

    def test_coordinator_burst_budget_is_capped_by_remaining_turn_budget(self):
        coordinator = PrivateTurnCoordinator.__new__(PrivateTurnCoordinator)
        coordinator.config = SimpleNamespace(
            timing=SimpleNamespace(vision_barrier_total_timeout_sec=300.0)
        )
        event = _Event()

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=100.0, main_reply_reserve_sec=90.0)
                return coordinator.vision_total_budget_sec()

        effective = asyncio.run(_run())
        self.assertLessEqual(effective, 10.0)
        self.assertGreater(effective, 0.0)


class MoodAnalysisBudgetTests(unittest.TestCase):
    def test_mood_analysis_timeout_is_capped_by_remaining_turn_budget(self):
        manager = MoodManager.__new__(MoodManager)
        manager.config = SimpleNamespace(timing=SimpleNamespace(mood_analysis_timeout_sec=30.0))
        event = _Event()

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=100.0, main_reply_reserve_sec=90.0)
                return manager._analysis_timeout_seconds()

        effective = asyncio.run(_run())
        self.assertLessEqual(effective, 10.0)
        self.assertGreater(effective, 0.0)


class LaneStorageBudgetTests(unittest.TestCase):
    def test_transcript_lane_prepare_timeout_is_capped_by_remaining_turn_budget(self):
        storage = LaneStorageMixin.__new__(LaneStorageMixin)
        storage.config = SimpleNamespace(timing=SimpleNamespace(lane_prepare_timeout_sec=20.0))
        event = _Event()

        async def _run():
            with turn_telemetry_scope(event):
                configure_turn_budget(event, total_budget_sec=100.0, main_reply_reserve_sec=90.0)
                return storage._lane_timeout("lane_prepare_timeout_sec", 20.0)

        effective = asyncio.run(_run())
        self.assertLessEqual(effective, 10.0)
        self.assertGreater(effective, 0.0)


if __name__ == "__main__":
    unittest.main()
