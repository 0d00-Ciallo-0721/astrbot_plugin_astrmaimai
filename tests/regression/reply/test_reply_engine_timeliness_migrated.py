import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers import (
    FakeEvent,
    FakeStateEngine,
    install_astrbot_stubs,
    install_reply_engine_stubs,
)


class _FakeRuntimeCoordinator:
    def __init__(self, latest_activity):
        self.latest_activity = latest_activity

    async def get_latest_activity(self, chat_id):
        return self.latest_activity


class ReplyEngineTimelinessMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_reply_engine_stubs()
        sys.modules.pop("astrmai.conversation.execution.reply_service", None)
        self.reply_engine_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.reply_engine_mod = importlib.reload(self.reply_engine_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_stale_reply_is_skipped_when_newer_activity_exists(self):
        state_engine = FakeStateEngine()
        base_ts = time.time() - 12.0
        coordinator = _FakeRuntimeCoordinator((base_ts + 10.0, "user-2", "Bob", "later message"))
        engine = self.reply_engine_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "old question")
        event.set_extra("astrmai_timestamp", base_ts)

        asyncio.run(engine.handle_reply(event, "这是一条过期回复", event.unified_msg_origin))

        self.assertEqual(state_engine.gateway.context.sent, [])
        self.assertFalse(event.get_extra("astrmai_reply_sent", False))

    def test_direct_wakeup_reply_is_allowed_when_no_newer_activity_exists(self):
        state_engine = FakeStateEngine()
        base_ts = time.time() - 45.0
        coordinator = _FakeRuntimeCoordinator((base_ts, "user-1", "Alice", "same message"))
        engine = self.reply_engine_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "@你 还在吗")
        event.set_extra("astrmai_timestamp", base_ts)
        event.set_extra("astrmai_group_direct_wakeup", True)

        asyncio.run(engine.handle_reply(event, "我在呀", event.unified_msg_origin))

        self.assertEqual(len(state_engine.gateway.context.sent), 1)
        self.assertTrue(event.get_extra("astrmai_reply_sent", False))


__all__ = ["ReplyEngineTimelinessMigratedTests"]
