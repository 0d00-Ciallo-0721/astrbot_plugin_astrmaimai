import asyncio
import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeConversation:
    def __init__(self, history=None):
        self.history = history or []


class _FakeConversationManager:
    def __init__(self):
        self.curr = {}
        self.conversations = {}
        self.counter = 0

    async def get_curr_conversation_id(self, unified_msg_origin):
        return self.curr.get(unified_msg_origin)

    async def new_conversation(self, unified_msg_origin, platform_id=None, content=None, title=None, persona_id=None):
        self.counter += 1
        cid = f"conv-{self.counter}"
        self.curr[unified_msg_origin] = cid
        self.conversations[cid] = _FakeConversation(history=content or [])
        return cid

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        if conversation_id not in self.conversations and create_if_not_exists:
            self.conversations[conversation_id] = _FakeConversation(history=[])
        return self.conversations.get(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        conversation_id = conversation_id or self.curr.get(unified_msg_origin)
        self.conversations[conversation_id] = _FakeConversation(history=history or [])


class _SlowConversationManager(_FakeConversationManager):
    def __init__(self):
        super().__init__()
        self.first_started = asyncio.Event()
        self.allow_finish = asyncio.Event()
        self.active_gets = 0
        self.max_active_gets = 0

    async def get_conversation(self, unified_msg_origin, conversation_id, create_if_not_exists=False):
        self.active_gets += 1
        self.max_active_gets = max(self.max_active_gets, self.active_gets)
        self.first_started.set()
        try:
            await self.allow_finish.wait()
            return await super().get_conversation(
                unified_msg_origin,
                conversation_id,
                create_if_not_exists=create_if_not_exists,
            )
        finally:
            self.active_gets -= 1


class LaneManagerBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_same_lane_reuses_same_conversation(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            lane_umo1, cid1, history1, _ = await manager.ensure_lane(lane_key, "default:GroupMessage:group-1")
            await manager.append_exchange(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                user_content="hello",
                assistant_content="world",
            )
            lane_umo2, cid2, history2, _ = await manager.ensure_lane(lane_key, "default:GroupMessage:group-1")
            return lane_umo1, cid1, history1, lane_umo2, cid2, history2

        lane_umo1, cid1, history1, lane_umo2, cid2, history2 = asyncio.run(_run())

        self.assertEqual(lane_umo1, lane_umo2)
        self.assertEqual(cid1, cid2)
        self.assertEqual(history1, [])
        self.assertEqual(len(history2), 2)
        self.assertEqual(history2[0]["role"], "user")
        self.assertEqual(history2[1]["content"], "world")

    def test_same_lane_allows_conversation_io_outside_lane_lock(self):
        conversation_manager = _SlowConversationManager()
        manager = self.lane_mod.LaneManager(conversation_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        async def _run():
            lane_umo = manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
            await conversation_manager.new_conversation(lane_umo, title="seed")
            first = asyncio.create_task(manager.ensure_lane(lane_key, "default:GroupMessage:group-1"))
            await conversation_manager.first_started.wait()
            second = asyncio.create_task(manager.ensure_lane(lane_key, "default:GroupMessage:group-1"))
            await asyncio.sleep(0.05)
            overlap_seen = conversation_manager.max_active_gets >= 2
            conversation_manager.allow_finish.set()
            await first
            await second
            return overlap_seen

        overlap_seen = asyncio.run(_run())

        self.assertTrue(overlap_seen)


if __name__ == "__main__":
    unittest.main()
