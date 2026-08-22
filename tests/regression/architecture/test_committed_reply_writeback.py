from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from astrmai.conversation.contracts.turn_target import TargetKind, TurnTarget
from astrmai.conversation.execution.reply_commit_service import ReplyCommitService
from astrmai.conversation.planning.planner import Planner


class _Event:
    def __init__(self) -> None:
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value) -> None:
        self._extra[key] = value


def _target() -> TurnTarget:
    return TurnTarget(
        target_kind=TargetKind.ACTOR,
        target_actor_id="user-1",
        target_actor_name="Alice",
        target_event_id="event-1",
        topic_epoch=7,
        attention_topic_key="attention-topic-7",
        source_event_ids=("event-1",),
        confidence=1.0,
    )


def _plan() -> ReplyPlan:
    return ReplyPlan.create(
        turn_id="turn-1",
        chat_id="ff:GroupMessage:group-1",
        chat_kind="group",
        target=_target(),
        planned_text="first\nsecond",
        planned_segments=("first", "second"),
        response_kind="final",
        created_at=1000.0,
    )


def test_committed_turn_is_built_only_from_sent_receipt_content():
    plan = _plan()
    receipt = ReplySendReceipt(
        status=ReplyCommitStatus.PARTIAL,
        sent_segments=("first",),
        outbound_message_ids=("message-1",),
        sent_at=1001.0,
        failure_reason="second segment failed",
    )

    committed = CommittedBotTurn.from_plan(plan, receipt)

    assert committed.partial_send is True
    assert committed.visible_text == "first"
    assert committed.persistable_text == "first"
    assert committed.sent_segments == ("first",)
    assert committed.outbound_message_ids == ("message-1",)
    assert committed.target.target_actor_id == "user-1"
    assert committed.attention_topic_key == "attention-topic-7"


def test_failed_receipt_cannot_become_committed_visible_turn():
    plan = _plan()
    receipt = ReplySendReceipt(
        status=ReplyCommitStatus.FAILED,
        sent_at=1001.0,
        failure_reason="first segment failed",
    )

    try:
        CommittedBotTurn.from_plan(plan, receipt)
    except ValueError as exc:
        assert "visible commit" in str(exc)
    else:
        raise AssertionError("failed send must not create a committed visible turn")


def test_commit_service_is_idempotent_per_consumer_and_retries_only_failed_consumers():
    event = _Event()
    committed = CommittedBotTurn.from_plan(
        _plan(),
        ReplySendReceipt(
            status=ReplyCommitStatus.SENT,
            sent_segments=("first", "second"),
            outbound_message_ids=("message-1", "message-2"),
            sent_at=1001.0,
        ),
    )
    calls = {"history": 0, "memory": 0}

    async def history_consumer(_turn):
        calls["history"] += 1
        return "committed"

    async def flaky_memory_consumer(_turn):
        calls["memory"] += 1
        if calls["memory"] == 1:
            raise RuntimeError("temporary memory failure")
        return "committed"

    async def run():
        service = ReplyCommitService()
        first = await service.commit(
            event,
            committed,
            consumers={
                "history": history_consumer,
                "memory": flaky_memory_consumer,
            },
        )
        second = await service.commit(
            event,
            committed,
            consumers={
                "history": history_consumer,
                "memory": flaky_memory_consumer,
            },
        )
        return first, second

    first, second = asyncio.run(run())

    assert first.consumer_status == {
        "history": "committed",
        "memory": "failed",
    }
    assert second.consumer_status == {
        "history": "committed",
        "memory": "committed",
    }
    assert calls == {"history": 1, "memory": 2}
    assert event.get_extra("astrmai_reply_commit_id") == committed.commit_id
    assert event.get_extra("astrmai_reply_commit_repair_scheduled") is False
    observation = event.get_extra("astrmai_architecture_observation")
    assert observation["reply_commit"]["commit_id"] == committed.commit_id
    assert observation["reply_commit"]["committed_consumer_count"] == 2
    assert observation["reply_commit"]["failed_consumer_count"] == 0
    assert observation["reply_commit"]["elapsed_ms"] >= 0.0


def test_group_store_receives_only_committed_sent_text_and_deduplicates_replay():
    store = GroupDialogueStore()
    committed = CommittedBotTurn.from_plan(
        _plan(),
        ReplySendReceipt(
            status=ReplyCommitStatus.PARTIAL,
            sent_segments=("first",),
            outbound_message_ids=("message-1",),
            sent_at=1001.0,
            failure_reason="second segment failed",
        ),
    )

    async def run():
        first = await store.append_committed_bot_turn(
            committed,
            bot_id="bot-1",
            bot_name="Bot",
            stance="reject",
            social_event="boundary_violation",
        )
        second = await store.append_committed_bot_turn(
            committed,
            bot_id="bot-1",
            bot_name="Bot",
            stance="reject",
            social_event="boundary_violation",
        )
        turns = await store.get_recent_bot_turns(
            committed.chat_id,
            target_sender_id="user-1",
            now=2000.0,
            ttl_seconds=2000.0,
        )
        return first, second, turns

    first, second, turns = asyncio.run(run())

    assert first is second
    assert len(turns) == 1
    assert turns[0].reply_text == "first"
    assert turns[0].source_event_ids == ["event-1"]
    assert turns[0].stance == "reject"
    assert turns[0].attention_topic_key == "attention-topic-7"


def test_planner_has_no_direct_assistant_history_append_path():
    source = inspect.getsource(Planner)

    assert "_record_planner_dialogue_segment" not in source
    assert "_append_dialogue_segment" not in source
    assert "append_committed_bot_turn" not in source
