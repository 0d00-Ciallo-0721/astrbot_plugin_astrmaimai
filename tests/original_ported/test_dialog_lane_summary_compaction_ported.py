import asyncio
import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class DialogLaneSummaryCompactionTests(unittest.TestCase):
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

    def test_dialog_lane_compacts_old_history_into_summary(self):
        manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")

        raw_history = []
        for index in range(16):
            raw_history.append({"role": "user", "content": f"user-{index}"})
            raw_history.append({"role": "assistant", "content": f"assistant-{index}"})

        async def _run():
            lane_umo, conversation_id, _, _ = await manager.ensure_lane(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
            )
            return await manager.save_lane_history(
                lane_key=lane_key,
                lane_umo=lane_umo,
                conversation_id=conversation_id,
                history=raw_history,
            )

        compacted = asyncio.run(_run())

        self.assertLess(len(compacted), len(raw_history))
        self.assertEqual(compacted[0]["role"], "assistant")
        self.assertTrue(str(compacted[0]["content"]).startswith("较早对话摘要："))
        self.assertEqual(compacted[-1]["content"], "assistant-15")


if __name__ == "__main__":
    unittest.main()
