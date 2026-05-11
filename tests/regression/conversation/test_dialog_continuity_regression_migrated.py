import asyncio
import importlib
import json
import sys
import tempfile
import unittest

from tests.helpers import install_astrbot_stubs


class _FakeConversation:
    def __init__(self, history=None):
        self.history = json.dumps(history or [], ensure_ascii=False)


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
        return self.conversations.get(conversation_id)

    async def update_conversation(self, unified_msg_origin, conversation_id=None, history=None, title=None, persona_id=None, token_usage=None):
        conversation_id = conversation_id or self.curr.get(unified_msg_origin)
        self.conversations[conversation_id] = _FakeConversation(history=history or [])


class DialogContinuityRegressionMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_recent_transcript_uses_cleaned_dialog_history(self):
        conversation_manager = _FakeConversationManager()
        lane_manager = self.lane_mod.LaneManager(conversation_manager)
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        lane_umo = lane_manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
        conversation_manager.curr[lane_umo] = "conv-1"
        conversation_manager.conversations["conv-1"] = _FakeConversation(
            history=[
                {"role": "user", "content": "（导演旁白：这是当前你看到的最新消息：\n[Alice] 说: 为什么不可以\n\n>> 请接话）"},
                {"role": "assistant", "content": "不可以和妹妹结婚呀！"},
            ]
        )

        transcript = asyncio.run(
            lane_manager.get_recent_transcript(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                max_turns=2,
            )
        )

        self.assertIn("[Alice] 说: 为什么不可以", transcript)
        self.assertIn("Bot: 不可以和妹妹结婚呀！", transcript)
        self.assertNotIn("导演旁白", transcript)


__all__ = ["DialogContinuityRegressionMigratedTests"]
