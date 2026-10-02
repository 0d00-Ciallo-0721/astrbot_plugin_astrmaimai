"""Round-4 independent replay for S5-24 (outbound ID capacity direction).

Written by the verification agent. Business source untouched; no existing test
assertion modified. Unlike the round-3 file (whose S5-24 case was rewritten by
the fixing agent), this file re-derives the whole contract from scratch through
the production call order:

    send result -> ReplySendReceipt.outbound_message_ids -> CommittedBotTurn
    -> extra "astrmai_committed_bot_turn" -> Planner._record_conversation_continuity
    -> ConversationTurnRecord.assistant_outbound_ids -> topic rotation
    -> next inbound reply target -> PlanningInputLoader -> BridgeDecision
    -> final dynamic prompt

All IDs are 脱敏 synthetic replay material.
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.dialog_history_policy import DialogHistoryPolicy
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.contracts.turn_target import TargetKind, TurnTarget
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner import Planner
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.planning.prompt_refiner import PromptRefiner

CHAT = "default:GroupMessage:group-s524"
OLD_TEXT = "项目计划的排期"
BOT_REPLY = "你周五有空讨论排期吗？"


class _Reply:
    type = "reply"

    def __init__(self, message_id):
        self.id = message_id
        self.message_id = message_id
        self.sender_id = "bot-1"
        self.sender_nickname = "小明"


class _Host:
    def __init__(self, *, text, platform_id="", reply_to="", sender="u-alice"):
        self.unified_msg_origin = CHAT
        self.message_str = text
        self.message_id = platform_id
        self.message_obj = SimpleNamespace(
            message=[_Reply(reply_to)] if reply_to else [], message_id=platform_id
        )
        self.timestamp = time.time()
        self._sender = sender
        self._extras = {}

    def get_group_id(self):
        return "group-s524"

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "小锦"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _committed_turn(*outbound_ids):
    target = TurnTarget(
        target_kind=TargetKind.MESSAGE,
        target_event_id="user-1",
        topic_epoch=1,
        source_event_ids=("user-1",),
    )
    plan = ReplyPlan.create(
        turn_id="turn-s524",
        chat_id=CHAT,
        chat_kind="group",
        target=target,
        planned_text=BOT_REPLY,
        planned_segments=(BOT_REPLY,),
        created_at=time.time(),
    )
    receipt = ReplySendReceipt(
        status=ReplyCommitStatus.SENT,
        sent_segments=(BOT_REPLY,),
        outbound_message_ids=outbound_ids,
        visible_text=BOT_REPLY,
        persistable_text=BOT_REPLY,
        sent_at=time.time(),
    )
    return CommittedBotTurn.from_plan(plan, receipt)


class S524Round4ReplayTests(unittest.TestCase):
    def setUp(self):
        self.store = ConversationContinuityStore()
        self.planner = object.__new__(Planner)
        self.planner.conversation_continuity = self.store
        self.loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=self.store))

    def _commit_previous_topic(self, outbound_ids, *, user_platform_id="user-1"):
        host = _Host(text=OLD_TEXT, platform_id=user_platform_id)
        canonical = ConversationEvent.from_astr_event(host, self_id="bot-1", topic_epoch=1)
        host.set_extra("astrmai_conversation_event", canonical)
        DialogHistoryPolicy(
            group_id=CHAT, topic_epoch=1, current_sender_id="u-alice"
        ).bind(host)
        host.set_extra("astrmai_committed_bot_turn", _committed_turn(*outbound_ids))
        self.planner._record_conversation_continuity(
            CHAT,
            PromptEnvelope(raw_user_text=OLD_TEXT, focus_message_text=OLD_TEXT),
            BOT_REPLY,
            [],
            SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
            event=host,
        )
        return self.store._state(CHAT).turns[-1]

    def _quote(self, quoted_id, *, text="回复机器人上一条：排期", platform_id="user-9"):
        host = _Host(text=text, platform_id=platform_id, reply_to=quoted_id)
        canonical = ConversationEvent.from_astr_event(host, self_id="bot-1", topic_epoch=2)
        host.set_extra("astrmai_conversation_event", canonical)
        DialogHistoryPolicy(
            group_id=CHAT, topic_epoch=2, rotation_reason="explicit_history_recall",
            current_sender_id="u-alice",
        ).bind(host)
        self.loader._apply_continuity(host, self.loader._continuity_snapshot(CHAT))
        return host, ensure_turn_context(host).continuity.topic_bridge

    def _render(self, host, decision):
        envelope = PromptEnvelope(
            raw_user_text=host.message_str,
            focus_message_text=host.message_str,
            cross_topic_bridge_block=host.get_extra("astrmai_cross_topic_bridge_prompt", ""),
            cross_topic_bridge_event_ids=list(decision.evidence_event_ids),
        )
        return asyncio.run(
            PromptRefiner(memory_engine=None).refine_prompt(
                event=host,
                system_prompt="stable system prompt",
                prompt="",
                context={"disable_rag_injection": True},
                prompt_envelope=envelope,
            )
        )

    # ── 三. 20 个真实 outbound ID 的容量与尾部语义 ────────────────────────
    def test_twenty_outbound_ids_keep_bounded_tail(self):
        ids = tuple(f"bot-{i}" for i in range(1, 21))
        turn = self._commit_previous_topic(ids)
        limit = self.store.ANCHOR_MAX_EVENT_IDS
        self.assertEqual(limit, 12, "容量上限不得被扩大")
        self.assertEqual(len(turn.assistant_outbound_ids), limit)
        self.assertEqual(turn.assistant_outbound_ids, ids[-limit:])
        self.assertEqual(turn.assistant_outbound_ids, tuple(f"bot-{i}" for i in range(9, 21)))
        self.assertIn("bot-20", turn.assistant_outbound_ids)
        self.assertNotIn("bot-1", turn.assistant_outbound_ids)

    def test_newest_outbound_id_is_valid_quote_target(self):
        ids = tuple(f"bot-{i}" for i in range(1, 21))
        self._commit_previous_topic(ids)
        host, decision = self._quote("bot-20")
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.reason, "explicit_reply")
        self.assertEqual(decision.confidence, 0.95)
        self.assertIn("bot-20", decision.evidence_event_ids)
        self.assertEqual(decision.evidence_event_ids, ("bot-20",))
        stable, rendered = self._render(host, decision)
        self.assertEqual(stable, "stable system prompt")
        self.assertIn("---跨话题桥接", rendered)
        self.assertNotIn("bot-20", rendered)

    def test_boundary_and_middle_retained_ids_are_valid_targets(self):
        ids = tuple(f"bot-{i}" for i in range(1, 21))
        self._commit_previous_topic(ids)
        for quoted in ("bot-9", "bot-13", "bot-20"):
            with self.subTest(quoted=quoted):
                _, decision = self._quote(quoted, platform_id=f"quote-{quoted}")
                self.assertTrue(decision.allowed, decision.reason)
                self.assertEqual(decision.reason, "explicit_reply")
                self.assertEqual(decision.evidence_event_ids, (quoted,))

    def test_capacity_evicted_head_id_is_denied_without_fabricated_evidence(self):
        ids = tuple(f"bot-{i}" for i in range(1, 21))
        self._commit_previous_topic(ids)
        for quoted in ("bot-1", "bot-2", "bot-8"):
            with self.subTest(quoted=quoted):
                _, decision = self._quote(quoted, platform_id=f"quote-{quoted}")
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "unverified_reply_target")
                self.assertEqual(decision.evidence_event_ids, ())
                self.assertEqual(decision.prompt_text(), "")

    def test_duplicate_outbound_ids_deduped_before_capacity(self):
        # 重复 + 超容量：先显式去重，再保留尾部，顺序遵循既有合同（发送顺序）
        raw = ("bot-1", "bot-1", "bot-2") + tuple(f"bot-{i}" for i in range(3, 23)) + ("bot-22",)
        turn = self._commit_previous_topic(raw)
        unique = tuple(dict.fromkeys(raw))
        self.assertEqual(len(turn.assistant_outbound_ids), 12)
        self.assertEqual(turn.assistant_outbound_ids, unique[-12:])
        self.assertEqual(len(set(turn.assistant_outbound_ids)), 12)
        _, newest = self._quote("bot-22", platform_id="u-new")
        self.assertTrue(newest.allowed, newest.reason)
        self.assertEqual(newest.evidence_event_ids, ("bot-22",))

    def test_planner_and_store_use_one_capacity_rule(self):
        """The only remaining bound must be the store's tail rule."""
        ids = tuple(f"bot-{i}" for i in range(1, 26))
        turn = self._commit_previous_topic(ids)
        direct = ConversationContinuityStore()
        canonical = ConversationEvent.from_astr_event(
            _Host(text=OLD_TEXT, platform_id="user-1"), self_id="bot-1", topic_epoch=1
        )
        direct.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=BOT_REPLY,
            goal_summary=BOT_REPLY,
            sender_id="u-alice",
            source_event_id=canonical.event_id,
            assistant_outbound_ids=ids,
            anchor_event=canonical,
            topic_epoch=1,
        )
        self.assertEqual(
            turn.assistant_outbound_ids,
            direct._state(CHAT).turns[-1].assistant_outbound_ids,
        )
        self.assertEqual(turn.assistant_outbound_ids, ids[-12:])

    def test_anchor_evidence_ids_stay_within_the_same_bound(self):
        ids = tuple(f"bot-{i}" for i in range(1, 21))
        self._commit_previous_topic(ids)
        state = self.store._state(CHAT)
        self.assertLessEqual(len(state.topic_anchor.recent_event_ids), self.store.ANCHOR_MAX_EVENT_IDS)
        self.assertLessEqual(len(state.event_id_map), self.store.ANCHOR_MAX_EVENT_IDS * 2)

    # ── 四. S5-19 与原有功能在本轮修复后不得回归 ──────────────────────────
    def test_single_segment_bot_quote_still_bridges(self):
        turn = self._commit_previous_topic(("bot-only-1",))
        self.assertEqual(turn.assistant_outbound_ids, ("bot-only-1",))
        _, decision = self._quote("bot-only-1")
        self.assertTrue(decision.allowed, decision.reason)
        self.assertEqual(decision.reason, "explicit_reply")
        self.assertEqual(decision.evidence_event_ids, ("bot-only-1",))

    def test_user_quote_and_two_weak_paths_still_bridge(self):
        self._commit_previous_topic(("bot-a", "bot-b"))
        _, explicit_user = self._quote("user-1", text="回复上一条：排期")
        self.assertTrue(explicit_user.allowed, explicit_user.reason)
        self.assertEqual(explicit_user.evidence_event_ids, ("user-1",))

        store = ConversationContinuityStore()
        canonical = ConversationEvent.from_astr_event(
            _Host(text=OLD_TEXT, platform_id="user-1"), self_id="bot-1", topic_epoch=1
        )
        store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=BOT_REPLY,
            goal_summary=BOT_REPLY,
            sender_id="u-alice",
            source_event_id=canonical.event_id,
            assistant_outbound_ids=("bot-a",),
            anchor_event=canonical,
            topic_epoch=1,
        )
        open_loop = store.evaluate_topic_bridge(
            CHAT,
            event=ConversationEvent.from_astr_event(
                _Host(text="周五有空", platform_id="user-2"), self_id="bot-1", topic_epoch=2
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertTrue(open_loop.allowed, open_loop.reason)
        self.assertEqual(open_loop.reason, "open_loop_answer")
        followup = store.evaluate_topic_bridge(
            CHAT,
            event=ConversationEvent.from_astr_event(
                _Host(text="上次然后呢", platform_id="user-3"), self_id="bot-1", topic_epoch=2
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertTrue(followup.allowed, followup.reason)
        self.assertEqual(followup.reason, "same_actor_followup")

    def test_hard_rejections_still_hold_after_the_fix(self):
        self._commit_previous_topic(("bot-x",))
        foreign = ConversationEvent.from_astr_event(
            _Host(text="回复机器人上一条", platform_id="user-9", reply_to="bot-x"),
            self_id="bot-1",
            topic_epoch=2,
        )
        denied_chat = self.store.evaluate_topic_bridge(
            "default:GroupMessage:other-chat",
            event=foreign,
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(denied_chat.allowed)
        self.assertEqual(denied_chat.reason, "chat_mismatch")

        unknown = self.store.evaluate_topic_bridge(
            CHAT,
            event=ConversationEvent.from_astr_event(
                _Host(text="回复机器人上一条", platform_id="user-10", reply_to="bot-ghost"),
                self_id="bot-1",
                topic_epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(unknown.allowed)
        self.assertEqual(unknown.reason, "unverified_reply_target")

        self.store._state(CHAT).goal_status = "guarded"
        guarded = self.store.evaluate_topic_bridge(
            CHAT,
            event=ConversationEvent.from_astr_event(
                _Host(text="回复机器人上一条", platform_id="user-11", reply_to="bot-x"),
                self_id="bot-1",
                topic_epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(guarded.allowed)
        self.assertEqual(guarded.reason, "closed_or_guarded_topic")

    def test_no_outbound_id_still_rejects_without_internal_substitute(self):
        turn = self._commit_previous_topic(())
        self.assertEqual(turn.assistant_outbound_ids, ())
        committed = _committed_turn()
        state = self.store._state(CHAT)
        blob = repr(state.turns) + repr(state.topic_anchor.as_dict()) + repr(dict(state.event_id_map))
        for forbidden in (committed.commit_id, committed.turn_id, committed.plan_id,
                          "reply_commit_", "fallback_", "evt_"):
            self.assertNotIn(forbidden, blob)
        _, decision = self._quote("bot-ghost", platform_id="user-12")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unverified_reply_target")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
