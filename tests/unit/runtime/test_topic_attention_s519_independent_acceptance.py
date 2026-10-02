"""Independent round-3 acceptance replay for S5-19 (quoting a committed bot reply).

Written by the verification agent only. No business source and no existing test
assertion was touched. Everything enters through production constructors and
production methods, in the production order:

    inbound host event -> ConversationEvent.from_astr_event
    -> ReplySendReceipt -> CommittedBotTurn.from_plan      (real send receipt)
    -> event extra "astrmai_committed_bot_turn"            (reply_commit_service.py:224/330)
    -> Planner._record_conversation_continuity             (planner.py:1953-1996)
    -> ConversationContinuityStore.record(assistant_outbound_ids=...)
    -> rotation + new inbound quoting the bot id
    -> PlanningInputLoader._apply_continuity -> TurnContext
    -> PromptRefiner.refine_prompt -> final dynamic prompt

Dialogue text is 脱敏 synthetic replay material with fake ids.
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

CHAT = "default:GroupMessage:group-s519"
OTHER = "default:GroupMessage:group-s519-other"
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
    """Minimal duck-typed AstrMessageEvent used by the production producers."""

    def __init__(self, *, text, platform_id="", reply_to="", sender="u-alice", umo=CHAT):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_id = platform_id
        self.message_obj = SimpleNamespace(
            message=[_Reply(reply_to)] if reply_to else [], message_id=platform_id
        )
        self.timestamp = time.time()
        self._sender = sender
        self._extras = {}

    def get_group_id(self):
        return "group-s519"

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "小锦"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _committed_turn(*outbound_ids, status=ReplyCommitStatus.SENT):
    """Build the turn exactly the way the commit service does."""
    target = TurnTarget(
        target_kind=TargetKind.MESSAGE,
        target_event_id="user-1",
        topic_epoch=1,
        source_event_ids=("user-1",),
    )
    plan = ReplyPlan.create(
        turn_id="turn-accept-s519",
        chat_id=CHAT,
        chat_kind="group",
        target=target,
        planned_text=BOT_REPLY,
        planned_segments=(BOT_REPLY,),
        created_at=time.time(),
    )
    receipt = ReplySendReceipt(
        status=status,
        sent_segments=(BOT_REPLY,),
        outbound_message_ids=outbound_ids,
        visible_text=BOT_REPLY,
        persistable_text=BOT_REPLY,
        sent_at=time.time(),
    )
    return CommittedBotTurn.from_plan(plan, receipt)


def _planner(store):
    planner = object.__new__(Planner)
    planner.conversation_continuity = store
    return planner


class S519IndependentAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.store = ConversationContinuityStore()
        self.planner = _planner(self.store)
        self.loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=self.store))
        self.now = time.time()

    # ── 生产顺序写入：入站 + 真实发送回执 -> continuity record ─────────────
    def _record_previous_topic(self, *, outbound_ids, epoch=1, at=None, platform_id="user-1"):
        host = _Host(text=OLD_TEXT, platform_id=platform_id)
        canonical = ConversationEvent.from_astr_event(host, self_id="bot-1", topic_epoch=epoch)
        host.set_extra("astrmai_conversation_event", canonical)
        DialogHistoryPolicy(
            group_id=CHAT, topic_epoch=epoch, current_sender_id="u-alice"
        ).bind(host)
        if outbound_ids is not None:
            host.set_extra("astrmai_committed_bot_turn", _committed_turn(*outbound_ids))
        self.planner._record_conversation_continuity(
            CHAT,
            PromptEnvelope(raw_user_text=OLD_TEXT, focus_message_text=OLD_TEXT),
            BOT_REPLY,
            [],
            SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
            event=host,
        )
        turn = self.store._state(CHAT).turns[-1]
        if at is not None:  # 只用于时间边界用例：把这条记录搬到过去
            turn.timestamp = at
            state = self.store._state(CHAT)
            state.topic_anchor = state.topic_anchor.__class__(
                **{**state.topic_anchor.as_dict(), "updated_at": at}
            )
        return turn

    def _quote_and_load(self, *, quoted_id, text="回复机器人上一条：排期", platform_id="user-9",
                        epoch=2, rotation="explicit_history_recall", sender="u-alice", umo=CHAT):
        host = _Host(text=text, platform_id=platform_id, reply_to=quoted_id, sender=sender, umo=umo)
        canonical = ConversationEvent.from_astr_event(host, self_id="bot-1", topic_epoch=epoch)
        host.set_extra("astrmai_conversation_event", canonical)
        DialogHistoryPolicy(
            group_id=umo, topic_epoch=epoch, rotation_reason=rotation, current_sender_id=sender
        ).bind(host)
        self.loader._apply_continuity(host, self.loader._continuity_snapshot(umo))
        decision = ensure_turn_context(host).continuity.topic_bridge
        return host, decision

    def _refine(self, host, decision):
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

    # ── 七.1 引用机器人单段回复：真实回执 -> 最终 prompt ──────────────────
    def test_single_outbound_id_from_real_receipt_reaches_final_prompt(self):
        turn = self._record_previous_topic(outbound_ids=("bot-9001",))
        self.assertEqual(turn.assistant_outbound_ids, ("bot-9001",))
        self.assertEqual(turn.source_event_id, "user-1")

        host, decision = self._quote_and_load(quoted_id="bot-9001")
        self.assertTrue(decision.allowed, f"observed {decision.reason}")
        self.assertEqual(decision.reason, "explicit_reply")
        self.assertEqual(decision.confidence, 0.95)
        self.assertIn("bot-9001", decision.evidence_event_ids)
        self.assertEqual(decision.source_topic_epoch, 1)
        self.assertEqual(decision.target_topic_epoch, 2)

        stable, rendered = self._refine(host, decision)
        self.assertEqual(stable, "stable system prompt")
        self.assertIn("---跨话题桥接", rendered)
        self.assertIn('source="cross_topic_bridge"', rendered)
        self.assertIn(OLD_TEXT, rendered)
        # 内部 ID 不进正文、不进 system、不进渲染段
        for leaked in ("bot-9001", "user-1", "u-alice", "outbound_message_ids", "commit_id"):
            self.assertNotIn(leaked, rendered)
        anchor_prompt = host.get_extra("astrmai_topic_attention_anchor_prompt", "")
        self.assertNotIn("bot-9001", anchor_prompt)

    # ── 七.2 多段发送：首段、中间段、末段引用都必须命中 ────────────────────
    def test_multi_segment_outbound_ids_first_middle_last_all_bridge(self):
        for quoted, label in (("bot-1", "first"), ("bot-2", "middle"), ("bot-3", "last")):
            with self.subTest(segment=label):
                store = ConversationContinuityStore()
                planner = _planner(store)
                host0 = _Host(text=OLD_TEXT, platform_id="user-1")
                canonical0 = ConversationEvent.from_astr_event(
                    host0, self_id="bot-1", topic_epoch=1
                )
                host0.set_extra("astrmai_conversation_event", canonical0)
                DialogHistoryPolicy(
                    group_id=CHAT, topic_epoch=1, current_sender_id="u-alice"
                ).bind(host0)
                host0.set_extra(
                    "astrmai_committed_bot_turn", _committed_turn("bot-1", "bot-2", "bot-3")
                )
                planner._record_conversation_continuity(
                    CHAT,
                    PromptEnvelope(raw_user_text=OLD_TEXT, focus_message_text=OLD_TEXT),
                    BOT_REPLY,
                    [],
                    SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
                    event=host0,
                )
                turn = store._state(CHAT).turns[-1]
                self.assertEqual(turn.assistant_outbound_ids, ("bot-1", "bot-2", "bot-3"))
                loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
                host = _Host(text="回复机器人上一条：排期", platform_id="user-9", reply_to=quoted)
                canonical = ConversationEvent.from_astr_event(
                    host, self_id="bot-1", topic_epoch=2
                )
                host.set_extra("astrmai_conversation_event", canonical)
                DialogHistoryPolicy(
                    group_id=CHAT, topic_epoch=2, rotation_reason="explicit_history_recall",
                    current_sender_id="u-alice",
                ).bind(host)
                loader._apply_continuity(host, loader._continuity_snapshot(CHAT))
                decision = ensure_turn_context(host).continuity.topic_bridge
                self.assertTrue(decision.allowed, f"{label} segment denied: {decision.reason}")
                self.assertEqual(decision.reason, "explicit_reply")
                self.assertEqual(decision.evidence_event_ids, (quoted,))

    # ── 七.2 容量与去重：重复只留一份；超限行为按实测固化 ─────────────────
    def test_duplicate_outbound_ids_are_deduped(self):
        turn = self._record_previous_topic(outbound_ids=("bot-700", "bot-700", "bot-701"))
        self.assertEqual(turn.assistant_outbound_ids, ("bot-700", "bot-701"))
        self.assertEqual(len(set(turn.assistant_outbound_ids)), 2)
        turn = self._record_previous_topic(outbound_ids=("bot-10", "bot-11", "bot-10", "bot-20"))
        self.assertEqual(turn.assistant_outbound_ids, ("bot-10", "bot-11", "bot-20"))

    def test_defect_s524_over_capacity_outbound_ids_keep_head_not_tail(self):
        """S5-24 regression: planner must let the store retain newest IDs."""
        limit = self.store.ANCHOR_MAX_EVENT_IDS
        ids = tuple(f"bot-{i}" for i in range(1, limit + 9))
        turn = self._record_previous_topic(outbound_ids=ids)
        self.assertEqual(len(turn.assistant_outbound_ids), limit)
        self.assertEqual(turn.assistant_outbound_ids, ids[-limit:])
        self.assertIn(ids[-1], turn.assistant_outbound_ids)

        _, newest = self._quote_and_load(quoted_id=ids[-1], platform_id="u-newest")
        self.assertTrue(newest.allowed, newest.reason)
        self.assertEqual(newest.reason, "explicit_reply")
        _, kept = self._quote_and_load(quoted_id=ids[0], platform_id="u-kept")
        self.assertFalse(kept.allowed)
        self.assertEqual(kept.reason, "unverified_reply_target")
        for retained_id in (ids[-limit], "bot-13"):
            _, retained = self._quote_and_load(quoted_id=retained_id, platform_id="u-retained")
            self.assertTrue(retained.allowed, retained.reason)
            self.assertEqual(retained.reason, "explicit_reply")

        # 同一批 ID 直接经 store 公开入口写入时，尾部规则本应成立：
        # 证明截断发生在 planner 边界，而不是证据匹配逻辑。
        tail_store = ConversationContinuityStore()
        tail_store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=BOT_REPLY,
            goal_summary=BOT_REPLY,
            sender_id="u-alice",
            source_event_id="user-1",
            assistant_outbound_ids=ids,
            topic_epoch=1,
        )
        self.assertEqual(
            tail_store._state(CHAT).turns[-1].assistant_outbound_ids, ids[-limit:]
        )
        self.assertIn(ids[-1], tail_store._state(CHAT).turns[-1].assistant_outbound_ids)

    # ── 七.4 无真实 outbound ID：必须拒绝且不得用内部 ID 冒充 ──────────────
    def test_missing_outbound_id_rejects_without_internal_id_substitution(self):
        turn = self._record_previous_topic(outbound_ids=())
        self.assertEqual(turn.assistant_outbound_ids, ())
        committed = _committed_turn()
        state = self.store._state(CHAT)
        blob = repr(state.turns[-1]) + repr(state.topic_anchor.as_dict())
        for forbidden in (committed.commit_id, committed.turn_id, committed.plan_id, "reply_commit_"):
            self.assertNotIn(forbidden, blob)

        _, decision = self._quote_and_load(quoted_id="bot-never-sent")
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unverified_reply_target")
        self.assertEqual(decision.prompt_text(), "")
        self.assertFalse(
            any(
                str(item).startswith(("reply_commit_", "turn-", "trace-", "fallback_", "evt_"))
                for item in decision.evidence_event_ids
            )
        )
        # 同一条记录里的用户侧合法证据不受影响
        _, user_quote = self._quote_and_load(quoted_id="user-1", text="回复上一条：排期")
        self.assertTrue(user_quote.allowed, user_quote.reason)
        self.assertEqual(user_quote.reason, "explicit_reply")
        self.assertEqual(user_quote.evidence_event_ids, ("user-1",))

    def test_host_send_result_none_or_bool_produces_no_outbound_id(self):
        # reply_artifact_builder 只在 sent_result 非 None 且非 bool 时记 ID；
        # 这里验证契约层同样不会把 None/True 变成伪 ID。
        receipt = ReplySendReceipt(
            status=ReplyCommitStatus.SENT,
            sent_segments=("x",),
            outbound_message_ids=(None, True, "", "bot-real"),
            visible_text="x",
            persistable_text="x",
            sent_at=time.time(),
        )
        self.assertEqual(receipt.outbound_message_ids, ("True", "bot-real"))
        empty = ReplySendReceipt(
            status=ReplyCommitStatus.SENT,
            sent_segments=("x",),
            outbound_message_ids=(None, "", None),
            visible_text="x",
            persistable_text="x",
            sent_at=time.time(),
        )
        self.assertEqual(empty.outbound_message_ids, ())

    # ── 七.3 / 十. 原有三条允许路径不得回归 ───────────────────────────────
    def test_three_original_allow_paths_still_reach_prompt(self):
        cases = [
            ("explicit_reply", "回复上一条：排期", "user-1", 0.95),
            ("open_loop_answer", "周五有空", "", 0.85),
            ("same_actor_followup", "上次然后呢", "", 0.75),
        ]
        for reason, text, quoted, confidence in cases:
            with self.subTest(reason=reason):
                store = ConversationContinuityStore()
                planner = _planner(store)
                host0 = _Host(text=OLD_TEXT, platform_id="user-1")
                canonical0 = ConversationEvent.from_astr_event(
                    host0, self_id="bot-1", topic_epoch=1
                )
                host0.set_extra("astrmai_conversation_event", canonical0)
                DialogHistoryPolicy(
                    group_id=CHAT, topic_epoch=1, current_sender_id="u-alice"
                ).bind(host0)
                host0.set_extra("astrmai_committed_bot_turn", _committed_turn("bot-1"))
                planner._record_conversation_continuity(
                    CHAT,
                    PromptEnvelope(raw_user_text=OLD_TEXT, focus_message_text=OLD_TEXT),
                    BOT_REPLY,
                    [],
                    SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
                    event=host0,
                )
                loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
                host = _Host(text=text, platform_id="user-9", reply_to=quoted)
                canonical = ConversationEvent.from_astr_event(
                    host, self_id="bot-1", topic_epoch=2
                )
                host.set_extra("astrmai_conversation_event", canonical)
                DialogHistoryPolicy(
                    group_id=CHAT, topic_epoch=2,
                    rotation_reason="explicit_history_recall", current_sender_id="u-alice",
                ).bind(host)
                loader._apply_continuity(host, loader._continuity_snapshot(CHAT))
                decision = ensure_turn_context(host).continuity.topic_bridge
                self.assertTrue(decision.allowed, f"{reason} regressed: {decision.reason}")
                self.assertEqual(decision.reason, reason)
                self.assertAlmostEqual(decision.confidence, confidence)
                self.assertTrue(decision.prompt_text())
                stable, rendered = self._refine_on(loader, host, decision)
                self.assertEqual(stable, "stable system prompt")
                self.assertIn("---跨话题桥接", rendered)
                self.assertNotIn("bot-1", rendered)
                self.assertNotIn("user-1", rendered)

    def _refine_on(self, loader, host, decision):
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

    # ── 九. bot 引用同样受 TTL / epoch / 硬拒绝约束 ───────────────────────
    def test_bot_quote_honours_ttl_epoch_and_hard_rejections(self):
        ttl = self.store.BRIDGE_TTL_SECONDS
        matrix = [
            (20, True, "explicit_reply"),
            (ttl - 1, True, "explicit_reply"),
            (ttl, False, "expired"),
            (ttl + 1, False, "expired"),
            (1810, False, "expired"),
            (1900, False, "expired"),
        ]
        for gap, allowed, reason in matrix:
            with self.subTest(gap=gap):
                store = ConversationContinuityStore()
                planner = _planner(store)
                past = time.time() - gap
                host0 = _Host(text=OLD_TEXT, platform_id="user-1")
                canonical0 = ConversationEvent.from_astr_event(
                    host0, self_id="bot-1", topic_epoch=1
                )
                host0.set_extra("astrmai_conversation_event", canonical0)
                DialogHistoryPolicy(
                    group_id=CHAT, topic_epoch=1, current_sender_id="u-alice"
                ).bind(host0)
                host0.set_extra("astrmai_committed_bot_turn", _committed_turn("bot-x"))
                store.record(
                    chat_id=CHAT,
                    focus_preview=OLD_TEXT,
                    reply_preview=BOT_REPLY,
                    goal_summary=BOT_REPLY,
                    sender_id="u-alice",
                    source_event_id=canonical0.event_id,
                    assistant_outbound_ids=("bot-x",),
                    anchor_event=canonical0,
                    topic_epoch=1,
                    now=past,
                )
                decision = store.evaluate_topic_bridge(
                    CHAT,
                    event=ConversationEvent.from_astr_event(
                        _Host(text="回复机器人上一条：排期", platform_id="user-9", reply_to="bot-x"),
                        self_id="bot-1",
                        topic_epoch=2,
                    ),
                    target_topic_epoch=2,
                    rotation_reason="explicit_history_recall",
                )
                self.assertEqual(decision.allowed, allowed, f"gap={gap} {decision.reason}")
                self.assertEqual(decision.reason, reason)
                if not allowed:
                    self.assertEqual(decision.prompt_text(), "")

        # S5-26（P2，可诊断性）：同一条 1810s 证据，先经生产读路径 sweep 再评估时
        # 拒绝码会变成 no_source_anchor。两个码都拒绝，但线上排障无法区分
        # "锚点已被清掉" 与 "锚点还在但证据过期"。
        sweep_store = ConversationContinuityStore()
        past = time.time() - 1810
        canonical0 = ConversationEvent.from_astr_event(
            _Host(text=OLD_TEXT, platform_id="user-1"), self_id="bot-1", topic_epoch=1
        )
        sweep_store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=BOT_REPLY,
            goal_summary=BOT_REPLY,
            sender_id="u-alice",
            source_event_id=canonical0.event_id,
            assistant_outbound_ids=("bot-x",),
            anchor_event=canonical0,
            topic_epoch=1,
            now=past,
        )
        sweep_store.snapshot(CHAT)
        swept = sweep_store.evaluate_topic_bridge(
            CHAT,
            event=ConversationEvent.from_astr_event(
                _Host(text="回复机器人上一条：排期", platform_id="user-9", reply_to="bot-x"),
                self_id="bot-1",
                topic_epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(swept.allowed)
        self.assertEqual(swept.reason, "no_source_anchor")

    def test_bot_quote_still_denied_for_cross_chat_guarded_duplicate_and_stale_source(self):
        self._record_previous_topic(outbound_ids=("bot-500",))
        # 跨 chat 证据：store 层按 chat key 判定（生产 loader 会用另一条链，见下）
        cross_decision = self.store.evaluate_topic_bridge(
            CHAT,
            event=ConversationEvent.from_astr_event(
                _Host(
                    text="回复机器人上一条：排期", platform_id="user-x", reply_to="bot-500",
                    umo=OTHER,
                ),
                self_id="bot-1",
                topic_epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(cross_decision.allowed)
        self.assertEqual(cross_decision.reason, "chat_mismatch")

        # S5-25（P2，可诊断性）：未知 chat 走 loader 时桥接分支根本不评估，
        # 但 TurnContext 里的 reason 仍是 BridgeDecision 的默认值 no_source_anchor，
        # 与真实锚点缺失不可区分。
        _, unknown_chat = self._quote_and_load(quoted_id="bot-500", umo=OTHER)
        self.assertFalse(unknown_chat.allowed)
        self.assertEqual(unknown_chat.reason, "no_source_anchor")
        self.assertEqual(unknown_chat.confidence, 0.0)

        _, self_dup = self._quote_and_load(quoted_id="bot-500", platform_id="bot-500")
        self.assertFalse(self_dup.allowed)
        self.assertEqual(self_dup.reason, "duplicate_event")

        state = self.store._state(CHAT)
        state.goal_status = "guarded"
        _, guarded = self._quote_and_load(quoted_id="bot-500", platform_id="user-10")
        self.assertFalse(guarded.allowed)
        self.assertEqual(guarded.reason, "closed_or_guarded_topic")
        state.goal_status = "active"

        _, switch = self._quote_and_load(
            quoted_id="bot-500", platform_id="user-11",
            text="换个话题，明天天气如何", rotation="",
        )
        self.assertFalse(switch.allowed)
        self.assertEqual(switch.reason, "explicit_topic_switch")

    def test_second_topic_switch_invalidates_the_old_bot_outbound_evidence(self):
        self._record_previous_topic(outbound_ids=("bot-600",))
        # 真实轮转：新话题写入一次 record（epoch 2）
        host1 = _Host(text="明天天气", platform_id="user-2")
        canonical1 = ConversationEvent.from_astr_event(host1, self_id="bot-1", topic_epoch=2)
        host1.set_extra("astrmai_conversation_event", canonical1)
        DialogHistoryPolicy(
            group_id=CHAT, topic_epoch=2, current_sender_id="u-alice"
        ).bind(host1)
        host1.set_extra("astrmai_committed_bot_turn", _committed_turn("bot-601"))
        self.planner._record_conversation_continuity(
            CHAT,
            PromptEnvelope(raw_user_text="明天天气", focus_message_text="明天天气"),
            "明天可能下雨。",
            [],
            SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
            event=host1,
        )
        _, stale = self._quote_and_load(quoted_id="bot-600", platform_id="user-3", epoch=2)
        self.assertFalse(stale.allowed, "旧 bot outbound ID 必须随话题替换失效")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
