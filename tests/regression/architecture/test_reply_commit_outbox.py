from __future__ import annotations

import asyncio
import sqlite3

from astrmai.conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from astrmai.conversation.contracts.turn_target import TargetKind, TurnTarget
from astrmai.conversation.execution.reply_commit_service import ReplyCommitService
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.infrastructure.persistence.reply_commit_outbox import ReplyCommitOutboxStore


def _committed_turn() -> CommittedBotTurn:
    plan = ReplyPlan.create(
        turn_id="turn-restart",
        chat_id="ff:GroupMessage:group-1",
        chat_kind="group",
        target=TurnTarget(
            target_kind=TargetKind.ACTOR,
            target_actor_id="user-1",
            target_actor_name="匿名用户",
            target_event_id="event-1",
            topic_epoch=3,
            source_event_ids=("event-1",),
            confidence=1.0,
        ),
        planned_text="已发送回复",
        planned_segments=("已发送回复",),
        created_at=1000.0,
    )
    return CommittedBotTurn.from_plan(
        plan,
        ReplySendReceipt(
            status=ReplyCommitStatus.SENT,
            sent_segments=("已发送回复",),
            outbound_message_ids=("message-1",),
            sent_at=1001.0,
        ),
    )


def test_committed_turn_round_trip_preserves_send_evidence():
    committed = _committed_turn()

    restored = CommittedBotTurn.from_dict(committed.as_dict())

    assert restored == committed
    assert restored.target.target_actor_id == "user-1"
    assert restored.send_status is ReplyCommitStatus.SENT


def test_reply_commit_outbox_recovers_after_restart_without_replaying_success(tmp_path):
    path = tmp_path / "astrmai.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 71")
        _run_migrations(db)
        db.commit()

    committed = _committed_turn()
    repair_context = {"user_text": "用户消息", "sender_id": "user-1"}
    calls = {"history": 0, "memory": 0}

    async def history(_turn):
        calls["history"] += 1
        return "committed"

    async def failing_memory(_turn):
        calls["memory"] += 1
        raise RuntimeError("temporary memory failure")

    async def recovered_memory(_turn):
        calls["memory"] += 1
        return "committed"

    async def run():
        first_store = ReplyCommitOutboxStore(path, retry_base_seconds=0.0)
        first_service = ReplyCommitService(first_store)
        first = await first_service.commit(
            None,
            committed,
            consumers={"history": history, "memory": failing_memory},
            repair_context=repair_context,
        )
        pending = await first_store.get(committed.commit_id)

        second_store = ReplyCommitOutboxStore(path, retry_base_seconds=0.0)
        second_service = ReplyCommitService(second_store)

        def consumer_factory(turn, context):
            assert turn == committed
            assert context == repair_context
            return {"history": history, "memory": recovered_memory}

        repaired = await second_service.repair_pending(consumer_factory)
        remaining = await second_store.get(committed.commit_id)
        return first, pending, repaired, remaining

    first, pending, repaired, remaining = asyncio.run(run())

    assert first.repair_scheduled is True
    assert pending is not None
    assert pending.consumer_status == {
        "history": "committed",
        "memory": "failed",
    }
    assert repaired == 1
    assert remaining is None
    assert calls == {"history": 1, "memory": 2}
