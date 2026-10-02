"""Stage 05 producer-side, bridge-window and snapshot-recovery replays.

Everything here enters through production constructors and public store
methods: ConversationEvent.from_astr_event, AttentionGate-independent policy
evaluation (evaluate_group_message), record/update_topic_anchor/
evaluate_topic_bridge, PlanningInputLoader._apply_continuity and
PromptRefiner.refine_prompt. Dialogue text is 脱敏 synthetic replay material.
"""

import asyncio
import time
import unittest
from types import SimpleNamespace

from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.topic_attention_anchor import (
    ANCHOR_MAX_EVENT_IDS,
    ANCHOR_MAX_ID_CHARS,
    ANCHOR_MAX_PARTICIPANTS,
    ANCHOR_MAX_SERIALIZED_CHARS,
    ANCHOR_MAX_SUBJECT_CHARS,
    TopicAttentionAnchor,
)
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.planning.prompt_refiner import PromptRefiner

CHAT = "default:GroupMessage:group-1"
OTHER_CHAT = "default:GroupMessage:group-2"


class _ReplyComponent:
    type = "reply"

    def __init__(self, message_id, sender_id="bot-1", sender_nickname="小明"):
        self.id = message_id
        self.message_id = message_id
        self.sender_id = sender_id
        self.sender_nickname = sender_nickname


class _HostMessage:
    """Minimal stand-in for an AstrBot message event.

    Components and the platform message id are read from ``message_obj``,
    exactly as ``ConversationEvent.from_astr_event`` does.
    """

    def __init__(self, *, text, platform_message_id="", reply_to="", sender="u-alice",
                 sender_name="小锦", umo=CHAT, stamp=None):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_id = platform_message_id
        self.message_obj = SimpleNamespace(
            message=[_ReplyComponent(reply_to)] if reply_to else [],
            message_id=platform_message_id,
        )
        self._sender = sender
        self._sender_name = sender_name
        self._stamp = stamp if stamp is not None else time.time()
        self.timestamp = self._stamp
        self._extras = {}

    def get_group_id(self):
        return "g-1"

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._sender_name

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _build(host, *, topic_epoch=0):
    return ConversationEvent.from_astr_event(
        host,
        self_id="bot-1",
        topic_epoch=topic_epoch,
    )


def _policy(store, host, *, sender="u-alice", has_reply_reference=False, approved=()):
    return store.evaluate_group_message(
        CHAT,
        host.message_str,
        sender_id=sender,
        has_reply_reference=has_reply_reference,
        approved_event_ids=approved,
    )


def _record(store, host, *, epoch, source_event_id=None, now=None, reply="", anchor_event=None):
    return store.record(
        chat_id=CHAT,
        focus_preview=host.message_str[:80],
        goal_summary=reply or host.message_str[:80],
        reply_preview=reply,
        sender_id=host.get_sender_id(),
        source_event_id=source_event_id or host.message_id or "synthetic",
        anchor_event=anchor_event if anchor_event is not None else _build(host, topic_epoch=epoch),
        topic_epoch=epoch,
        now=now,
    )


class ProducerAndRecoveryStage05Tests(unittest.TestCase):
    # ── 三.2 producer event id reachability ────────────────────────────────
    def test_producer_uses_platform_message_id_and_replies_in_the_same_id_space(self):
        old = _HostMessage(text="外星肉包章鱼人是什么", platform_message_id="msg-900")
        current = _HostMessage(
            text="回复上一条：就是小锦家里那个",
            platform_message_id="msg-901",
            reply_to="msg-900",
        )
        canonical_old = _build(old)
        canonical_current = _build(current)
        self.assertEqual(canonical_old.event_id, "msg-900")
        self.assertEqual(canonical_old.event_id_source, "platform_message_id")
        self.assertEqual(canonical_current.reply_target_event_id, "msg-900")
        self.assertEqual(canonical_current.quote_event_id, "msg-900")
        self.assertEqual(canonical_current.causal_parent_event_id, "msg-900")

        store = ConversationContinuityStore()
        now = time.time()
        _record(store, old, epoch=1, source_event_id=canonical_old.event_id, now=now,
                reply="我还不确定你说的是哪一个设定。")
        policy = _policy(store, current, has_reply_reference=True, approved=(canonical_old.event_id,))
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=canonical_current,
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 10,
        )
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.reason, "explicit_reply")
        self.assertEqual(decision.evidence_event_ids, ("msg-900",))

    def test_producer_fallback_hash_cannot_match_a_platform_reply_id(self):
        """Finding S5-15: mixed id spaces make verified quotes undetectable."""
        old = _HostMessage(text="外星肉包章鱼人是什么", platform_message_id="")
        current = _HostMessage(
            text="回复上一条：就是小锦家里那个",
            platform_message_id="",
            reply_to="msg-900",
        )
        canonical_old = _build(old)
        canonical_current = _build(current)
        self.assertEqual(canonical_old.event_id_source, "fallback_hash")
        self.assertNotEqual(canonical_old.event_id, "msg-900")

        store = ConversationContinuityStore()
        now = time.time()
        _record(store, old, epoch=1, source_event_id=canonical_old.event_id, now=now,
                reply="我还不确定你说的是哪一个设定。")
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=canonical_current,
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 10,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "unverified_reply_target")

    # ── 二.三条允许路径经生产入口，且只在 recall 窗口内可达 ─────────────────
    def _prepare_previous_topic(self, store, gap_seconds):
        old = _HostMessage(text="项目计划的排期", platform_message_id="msg-800")
        past = time.time() - gap_seconds
        _record(
            store,
            old,
            epoch=1,
            source_event_id="msg-800",
            now=past,
            reply="你周五有空讨论排期吗？",
        )
        return past

    def _allow_path_case(self, *, text, reply_to="", gap_seconds=20):
        store = ConversationContinuityStore()
        self._prepare_previous_topic(store, gap_seconds)
        host = _HostMessage(text=text, platform_message_id="msg-801", reply_to=reply_to)
        canonical = _build(host)
        policy = _policy(store, host, has_reply_reference=bool(reply_to),
                         approved=("msg-800",) if reply_to else ())
        # production order: PlanningInputLoader builds the continuity snapshot
        # first, and snapshot() runs the stale-state sweep, then evaluates the
        # bridge from the epoch that snapshot reported.
        snapshot = store.snapshot(CHAT)
        anchor_epoch = int((snapshot.get("topic_anchor") or {}).get("topic_epoch", 0) or 0)
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=canonical,
            target_topic_epoch=policy.topic_epoch,
            rotation_reason=policy.rotation_reason,
        )
        branch_taken = bool(
            policy.group_id and anchor_epoch and policy.topic_epoch != anchor_epoch
        )
        return store, canonical, policy, decision, branch_taken

    def test_explicit_reply_path_is_allowed_only_via_recall_window(self):
        store, canonical, policy, decision, branch_taken = self._allow_path_case(
            text="上次那个排期周五有空", reply_to="msg-800"
        )
        self.assertEqual(policy.rotation_reason, "explicit_history_recall")
        self.assertTrue(branch_taken)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "explicit_reply")

    def test_open_loop_answer_path_is_allowed_only_via_recall_window(self):
        store, canonical, policy, decision, branch_taken = self._allow_path_case(
            text="周五有空上次排期那事"
        )
        self.assertTrue(branch_taken)
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.reason, "open_loop_answer")

    def test_same_actor_followup_path_is_allowed_only_via_recall_window(self):
        store, canonical, policy, decision, branch_taken = self._allow_path_case(
            text="上次然后呢"
        )
        self.assertTrue(branch_taken)
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.reason, "same_actor_followup")

    def test_the_same_three_paths_all_denied_once_the_topic_actually_stale(self):
        for text, reply_to in (("上次那个排期周五有空", "msg-800"), ("周五有空", ""), ("然后呢", "")):
            with self.subTest(text=text):
                _store, _c, policy, decision, branch_taken = self._allow_path_case(
                    text=text, reply_to=reply_to, gap_seconds=1900
                )
                self.assertIn(policy.rotation_reason, {"topic_stale", "new_topic", "explicit_history_recall"})
                self.assertFalse(decision.allowed)
                if branch_taken:
                    self.assertEqual(decision.reason, "expired")
                else:
                    self.assertEqual(decision.reason, "no_source_anchor")

    def test_accepted_bridge_reaches_final_prompt_only_as_dynamic_section(self):
        store, canonical, policy, decision, _branch = self._allow_path_case(
            text="上次那个排期周五有空", reply_to="msg-800"
        )

        class _LoaderEvent:
            def __init__(self, event, bound_policy):
                self._extras = {"astrmai_conversation_event": event}
                self.message_str = event.visible_text
                self.unified_msg_origin = CHAT
                bound_policy.bind(self)

            def get_extra(self, key, default=None):
                return self._extras.get(key, default)

            def set_extra(self, key, value):
                self._extras[key] = value

            def get_sender_name(self):
                return "小锦"

        loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
        event = _LoaderEvent(canonical, policy)
        loader._apply_continuity(event, loader._continuity_snapshot(CHAT))
        context = ensure_turn_context(event)
        self.assertTrue(context.continuity.topic_bridge.allowed)
        envelope = PromptEnvelope(
            raw_user_text="小锦: " + canonical.visible_text,
            focus_message_text="小锦: " + canonical.visible_text,
            recent_transcript="小锦: 项目计划的排期\n小明: 你周五有空讨论排期吗？",
            recent_transcript_source="lane",
            cross_topic_bridge_block=str(
                event.get_extra("astrmai_cross_topic_bridge_prompt", "")
            ),
            cross_topic_bridge_event_ids=list(context.continuity.topic_bridge.evidence_event_ids),
        )
        system, rendered = asyncio.run(
            PromptRefiner(memory_engine=None).refine_prompt(
                event=event,
                system_prompt="stable system prompt",
                prompt="",
                context={"disable_rag_injection": True},
                prompt_envelope=envelope,
            )
        )
        self.assertEqual(system, "stable system prompt")
        self.assertIn("---跨话题桥接", rendered)
        self.assertIn('source="cross_topic_bridge"', rendered)
        self.assertNotIn("msg-800", rendered)

    # ── 二.拒绝矩阵补全（生产入口） ────────────────────────────────────────
    def test_reject_matrix_through_production_entry(self):
        store = ConversationContinuityStore()
        self._prepare_previous_topic(store, 20)
        now = time.time()
        cases = [
            ("chat_mismatch", _build(_HostMessage(text="然后呢", umo=OTHER_CHAT)), CHAT),
            ("same_topic", _build(_HostMessage(text="然后呢")), CHAT),
            ("non_adjacent_topic", _build(_HostMessage(text="然后呢")), CHAT),
            ("actor_mismatch", _build(_HostMessage(text="然后呢", sender="u-bob")), CHAT),
            ("explicit_topic_switch", _build(_HostMessage(text="别聊这个了，说点别的")), CHAT),
            ("duplicate_event", _build(_HostMessage(text="然后呢", platform_message_id="msg-800")), CHAT),
        ]
        expectations = {
            "chat_mismatch": dict(epoch=2, chat=OTHER_CHAT),
            "same_topic": dict(epoch=1, chat=CHAT),
            "non_adjacent_topic": dict(epoch=3, chat=CHAT),
            "actor_mismatch": dict(epoch=2, chat=CHAT),
            "explicit_topic_switch": dict(epoch=2, chat=CHAT),
            "duplicate_event": dict(epoch=2, chat=CHAT),
        }
        for reason, event, chat in cases:
            with self.subTest(reason=reason):
                kwargs = expectations[reason]
                decision = store.evaluate_topic_bridge(
                    chat,
                    event=event,
                    target_topic_epoch=kwargs["epoch"],
                    rotation_reason="explicit_history_recall",
                    now=now,
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, reason)
                self.assertEqual(decision.prompt_text(), "")

        guarded = ConversationContinuityStore()
        self._prepare_previous_topic(guarded, 20)
        state = guarded._state(CHAT)
        state.goal_status = "guarded"
        denied = guarded.evaluate_topic_bridge(
            CHAT,
            event=_build(_HostMessage(text="然后呢", platform_message_id="msg-802")),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=time.time(),
        )
        self.assertEqual(denied.reason, "closed_or_guarded_topic")

        empty = ConversationContinuityStore()
        no_evidence = empty.evaluate_topic_bridge(
            CHAT,
            event=_build(_HostMessage(text="然后呢")),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=time.time(),
        )
        self.assertEqual(no_evidence.reason, "no_source_anchor")

    # ── 三.3 坏字段只局部降级 ─────────────────────────────────────────────
    def test_snapshot_recovery_degrades_fields_locally_not_the_whole_anchor(self):
        store = ConversationContinuityStore()
        healthy = TopicAttentionAnchor.from_value(
            {
                "topic_epoch": 3,
                "participants": ["u-alice", "u-alice", "u-bob"],
                "subject_preview": "小锦家的猫" + "X" * 500,
                "open_loop": "它上次也是从窗户出去的吗" + "？" * 400,
                "recent_event_ids": ["e1", "e1", "e2"],
                "updated_at": 1000.0,
                "confidence": 5.0,
                "source": ["user_message", "not_a_source"],
            }
        )
        self.assertLessEqual(len(healthy.subject_preview), ANCHOR_MAX_SUBJECT_CHARS)
        self.assertEqual(healthy.participants, ("u-alice", "u-bob"))
        self.assertEqual(healthy.recent_event_ids, ("e1", "e2"))
        self.assertEqual(healthy.confidence, 1.0)
        self.assertEqual(healthy.source, ("user_message",))

        broken = TopicAttentionAnchor.from_value(
            {
                "topic_epoch": "非数字",
                "participants": "不是列表",
                "subject_preview": 12345,
                "open_loop": None,
                "recent_event_ids": [None, {"x": 1}, ""],
                "updated_at": "昨天",
                "confidence": [],
                "source": {"user_message": True},
            }
        )
        self.assertEqual(broken.topic_epoch, 0)
        self.assertEqual(broken.subject_preview, "")
        self.assertEqual(broken.open_loop, "")
        self.assertEqual(broken.confidence, 0.0)
        self.assertEqual(broken.source, ())
        self.assertEqual(broken.updated_at, 0.0)
        self.assertLessEqual(len(broken.as_dict()["recent_event_ids"]), ANCHOR_MAX_EVENT_IDS)

        missing = TopicAttentionAnchor.from_value({"subject_preview": "只有主题"})
        self.assertEqual(missing.subject_preview, "只有主题")
        self.assertEqual(missing.topic_epoch, 0)
        self.assertEqual(missing.recent_event_ids, ())
        self.assertLessEqual(len(str(missing.as_dict())), ANCHOR_MAX_SERIALIZED_CHARS)
        self.assertEqual(TopicAttentionAnchor.from_value(None), TopicAttentionAnchor())
        self.assertEqual(TopicAttentionAnchor.from_value("字符串"), TopicAttentionAnchor())

        long_ids = TopicAttentionAnchor.from_value(
            {"recent_event_ids": [f"e{index}" * 200 for index in range(30)],
             "participants": [f"p{index}" * 200 for index in range(30)]}
        )
        self.assertLessEqual(len(long_ids.recent_event_ids), ANCHOR_MAX_EVENT_IDS)
        self.assertLessEqual(len(long_ids.participants), ANCHOR_MAX_PARTICIPANTS)
        self.assertTrue(all(len(item) <= ANCHOR_MAX_ID_CHARS for item in long_ids.recent_event_ids))

    def test_restore_snapshot_keeps_unrelated_continuity_state(self):
        store = ConversationContinuityStore()
        store.record(
            chat_id=CHAT,
            focus_preview="项目计划的排期",
            goal_summary="你周五有空讨论排期吗？",
            reply_preview="你周五有空讨论排期吗？",
            sender_id="u-alice",
            source_event_id="msg-800",
            anchor_event=_build(_HostMessage(text="项目计划的排期", platform_message_id="msg-800")),
            topic_epoch=1,
            now=time.time(),
        )
        before = store.snapshot(CHAT)
        store.restore_snapshot(CHAT, {"current_goal": "确认排期", "topic_anchor": {"subject_preview": 123}})
        after = store.snapshot(CHAT)
        self.assertEqual(after["current_goal"], "确认排期")
        self.assertEqual(after["topic_epoch"], before["topic_epoch"])
        self.assertEqual(after["topic_anchor"]["subject_preview"], "")
        store.restore_snapshot(CHAT, "不是映射")
        self.assertEqual(store.snapshot(CHAT)["current_goal"], "确认排期")

    def test_restore_snapshot_of_corrupt_anchor_does_not_refresh_ttl_or_advance_state(self):
        store = ConversationContinuityStore()
        store.restore_snapshot(
            CHAT,
            {
                "current_topic": "排期",
                "goal_status": "continuing",
                "topic_anchor": {
                    "topic_epoch": 2,
                    "subject_preview": "排期讨论",
                    "updated_at": 1000.0,
                    "recent_event_ids": ["e1"],
                },
            },
        )
        restored = store.snapshot(CHAT, now=1000.0)
        self.assertEqual(restored["topic_anchor"]["subject_preview"], "排期讨论")
        self.assertEqual(restored["topic_anchor"]["updated_at"], 1000.0)
        stale_view = store.topic_anchor_view(CHAT, now=1000.0 + store.TURN_TTL_SECONDS + 1)
        self.assertEqual(stale_view, {})
        self.assertEqual(store.topic_anchor_prompt(CHAT, now=1000.0 + store.TURN_TTL_SECONDS + 1), "")
        self.assertEqual(
            store.evaluate_topic_bridge(
                CHAT,
                event=_build(_HostMessage(text="然后呢", platform_message_id="msg-9")),
                target_topic_epoch=3,
                rotation_reason="explicit_history_recall",
                now=1000.0 + store.TURN_TTL_SECONDS + 1,
            ).reason,
            # restore_snapshot() puts topic_epoch 2 back into the anchor but not
            # into state.topic_epoch, so the bridge sees source epoch 0 here.
            "non_adjacent_topic",
        )
        store.restore_snapshot(
            CHAT, {"current_topic": "排期", "topic_anchor": {"updated_at": "非法", "open_loop": []}}
        )
        degraded = store.snapshot(CHAT, now=1000.0)
        self.assertEqual(degraded["current_topic"], "排期")
        self.assertEqual(degraded["topic_anchor"]["updated_at"], 0.0)
        self.assertEqual(degraded["topic_anchor"]["open_loop"], "")

    def test_restore_snapshot_does_not_restore_state_topic_epoch(self):
        """Finding S5-17: a restored snapshot cannot feed an adjacent bridge."""
        store = ConversationContinuityStore()
        store.restore_snapshot(
            CHAT,
            {
                "topic_epoch": 4,
                "topic_anchor": {
                    "topic_epoch": 4,
                    "subject_preview": "排期讨论",
                    "recent_event_ids": ["msg-1"],
                    "updated_at": time.time(),
                },
            },
        )
        state = store._state(CHAT)
        self.assertEqual(state.topic_anchor.topic_epoch, 4)
        self.assertEqual(state.topic_epoch, 0)
        self.assertEqual(state.turns, [])
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=_build(_HostMessage(text="然后呢", platform_message_id="msg-2")),
            target_topic_epoch=5,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "non_adjacent_topic")

    def test_non_string_anchor_elements_are_dropped_not_stringified(self):
        """Finding S5-16: a dict/list element becomes a synthetic id string."""
        anchor = TopicAttentionAnchor.from_value(
            {
                "recent_event_ids": [None, {"x": 1}, ""],
                "participants": [123, ["nested"]],
                "source": {"user_message": True},
            }
        )
        self.assertEqual(anchor.recent_event_ids, ())
        self.assertEqual(anchor.participants, ())
        self.assertEqual(anchor.source, ())

    def test_platform_target_can_match_canonical_source_through_explicit_mapping(self):
        old = _HostMessage(text="项目计划", platform_message_id="platform-old")
        current = _HostMessage(text="回复上一条：排期", reply_to="platform-old")
        canonical_old = _build(old)
        canonical_current = _build(current)
        store = ConversationContinuityStore()
        now = time.time()
        _record(store, old, epoch=1, source_event_id="canonical-old", now=now,
                reply="你周五有空吗？", anchor_event=canonical_old)
        decision = store.evaluate_topic_bridge(
            CHAT, event=canonical_current, target_topic_epoch=2,
            rotation_reason="explicit_history_recall", now=now + 10,
        )
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.evidence_event_ids, ("canonical-old",))

    def test_bridge_source_survives_real_epoch_record_until_bridge_ttl(self):
        store = ConversationContinuityStore()
        now = time.time()
        old = _HostMessage(text="项目计划", platform_message_id="msg-old")
        new = _HostMessage(text="明天天气", platform_message_id="msg-new")
        _record(store, old, epoch=1, source_event_id="msg-old", now=now,
                reply="你周五有空吗？", anchor_event=_build(old, topic_epoch=1))
        _record(store, new, epoch=2, source_event_id="msg-new", now=now + 10,
                reply="明天可能下雨。", anchor_event=_build(new, topic_epoch=2))
        current = _HostMessage(text="回复上一条：排期", reply_to="msg-old")
        decision = store.evaluate_topic_bridge(
            CHAT, event=_build(current, topic_epoch=2), target_topic_epoch=2,
            rotation_reason="", now=now + 20,
        )
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.reason, "explicit_reply")
        self.assertEqual(decision.evidence_event_ids, ("msg-old",))
        expired = store.evaluate_topic_bridge(
            CHAT, event=_build(current, topic_epoch=2), target_topic_epoch=2,
            rotation_reason="", now=now + store.BRIDGE_TTL_SECONDS + 1,
        )
        self.assertFalse(expired.allowed)

    # ── 一.三条允许路径全部经生产入口，逐条记录到最终 prompt ───────────────
    def test_all_three_allow_paths_reach_final_prompt_through_production_entry(self):
        cases = [
            ("explicit_reply", "上次那个排期周五有空", "msg-800", 0.95),
            ("open_loop_answer", "周五有空上次排期那事", "", 0.85),
            ("same_actor_followup", "上次然后呢", "", 0.75),
        ]
        for reason, text, reply_to, confidence in cases:
            with self.subTest(reason=reason):
                store, canonical, policy, decision, branch_taken = self._allow_path_case(
                    text=text, reply_to=reply_to
                )
                self.assertTrue(branch_taken)
                self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
                self.assertEqual(decision.reason, reason)
                self.assertAlmostEqual(decision.confidence, confidence)
                self.assertEqual(
                    (decision.source_topic_epoch, decision.target_topic_epoch), (1, 2)
                )
                self.assertEqual(decision.source_chat_key == decision.target_chat_key == CHAT, True)
                self.assertLessEqual(len(decision.evidence_event_ids), 4)
                self.assertLessEqual(len(decision.turns), 2)
                self.assertTrue(decision.prompt_text())

                class _LoaderEvent:
                    def __init__(self, event, bound_policy):
                        self._extras = {"astrmai_conversation_event": event}
                        self.message_str = event.visible_text
                        self.unified_msg_origin = CHAT
                        bound_policy.bind(self)

                    def get_extra(self, key, default=None):
                        return self._extras.get(key, default)

                    def set_extra(self, key, value):
                        self._extras[key] = value

                    def get_sender_name(self):
                        return "小锦"

                loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
                event = _LoaderEvent(canonical, policy)
                loader._apply_continuity(event, loader._continuity_snapshot(CHAT))
                self.assertEqual(
                    ensure_turn_context(event).continuity.topic_bridge.reason, reason
                )
                envelope = PromptEnvelope(
                    raw_user_text="小锦: " + canonical.visible_text,
                    focus_message_text="小锦: " + canonical.visible_text,
                    recent_transcript="小锦: 项目计划的排期\n小明: 你周五有空讨论排期吗？",
                    recent_transcript_source="lane",
                    cross_topic_bridge_block=str(
                        event.get_extra("astrmai_cross_topic_bridge_prompt", "")
                    ),
                    cross_topic_bridge_event_ids=list(
                        ensure_turn_context(event).continuity.topic_bridge.evidence_event_ids
                    ),
                )
                system, rendered = asyncio.run(
                    PromptRefiner(memory_engine=None).refine_prompt(
                        event=event,
                        system_prompt="stable system prompt",
                        prompt="",
                        context={"disable_rag_injection": True},
                        prompt_envelope=envelope,
                    )
                )
                self.assertEqual(system, "stable system prompt")
                self.assertIn("---跨话题桥接", rendered)
                self.assertIn(f'reason={reason}; confidence={confidence:.2f}', rendered)
                self.assertIn('source="cross_topic_bridge"', rendered)
                for evidence_id in envelope.cross_topic_bridge_event_ids:
                    self.assertNotIn(evidence_id, rendered)

    # ── 二.fallback hash 的幂等口径 ───────────────────────────────────────
    def test_fallback_hash_is_derived_from_timestamp_so_replay_idempotency_differs(self):
        """Same text, same stamp -> same id (dedup, no TTL refresh).

        Same text, different stamp -> different id, so on hosts without a
        platform message id a re-delivery is treated as a new event.
        """
        first = _build(_HostMessage(text="项目计划的排期", stamp=1_700_000_000.0))
        same = _build(_HostMessage(text="项目计划的排期", stamp=1_700_000_000.0))
        restamped = _build(_HostMessage(text="项目计划的排期", stamp=1_700_000_001.0))
        self.assertEqual(first.event_id, same.event_id)
        self.assertTrue(first.event_id.startswith("fallback_"))
        self.assertNotEqual(first.event_id, restamped.event_id)

        store = ConversationContinuityStore()
        now = time.time()
        _record(store, _HostMessage(text="项目计划的排期", stamp=1_700_000_000.0), epoch=1,
                source_event_id=first.event_id, now=now, reply="你周五有空讨论排期吗？",
                anchor_event=first)
        before = store.snapshot(CHAT, now=now)["topic_anchor"]
        _record(store, _HostMessage(text="项目计划的排期", stamp=1_700_000_000.0), epoch=1,
                source_event_id=first.event_id, now=now + 30, reply="你周五有空讨论排期吗？",
                anchor_event=same)
        after_replay = store.snapshot(CHAT, now=now + 30)["topic_anchor"]
        self.assertEqual(after_replay["updated_at"], before["updated_at"])
        self.assertEqual(after_replay["recent_event_ids"], before["recent_event_ids"])
        _record(store, _HostMessage(text="项目计划的排期", stamp=1_700_000_001.0), epoch=1,
                source_event_id=restamped.event_id, now=now + 60, reply="你周五有空讨论排期吗？",
                anchor_event=restamped)
        after_new = store.snapshot(CHAT, now=now + 60)["topic_anchor"]
        self.assertEqual(after_new["updated_at"], now + 60)
        self.assertIn(restamped.event_id, after_new["recent_event_ids"])

    # ── 四.完全损坏快照不得注入 prompt ────────────────────────────────────
    def test_corrupt_anchor_snapshot_injects_no_prompt_section_and_keeps_system(self):
        store = ConversationContinuityStore()
        store.record(
            chat_id=CHAT,
            focus_preview="项目计划的排期",
            reply_preview="你周五有空讨论排期吗？",
            goal_summary="你周五有空讨论排期吗？",
            sender_id="u-alice",
            source_event_id="msg-800",
            anchor_event=_build(_HostMessage(text="项目计划的排期", platform_message_id="msg-800")),
            topic_epoch=1,
            now=time.time(),
        )
        store.restore_snapshot(
            CHAT,
            {"current_topic": "排期", "topic_anchor": [{"不是": "映射"}]},
        )
        anchor_prompt = store.topic_anchor_prompt(CHAT)
        self.assertEqual(anchor_prompt, "")
        envelope = PromptEnvelope(
            raw_user_text="小锦: 继续",
            focus_message_text="小锦: 继续",
            recent_transcript="小锦: 项目计划的排期\n小明: 你周五有空讨论排期吗？",
            recent_transcript_source="lane",
            topic_attention_anchor_block=anchor_prompt,
        )

        class _Event:
            message_str = "小锦: 继续"
            unified_msg_origin = CHAT

            def __init__(self):
                self.extras = {"retrieve_keys": []}

            def get_extra(self, key, default=None):
                return self.extras.get(key, default)

            def set_extra(self, key, value):
                self.extras[key] = value

        system, rendered = asyncio.run(
            PromptRefiner(memory_engine=None).refine_prompt(
                event=_Event(),
                system_prompt="stable system prompt",
                prompt="",
                context={"disable_rag_injection": True},
                prompt_envelope=envelope,
            )
        )
        self.assertEqual(system, "stable system prompt")
        self.assertNotIn("---话题注意力锚点", rendered)
        self.assertIn("小锦: 继续", rendered)

    def test_duplicate_event_ids_in_snapshot_are_deduped_without_growing_anchor(self):
        anchor = TopicAttentionAnchor.from_value(
            {
                "topic_epoch": 2,
                "recent_event_ids": ["e1", "e1", "e1", "e2", "e2"],
                "participants": ["u-a", "u-a"],
                "subject_preview": "排期",
                "updated_at": 500.0,
            }
        )
        self.assertEqual(anchor.recent_event_ids, ("e1", "e2"))
        self.assertEqual(anchor.participants, ("u-a",))
        store = ConversationContinuityStore()
        store.restore_snapshot(CHAT, {"topic_anchor": anchor.as_dict()})
        view = store.topic_anchor_view(CHAT, now=500.0)
        self.assertEqual(view["recent_event_ids"], ["e1", "e2"])
        self.assertEqual(view["updated_at"], 500.0)

    def test_fallback_id_collides_when_host_exposes_no_timestamp(self):
        """Finding S5-18: identical text collapses to one synthetic id.

        ``_stable_fallback_event_id`` hashes chat/actor/kind/text/timestamp.
        When the host exposes neither ``astrmai_timestamp`` nor ``timestamp``,
        two genuinely different messages with the same text get the SAME id, so
        the anchor treats the later one as a duplicate (no TTL refresh) and the
        bridge denies it as ``duplicate_event``. This test records the observed
        behaviour; it must be flipped when the producer starts carrying a real
        per-message identity.
        """

        def bare(text):
            return SimpleNamespace(
                unified_msg_origin=CHAT,
                message_str=text,
                message_obj=SimpleNamespace(message=[], message_id=""),
                get_group_id=lambda: "g-1",
                get_sender_id=lambda: "u-alice",
                get_sender_name=lambda: "小锦",
                get_extra=lambda key, default=None: default,
            )

        first = ConversationEvent.from_astr_event(bare("项目计划的排期"), self_id="bot-1")
        second = ConversationEvent.from_astr_event(bare("项目计划的排期"), self_id="bot-1")
        self.assertEqual(first.event_id_source, "fallback_hash")
        self.assertEqual(first.event_id, second.event_id)

        store = ConversationContinuityStore()
        now = time.time()
        _record(store, _HostMessage(text="项目计划的排期", platform_message_id=first.event_id),
                epoch=1, source_event_id=first.event_id, now=now - 20,
                reply="你周五有空讨论排期吗？")
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=second,
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "duplicate_event")

    # ── 三.1 恢复生命周期：生产是否真的调用 ───────────────────────────────
    def test_continuity_store_is_not_restored_by_plugin_lifecycle(self):
        """Record of the actual wiring, not a spec assertion.

        ConversationContinuityStore is created per planner instance and
        restore_snapshot() has no production caller, so anchor/bridge state
        does not survive a reload or restart.
        """
        import inspect

        from astrmai.app import lifecycle as lifecycle_module

        source = inspect.getsource(lifecycle_module)
        self.assertIn("dialogue_snapshot", source)
        self.assertNotIn("conversation_continuity", source)
        from astrmai.conversation.planning import planner as planner_module

        planner_source = inspect.getsource(planner_module)
        self.assertIn("ConversationContinuityStore()", planner_source)
        self.assertNotIn("restore_snapshot(", planner_source)


if __name__ == "__main__":
    unittest.main()
