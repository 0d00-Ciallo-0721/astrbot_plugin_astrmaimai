import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


class _Event:
    def __init__(self, text=""):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id="m1")
        self._extras = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return "user-1"


class _Resolver:
    async def resolve_event_images(self, event):
        from astrmai.multimodal.napcat_image_resolver import ImageResolutionBatch, ResolvedImage

        return ImageResolutionBatch(
            had_images=True,
            images=[ResolvedImage(index=0, source_ref="opaque", local_path="resolved.jpg")],
        )


class _VisualCortex:
    async def analyze_image_path(self, picid, image_path, scope_id="global"):
        return {"type": "image", "description": "一只白猫坐在窗边", "emotion_tags": ["平静"]}


class _EmojiVisualCortex:
    async def analyze_image_path(self, picid, image_path, scope_id="global"):
        return {"type": "emoji", "description": "熊猫头低着头，文字为“我太难了”。通常用于自我调侃。", "emotion_tags": ["无奈", "自嘲"]}


class _Persistence:
    def __init__(self):
        self.added = []
        self.marked = []

    async def add_last_message_meta(self, chat_id, sender_id, has_image, image_ids):
        self.added.append((chat_id, sender_id, has_image, image_ids))

    async def mark_last_message_vision_executed(self, chat_id, sender_id):
        self.marked.append((chat_id, sender_id))


class PrivateTurnCoordinatorTests(unittest.TestCase):
    def test_wait_for_input_stability_resets_after_new_activity(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(private_chat=SimpleNamespace(input_settle_sec=0.03))
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        session = SimpleNamespace(lock=asyncio.Lock(), last_active_time=time.time())

        async def run():
            async def touch_again():
                await asyncio.sleep(0.015)
                async with session.lock:
                    session.last_active_time = time.time()

            task = asyncio.create_task(touch_again())
            started = time.monotonic()
            await coordinator.wait_for_input_stability(session)
            await task
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        self.assertGreaterEqual(elapsed, 0.04)

    def test_pending_batch_revision_preserves_new_message_during_slow_reply(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(private_chat=SimpleNamespace(input_settle_sec=0.0))
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        first = _Event("第一句")
        first.message_obj.message_id = "first"
        second = _Event("第二句")
        second.message_obj.message_id = "second"
        chat_id = "ff:FriendMessage:user-1"

        first_revision = coordinator.begin_pending_batch(chat_id, [first])
        coordinator.note_new_message(chat_id)
        coordinator.finish_pending_batch(chat_id, first_revision, reply_sent=True)

        merged = coordinator.merge_pending_batch(chat_id, [second])

        self.assertEqual(merged, [first, second])
        self.assertTrue(first.get_extra("astrmai_private_pending_context"))
        self.assertIn(chat_id, coordinator._pending_batches)

        second_revision = coordinator.begin_pending_batch(chat_id, merged)
        coordinator.finish_pending_batch(chat_id, second_revision, reply_sent=True)
        self.assertNotIn(chat_id, coordinator._pending_batches)
        self.assertFalse(coordinator.clear_pending_batch(chat_id))

    def test_prepare_batch_waits_for_vision_and_builds_rich_context(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.01,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=1,
            ),
            vision=SimpleNamespace(enable_vision=True),
        )
        persistence = _Persistence()
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_Resolver(),
            visual_cortex=_VisualCortex(),
            persistence=persistence,
        )
        event = _Event("你看这张图")

        asyncio.run(coordinator.prepare_batch([event], "ff:FriendMessage:user-1"))

        self.assertTrue(event.get_extra("astrmai_vision_barrier_complete"))
        self.assertEqual(event.get_extra("direct_image_refs"), ["resolved.jpg"])
        self.assertIn("一只白猫坐在窗边", event.get_extra("astrmai_rich_text"))
        self.assertEqual(event.get_extra("astrmai_vision_descriptions"), ["一只白猫坐在窗边"])
        self.assertEqual(event.get_extra("astrmai_vision_records")[0]["type"], "image")
        self.assertEqual(event.get_extra("astrmai_vision_records")[0]["emotion_tags"], [])
        self.assertEqual(event.get_extra("astrmai_visual_context")[0]["description"], "一只白猫坐在窗边")
        self.assertEqual(len(persistence.added), 1)
        self.assertEqual(persistence.marked, [("ff:FriendMessage:user-1", "user-1")])

    def test_prepare_batch_formats_emoji_context_with_tags(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.01,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=1,
            ),
            vision=SimpleNamespace(enable_vision=True),
        )
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_Resolver(),
            visual_cortex=_EmojiVisualCortex(),
            persistence=_Persistence(),
        )
        event = _Event("这个表情什么意思")

        asyncio.run(coordinator.prepare_batch([event], "ff:FriendMessage:user-1"))

        rich_text = event.get_extra("astrmai_rich_text")
        self.assertIn("[表情包转述：熊猫头低着头", rich_text)
        self.assertIn("传达情绪：无奈、自嘲", rich_text)
        self.assertEqual(event.get_extra("astrmai_vision_records")[0]["type"], "emoji")
        self.assertEqual(event.get_extra("astrmai_vision_records")[0]["emotion_tags"], ["无奈", "自嘲"])

    def test_prepare_batch_fails_open_when_one_event_raises_unexpectedly(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(enable_vision=True),
        )
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=object(),
            visual_cortex=None,
        )
        first = _Event("第一张")
        second = _Event("后续文字")
        calls = []

        async def prepare(event, chat_id):
            calls.append(event.message_str)
            if event is first:
                raise RuntimeError("unexpected vision failure")

        coordinator._prepare_event = prepare

        asyncio.run(coordinator.prepare_batch([first, second], "ff:FriendMessage:user-1"))

        self.assertEqual(calls, ["第一张", "后续文字"])
        self.assertTrue(first.get_extra("astrmai_vision_barrier_complete"))
        self.assertTrue(first.get_extra("astrmai_vision_barrier_failed"))
        self.assertIn("禁止猜测", first.get_extra("astrmai_rich_text"))


if __name__ == "__main__":
    unittest.main()
