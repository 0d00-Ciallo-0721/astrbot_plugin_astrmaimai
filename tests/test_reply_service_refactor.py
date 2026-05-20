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


async def _noop_post_send(*args, **kwargs):
    return None


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

    def _build_memory_summarizer(self, *, threshold=2):
        sys.modules.pop("astrmai.memory.services.summarizer", None)
        summarizer_mod = importlib.import_module("astrmai.memory.services.summarizer")
        summarizer_mod = importlib.reload(summarizer_mod)
        gateway = SimpleNamespace(context=SimpleNamespace(astrmai=None))
        config = SimpleNamespace(memory=SimpleNamespace(summary_threshold=threshold, cleanup_interval=3600))
        return summarizer_mod.ChatHistorySummarizer(
            context=SimpleNamespace(astrmai_plugin=None),
            gateway=gateway,
            engine=SimpleNamespace(),
            config=config,
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

        asyncio.run(engine.handle_reply(event, "this reply is stale", event.unified_msg_origin))

        self.assertEqual(state_engine.gateway.context.sent, [])
        self.assertFalse(event.get_extra("astrmai_reply_sent", False))

    def test_short_multi_sentence_reply_stays_single(self):
        service = self._service(max_len=80)

        artifact = service._build_visible_reply_artifact("I get what you mean. We can keep this simple for now.")

        self.assertEqual(len(artifact.segments), 1)
        self.assertEqual(artifact.metadata["segment_reason"], "within_single_limit")

    def test_long_reply_uses_natural_segments_and_caps_at_three(self):
        service = self._service(max_len=36)
        text = (
            "I was still thinking about that point. "
            "The stuck feeling may be information overload, not lack of effort. "
            "Let's peel out the smallest next step first."
        )

        artifact = service._build_visible_reply_artifact(text)

        self.assertGreater(len(artifact.segments), 1)
        self.assertLessEqual(len(artifact.segments), 3)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_forced_paragraph_boundary_can_split_below_single_limit(self):
        service = self._service(max_len=200)

        artifact = service._build_visible_reply_artifact("First paragraph.\n\nSecond paragraph.")

        self.assertEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")

    def test_segmenter_preserves_code_url_and_decimal_fragments(self):
        service = self._service(max_len=34)
        text = "Version is 3.14.15, check https://example.com/a.b?x=1, and keep ```a.b()``` intact."

        artifact = service._build_visible_reply_artifact(text)
        visible = "\n".join(artifact.segments)

        self.assertIn("3.14.15", visible)
        self.assertIn("https://example.com/a.b?x=1", visible)
        self.assertIn("```a.b()```", visible)

    def test_reply_modes_apply_human_segment_limits(self):
        service = self._service(max_len=32)
        text = "I can stay here with you for a bit. You do not need to explain everything at once. Catch your breath first."

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
        text = "I just thought of one more thing. We do not need to rush to a conclusion. We can keep this light."

        artifact = service._build_visible_reply_artifact(text, is_proactive=True)

        self.assertLessEqual(len(artifact.segments), 2)
        self.assertEqual(artifact.metadata["delay_profile"], "proactive")

    def test_successful_reply_feeds_memory_buffer_after_send(self):
        state_engine = FakeStateEngine()
        state_engine.config.reply.typing_speed_factor = 0.0
        summarizer = self._build_memory_summarizer(threshold=100)
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=SimpleNamespace(summarizer=summarizer),
        )
        service._settle_post_send = _noop_post_send

        first = FakeEvent("user-1", "Alice", "turn-1")
        second = FakeEvent("user-1", "Alice", "turn-2")

        async def _run():
            await service.handle_reply(first, "reply-1", first.unified_msg_origin)
            after_first = await summarizer.describe_session_eligibility(first.unified_msg_origin)
            await service.handle_reply(second, "reply-2", second.unified_msg_origin)
            summarizer.config.memory.summary_threshold = 2
            after_second = await summarizer.describe_session_eligibility(second.unified_msg_origin)
            return after_first, after_second

        after_first, after_second = asyncio.run(_run())

        self.assertEqual(after_first["reason"], "below_threshold")
        self.assertTrue(after_first["candidate_present"])
        self.assertEqual(after_second["reason"], "eligible")
        self.assertTrue(after_second["eligible"])

    def test_failed_send_does_not_feed_memory_buffer(self):
        state_engine = FakeStateEngine()
        summarizer = self._build_memory_summarizer(threshold=2)
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=SimpleNamespace(summarizer=summarizer),
        )

        async def _send_fail(*args, **kwargs):
            return False

        service._send_segments = _send_fail
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "send-failed")

        asyncio.run(service.handle_reply(event, "will-not-write-memory", event.unified_msg_origin))

        self.assertNotIn(event.unified_msg_origin, summarizer._session_history_buffer)

    def test_proactive_reply_does_not_feed_memory_buffer(self):
        state_engine = FakeStateEngine()
        summarizer = self._build_memory_summarizer(threshold=2)
        service = self.reply_mod.ReplyService(
            state_engine=state_engine,
            mood_manager=SimpleNamespace(),
            memory_engine=SimpleNamespace(summarizer=summarizer),
        )
        service._settle_post_send = _noop_post_send
        event = FakeEvent("user-1", "Alice", "proactive-message")
        event.set_extra("astrmai_is_proactive_event", True)

        asyncio.run(service.handle_reply(event, "proactive-reply", event.unified_msg_origin))

        self.assertNotIn(event.unified_msg_origin, summarizer._session_history_buffer)


if __name__ == "__main__":
    unittest.main()
