import asyncio
import importlib
import sys
import tempfile
import time
import unittest

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class LaneHistorySanitizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        datamodels_mod = sys.modules.get("astrmai.infra.datamodels")
        if datamodels_mod is not None and not hasattr(datamodels_mod, "ExpressionPattern"):
            for name in ("astrmai.infra.datamodels", "astrmai.infra.database", "astrmai.infra"):
                sys.modules.pop(name, None)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_recent_transcript_filters_dirty_turns(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        dirty_history = [
            {"role": "assistant", "content": "[RollingSummary]\nassistant: old-summary"},
            {"role": "user", "content": "导演旁白：请仔细阅读设定和前面的剧本"},
            {"role": "assistant", "content": "JSON 响应: {\"candidates\": [{\"finishReason\": \"SAFETY\"}]}"},
            {"role": "assistant", "content": "如果对话已结束，请调用 wait_and_listen。"},
            {"role": "assistant", "content": "assistant: 呜……\n不要难过，亚托莉抱抱你！"},
            {"role": "user", "content": "[萤] 说: 呜呜呜"},
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
                history=dirty_history,
            )
            return await manager.get_recent_transcript(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                max_turns=4,
            )

        transcript = asyncio.run(_run())

        self.assertNotIn("[RollingSummary]", transcript)
        self.assertNotIn("JSON 响应", transcript)
        self.assertNotIn("wait_and_listen", transcript)
        self.assertNotIn("assistant:", transcript)
        self.assertIn("呜……", transcript)
        self.assertIn("[萤] 说: 呜呜呜", transcript)

    def test_recent_transcript_honors_age_window_and_ignores_untimed_history(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        now = time.time()
        history = [
            {"role": "assistant", "content": "ATRI: old reply", "timestamp": now - 7200},
            {"role": "user", "content": "<message speaker=\"Alice\">old poke</message>"},
            {"role": "assistant", "content": "fresh reply", "timestamp": now - 60},
            {"role": "user", "content": "<message speaker=\"Alice\">fresh message</message>", "timestamp": now - 50},
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
                max_age_seconds=900,
            )

        transcript = asyncio.run(_run())

        self.assertIn("fresh reply", transcript)
        self.assertIn("fresh message", transcript)
        self.assertNotIn("old reply", transcript)
        self.assertNotIn("old poke", transcript)


if __name__ == "__main__":
    unittest.main()
