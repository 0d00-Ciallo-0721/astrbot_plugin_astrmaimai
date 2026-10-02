"""Regression coverage for S5-19: quoting a committed bot reply bridges topics."""

import asyncio
import time
from types import SimpleNamespace

from astrmai.conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.contracts.turn_target import TargetKind, TurnTarget
from astrmai.conversation.contracts.turn_context import ensure_turn_context
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner import Planner
from astrmai.conversation.planning.planning_input_loader import PlanningInputLoader
from astrmai.conversation.planning.prompt_refiner import PromptRefiner


CHAT = "default:GroupMessage:s519"


class _Reply:
    type = "reply"

    def __init__(self, message_id: str):
        self.id = message_id
        self.message_id = message_id
        self.sender_id = "bot-1"
        self.sender_nickname = "小明"


class _HostEvent:
    def __init__(self, text: str, message_id: str, *, reply_to: str = "", sender: str = "u-alice"):
        self.unified_msg_origin = CHAT
        self.message_str = text
        self.message_id = message_id
        self.message_obj = SimpleNamespace(
            message=[_Reply(reply_to)] if reply_to else [],
            message_id=message_id,
        )
        self._sender = sender
        self._extras: dict[str, object] = {}
        self.timestamp = time.time()

    def get_group_id(self):
        return "s519"

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "小锦"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


def _canonical(host: _HostEvent, *, topic_epoch: int) -> ConversationEvent:
    return ConversationEvent.from_astr_event(host, self_id="bot-1", topic_epoch=topic_epoch)


def _bind(event: _HostEvent, canonical: ConversationEvent, *, epoch: int, rotation_reason: str = ""):
    event.set_extra("astrmai_conversation_event", canonical)
    event.set_extra(
        "astrmai_dialog_history_policy",
        {
            "history_mode": "explicit_recall" if rotation_reason else "current_topic",
            "group_id": CHAT,
            "topic_epoch": epoch,
            "current_sender_id": canonical.actor_id,
            "rotation_reason": rotation_reason,
        },
    )
    return event


def _committed_turn(*outbound_ids: str) -> CommittedBotTurn:
    target = TurnTarget(
        target_kind=TargetKind.MESSAGE,
        target_event_id="user-1",
        topic_epoch=1,
        source_event_ids=("user-1",),
    )
    plan = ReplyPlan.create(
        turn_id="turn-s519",
        chat_id=CHAT,
        chat_kind="group",
        target=target,
        planned_text="已记录这个回复。",
        planned_segments=("已记录这个回复。",),
        created_at=time.time(),
    )
    receipt = ReplySendReceipt(
        status=ReplyCommitStatus.SENT,
        sent_segments=("已记录这个回复。",),
        outbound_message_ids=outbound_ids,
        visible_text="已记录这个回复。",
        persistable_text="已记录这个回复。",
        sent_at=time.time(),
    )
    return CommittedBotTurn.from_plan(plan, receipt)


def _planner(store: ConversationContinuityStore) -> Planner:
    planner = object.__new__(Planner)
    planner.conversation_continuity = store
    return planner


def _record(planner, event, *, epoch: int, reply_text: str, now: float):
    planner._record_conversation_continuity(
        CHAT,
        PromptEnvelope(raw_user_text=event.message_str, focus_message_text=event.message_str),
        reply_text,
        [],
        SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
        event=event,
    )


def test_s519_real_send_receipt_to_loader_prompt_bridge_for_bot_quote():
    now = time.time()
    store = ConversationContinuityStore()
    planner = _planner(store)

    inbound_host = _HostEvent("项目计划的排期", "user-1")
    inbound = _bind(inbound_host, _canonical(inbound_host, topic_epoch=1), epoch=1)
    inbound.set_extra("astrmai_committed_bot_turn", _committed_turn("bot-1", "bot-2", "bot-1"))
    _record(planner, inbound, epoch=1, reply_text="你周五有空讨论排期吗？", now=now)
    assert store._state(CHAT).turns[-1].assistant_outbound_ids == ("bot-1", "bot-2")

    quoted_host = _HostEvent("回复机器人上一条：排期周五有空吗？", "user-3", reply_to="bot-2")
    quoted = _bind(
        quoted_host,
        _canonical(quoted_host, topic_epoch=2),
        epoch=2,
        rotation_reason="explicit_history_recall",
    )
    loader = PlanningInputLoader(SimpleNamespace(conversation_continuity=store))
    loader._apply_continuity(quoted, loader._continuity_snapshot(CHAT))

    bridge = ensure_turn_context(quoted).continuity.topic_bridge
    assert bridge.allowed is True
    assert bridge.reason == "explicit_reply"
    assert bridge.confidence == 0.95
    assert "bot-2" in bridge.evidence_event_ids

    envelope = PromptEnvelope(
        raw_user_text=quoted.message_str,
        focus_message_text=quoted.message_str,
        cross_topic_bridge_block=quoted.get_extra("astrmai_cross_topic_bridge_prompt", ""),
        cross_topic_bridge_event_ids=list(bridge.evidence_event_ids),
    )
    stable, rendered = asyncio.run(
        PromptRefiner(memory_engine=None).refine_prompt(
            event=quoted,
            system_prompt="stable system prompt",
            prompt="",
            context={"disable_rag_injection": True},
            prompt_envelope=envelope,
        )
    )
    assert stable == "stable system prompt"
    assert "---跨话题桥接" in rendered
    assert 'source="cross_topic_bridge"' in rendered
    assert "bot-2" not in rendered
    assert "outbound_message_ids" not in rendered
    _record(planner, quoted, epoch=2, reply_text="可以，周五下午有空。", now=now + 5)


def test_s519_missing_outbound_id_is_rejected_without_internal_id_fallback():
    now = time.time()
    store = ConversationContinuityStore()
    planner = _planner(store)
    inbound_host = _HostEvent("项目计划的排期", "user-1")
    inbound = _bind(inbound_host, _canonical(inbound_host, topic_epoch=1), epoch=1)
    inbound.set_extra("astrmai_committed_bot_turn", _committed_turn())
    _record(planner, inbound, epoch=1, reply_text="你周五有空讨论排期吗？", now=now)
    quoted_host = _HostEvent("回复机器人上一条：排期", "user-3", reply_to="bot-1")
    quoted = _bind(
        quoted_host,
        _canonical(quoted_host, topic_epoch=2),
        epoch=2,
        rotation_reason="explicit_history_recall",
    )
    decision = store.evaluate_topic_bridge(
        CHAT,
        event=quoted.get_extra("astrmai_conversation_event"),
        target_topic_epoch=2,
        rotation_reason="explicit_history_recall",
    )
    assert decision.allowed is False
    assert decision.reason == "unverified_reply_target"
    assert not any(value.startswith(("reply_commit_", "turn-", "trace-", "fallback_")) for value in decision.evidence_event_ids)


def test_s519_committed_turn_round_trip_preserves_bounded_unique_outbound_ids():
    committed = _committed_turn("bot-1", "bot-2", "bot-1", *[f"bot-{i}" for i in range(3, 40)])
    restored = CommittedBotTurn.from_dict(committed.as_dict())
    assert restored.outbound_message_ids == tuple(dict.fromkeys(committed.outbound_message_ids))
    store = ConversationContinuityStore()
    store.record(
        chat_id=CHAT,
        focus_preview="项目计划",
        reply_preview="已记录。",
        sender_id="u-alice",
        source_event_id="user-1",
        assistant_outbound_ids=restored.outbound_message_ids,
        topic_epoch=1,
        now=time.time(),
    )
    record = store._state(CHAT).turns[-1]
    assert len(record.assistant_outbound_ids) <= store.ANCHOR_MAX_EVENT_IDS
    assert len(record.assistant_outbound_ids) == len(set(record.assistant_outbound_ids))
    assert "reply_commit_" not in record.assistant_outbound_ids
