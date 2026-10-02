"""Stage 05 integrated chain replay.

Drives the real producer->consumer chain for topic attention continuity:
canonical event -> gateway history text -> lane persistence -> lane transcript
-> continuity anchor/bridge -> planning input loader -> prompt refiner.

All dialogue data is 脱敏 synthetic replay material; sender ids are fake.
"""

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.reply_artifact import VisibleReplyArtifact
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from tests.original_ported.helpers import _FakeConversationManager, _install_astrbot_stubs

CHAT = "default:GroupMessage:group-1"


def _artifact(text):
    return VisibleReplyArtifact(visible_text=text, segments=[text], persistable_text=text)


def _canonical(event_id, text, *, actor_id="u-alice", actor_name="小锦", **extra):
    return ConversationEvent(
        event_id=event_id,
        chat_id=CHAT,
        chat_kind="group",
        timestamp=time.time(),
        actor_id=actor_id,
        actor_name=actor_name,
        visible_text=text,
        rich_text=text,
        message_kind="text",
        role="user",
        **extra,
    )


class _HostEvent:
    def __init__(self, canonical=None, text="然后呢"):
        self._extras = {}
        if canonical is not None:
            self._extras["astrmai_conversation_event"] = canonical
        self.message_str = text
        self.unified_msg_origin = CHAT

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_name(self):
        return "小锦"


def _refine(event, envelope):
    return asyncio.run(
        PromptRefiner(memory_engine=None).refine_prompt(
            event=event,
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": True},
            prompt_envelope=envelope,
        )
    )


class IntegratedChainStage05Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.gateway_lane", None)
        self.lane_mod = importlib.reload(
            importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        )
        self.gateway_lane_mod = importlib.reload(
            importlib.import_module("astrmai.infrastructure.gateway.gateway_lane")
        )
        self.conversation_manager = _FakeConversationManager()
        self.lane_manager = self.lane_mod.LaneManager(self.conversation_manager)
        self.lane_key = self.lane_mod.LaneKey(
            subsystem="sys2", task_family="dialog", scope_id="group-1"
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _history_user_text(self, event_id, text):
        return self.gateway_lane_mod.GatewayLaneMixin._build_history_user_text(
            _HostEvent(_canonical(event_id, text)),
            raw_user_text=(
                f"[事件={event_id} | 发言人=小锦（ID:10001） | 角色=成员 | 类型=text]\n内容：{text}"
            ),
            prompt="包装提示词",
        )

    async def _append_turn(self, event_id, user_text, assistant_text, prefix_hash="prefix-a"):
        await self.lane_manager.append_visible_reply_artifact(
            lane_key=self.lane_key,
            base_origin=CHAT,
            raw_user_text="[事件=internal] 原始包装文本",
            artifact=_artifact(assistant_text),
            history_user_text=self._history_user_text(event_id, user_text),
            user_event_id=event_id,
            prefix_hash=prefix_hash,
        )

    # ── stage 00 + 04 ───────────────────────────────────────────────────────
    def test_canonical_user_turn_replays_into_lane_history_and_prompt_tail(self):
        async def _run():
            await self._append_turn(
                "ce-u-1",
                "人还好，就是什么都不记得了",
                "先确认一下，你是不记得最近发生的事情，还是更早的经历？",
            )
            await self._append_turn("ce-u-2", "就是刚才那段", "那就是刚才那段没印象。")
            lane_umo = self.lane_manager.resolve_lane_umo(CHAT, self.lane_key)
            conversation_id = await self.conversation_manager.get_curr_conversation_id(lane_umo)
            history = self.conversation_manager.conversations[conversation_id].history
            transcript = await self.lane_manager.get_recent_transcript(self.lane_key, CHAT)
            return history, transcript

        history, transcript = asyncio.run(_run())

        self.assertEqual(
            [item["role"] for item in history], ["user", "assistant", "user", "assistant"]
        )
        self.assertIn("人还好，就是什么都不记得了", history[0]["content"])
        self.assertNotIn("ce-u-1", history[0]["content"])
        self.assertNotIn("10001", history[0]["content"])
        self.assertLess(transcript.index("先确认一下"), transcript.index("就是刚才那段"))

        system, rendered = _refine(
            _HostEvent(text="那他后来答应了吗"),
            PromptEnvelope(
                raw_user_text="小锦: 那他后来答应了吗",
                focus_message_text="小锦: 那他后来答应了吗",
                recent_transcript=transcript,
                recent_transcript_source="lane",
            ),
        )
        self.assertEqual(system, "stable system prompt")
        self.assertIn("---对话记录", rendered)
        self.assertIn("先确认一下，你是不记得最近发生的事情", rendered)

    # ── stage 04 contract on the production producer path ───────────────────
    def test_recent_transcript_publishes_canonical_event_ids_for_dedup(self):
        """Producer metadata survives persistence without entering visible text."""
        async def _run():
            await self._append_turn("ce-id-1", "问题正文", "回答正文")
            return await self.lane_manager.get_recent_transcript(
                self.lane_key, CHAT, include_event_ids=True
            )

        transcript, event_ids = asyncio.run(_run())
        self.assertEqual(len(transcript.splitlines()), 2)
        self.assertIn("ce-id-1", event_ids)

    def test_recent_event_id_survives_rotation_and_old_history_without_id(self):
        async def _run():
            await self._append_turn("ce-id-1", "问题正文", "回答正文")
            await self._append_turn("ce-id-2", "后续问题", "后续回答", prefix_hash="prefix-b")
            lane_umo = self.lane_manager.resolve_lane_umo(CHAT, self.lane_key)
            conversation_id = await self.conversation_manager.get_curr_conversation_id(lane_umo)
            history = self.conversation_manager.conversations[conversation_id].history
            transcript, ids = await self.lane_manager.get_recent_transcript(
                self.lane_key, CHAT, include_event_ids=True
            )
            return history, transcript, ids

        history, transcript, ids = asyncio.run(_run())
        self.assertIn("ce-id-1", ids)
        self.assertIn("ce-id-2", ids)
        self.assertNotIn("ce-id-1", transcript)
        self.assertTrue(any(turn.get("event_id") == "ce-id-1" for turn in history))

    # ── stage 01 + 04 ───────────────────────────────────────────────────────
    def test_prefix_rotation_seed_reaches_prompt_with_latest_real_pair(self):
        async def _run():
            lane_umo, conversation_id, _, _ = await self.lane_manager.ensure_lane(
                lane_key=self.lane_key, base_origin=CHAT, prefix_hash="prefix-a"
            )
            history = []
            for index in range(5):
                history.append(
                    {
                        "role": "user",
                        "content": f"小锦: 我在考虑换工作 {index}",
                        "timestamp": 1_000_000.0 + index,
                    }
                )
                history.append(
                    {
                        "role": "assistant",
                        "content": f"你最在意薪资、环境还是发展？ {index}",
                        "timestamp": 1_000_000.5 + index,
                    }
                )
            await self.conversation_manager.update_conversation(
                unified_msg_origin=lane_umo,
                conversation_id=conversation_id,
                history=history,
            )
            _, new_id, rotated, _ = await self.lane_manager.ensure_lane(
                lane_key=self.lane_key, base_origin=CHAT, prefix_hash="prefix-b"
            )
            transcript = await self.lane_manager.get_recent_transcript(self.lane_key, CHAT)
            return conversation_id, new_id, rotated, transcript

        old_id, new_id, rotated, transcript = asyncio.run(_run())
        self.assertNotEqual(old_id, new_id)
        self.assertLessEqual(len(rotated), 7)
        self.assertIn("我在考虑换工作 4", transcript)
        self.assertIn("薪资、环境还是发展？ 4", transcript)
        self.assertNotIn("历史上下文摘要（轮换桥接", transcript)

        system, rendered = _refine(
            _HostEvent(text="主要是环境"),
            PromptEnvelope(
                raw_user_text="小锦: 主要是环境",
                focus_message_text="小锦: 主要是环境",
                recent_transcript=transcript,
                recent_transcript_source="lane",
                warm_zone_summary="更早的对话摘要：小锦在考虑换工作。",
                warm_zone_transcript_source="store",
            ),
        )
        self.assertEqual(system, "stable system prompt")
        self.assertLess(rendered.index("---对话记录"), rendered.index("---近期对话脉络"))
        self.assertIn("薪资、环境还是发展？ 4", rendered)

    def _production_bridge_case(self, gap_seconds):
        """Replay one topic boundary at a given real elapsed gap.

        Nothing here is mocked inside the store: the gap is produced by
        recording the previous topic in the past, so evaluate_group_message,
        snapshot() and evaluate_topic_bridge all see the same wall clock the
        host would see.
        """
        store = ConversationContinuityStore()
        past = time.time() - gap_seconds
        store.record(
            chat_id=CHAT,
            focus_preview="外星肉包章鱼人是什么",
            reply_preview="我还不确定你说的是哪一个设定。",
            sender_id="u-alice",
            source_event_id="old-1",
            anchor_event=_canonical("old-1", "外星肉包章鱼人是什么"),
            topic_epoch=1,
            now=past,
        )
        policy = store.evaluate_group_message(
            CHAT,
            "就是小锦家里那个",
            sender_id="u-alice",
            has_reply_reference=True,
            approved_event_ids=("old-1",),
        )
        snapshot = store.snapshot(CHAT)
        anchor_epoch = int((snapshot.get("topic_anchor") or {}).get("topic_epoch", 0) or 0)
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=_canonical(
                "ce-new", "回复上一条：就是小锦家里那个", reply_target_event_id="old-1"
            ),
            target_topic_epoch=policy.topic_epoch,
            rotation_reason=policy.rotation_reason,
        )
        return {
            "store": store,
            "policy": policy,
            "snapshot": snapshot,
            # planning_input_loader.py:568 gate
            "bridge_branch_taken": bool(
                policy.group_id and anchor_epoch and policy.topic_epoch != anchor_epoch
            ),
            "decision": decision,
        }

    def test_elapsed_gap_alone_never_overrides_bridge_ttl(self):
        """Age alone does not create a topic switch or revive expired evidence."""
        expectations = {
            60: ("", "same_topic"),
            300: ("", "same_topic"),
            1210: ("weak_window_evidence", "same_topic"),
            1750: ("weak_window_evidence", "same_topic"),
            1810: ("topic_stale", "no_source_anchor"),
            3600: ("topic_stale", "no_source_anchor"),
        }
        for gap, (rotation_reason, bridge_reason) in expectations.items():
            with self.subTest(gap=gap):
                case = self._production_bridge_case(gap)
                self.assertEqual(case["policy"].rotation_reason, rotation_reason)
                self.assertFalse(case["bridge_branch_taken"])
                self.assertFalse(case["decision"].allowed)
                self.assertEqual(case["decision"].reason, bridge_reason)
                self.assertEqual(case["decision"].prompt_text(), "")

    def test_stale_topic_boundary_rejects_reply_after_bridge_ttl(self):
        """A 1810-second-old reply target is outside the 120-second bridge TTL."""
        case = self._production_bridge_case(1810)
        decision = case["decision"]
        self.assertFalse(case["bridge_branch_taken"])
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "no_source_anchor")

    def test_stale_topic_boundary_does_not_inject_expired_context(self):
        """The real loader must not inject expired context."""
        case = self._production_bridge_case(1810)
        loader = PlanningInputLoader(
            SimpleNamespace(conversation_continuity=case["store"])
        )

        class _LoaderEvent(_HostEvent):
            def __init__(self, canonical):
                super().__init__(canonical, text="回复上一条：就是小锦家里那个")
                case["policy"].bind(self)

        event = _LoaderEvent(
            _canonical("ce-new", "回复上一条：就是小锦家里那个", reply_target_event_id="old-1")
        )
        loader._apply_continuity(event, loader._continuity_snapshot(CHAT))
        context = ensure_turn_context(event)
        self.assertEqual(
            (case["snapshot"].get("topic_anchor") or {}).get("subject_preview", ""), ""
        )
        self.assertFalse(context.continuity.topic_bridge.allowed)
        self.assertEqual(event.get_extra("astrmai_topic_attention_anchor_prompt", ""), "")
        self.assertEqual(event.get_extra("astrmai_cross_topic_bridge_prompt", ""), "")

    def test_fresh_boundary_keeps_anchor_but_still_does_not_bridge(self):
        """Within the active ttl the old anchor is preserved but not rotated."""
        case = self._production_bridge_case(60)
        subject = (case["snapshot"].get("topic_anchor") or {}).get("subject_preview", "")
        self.assertIn("外星肉包章鱼人", subject)
        self.assertFalse(case["decision"].allowed)

    # ── stage 02 / 03 / 04 layering with all dynamic sources at once ────────
    def test_all_dynamic_sections_keep_order_identity_and_shared_budget(self):
        store = ConversationContinuityStore()
        now = time.time()
        store.record(
            chat_id=CHAT,
            focus_preview="小锦家的那只猫又跑出去了",
            reply_preview="它上次也是从窗户出去的吗？",
            sender_id="u-alice",
            source_event_id="cat-1",
            anchor_event=_canonical("cat-1", "小锦家的那只猫又跑出去了"),
            topic_epoch=1,
            now=now,
        )
        anchor_prompt = store.topic_anchor_prompt(CHAT, now=now)
        self.assertTrue(anchor_prompt)
        self.assertIn("猫", anchor_prompt)
        self.assertLessEqual(len(anchor_prompt), 900)
        view = store.topic_anchor_view(CHAT, now=now)
        self.assertEqual(view["open_loop"], "它上次也是从窗户出去的吗？")
        self.assertEqual(list(view["recent_event_ids"]), ["cat-1"])

        recent = "\n".join(
            f"小锦: 问题 {index} " + "U" * 90 + "\nBot: 回答 " + "A" * 90
            for index in range(6)
        )
        bridge = store.evaluate_topic_bridge(
            CHAT,
            event=_canonical("cat-2", "这次是门", reply_target_event_id="unknown"),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 10,
        )
        self.assertFalse(bridge.allowed)
        self.assertEqual(bridge.reason, "unverified_reply_target")
        self.assertEqual(bridge.prompt_text(), "")

        envelope = PromptEnvelope(
            raw_user_text="小锦: 后来它自己回来了",
            focus_message_text="小锦: 后来它自己回来了",
            recent_transcript=recent,
            recent_transcript_source="lane",
            topic_attention_anchor_block=anchor_prompt,
            topic_attention_anchor_event_ids=["cat-1"],
            cross_topic_bridge_block="跨话题临时承接（仅供本轮参考）：\n- subject=更早话题",
            cross_topic_bridge_event_ids=["earlier-1"],
            warm_zone_summary="更早的群聊摘要：" + "S" * 1200,
            warm_zone_quotes="小锦: 问题 0 " + "U" * 90 + "\nBot: 回答 " + "A" * 90,
            warm_zone_quote_event_ids=["cat-1"],
        )
        system, rendered = _refine(
            _HostEvent(text="后来它自己回来了"),
            envelope,
        )
        self.assertEqual(system, "stable system prompt")
        self.assertIn("后来它自己回来了", rendered)
        self.assertIn("---对话记录", rendered)
        self.assertIn("---话题注意力锚点", rendered)
        self.assertIn("---跨话题桥接", rendered)
        self.assertIn('source="topic_attention_anchor"', rendered)
        self.assertIn('source="cross_topic_bridge"', rendered)
        self.assertNotIn("[escaped:untrusted_context]", rendered)
        if "---近期对话脉络" in rendered:
            self.assertLess(
                rendered.index("---对话记录"), rendered.index("---近期对话脉络")
            )
        # the warm copy of the same event never duplicates the real tail line
        self.assertEqual(rendered.count("问题 0 "), 1)
        self.assertNotIn("S" * 600, rendered)
        self.assertIn("问题 5", rendered)
        self.assertNotIn("cat-1", rendered)
        self.assertGreater(envelope.recent_context_rendered_chars, 0)
        self.assertTrue(any("warm" in item for item in envelope.flex_context_trimmed_sections))
        self.assertLess(
            envelope.warm_context_rendered_chars,
            len("更早的群聊摘要：") + 1200,
        )
        self.assertLessEqual(
            envelope.recent_context_rendered_chars
            + envelope.warm_context_rendered_chars,
            PromptRefiner.FLEX_CONTEXT_BUDGET_CHARS,
        )


if __name__ == "__main__":
    unittest.main()
