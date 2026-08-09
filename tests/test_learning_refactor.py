import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


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


if __name__ == "__main__":
    unittest.main()
