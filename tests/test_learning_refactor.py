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


if __name__ == "__main__":
    unittest.main()
