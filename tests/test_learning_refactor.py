import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget


class _FakeDB:
    def __init__(self):
        self.logged = []

    async def add_message_log_async(self, **kwargs):
        self.logged.append(kwargs)


class _FakeExpressionPatternService:
    def __init__(self, result="async patterns"):
        self.result = result
        self.calls = []

    async def render_active_patterns(self, chat_id, limit=5):
        self.calls.append((chat_id, limit))
        return self.result


class LearningRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.learning.evolution_manager", None)
        self.mod = importlib.import_module("astrmai.learning.evolution_manager")
        self.mod = importlib.reload(self.mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_process_bot_reply_skips_polluted_reply(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(mining_window_sec=60, mining_window_min_messages=2, mining_cooldown_sec=60, mining_trigger=20),
            reply=SimpleNamespace(fallback_text="（陷入了短暂的沉默...）"),
        )
        manager = self.mod.EvolutionManager(_FakeDB(), SimpleNamespace(config=config), config=config)
        asyncio.run(manager.process_bot_reply("chat-1", "bot-1", "Traceback: fail"))
        self.assertEqual(manager.db.logged, [])

    def test_process_bot_reply_persists_non_human_provenance(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(mining_window_sec=60, mining_window_min_messages=2, mining_cooldown_sec=60, mining_trigger=20),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.mod.EvolutionManager(_FakeDB(), SimpleNamespace(config=config), config=config)

        asyncio.run(
            manager.process_bot_reply(
                "ff:GroupMessage:123",
                "bot-1",
                "唉嘿嘿～这是机器人自己的回复。",
            )
        )

        event = manager.db.logged[0]["conversation_event"]
        self.assertEqual(event["chat_kind"], "group")
        self.assertEqual(event["role"], "assistant")
        self.assertTrue(event["is_bot"])
        self.assertEqual(event["provenance"], "bot_echo")

    def test_get_active_patterns_canonical_async_works_inside_running_loop(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(mining_window_sec=60, mining_window_min_messages=2, mining_cooldown_sec=60, mining_trigger=20),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        service = _FakeExpressionPatternService("active patterns from async")
        db = _FakeDB()
        db.memory_engine = SimpleNamespace(expression_pattern_service=service)
        manager = self.mod.EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _run():
            return await manager.get_active_patterns_canonical_async("chat-async", limit=3)

        self.assertEqual(asyncio.run(_run()), "active patterns from async")
        self.assertEqual(service.calls, [("chat-async", 3)])

    def test_get_active_patterns_canonical_sync_rejects_running_loop(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(mining_window_sec=60, mining_window_min_messages=2, mining_cooldown_sec=60, mining_trigger=20),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        service = _FakeExpressionPatternService("unused")
        db = _FakeDB()
        db.memory_engine = SimpleNamespace(expression_pattern_service=service)
        manager = self.mod.EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _run():
            with self.assertRaisesRegex(RuntimeError, "sync-only"):
                manager.get_active_patterns_canonical("chat-sync", limit=2)

        asyncio.run(_run())
        self.assertEqual(service.calls, [])

    def test_backlog_scheduler_sleep_does_not_hold_shared_budget(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                mining_window_sec=60,
                mining_window_min_messages=2,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        budget = BackgroundTaskBudget(1)
        manager = self.mod.EvolutionManager(
            _FakeDB(),
            SimpleNamespace(config=config),
            config=config,
            background_task_budget=budget,
        )

        async def _run():
            await manager.start_background_tasks()
            await asyncio.sleep(0.01)
            self.assertEqual(budget.status()["active"], 0)
            await manager.stop_background_tasks()

        asyncio.run(_run())

    def test_get_active_patterns_canonical_sync_still_works_without_running_loop(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(mining_window_sec=60, mining_window_min_messages=2, mining_cooldown_sec=60, mining_trigger=20),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        service = _FakeExpressionPatternService("active patterns from sync")
        db = _FakeDB()
        db.memory_engine = SimpleNamespace(expression_pattern_service=service)
        manager = self.mod.EvolutionManager(db, SimpleNamespace(config=config), config=config)

        self.assertEqual(
            manager.get_active_patterns_canonical("chat-sync", limit=4),
            "active patterns from sync",
        )
        self.assertEqual(service.calls, [("chat-sync", 4)])

    def test_learning_pipeline_concurrency_is_bounded(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                mining_window_sec=60,
                mining_window_min_messages=2,
                mining_cooldown_sec=60,
                mining_trigger=20,
                learning_pipeline_concurrency=1,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.mod.EvolutionManager(_FakeDB(), SimpleNamespace(config=config), config=config)
        started = 0
        peak = 0
        release = asyncio.Event()

        async def _fake_pipeline(_pipeline, _group_id, _logs):
            nonlocal started, peak
            started += 1
            peak = max(peak, started)
            await release.wait()
            started -= 1
            return {"status": "completed"}

        manager._run_learning_pipeline_unlimited = _fake_pipeline

        async def _run():
            first = asyncio.create_task(manager._run_learning_pipeline("expression", "chat-1", []))
            await asyncio.sleep(0)
            second = asyncio.create_task(manager._run_learning_pipeline("expression", "chat-2", []))
            await asyncio.sleep(0)
            self.assertEqual(peak, 1)
            release.set()
            await asyncio.gather(first, second)

        asyncio.run(_run())
        self.assertEqual(manager._active_pipeline_tasks, 0)

    def test_mining_trigger_while_running_is_coalesced_into_one_rerun(self):
        config = SimpleNamespace(
            evolution=SimpleNamespace(
                mining_window_sec=60,
                mining_window_min_messages=2,
                mining_cooldown_sec=60,
                mining_trigger=20,
            ),
            reply=SimpleNamespace(fallback_text="fallback"),
        )
        manager = self.mod.EvolutionManager(_FakeDB(), SimpleNamespace(config=config), config=config)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def _mine(group_id):
            calls.append(group_id)
            if len(calls) == 1:
                started.set()
                await release.wait()

        manager._try_trigger_mining = _mine

        async def _run():
            manager._schedule_mining_if_triggered("chat-1", True)
            await started.wait()
            manager._schedule_mining_if_triggered("chat-1", True)
            manager._schedule_mining_if_triggered("chat-1", True)
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)
                if len(calls) == 2 and not manager._mining_tasks:
                    break
            await manager.stop_background_tasks()

        asyncio.run(_run())
        self.assertEqual(calls, ["chat-1", "chat-1"])


if __name__ == "__main__":
    unittest.main()
