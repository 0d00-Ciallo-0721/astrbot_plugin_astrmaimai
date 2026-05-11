import asyncio
import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class SingleHistorySourceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.runtime.runtime_contracts", None)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.contracts_mod = importlib.import_module("astrmai.infrastructure.runtime.runtime_contracts")
        self.contracts_mod = importlib.reload(self.contracts_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_append_visible_reply_artifact_uses_lane_sanitizer(self):
        lane_manager = self.lane_mod.LaneManager(_FakeConversationManager())
        lane_key = self.lane_mod.LaneKey(subsystem="sys2", task_family="dialog", scope_id="group-1")
        artifact = self.contracts_mod.VisibleReplyArtifact(
            visible_text="呜……\n不要难过，亚托莉抱抱你！",
            segments=["呜……", "不要难过，亚托莉抱抱你！"],
            persistable_text="assistant: 呜……\n不要难过，亚托莉抱抱你！",
        )

        async def _run():
            await lane_manager.append_visible_reply_artifact(
                lane_key=lane_key,
                base_origin="default:GroupMessage:group-1",
                raw_user_text="这是当前你看到的最新消息： [Alice] 说: 为什么不可以\n\n>> 请继续回应",
                artifact=artifact,
                prefix_hash="hash-1",
                model_id="model-a",
            )
            lane_umo = lane_manager.resolve_lane_umo("default:GroupMessage:group-1", lane_key)
            conversation_id = await lane_manager.conversation_manager.get_curr_conversation_id(lane_umo)
            return await lane_manager.conversation_manager.get_conversation(lane_umo, conversation_id)

        conversation = asyncio.run(_run())
        self.assertEqual(conversation.history[-2]["content"], "[Alice] 说: 为什么不可以")
        self.assertEqual(conversation.history[-1]["content"], "呜……\n不要难过，亚托莉抱抱你！")


if __name__ == "__main__":
    unittest.main()
