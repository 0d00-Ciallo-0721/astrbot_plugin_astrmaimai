"""G7 / RT-11 回归测试：关键路径并发配额。

线上证据：skipped 轮 judge 的 ledger elapsed 高达 51.7s 而 attempt 仅数秒——差值即
排队。关键路径（用户可见回复）与后台调用共用一把全局信号量(默认3)，高峰期被 ambient
judge/mood 占满后真实回复只能干等。

守护不变式：
1. **总并发上限不变**（原设计是 429 保护，绝不能因优先级而放大）。
2. 后台调用最多占 `max - reserved` 个槽，关键路径始终有保底名额，不会被饿死。
3. 获取顺序：后台先取子槽再取全局槽——反序会让后台攥着全局槽等子槽，反而堵死关键路径。
4. 边界：reserved=0 等同旧行为；reserved>=max 时自动收敛（至少给后台留 1 个槽，
   total=1 时无法预留）。
"""

import asyncio
import unittest
from types import SimpleNamespace

from astrmai.infrastructure.gateway.gateway_call import GatewayCallMixin
from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway


class _Limiter(GatewayCallMixin):
    """只装配并发原语的最小载体（不触碰真实网关的其余依赖）。"""

    def __init__(self, total: int, background: int | None):
        self._global_semaphore = asyncio.Semaphore(total)
        self._background_semaphore = asyncio.Semaphore(background) if background is not None else None


class SlotArithmeticTests(unittest.TestCase):
    def _gateway(self, total: int, reserved: int):
        gateway = GlobalModelGateway.__new__(GlobalModelGateway)
        gateway.settings = SimpleNamespace(
            max_concurrent_llm_calls=total,
            critical_path_reserved_slots=reserved,
        )
        return gateway

    def test_default_reserves_one_slot(self):
        gateway = self._gateway(total=3, reserved=1)
        self.assertEqual(gateway._reserved_critical_slots(), 1)
        self.assertEqual(gateway._background_limit(), 2)
        self.assertIsNotNone(gateway._build_background_semaphore())

    def test_zero_reserved_keeps_legacy_behavior(self):
        gateway = self._gateway(total=3, reserved=0)
        self.assertEqual(gateway._background_limit(), 3)
        self.assertIsNone(
            gateway._build_background_semaphore(),
            "不预留时不应引入额外信号量（保持旧路径）",
        )

    def test_reserved_cannot_starve_background(self):
        gateway = self._gateway(total=3, reserved=99)
        self.assertEqual(gateway._reserved_critical_slots(), 2)
        self.assertEqual(gateway._background_limit(), 1, "后台至少保留 1 个槽")

    def test_single_slot_total_cannot_reserve(self):
        gateway = self._gateway(total=1, reserved=1)
        self.assertEqual(gateway._reserved_critical_slots(), 0)
        self.assertEqual(gateway._background_limit(), 1)
        self.assertIsNone(gateway._build_background_semaphore())

    def test_malformed_reserved_value_falls_back(self):
        gateway = self._gateway(total=3, reserved="oops")
        self.assertEqual(gateway._reserved_critical_slots(), 1)

    def test_refresh_rebuilds_background_limit_when_only_reserved_slots_change(self):
        gateway = self._gateway(total=3, reserved=1)
        gateway.config = SimpleNamespace()
        gateway._global_semaphore = asyncio.Semaphore(3)
        gateway._background_semaphore = gateway._build_background_semaphore()

        gateway.refresh_config(
            SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=3,
                    critical_path_reserved_slots=2,
                )
            )
        )

        self.assertEqual(gateway._background_limit(), 1)
        self.assertIsNotNone(gateway._background_semaphore)
        self.assertEqual(gateway._background_semaphore._value, 1)


class ConcurrencyPriorityTests(unittest.TestCase):
    def test_critical_path_not_starved_by_background_flood(self):
        # total=3, background<=2：两个后台调用占满子槽后，关键路径仍能立即拿到第三个槽
        limiter = _Limiter(total=3, background=2)

        async def _run():
            release = asyncio.Event()
            background_entered = asyncio.Semaphore(0)

            async def _background():
                async with limiter._concurrency_slot(False):
                    background_entered.release()
                    await release.wait()

            tasks = [asyncio.create_task(_background()) for _ in range(2)]
            # 加超时：实现缺失时后台任务会抛异常，无超时会让本用例永久挂死
            for _ in range(2):
                await asyncio.wait_for(background_entered.acquire(), timeout=2.0)

            critical_ran = False
            async with limiter._concurrency_slot(True):
                critical_ran = True

            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            return critical_ran

        self.assertTrue(asyncio.run(_run()), "关键路径必须能在后台占满子槽时立即执行")

    def test_background_calls_are_capped_below_total(self):
        limiter = _Limiter(total=3, background=2)

        async def _run():
            release = asyncio.Event()
            entered = []
            entered_event = asyncio.Semaphore(0)

            async def _background(index):
                async with limiter._concurrency_slot(False):
                    entered.append(index)
                    entered_event.release()
                    await release.wait()

            tasks = [asyncio.create_task(_background(i)) for i in range(3)]
            for _ in range(2):
                await asyncio.wait_for(entered_event.acquire(), timeout=2.0)
            await asyncio.sleep(0)  # 给第三个任务一次被调度的机会
            concurrent_after_settle = len(entered)

            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            return concurrent_after_settle, len(entered)

        concurrent, total_done = asyncio.run(_run())

        self.assertEqual(concurrent, 2, "后台并发不得超过 max-reserved")
        self.assertEqual(total_done, 3, "被限流的后台任务最终仍要完成")

    def test_total_concurrency_never_exceeds_global_cap(self):
        # 429 保护红线：关键路径 + 后台的同时在飞数不得超过总上限
        limiter = _Limiter(total=2, background=1)

        async def _run():
            release = asyncio.Event()
            live = 0
            peak = 0
            lock = asyncio.Lock()

            async def _call(critical):
                nonlocal live, peak
                async with limiter._concurrency_slot(critical):
                    async with lock:
                        live += 1
                        peak = max(peak, live)
                    await release.wait()
                    async with lock:
                        live -= 1

            tasks = [asyncio.create_task(_call(i % 2 == 0)) for i in range(6)]
            await asyncio.sleep(0.02)
            release.set()
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2.0)
            return peak

        self.assertLessEqual(asyncio.run(_run()), 2, "总并发绝不能超过全局上限（429 保护）")

    def test_no_background_semaphore_behaves_like_legacy(self):
        limiter = _Limiter(total=2, background=None)

        async def _run():
            async with limiter._concurrency_slot(False):
                return True

        self.assertTrue(asyncio.run(_run()))


if __name__ == "__main__":
    unittest.main()
