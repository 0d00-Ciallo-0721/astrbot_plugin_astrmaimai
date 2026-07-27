"""G4 / OPT-14 回归测试：重载韧性（PL-10 闩锁 + PL-09 上下文快照）。

PL-10：`_terminated` 置位后无复位路径，若 AstrBot 的禁用→启用复用同一插件实例，
插件会静默死到进程重启（日志仅一行 rejected）。terminate→initialize 必须幂等安全。

PL-09：GroupDialogueStore 纯内存，面板改配置触发重载即丢全部群热/温区，
表现为 bot "突然接不上话"。策略三要素：TTL 约束、schema 版本门槛、terminate 时机。
"""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrmai.app.lifecycle import PluginLifecycleManager
from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.infrastructure.runtime.event_bus import EventBus
from astrmai.multimodal.visual_cortex import VisualCortex
from astrmai.workmode.cron_guard.heartbeat import CronHeartbeatGuard


class TerminateLatchTests(unittest.TestCase):
    """PL-10：同实例 terminate → initialize 必须能复活。"""

    def _manager(self):
        manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
        manager._terminated = False
        manager._shutdown_requested = False
        manager._startup_task = None
        started = []

        manager.runtime = SimpleNamespace(
            status=SimpleNamespace(is_running=False, lifecycle_started=False, persona_state="idle"),
            set_boot_phase=lambda phase: None,
            dialogue_store=None,
        )

        def _track(coro):
            # 不真正跑完整启动链，只记录"启动被发起"并关闭协程
            started.append(True)
            coro.close()
            return SimpleNamespace(done=lambda: True)

        manager.track_task = _track
        return manager, started

    def test_explicit_reinitialize_after_terminate_resets_latch(self):
        # 面板禁用→启用复用同一实例：必须能复活（PL-10 要修的场景）
        manager, started = self._manager()
        manager._terminated = True

        asyncio.run(manager.on_program_start(source="plugin_initialize"))

        self.assertFalse(manager._terminated, "显式重新初始化必须复位闩锁，否则同实例永久拒启")
        self.assertEqual(len(started), 1, "复位后必须真正发起启动")

    def test_late_framework_hook_after_terminate_is_still_rejected(self):
        # shutdown 期间迟到的框架 hook 绝不能复活插件——与上一条是真实张力，
        # 靠启动来源区分（既有 test_terminated_lifecycle_cannot_be_restarted_by_late_hook 守护同一语义）
        for source in ("astrbot_loaded", ""):
            with self.subTest(source=source or "<unset>"):
                manager, started = self._manager()
                manager._terminated = True

                asyncio.run(manager.on_program_start(source=source))

                self.assertTrue(manager._terminated, "迟到 hook 不得复位闩锁")
                self.assertEqual(started, [], "迟到 hook 不得发起启动")

    def test_normal_startup_unchanged(self):
        manager, started = self._manager()

        asyncio.run(manager.on_program_start())

        self.assertEqual(len(started), 1)
        self.assertFalse(manager._terminated)

    def test_already_running_still_skips(self):
        manager, started = self._manager()
        manager.runtime.status.is_running = True
        manager.runtime.status.lifecycle_started = True

        asyncio.run(manager.on_program_start())

        self.assertEqual(started, [], "已在运行时不得重复启动（既有语义不能被 PL-10 破坏）")


class RuntimeReinitializeTests(unittest.TestCase):
    """显式重新启用必须恢复真正的运行能力，而不只是复位生命周期布尔值。"""

    def setUp(self):
        EventBus._instance = None

    def tearDown(self):
        EventBus._instance = None

    def test_runtime_coordinator_reopens_after_shutdown(self):
        async def _run():
            coordinator = ChatRuntimeCoordinator()
            await coordinator.shutdown()
            self.assertEqual(await coordinator.advance_generation("chat", "thread"), 0)

            await coordinator.reopen()

            generation = await coordinator.advance_generation("chat", "thread")
            claimed = await coordinator.claim_send("chat", "send-1")
            return generation, claimed

        generation, claimed = asyncio.run(_run())

        self.assertGreater(generation, 0)
        self.assertTrue(claimed)

    def test_visual_cortex_worker_can_restart_after_stop(self):
        async def _run():
            cortex = VisualCortex(gateway=Mock(), db_service=Mock())
            cortex.start()
            first_task = cortex._worker_task
            cortex.stop()
            await asyncio.sleep(0)

            cortex.start()
            second_task = cortex._worker_task
            running = cortex.describe_status()["worker_running"]
            cortex.stop()
            await asyncio.sleep(0)
            return first_task, second_task, running

        first_task, second_task, running = asyncio.run(_run())

        self.assertIsNot(first_task, second_task)
        self.assertTrue(running)

    def test_visual_cortex_stop_discards_pending_tasks_before_restart(self):
        async def _run():
            cortex = VisualCortex(gateway=Mock(), db_service=Mock())
            self.assertTrue(cortex.submit_task("old-1", "payload-1"))
            self.assertTrue(cortex.submit_task("old-2", "payload-2"))

            cortex.stop()
            await asyncio.wait_for(cortex.queue.join(), timeout=1.0)

            cortex.start()
            status = cortex.describe_status()
            cortex.stop()
            await asyncio.sleep(0)
            return status

        status = asyncio.run(_run())

        self.assertEqual(status["queue_size"], 0)
        self.assertTrue(status["worker_running"])

    def test_cron_guard_can_restart_after_stop(self):
        guard = CronHeartbeatGuard(db_service=Mock(), context=SimpleNamespace(cron_manager=None))

        guard.stop()
        guard.start()

        self.assertTrue(guard.describe_status()["running"])

    def test_prepare_reinitialize_reopens_services_and_rebinds_learning(self):
        async def _run():
            event_bus = EventBus()
            state_handler = AsyncMock()
            reply_handler = AsyncMock()
            mining_handler = AsyncMock()
            coordinator = SimpleNamespace(reopen=AsyncMock())
            persona = SimpleNamespace(reopen=Mock())
            cron_guard = SimpleNamespace(start=Mock())
            runtime = SimpleNamespace(
                runtime_coordinator=coordinator,
                persona_summarizer=persona,
                cron_guard=cron_guard,
                event_bus=event_bus,
                state_engine=SimpleNamespace(on_learning_message_recorded=state_handler),
                memory_engine=SimpleNamespace(
                    on_learning_bot_reply_recorded=reply_handler,
                    on_learning_mining_completed=mining_handler,
                ),
            )
            manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
            manager.runtime = runtime

            await manager._prepare_reinitialize()

            topics = {
                topic: [ref() if callable(getattr(ref, "__call__", None)) and type(ref).__name__ == "WeakMethod" else ref
                        for ref in refs]
                for topic, refs in event_bus.subscribers.items()
            }
            return coordinator, persona, cron_guard, event_bus, topics

        coordinator, persona, cron_guard, event_bus, topics = asyncio.run(_run())

        coordinator.reopen.assert_awaited_once()
        persona.reopen.assert_called_once()
        cron_guard.start.assert_called_once()
        self.assertIn(
            event_bus.TOPIC_LEARNING_MESSAGE_RECORDED,
            topics,
        )
        self.assertIn(
            event_bus.TOPIC_LEARNING_BOT_REPLY_RECORDED,
            topics,
        )
        self.assertIn(
            event_bus.TOPIC_LEARNING_MINING_COMPLETED,
            topics,
        )


class DialogueSnapshotTests(unittest.TestCase):
    """PL-09：快照落盘/恢复 + TTL + schema 版本门槛。"""

    CHAT = "default:GroupMessage:7000"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cache_dir = Path(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _store(self, **kwargs):
        return GroupDialogueStore(snapshot_dir=self.cache_dir, **kwargs)

    async def _seed(self, store, *, age_seconds=10.0):
        now = time.time()
        await store.append_segment(
            self.CHAT,
            event_id="msg-1",
            speaker_id="111",
            speaker_name="阿甲",
            content="我们刚才说的那个方案",
            role="user",
            timestamp=now - age_seconds,
        )
        await store.set_cold_summary(self.CHAT, "早前讨论了排期")

    def test_snapshot_roundtrip_restores_hot_zone_and_cold_summary(self):
        async def _run():
            source = self._store()
            await self._seed(source)
            persisted = await source.persist_snapshot()

            # 模拟重载：全新实例（内存为空）
            restored_store = self._store()
            restored = await restored_store.restore_snapshot()
            bundle = await restored_store.get_warm_context_bundle(self.CHAT)
            cold = await restored_store.get_cold_summary(self.CHAT)
            return persisted, restored, bundle, cold

        persisted, restored, bundle, cold = asyncio.run(_run())

        self.assertTrue(persisted)
        self.assertEqual(restored, 1)
        self.assertIn("那个方案", bundle.quote_text, "重载后热区必须接得上")
        self.assertEqual(cold, "早前讨论了排期")

    def test_expired_segments_are_not_restored(self):
        async def _run():
            source = self._store(warm_zone_ttl_seconds=5.0)
            # 段龄 60s > TTL 5s：陈旧上下文宁可不要
            await self._seed(source, age_seconds=60.0)
            await source.persist_snapshot()

            restored_store = self._store(warm_zone_ttl_seconds=5.0)
            restored = await restored_store.restore_snapshot()
            bundle = await restored_store.get_warm_context_bundle(self.CHAT)
            return restored, bundle

        restored, bundle = asyncio.run(_run())

        # cold_summary 仍恢复（不受段龄约束），但过期 segment 不得回到热区
        self.assertNotIn("那个方案", bundle.quote_text)

    def test_schema_version_mismatch_discards_whole_snapshot(self):
        async def _run():
            source = self._store()
            await self._seed(source)
            await source.persist_snapshot()

            path = self.cache_dir / GroupDialogueStore.SNAPSHOT_FILENAME
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["schema_version"] = GroupDialogueStore.SNAPSHOT_SCHEMA_VERSION + 99
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            restored_store = self._store()
            return await restored_store.restore_snapshot()

        self.assertEqual(asyncio.run(_run()), 0, "schema 不兼容必须整份弃用，不做半解析")

    def test_corrupt_snapshot_is_safe(self):
        async def _run():
            (self.cache_dir / GroupDialogueStore.SNAPSHOT_FILENAME).write_text("{not json", encoding="utf-8")
            return await self._store().restore_snapshot()

        self.assertEqual(asyncio.run(_run()), 0)

    def test_persistence_disabled_when_no_snapshot_dir(self):
        async def _run():
            store = GroupDialogueStore()  # 未传 snapshot_dir → 开关关闭形态
            await self._seed(store)
            return await store.persist_snapshot(), await store.restore_snapshot()

        persisted, restored = asyncio.run(_run())

        self.assertFalse(persisted)
        self.assertEqual(restored, 0)
        self.assertFalse((self.cache_dir / GroupDialogueStore.SNAPSHOT_FILENAME).exists())

    def test_recalled_tombstone_survives_reload(self):
        # 与 G3 协同：撤回墓碑不能因重载而"复活"成原文
        async def _run():
            source = self._store()
            await self._seed(source)
            await source.mark_recalled(self.CHAT, "msg-1")
            await source.persist_snapshot()

            restored_store = self._store()
            await restored_store.restore_snapshot()
            bundle = await restored_store.get_warm_context_bundle(self.CHAT)
            return bundle

        bundle = asyncio.run(_run())

        self.assertNotIn("那个方案", bundle.quote_text)
        self.assertIn("[已撤回]", bundle.quote_text)


class LifecycleSnapshotWiringTests(unittest.TestCase):
    """装配断言：启动恢复 / terminate 落盘确实被调用。"""

    def _manager_with_store(self):
        calls = []

        class _Store:
            async def restore_snapshot(self):
                calls.append("restore")
                return 1

            async def persist_snapshot(self):
                calls.append("persist")
                return True

        manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
        manager.runtime = SimpleNamespace(dialogue_store=_Store())
        return manager, calls

    def test_restore_and_persist_are_wired(self):
        manager, calls = self._manager_with_store()

        asyncio.run(manager._restore_dialogue_snapshot())
        asyncio.run(manager._persist_dialogue_snapshot())

        self.assertEqual(calls, ["restore", "persist"])

    def test_missing_store_is_tolerated(self):
        manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
        manager.runtime = SimpleNamespace(dialogue_store=None)

        asyncio.run(manager._restore_dialogue_snapshot())
        asyncio.run(manager._persist_dialogue_snapshot())

    def test_store_failure_does_not_break_lifecycle(self):
        class _BrokenStore:
            async def restore_snapshot(self):
                raise RuntimeError("disk gone")

            async def persist_snapshot(self):
                raise RuntimeError("disk gone")

        manager = PluginLifecycleManager.__new__(PluginLifecycleManager)
        manager.runtime = SimpleNamespace(dialogue_store=_BrokenStore())

        asyncio.run(manager._restore_dialogue_snapshot())
        asyncio.run(manager._persist_dialogue_snapshot())


if __name__ == "__main__":
    unittest.main()
