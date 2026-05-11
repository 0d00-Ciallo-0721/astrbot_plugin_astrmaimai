import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.reply_engine_stubs import (
    FakeEvent,
    FakeStateEngine,
    install_reply_engine_stubs,
)


class _FakeRuntimeCoordinator:
    def __init__(self, latest_activity):
        self.latest_activity = latest_activity

    async def get_latest_activity(self, chat_id):
        return self.latest_activity


class RefactoredReplyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        install_reply_engine_stubs()
        sys.modules.pop("astrmai.Brain.reply_engine", None)
        sys.modules.pop("astrmai.conversation.execution.reply_service", None)
        reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.reply_mod = importlib.reload(reply_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _service(self, *, min_len=8, max_len=48):
        state_engine = FakeStateEngine()
        state_engine.config.reply.segment_min_len = min_len
        state_engine.config.reply.no_segment_max_len = max_len
        state_engine.config.reply.typing_speed_factor = 0.1
        return self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
        )

    def test_stale_reply_is_still_skipped(self):
        state_engine = FakeStateEngine()
        base_ts = time.time() - 12.0
        coordinator = _FakeRuntimeCoordinator((base_ts + 10.0, "user-2", "Bob", "later message"))
        engine = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            runtime_coordinator=coordinator,
        )
        event = FakeEvent("user-1", "Alice", "old question")
        event.set_extra("astrmai_timestamp", base_ts)

        asyncio.run(engine.handle_reply(event, "这是一条过期回复", event.unified_msg_origin))

        self.assertEqual(state_engine.gateway.context.sent, [])
        self.assertFalse(event.get_extra("astrmai_reply_sent", False))

    def test_short_multi_sentence_reply_stays_single(self):
        service = self._service(max_len=80)

        artifact = service._build_visible_reply_artifact("嗯，我懂你的意思。这个其实不用急，先按你现在能做的来。")

        self.assertEqual(len(artifact.segments), 1)
        self.assertEqual(artifact.metadata["segment_reason"], "within_single_limit")

    def test_long_reply_uses_natural_segments_and_caps_at_three(self):
        service = self._service(max_len=36)
        text = (
            "刚刚那个点我还在想。你说的那种卡住感，可能不是你没努力，"
            "而是现在信息太挤了。先把最小的一步拆出来就好。"
        )

        artifact = service._build_visible_reply_artifact(text)

        self.assertGreater(len(artifact.segments), 1)
        self.assertLessEqual(len(artifact.segments), 3)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_forced_paragraph_boundary_can_split_below_single_limit(self):
        service = self._service(max_len=200)

        artifact = service._build_visible_reply_artifact("第一段先放这里。\n\n第二段单独接上。")

        self.assertEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_segmenter_preserves_code_url_and_decimal_fragments(self):
        service = self._service(max_len=34)
        text = "版本是 3.14.15，可以先看 https://example.com/a.b?x=1。代码 ```a.b()``` 不要拆。"

        artifact = service._build_visible_reply_artifact(text)
        visible = "\n".join(artifact.segments)

        self.assertIn("3.14.15", visible)
        self.assertIn("https://example.com/a.b?x=1", visible)
        self.assertIn("```a.b()```", visible)

    def test_reply_modes_apply_human_segment_limits(self):
        service = self._service(max_len=32)
        text = "我先在这儿陪你一会儿。你不用马上解释清楚所有东西。先让自己缓一下。"

        emotional = service._build_visible_reply_artifact(
            text,
            reply_mode=self.reply_mod.ReplyMode.EMOTIONAL_SUPPORT,
        )
        playful = service._build_visible_reply_artifact(
            text,
            reply_mode=self.reply_mod.ReplyMode.PLAYFUL_INTERACTION,
        )

        self.assertLessEqual(len(emotional.segments), 2)
        self.assertEqual(emotional.metadata["delay_profile"], "gentle")
        self.assertEqual(len(playful.segments), 1)

    def test_proactive_reply_defaults_to_low_segment_count(self):
        service = self._service(max_len=28)
        text = "刚刚那个话题我还想到一点。其实可以先不用急着定论。我们先放轻一点聊。"

        artifact = service._build_visible_reply_artifact(text, is_proactive=True)

        self.assertLessEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["delay_profile"], "proactive")


if __name__ == "__main__":
    unittest.main()
