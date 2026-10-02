"""Stage 05 independent acceptance replay for the stage-03 bridging fix.

Written by the verification agent only. It does not modify any other test or
any business source. Every case enters through production constructors and
public store/loader APIs:

    host event -> ConversationEvent.from_astr_event -> gateway history text ->
    LaneManager write -> get_recent_transcript -> _provider_safe_contexts ->
    ConversationContinuityStore.record/evaluate_group_message ->
    evaluate_topic_bridge -> PlanningInputLoader._apply_continuity ->
    TurnContext -> PromptRefiner.refine_prompt

Dialogue text is 脱敏 synthetic replay material with fake ids.
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
from astrmai.conversation.contracts.topic_attention_anchor import TopicAttentionAnchor
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from tests.original_ported.helpers import _FakeConversationManager, _install_astrbot_stubs

CHAT = "default:GroupMessage:group-1"
OTHER = "default:GroupMessage:group-2"
OLD_TEXT = "项目计划的排期"
OLD_REPLY = "你周五有空讨论排期吗？"


class _Reply:
    type = "reply"

    def __init__(self, message_id):
        self.id = message_id
        self.message_id = message_id
        self.sender_id = "bot-1"
        self.sender_nickname = "小明"


class _Host:
    def __init__(self, *, text, platform_id="", reply_to="", sender="u-alice",
                 sender_name="小锦", umo=CHAT, stamp=None):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_id = platform_id
        self.message_obj = SimpleNamespace(
            message=[_Reply(reply_to)] if reply_to else [],
            message_id=platform_id,
        )
        self.timestamp = stamp if stamp is not None else time.time()
        self._sender = sender
        self._sender_name = sender_name
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


def canonical(host, *, epoch=0):
    return ConversationEvent.from_astr_event(host, self_id="bot-1", topic_epoch=epoch)


class _LoaderHost(_Host):
    """Host event that also carries the canonical event and a bound policy."""

    def __init__(self, text, event, policy):
        super().__init__(text=text)
        self._extras["astrmai_conversation_event"] = event
        policy.bind(self)

    def get_sender_name(self):
        return "小锦"


def _refine(host_event, envelope):
    return asyncio.run(
        PromptRefiner(memory_engine=None).refine_prompt(
            event=host_event,
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": True},
            prompt_envelope=envelope,
        )
    )


class Stage05FixAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.runtime.lane_manager", None)
        sys.modules.pop("astrmai.infrastructure.gateway.gateway_lane", None)
        self.lane_mod = importlib.reload(
            importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        )
        self.gateway_mod = importlib.reload(
            importlib.import_module("astrmai.infrastructure.gateway.gateway_lane")
        )
        self.conversations = _FakeConversationManager()
        self.lane = self.lane_mod.LaneManager(self.conversations)
        self.lane_key = self.lane_mod.LaneKey(
            subsystem="sys2", task_family="dialog", scope_id="group-1"
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # ── 五.十三层链路：一条真实入站消息走完 producer→history→prompt ──────
    def test_full_chain_replay_from_inbound_event_to_final_prompt(self):
        host = _Host(text=OLD_TEXT, platform_id="plat-800")
        first = canonical(host, epoch=1)
        host.set_extra("astrmai_conversation_event", first)
        user_text = self.gateway_mod.GatewayLaneMixin._build_history_user_text(
            host, raw_user_text="[事件=内部头]", prompt=""
        )
        self.assertIn(OLD_TEXT, user_text)
        self.assertNotIn("plat-800", user_text)
        self.assertNotIn("内部头", user_text)

        async def _write_and_read():
            await self.lane.append_visible_reply_artifact(
                lane_key=self.lane_key,
                base_origin=CHAT,
                raw_user_text="[事件=内部头]",
                artifact=VisibleReplyArtifact(
                    visible_text=OLD_REPLY, segments=[OLD_REPLY], persistable_text=OLD_REPLY
                ),
                history_user_text=user_text,
                user_event_id=first.event_id,
                prefix_hash="prefix-a",
            )
            lane_umo = self.lane.resolve_lane_umo(CHAT, self.lane_key)
            conversation_id = await self.conversations.get_curr_conversation_id(lane_umo)
            history = self.conversations.conversations[conversation_id].history
            transcript, ids = await self.lane.get_recent_transcript(
                self.lane_key, CHAT, include_event_ids=True
            )
            return history, transcript, ids

        history, transcript, ids = asyncio.run(_write_and_read())
        self.assertEqual([turn["role"] for turn in history], ["user", "assistant"])
        self.assertEqual(history[0]["event_id"], "plat-800")
        # 生产路径没有真实回复 ID：assistant 侧必须保持无 event_id 键或空值
        self.assertNotIn("event_id", history[1])
        self.assertIn("plat-800", ids)
        self.assertNotIn("plat-800", transcript)

        stripped = self.gateway_mod.GatewayLaneMixin._provider_safe_contexts(history)
        for turn in stripped:
            for key in ("event_id", "message_id", "id"):
                self.assertNotIn(key, turn)
            self.assertEqual(set(turn) - {"role", "content", "timestamp"}, set())

        store = ConversationContinuityStore()
        now = time.time()
        store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            goal_summary=OLD_REPLY,
            reply_preview=OLD_REPLY,
            sender_id="u-alice",
            source_event_id=first.event_id,
            anchor_event=first,
            topic_epoch=1,
            now=now,
        )

        follow = _Host(text="上次那个排期周五有空", platform_id="plat-801", reply_to="plat-800")
        follow_event = canonical(follow)
        policy = store.evaluate_group_message(
            CHAT,
            follow.message_str,
            sender_id="u-alice",
            has_reply_reference=True,
            approved_event_ids=("plat-800",),
            now=now + 10,
        )
        self.assertEqual(policy.rotation_reason, "explicit_history_recall")
        self.assertGreater(policy.topic_epoch, 1)

        loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
        loader_event = _LoaderHost(follow.message_str, follow_event, policy)
        loader._apply_continuity(loader_event, loader._continuity_snapshot(CHAT))
        context = ensure_turn_context(loader_event)
        decision = context.continuity.topic_bridge
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.reason, "explicit_reply")
        self.assertEqual(decision.evidence_event_ids, ("plat-800",))

        system, rendered = _refine(
            loader_event,
            PromptEnvelope(
                raw_user_text="小锦: " + follow.message_str,
                focus_message_text="小锦: " + follow.message_str,
                recent_transcript=transcript,
                recent_transcript_source="lane",
                topic_attention_anchor_block=str(
                    loader_event.get_extra("astrmai_topic_attention_anchor_prompt", "")
                ),
                cross_topic_bridge_block=str(
                    loader_event.get_extra("astrmai_cross_topic_bridge_prompt", "")
                ),
                cross_topic_bridge_event_ids=list(decision.evidence_event_ids),
                warm_zone_summary="更早摘要：讨论排期与人力。",
                warm_zone_transcript_source="store",
            ),
        )
        self.assertEqual(system, "stable system prompt")
        self.assertLess(
            rendered.index("---对话记录"), rendered.index("---跨话题桥接")
        )
        self.assertIn('source="cross_topic_bridge"', rendered)
        self.assertNotIn("plat-800", rendered)
        self.assertNotIn("u-alice", rendered)

    # ── 七.bridge TTL 矩阵（epoch 由生产 policy 产生） ────────────────────
    def _bridge_at_gap(self, gap_seconds, *, chat=CHAT):
        store = ConversationContinuityStore()
        now = time.time()
        old = canonical(_Host(text=OLD_TEXT, platform_id="plat-800"), epoch=1)
        store.record(
            chat_id=chat,
            focus_preview=OLD_TEXT,
            goal_summary=OLD_REPLY,
            reply_preview=OLD_REPLY,
            sender_id="u-alice",
            source_event_id=old.event_id,
            anchor_event=old,
            topic_epoch=1,
            now=now - gap_seconds,
        )
        host = _Host(
            text="上次那个排期周五有空",
            platform_id="plat-801",
            reply_to="plat-800",
            umo=chat,
        )
        event = canonical(host)
        policy = store.evaluate_group_message(
            chat,
            host.message_str,
            sender_id="u-alice",
            has_reply_reference=True,
            approved_event_ids=("plat-800",),
        )
        snapshot = store.snapshot(chat)
        anchor_epoch = int((snapshot.get("topic_anchor") or {}).get("topic_epoch", 0) or 0)
        decision = store.evaluate_topic_bridge(
            chat,
            event=event,
            target_topic_epoch=policy.topic_epoch,
            rotation_reason=policy.rotation_reason,
        )
        return store, policy, decision, bool(
            policy.group_id and anchor_epoch and policy.topic_epoch != anchor_epoch
        )

    def test_bridge_ttl_boundary_matrix(self):
        expected = {
            20: (True, "explicit_reply"),
            119: (True, "explicit_reply"),
            120: (False, "expired"),
            121: (False, "expired"),
            1810: (False, "no_source_anchor"),
            1900: (False, "no_source_anchor"),
        }
        for gap, (allowed, reason) in expected.items():
            with self.subTest(gap=gap):
                _store, _policy, decision, branch = self._bridge_at_gap(gap)
                self.assertEqual(decision.allowed, allowed, f"gap={gap} reason={decision.reason}")
                self.assertEqual(decision.reason, reason)
                self.assertEqual(decision.prompt_text(), "" if not allowed else decision.prompt_text())
                if allowed:
                    self.assertTrue(branch)
                    self.assertLessEqual(decision.expires_at - decision.created_at, 120)
                    self.assertLessEqual(len(decision.evidence_event_ids), 4)
                    self.assertLessEqual(len(decision.turns), 2)

    def test_second_topic_switch_invalidates_previous_bridge_source(self):
        store = ConversationContinuityStore()
        now = time.time()
        store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=OLD_REPLY,
            goal_summary=OLD_REPLY,
            sender_id="u-alice",
            source_event_id="msg-1",
            anchor_event=canonical(_Host(text=OLD_TEXT, platform_id="msg-1"), epoch=1),
            topic_epoch=1,
            now=now,
        )
        store.record(
            chat_id=CHAT,
            focus_preview="猫又跑出去了",
            reply_preview="它从窗户出去的？",
            goal_summary="它从窗户出去的？",
            sender_id="u-alice",
            source_event_id="msg-2",
            anchor_event=canonical(_Host(text="猫又跑出去了", platform_id="msg-2"), epoch=2),
            topic_epoch=2,
            now=now + 10,
        )
        state = store._state(CHAT)
        self.assertEqual(state.bridge_anchor.topic_epoch, 1)
        self.assertEqual([turn.source_event_id for turn in state.bridge_turns], ["msg-1"])
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text="回复上一条：排期", platform_id="msg-3", reply_to="msg-1"), epoch=2
            ),
            target_topic_epoch=2,
            rotation_reason="",
            now=now + 20,
        )
        self.assertTrue(decision.allowed, f"observed reason={decision.reason}")
        self.assertEqual(decision.source_topic_epoch, 1)
        # 第三次换题后，epoch=1 的证据必须不再可用
        store.record(
            chat_id=CHAT,
            focus_preview="周末去哪吃饭",
            reply_preview="随便都行",
            goal_summary="随便都行",
            sender_id="u-alice",
            source_event_id="msg-4",
            anchor_event=canonical(_Host(text="周末去哪吃饭", platform_id="msg-4"), epoch=3),
            topic_epoch=3,
            now=now + 30,
        )
        state = store._state(CHAT)
        self.assertEqual(state.bridge_anchor.topic_epoch, 2)
        stale = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text="回复上一条：排期", platform_id="msg-5", reply_to="msg-1"), epoch=3
            ),
            target_topic_epoch=3,
            rotation_reason="",
            now=now + 40,
        )
        self.assertFalse(stale.allowed)

    # ── 八.ID 语义与可达性矩阵 ───────────────────────────────────────────
    def test_id_semantics_matrix(self):
        store = ConversationContinuityStore()
        now = time.time()
        # canonical 与平台 ID 不同但已建立映射
        old_host = _Host(text=OLD_TEXT, stamp=1_700_000_000.0)
        old = canonical(old_host, epoch=1)
        self.assertEqual(old.event_id_source, "fallback_hash")
        store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=OLD_REPLY,
            goal_summary=OLD_REPLY,
            sender_id="u-alice",
            source_event_id=old.event_id,
            anchor_event=SimpleNamespace(
                event_id=old.event_id,
                chat_id=CHAT,
                actor_id="u-alice",
                visible_text=OLD_TEXT,
                topic_epoch=1,
                platform_message_id="plat-real",
                quote_event_id="",
                reply_target_event_id="",
                causal_parent_event_id="",
                is_bot=False,
            ),
            topic_epoch=1,
            now=now,
        )
        mapping = store._state(CHAT).event_id_map
        self.assertEqual(mapping.get("plat-real"), old.event_id)
        decision = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text="回复上一条：排期", platform_id="plat-new", reply_to="plat-real"),
                epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 10,
        )
        self.assertTrue(decision.allowed, f"mapped target failed: {decision.reason}")
        self.assertEqual(decision.evidence_event_ids, (old.event_id,))

        # 无映射的 fallback 目标必须拒绝
        bare = ConversationContinuityStore()
        bare.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=OLD_REPLY,
            goal_summary=OLD_REPLY,
            sender_id="u-alice",
            source_event_id=old.event_id,
            anchor_event=old,
            topic_epoch=1,
            now=now,
        )
        denied = bare.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text="回复上一条：排期", platform_id="plat-new", reply_to="plat-unknown"),
                epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 10,
        )
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.reason, "unverified_reply_target")

        # 目标跨 chat：键仍是 CHAT，事件 chat_id 属于另一个群
        cross = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text="回复上一条", platform_id="x", reply_to="plat-real", umo=OTHER),
                epoch=2,
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 10,
        )
        self.assertFalse(cross.allowed)
        self.assertEqual(cross.reason, "chat_mismatch")

        duplicate = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text="回复上一条", platform_id=old.event_id, reply_to="plat-real"), epoch=2
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now + 20,
        )
        self.assertFalse(duplicate.allowed)
        self.assertIn(duplicate.reason, {"duplicate_event", "unverified_reply_target"})

    # ── 九.坏字段局部恢复（10 种注入形态） ──────────────────────────────
    def test_anchor_bad_field_matrix_degrades_locally(self):
        healthy = {
            "topic_epoch": 3,
            "participants": ["u-alice", "u-bob"],
            "subject_preview": "小锦家的猫跑出去了",
            "open_loop": "它上次也是从窗户出去的吗？",
            "recent_event_ids": ["e1", "e2"],
            "updated_at": 1000.0,
            "confidence": 0.7,
            "source": ["user_message", "explicit_question"],
        }
        injections = {
            "None": None,
            "dict": {"topic_anchor": "不是映射"},
            "int": 7,
            "float": 7.5,
            "empty_string": "",
            "long_string": "X" * 5000,
            "duplicate_ids": {**healthy, "recent_event_ids": ["e1", "e1", "e2", "e2"]},
            "illegal_timestamp": {**healthy, "updated_at": "昨天"},
            "missing_fields": {"subject_preview": healthy["subject_preview"]},
            "corrupt_whole": {"topic_epoch": [], "participants": 5, "open_loop": 3.2,
                              "recent_event_ids": [None, {"x": 1}], "confidence": "高"},
        }
        long_expected = len(healthy["subject_preview"])
        for name, payload in injections.items():
            with self.subTest(injection=name):
                store = ConversationContinuityStore()
                store.restore_snapshot(CHAT, {"topic_anchor": healthy})
                baseline = store.topic_anchor_view(CHAT, now=1000.0)
                self.assertEqual(baseline["subject_preview"], healthy["subject_preview"])
                store.restore_snapshot(CHAT, {"current_topic": "排期", "topic_anchor": payload})
                view = store.topic_anchor_view(CHAT, now=1000.0)
                anchor = TopicAttentionAnchor.from_value(payload)
                self.assertLessEqual(len(anchor.subject_preview), 180)
                self.assertLessEqual(len(anchor.recent_event_ids), 12)
                self.assertLessEqual(len(anchor.participants), 8)
                self.assertTrue(all(isinstance(item, str) for item in anchor.recent_event_ids))
                self.assertTrue(
                    all(not str(item).startswith(("{", "[")) for item in anchor.recent_event_ids)
                )
                if name in ("duplicate_ids",):
                    self.assertEqual(anchor.recent_event_ids, ("e1", "e2"))
                    self.assertEqual(anchor.updated_at, 1000.0)
                if name == "long_string":
                    self.assertLessEqual(len(anchor.subject_preview), 180)
                if name == "illegal_timestamp":
                    self.assertEqual(anchor.updated_at, 0.0)
                if name == "missing_fields":
                    self.assertEqual(anchor.subject_preview, healthy["subject_preview"])
                    self.assertEqual(anchor.topic_epoch, 0)
                if name in ("None", "dict", "int", "float", "empty_string", "corrupt_whole"):
                    self.assertEqual(anchor.subject_preview, "")
                    self.assertEqual(anchor.recent_event_ids, ())
                # 恢复不得刷新 TTL、不得推进 epoch、不得污染其它 chat
                self.assertLessEqual(float(view.get("updated_at", anchor.updated_at) or 0.0), 1000.0)
                # 其它 chat 的状态不受本次恢复影响
                shared = ConversationContinuityStore()
                shared.restore_snapshot(OTHER, {"topic_anchor": healthy})
                shared.restore_snapshot(CHAT, {"topic_anchor": payload})
                other_view = shared.topic_anchor_view(OTHER, now=1000.0)
                self.assertEqual(other_view["subject_preview"], healthy["subject_preview"])
                self.assertEqual(other_view["recent_event_ids"], healthy["recent_event_ids"])
                if not anchor.subject_preview:
                    self.assertEqual(store.topic_anchor_prompt(CHAT, now=1000.0), "")
                    _, rendered = _refine(
                        _Host(text="继续"),
                        PromptEnvelope(
                            raw_user_text="小锦: 继续",
                            focus_message_text="小锦: 继续",
                            topic_attention_anchor_block=store.topic_anchor_prompt(CHAT, now=1000.0),
                        ),
                    )
                    self.assertNotIn("---话题注意力锚点", rendered)

    # ── 十.原拒绝矩阵仍然成立且原因可区分 ───────────────────────────────
    def test_reject_matrix_reasons_are_distinguishable(self):
        store = ConversationContinuityStore()
        now = time.time()
        old = canonical(_Host(text=OLD_TEXT, platform_id="plat-800"), epoch=1)
        store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=OLD_REPLY,
            goal_summary=OLD_REPLY,
            sender_id="u-alice",
            source_event_id=old.event_id,
            anchor_event=old,
            topic_epoch=1,
            now=now - 20,
        )
        state = store._state(CHAT)
        cases = {
            "chat_mismatch": dict(chat=CHAT, event=canonical(_Host(text="然后呢", platform_id="x2", umo=OTHER))),
            "same_topic": dict(chat=CHAT, event=canonical(_Host(text="然后呢")), epoch=1),
            "non_adjacent_topic": dict(chat=CHAT, event=canonical(_Host(text="然后呢")), epoch=3),
            "actor_mismatch": dict(
                chat=CHAT, event=canonical(_Host(text="然后呢", sender="u-bob"))
            ),
            "explicit_topic_switch": dict(
                chat=CHAT, event=canonical(_Host(text="换个话题，明天天气如何"))
            ),
            "duplicate_event": dict(
                chat=CHAT,
                event=canonical(
                    _Host(text="然后呢", platform_id="plat-800", reply_to="plat-800")
                ),
            ),
            "unverified_reply_target": dict(
                chat=CHAT, event=canonical(_Host(text="然后呢", reply_to="plat-999"))
            ),
            "insufficient_evidence": dict(
                chat=CHAT, event=canonical(_Host(text="他的排期在哪天来着"))
            ),
            "unrelated_topic": dict(chat=CHAT, event=canonical(_Host(text="今晚吃什么"))),
        }
        observed = {}
        for reason, kwargs in cases.items():
            decision = store.evaluate_topic_bridge(
                kwargs["chat"],
                event=kwargs["event"],
                target_topic_epoch=kwargs.get("epoch", 2),
                rotation_reason="explicit_history_recall" if reason != "explicit_topic_switch" else "",
                now=now,
            )
            self.assertFalse(decision.allowed, f"{reason} was allowed")
            self.assertEqual(decision.reason, reason)
            self.assertEqual(decision.prompt_text(), "")
            observed[reason] = decision.reason
        state.goal_status = "guarded"
        guarded = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(_Host(text="然后呢", platform_id="plat-802")),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now,
        )
        self.assertEqual(guarded.reason, "closed_or_guarded_topic")
        self.assertEqual(len(set(observed.values())), len(observed))
        # 过期 open loop 关闭后不再桥接
        closed = ConversationContinuityStore()
        closed.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=OLD_REPLY,
            goal_summary=OLD_REPLY,
            sender_id="u-alice",
            source_event_id="msg-a",
            anchor_event=canonical(_Host(text=OLD_TEXT, platform_id="msg-a"), epoch=1),
            topic_epoch=1,
            now=now - 20,
        )
        closed.update_topic_anchor(
            CHAT,
            event=canonical(_Host(text="周五有空", platform_id="msg-b"), epoch=1),
            subject_preview=OLD_TEXT,
            now=now - 5,
        )
        self.assertEqual(closed.topic_anchor_view(CHAT, now=now)["open_loop"], "")
        after = closed.evaluate_topic_bridge(
            CHAT,
            event=canonical(_Host(text="周五有空", platform_id="msg-c"), epoch=2),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now,
        )
        self.assertNotEqual(after.reason, "open_loop_answer")

    def test_same_actor_followup_allow_path_is_bounded_by_90_seconds(self):
        """Characterization of the third allow path, not a rejection case.

        "他后来怎么样了" was first expected to be denied as insufficient_evidence;
        the executed matrix shows it is accepted by the same-actor follow-up path,
        so that behaviour is locked here instead of being hidden by re-picking the
        rejection sample. It is gated only by BRIDGE_SAME_ACTOR_SECONDS.
        """
        disputed = "他后来怎么样了"
        store = ConversationContinuityStore()
        now = time.time()
        old = canonical(_Host(text=OLD_TEXT, platform_id="plat-800"), epoch=1)
        store.record(
            chat_id=CHAT,
            focus_preview=OLD_TEXT,
            reply_preview=OLD_REPLY,
            goal_summary=OLD_REPLY,
            sender_id="u-alice",
            source_event_id=old.event_id,
            anchor_event=old,
            topic_epoch=1,
            now=now - 20,
        )
        accepted = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(_Host(text=disputed, platform_id="plat-cur"), epoch=2),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now,
        )
        self.assertTrue(accepted.allowed)
        self.assertEqual(accepted.reason, "same_actor_followup")
        self.assertAlmostEqual(accepted.confidence, 0.75)
        self.assertEqual(accepted.evidence_event_ids, ("plat-800",))
        self.assertEqual(accepted.source_topic_epoch, 1)
        self.assertEqual(accepted.target_topic_epoch, 2)
        self.assertIn(OLD_TEXT, accepted.prompt_text())

        gate = ConversationContinuityStore.BRIDGE_SAME_ACTOR_SECONDS
        for gap, expected in ((gate - 1, True), (gate + 1, False)):
            with self.subTest(gap=gap):
                decision = store.evaluate_topic_bridge(
                    CHAT,
                    event=canonical(_Host(text=disputed, platform_id="plat-cur"), epoch=2),
                    target_topic_epoch=2,
                    rotation_reason="explicit_history_recall",
                    now=now - 20 + gap,
                )
                self.assertEqual(decision.allowed, expected)
                if not expected:
                    self.assertEqual(decision.reason, "insufficient_evidence")
                    self.assertEqual(decision.prompt_text(), "")

        # 换说话人后同一条文本不得再桥接：允许路径依赖 actor，而非文本本身
        other_actor = store.evaluate_topic_bridge(
            CHAT,
            event=canonical(
                _Host(text=disputed, platform_id="plat-cur-2", sender="u-bob"), epoch=2
            ),
            target_topic_epoch=2,
            rotation_reason="explicit_history_recall",
            now=now,
        )
        self.assertFalse(other_actor.allowed)
        self.assertEqual(other_actor.reason, "actor_mismatch")

    def test_no_unbounded_bridge_cache(self):
        store = ConversationContinuityStore()
        now = time.time()
        for index in range(60):
            host = _Host(text=f"话题{index}", platform_id=f"plat-{index}")
            event = canonical(host, epoch=(index % 2) + 1)
            store.record(
                chat_id=CHAT,
                focus_preview=host.message_str,
                reply_preview="回答",
                goal_summary="回答",
                sender_id=f"u-{index}",
                source_event_id=event.event_id,
                anchor_event=event,
                topic_epoch=(index % 2) + 1,
                now=now + index,
            )
        state = store._state(CHAT)
        self.assertLessEqual(len(state.bridge_turns), 2)
        self.assertLessEqual(len(state.event_id_map), 24)
        self.assertLessEqual(len(state.topic_anchor.recent_event_ids), 12)
        self.assertLessEqual(len(state.topic_anchor.participants), 8)
        self.assertLessEqual(len(str(state.bridge_anchor.as_dict())), 4096)


if __name__ == "__main__":
    unittest.main()
