import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeStateEngine:
    def __init__(self):
        self.received = []

    async def on_learning_message_recorded(self, payload):
        self.received.append(payload)


class _FakeMemoryEngine:
    def __init__(self):
        self.received = []

    async def on_learning_bot_reply_recorded(self, payload):
        self.received.append(("bot", payload))

    async def on_learning_mining_completed(self, payload):
        self.received.append(("mining", payload))


class LearningEventCollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for mod in [
            "astrmai.infrastructure.runtime.event_bus",
            "astrmai.learning.evolution_manager",
        ]:
            sys.modules.pop(mod, None)
        self.bus_mod = importlib.import_module("astrmai.infrastructure.runtime.event_bus")
        self.bus_mod = importlib.reload(self.bus_mod)
        self.learning_mod = importlib.import_module("astrmai.learning.evolution_manager")
        self.learning_mod = importlib.reload(self.learning_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_learning_events_publish_to_state_and_memory_topics(self):
        event_bus = self.bus_mod.EventBus()
        event_bus._init_bus()
        state_engine = _FakeStateEngine()
        memory_engine = _FakeMemoryEngine()
        event_bus.subscribe(event_bus.TOPIC_LEARNING_MESSAGE_RECORDED, state_engine.on_learning_message_recorded)
        event_bus.subscribe(event_bus.TOPIC_LEARNING_BOT_REPLY_RECORDED, memory_engine.on_learning_bot_reply_recorded)
        event_bus.subscribe(event_bus.TOPIC_LEARNING_MINING_COMPLETED, memory_engine.on_learning_mining_completed)

        config = SimpleNamespace(
            evolution=SimpleNamespace(mining_window_sec=60, mining_window_min_messages=2, mining_cooldown_sec=60, mining_trigger=1),
            reply=SimpleNamespace(fallback_text="（陷入了短暂的沉默...）"),
        )
        db = SimpleNamespace(
            logged=[],
            unprocessed=[SimpleNamespace(id=1)],
            async_add=[],
        )

        async def _add_message_log_async(**kwargs):
            db.logged.append(kwargs)

        async def _get_unprocessed_logs_async(group_id, limit=100):
            return list(db.unprocessed)

        async def _save_pattern_async(pattern):
            return None

        async def _save_jargon_async(jargon):
            return None

        async def _mark_logs_processed_async(ids):
            db.unprocessed = []

        db.add_message_log_async = _add_message_log_async
        db.get_unprocessed_logs_async = _get_unprocessed_logs_async
        db.save_pattern_async = _save_pattern_async
        db.save_jargon_async = _save_jargon_async
        db.mark_logs_processed_async = _mark_logs_processed_async

        manager = self.learning_mod.EvolutionManager(db, SimpleNamespace(config=config), config=config, event_bus=event_bus)

        async def _mine(group_id, logs):
            return [SimpleNamespace()]

        async def _mine_jargons(group_id, logs):
            return [SimpleNamespace()]

        manager.expression_miner.mine = _mine
        manager.jargon_miner.mine = _mine_jargons

        fake_event = SimpleNamespace(
            unified_msg_origin="chat-1",
            message_str="hello",
            get_extra=lambda key, default=None: default,
            get_sender_id=lambda: "user-1",
            get_sender_name=lambda: "Alice",
        )

        async def _run():
            await manager.record_user_message(fake_event)
            await manager.process_bot_reply("chat-1", "bot-1", "reply")
            await manager.process_logs_and_mine("chat-1", [SimpleNamespace(id=1)])
            await asyncio.sleep(0.05)

        asyncio.run(_run())

        self.assertEqual(state_engine.received[0]["sender_id"], "user-1")
        self.assertEqual(memory_engine.received[0][0], "bot")
        self.assertEqual(memory_engine.received[1][0], "mining")


if __name__ == "__main__":
    unittest.main()
