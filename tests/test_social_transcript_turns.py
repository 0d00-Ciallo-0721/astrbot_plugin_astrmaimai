import asyncio
import importlib
import sys
import tempfile
import unittest

from tests.test_lane_manager_conversation_binding import _FakeConversationManager
from tests.test_persistence_regressions import _install_astrbot_stubs


class SocialTranscriptTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infra.lane_manager", None)
        self.lane_mod = importlib.import_module("astrmai.infra.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_recent_transcript_keeps_socially_rendered_turns(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        history = [
            {"role": "assistant", "content": "我在这儿。"},
            {"role": "user", "content": "[互动事件：刚刚，哈基亚(3874287208) 对 恸(516779421) 发起了一次“戳一戳”]"},
            {"role": "user", "content": "今天好困"},
        ]

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
            )
            await manager.save_lane_history(
                lane_key=lane_key,
                lane_umo=lane_umo,
                conversation_id=conversation_id,
                history=history,
            )
            return await manager.get_recent_transcript(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                max_turns=4,
            )

        transcript = asyncio.run(_run())
        self.assertIn("[互动事件：刚刚", transcript)
        self.assertIn("Bot: 我在这儿。", transcript)


if __name__ == "__main__":
    unittest.main()
