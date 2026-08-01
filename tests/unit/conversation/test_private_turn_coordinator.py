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
        self.unified_msg_origin = "ff:FriendMessage:user-1"
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


class _SlowVisualCortex:
    def __init__(self):
        self.calls = 0

    async def analyze_image_path(self, picid, image_path, scope_id="global", timeout_override=None):
        self.calls += 1
        await asyncio.sleep(0.2)
        return {"type": "image", "description": "迟到的图片描述", "emotion_tags": []}


class _PartialVisualCortex:
    def __init__(self):
        self.calls = 0

    async def analyze_image_path(self, picid, image_path, scope_id="global", timeout_override=None):
        self.calls += 1
        if self.calls == 1:
            return {"type": "image", "description": "第一张识别成功", "emotion_tags": []}
        return None


class _TwoImageResolver:
    async def resolve_event_images(self, event):
        from astrmai.multimodal.napcat_image_resolver import ImageResolutionBatch, ResolvedImage

        return ImageResolutionBatch(
            had_images=True,
            images=[
                ResolvedImage(index=0, source_ref="first", local_path="first.jpg"),
                ResolvedImage(index=1, source_ref="second", local_path="second.jpg"),
            ],
        )


class _PerEventResolver:
    async def resolve_event_images(self, event):
        from astrmai.multimodal.napcat_image_resolver import ImageResolutionBatch, ResolvedImage

        message_id = str(event.message_obj.message_id)
        return ImageResolutionBatch(
            had_images=True,
            images=[
                ResolvedImage(
                    index=0,
                    source_ref=message_id,
                    local_path=f"{message_id}.jpg",
                )
            ],
        )


class _MixedSpeedVisualCortex:
    async def analyze_image_path(self, picid, image_path, scope_id="global", timeout_override=None):
        if image_path == "slow.jpg":
            await asyncio.sleep(0.2)
        return {"type": "image", "description": f"识别完成:{image_path}", "emotion_tags": []}


class _Persistence:
    def __init__(self):
        self.added = []
        self.marked = []

    async def add_last_message_meta(self, chat_id, sender_id, has_image, image_ids):
        self.added.append((chat_id, sender_id, has_image, image_ids))

    async def mark_last_message_vision_executed(self, chat_id, sender_id):
        self.marked.append((chat_id, sender_id))


class PrivateTurnCoordinatorTests(unittest.TestCase):
    def test_vision_timeout_defaults_match_slow_model_profile(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(),
            timing=SimpleNamespace(),
            vision=SimpleNamespace(),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)

        self.assertEqual(coordinator._vision_resolve_timeout(), 15.0)
        self.assertEqual(coordinator._vision_analysis_timeout(), 90.0)
        self.assertEqual(coordinator._vision_total_timeout(), 300.0)

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

    def test_bind_batch_context_merges_distinct_messages_in_order(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(turn_merge_enabled=True),
            vision=SimpleNamespace(enable_vision=True),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        events = [_Event("额妃爱你注意力强吗？"), _Event("就是如果一个话题开启"), _Event("会一直聊下去吗？")]
        for index, event in enumerate(events):
            event.message_obj.message_id = f"m{index}"

        coordinator.bind_batch_context(events, events[-1])

        self.assertEqual(
            events[-1].get_extra("astrmai_private_batch_text"),
            "额妃爱你注意力强吗？\n就是如果一个话题开启\n会一直聊下去吗？",
        )
        self.assertEqual(events[-1].get_extra("astrmai_private_batch_message_ids"), ["m0", "m1", "m2"])
        self.assertEqual(events[-1].get_extra("astrmai_private_batch_message_count"), 3)
        self.assertTrue(events[-1].get_extra("astrmai_private_batch_merged"))

    def test_turn_merge_flag_restores_single_event_legacy_behavior(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(private_chat=SimpleNamespace(turn_merge_enabled=False))
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        first = _Event("第一句")
        second = _Event("第二句")

        self.assertEqual(coordinator.merge_pending_batch("ff:FriendMessage:user-1", [second]), [second])
        self.assertEqual(coordinator.begin_pending_batch("ff:FriendMessage:user-1", [first]), 0)
        coordinator.bind_batch_context([first, second], second)
        self.assertIsNone(second.get_extra("astrmai_private_batch_text"))

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

    def test_batch_image_description_is_appended_once_after_text_merge(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                turn_merge_enabled=True,
                input_settle_sec=0.0,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=1,
            ),
            vision=SimpleNamespace(enable_vision=True),
        )
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_Resolver(),
            visual_cortex=_VisualCortex(),
            persistence=_Persistence(),
        )
        first = _Event("还记得我们一起去泡温泉吗？")
        first.message_obj.message_id = "m-image"
        second = _Event("妃爱？")
        second.message_obj.message_id = "m-nudge"

        asyncio.run(coordinator.prepare_batch([first], "ff:FriendMessage:user-1"))
        second.set_extra("astrmai_vision_barrier_complete", True)
        coordinator.bind_batch_context([first, second], second)

        rich_text = second.get_extra("astrmai_rich_text")
        self.assertIn("还记得我们一起去泡温泉吗？\n妃爱？", rich_text)
        self.assertEqual(rich_text.count("一只白猫坐在窗边"), 1)

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

        async def prepare(event, chat_id, **_kwargs):
            calls.append(event.message_str)
            if event is first:
                raise RuntimeError("unexpected vision failure")

        coordinator._prepare_event = prepare

        asyncio.run(coordinator.prepare_batch([first, second], "ff:FriendMessage:user-1"))

        self.assertEqual(calls, ["第一张", "后续文字"])
        self.assertTrue(first.get_extra("astrmai_vision_barrier_complete"))
        self.assertTrue(first.get_extra("astrmai_vision_barrier_failed"))
        self.assertIn("[图片]", first.get_extra("astrmai_rich_text"))

    def test_timeout_fallback_uses_placeholder_and_clears_image_refs(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.0,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=5,
            ),
            timing=SimpleNamespace(
                image_resolve_timeout_sec=1.0,
                image_analysis_timeout_sec=1.0,
                vision_barrier_total_timeout_sec=0.05,
            ),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
                image_analysis_retries=5,
            ),
        )
        persistence = _Persistence()
        cortex = _SlowVisualCortex()
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_Resolver(),
            visual_cortex=cortex,
            persistence=persistence,
        )
        event = _Event("你看")

        started = time.monotonic()
        outcome = asyncio.run(coordinator.prepare_batch([event], "ff:FriendMessage:user-1"))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(outcome.downstream_action, "continue_with_placeholder")
        self.assertEqual(outcome.timeout_count, 1)
        self.assertEqual(cortex.calls, 1)
        self.assertEqual(event.get_extra("direct_image_refs"), [])
        self.assertEqual(event.get_extra("extracted_image_refs"), [])
        self.assertIn("你看", event.get_extra("astrmai_rich_text"))
        self.assertIn("[图片]", event.get_extra("astrmai_rich_text"))
        self.assertTrue(event.get_extra("astrmai_vision_no_guess"))
        self.assertEqual(persistence.marked, [])

    def test_required_vision_failure_aborts_without_placeholder_reply_context(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.0,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=2,
            ),
            timing=SimpleNamespace(
                image_resolve_timeout_sec=1.0,
                image_analysis_timeout_sec=1.0,
                vision_barrier_total_timeout_sec=0.05,
            ),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="必须识别成功后再回复",
                image_analysis_retries=2,
            ),
        )
        persistence = _Persistence()
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_Resolver(),
            visual_cortex=_SlowVisualCortex(),
            persistence=persistence,
        )
        event = _Event("这张图是什么")

        outcome = asyncio.run(coordinator.prepare_direct_event(event, "ff:GroupMessage:group-1"))

        self.assertEqual(outcome.downstream_action, "abort_required_vision")
        self.assertTrue(outcome.should_abort)
        self.assertTrue(event.get_extra("astrmai_vision_required_failed"))
        self.assertNotIn("[图片]", event.get_extra("astrmai_rich_text", ""))
        self.assertEqual(persistence.marked, [])

    def test_partial_success_keeps_successful_description_but_does_not_mark_complete(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.0,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=1,
            ),
            timing=SimpleNamespace(
                image_resolve_timeout_sec=1.0,
                image_analysis_timeout_sec=1.0,
                vision_barrier_total_timeout_sec=1.0,
            ),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
                image_analysis_retries=1,
            ),
        )
        persistence = _Persistence()
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_TwoImageResolver(),
            visual_cortex=_PartialVisualCortex(),
            persistence=persistence,
        )
        event = _Event("两张图")

        outcome = asyncio.run(coordinator.prepare_batch([event], "ff:FriendMessage:user-1"))

        self.assertEqual(outcome.image_count, 2)
        self.assertEqual(outcome.analyzed_count, 1)
        self.assertEqual(outcome.failed_count, 1)
        self.assertEqual(outcome.downstream_action, "continue_with_placeholder")
        self.assertIn("第一张识别成功", event.get_extra("astrmai_rich_text"))
        self.assertEqual(event.get_extra("astrmai_rich_text").count("[图片]"), 1)
        self.assertEqual(persistence.marked, [])

    def test_fallback_does_not_duplicate_existing_image_placeholder(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
            ),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        event = _Event("你看 [图片]")

        outcome = coordinator._apply_failed_policy(
            event,
            image_count=1,
            failed_count=1,
            timeout_count=1,
            elapsed_ms=10,
            outcome="timeout",
        )

        self.assertEqual(outcome.downstream_action, "continue_with_placeholder")
        self.assertEqual(event.get_extra("astrmai_rich_text").count("[图片]"), 1)

    def test_image_only_fallback_is_marked_nonsemantic(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
            ),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        event = _Event("obj_len_301 [图片]")

        coordinator._apply_failed_policy(
            event,
            image_count=1,
            failed_count=1,
            timeout_count=1,
            elapsed_ms=10,
            outcome="timeout",
        )

        self.assertTrue(event.get_extra("astrmai_media_status_nonsemantic"))
        self.assertTrue(event.get_extra("astrmai_media_only_failure"))
        self.assertTrue(event.get_extra("astrmai_media_status")["image_only"])

    def test_text_with_image_fallback_remains_semantically_persistable(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
            ),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        event = _Event("你看这张图 [图片]")

        coordinator._apply_failed_policy(
            event,
            image_count=1,
            failed_count=1,
            timeout_count=1,
            elapsed_ms=10,
            outcome="timeout",
        )

        self.assertTrue(event.get_extra("astrmai_media_status_nonsemantic"))
        self.assertFalse(event.get_extra("astrmai_media_only_failure"))
        self.assertFalse(event.get_extra("astrmai_media_status")["image_only"])

    def test_outcome_summary_uses_real_scope_and_excludes_image_content(self):
        from astrmai.conversation.attention.private_turn_coordinator import (
            PrivateTurnCoordinator,
            VisionBarrierOutcome,
        )

        event = _Event("敏感图片正文")
        outcome = VisionBarrierOutcome(
            policy="timeout_fallback",
            outcome="timeout",
            image_count=1,
            failed_count=1,
            timeout_count=1,
            elapsed_ms=50,
            downstream_action="continue_with_placeholder",
        )

        PrivateTurnCoordinator._record_outcome(event, outcome, "")

        payload = event.get_extra("astrmai_vision_barrier_outcome")
        self.assertEqual(payload["scope"], "private")
        self.assertEqual(payload["downstream_action"], "continue_with_placeholder")
        self.assertNotIn("description", payload)
        self.assertNotIn("content", payload)
        self.assertNotIn("raw_query", payload)

    def test_vision_observation_records_statuses_and_opaque_memory_ids(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator
        from astrmai.infrastructure.runtime.turn_call_ledger import turn_telemetry_snapshot

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.0,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=1,
            ),
            timing=SimpleNamespace(
                image_resolve_timeout_sec=1.0,
                image_analysis_timeout_sec=1.0,
                vision_barrier_total_timeout_sec=2.0,
            ),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
                image_analysis_retries=1,
            ),
        )
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_Resolver(),
            visual_cortex=_VisualCortex(),
        )
        event = _Event("帮我看看")
        event.set_extra("extracted_image_refs", ["https://example.invalid/image.jpg"])

        outcome = asyncio.run(
            coordinator.prepare_direct_event(event, "ff:FriendMessage:user-1")
        )

        self.assertEqual(outcome.outcome, "success")
        observation = event.get_extra("astrmai_vision_observability")
        self.assertEqual(observation["image_source"], ["url"])
        self.assertEqual(observation["image_resolve_status"], "success")
        self.assertEqual(observation["vision_barrier_status"], "completed")
        self.assertFalse(observation["vision_fallback"])
        self.assertTrue(observation["visual_memory_id"])
        self.assertNotIn("description", observation)
        snapshot = turn_telemetry_snapshot(event)
        self.assertEqual(snapshot["vision_observation"]["image_count"], 1)
        self.assertNotIn("resolved.jpg", str(snapshot["vision_observation"]))

    def test_missing_resolver_obeys_required_vision_policy_for_image_event(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="必须识别成功后再回复",
            ),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        event = _Event("帮我看看")
        event.set_extra("extracted_image_refs", ["opaque-image-ref"])

        outcome = asyncio.run(
            coordinator.prepare_direct_event(event, "ff:GroupMessage:group-1")
        )

        self.assertTrue(outcome.should_abort)
        self.assertEqual(outcome.outcome, "resolver_unavailable")
        self.assertTrue(event.get_extra("astrmai_vision_required_failed"))

    def test_missing_resolver_does_not_block_text_only_event(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="必须识别成功后再回复",
            ),
        )
        coordinator = PrivateTurnCoordinator(config=config, image_resolver=None, visual_cortex=None)
        event = _Event("纯文字")

        outcome = asyncio.run(
            coordinator.prepare_direct_event(event, "ff:GroupMessage:group-1")
        )

        self.assertFalse(outcome.should_abort)
        self.assertEqual(outcome.outcome, "resolver_unavailable")

    def test_slow_vision_in_one_session_does_not_block_another_session(self):
        from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

        config = SimpleNamespace(
            private_chat=SimpleNamespace(
                input_settle_sec=0.0,
                image_resolve_timeout_sec=1.0,
                image_barrier_timeout_sec=1.0,
                image_analysis_retries=1,
            ),
            timing=SimpleNamespace(
                image_resolve_timeout_sec=1.0,
                image_analysis_timeout_sec=1.0,
                vision_barrier_total_timeout_sec=0.05,
            ),
            vision=SimpleNamespace(
                enable_vision=True,
                vision_reply_policy="超时后忽略图片并继续回复",
                image_analysis_retries=1,
            ),
        )
        coordinator = PrivateTurnCoordinator(
            config=config,
            image_resolver=_PerEventResolver(),
            visual_cortex=_MixedSpeedVisualCortex(),
            persistence=_Persistence(),
        )
        slow_event = _Event("慢图")
        slow_event.message_obj.message_id = "slow"
        fast_event = _Event("快图")
        fast_event.message_obj.message_id = "fast"

        async def run():
            slow_task = asyncio.create_task(
                coordinator.prepare_direct_event(slow_event, "ff:FriendMessage:slow-user")
            )
            fast_task = asyncio.create_task(
                coordinator.prepare_direct_event(fast_event, "ff:FriendMessage:fast-user")
            )
            fast_outcome = await fast_task
            slow_done_when_fast_completed = slow_task.done()
            slow_outcome = await slow_task
            return fast_outcome, slow_done_when_fast_completed, slow_outcome

        fast_outcome, slow_done_when_fast_completed, slow_outcome = asyncio.run(run())

        self.assertEqual(fast_outcome.outcome, "success")
        self.assertFalse(slow_done_when_fast_completed)
        self.assertEqual(slow_outcome.downstream_action, "continue_with_placeholder")


if __name__ == "__main__":
    unittest.main()
