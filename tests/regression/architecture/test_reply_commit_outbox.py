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


def test_reply_commit_enqueue_does_not_wait_for_consumer(tmp_path):
    path = tmp_path / "astrmai.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 71")
        _run_migrations(db)
        db.commit()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_consumer(_turn):
        started.set()
        await release.wait()
        calls.append("done")
        return "committed"

    async def run():
        service = ReplyCommitService(ReplyCommitOutboxStore(path))
        result = await asyncio.wait_for(
            service.enqueue(None, _committed_turn(), consumers={"memory": slow_consumer}),
            timeout=0.5,
        )
        assert result.consumer_status == {"memory": "pending"}
        await asyncio.wait_for(started.wait(), timeout=1.0)
        release.set()
        await asyncio.sleep(0)
        await service.stop()

    asyncio.run(run())
    assert calls == ["done"]


def test_reply_commit_inline_consumer_keeps_lease_for_detached_consumer(tmp_path):
    path = tmp_path / "inline-and-detached.db"
    _init_reply_outbox_db(path)
    inline_calls = []
    detached_done = asyncio.Event()

    async def inline_history(_turn):
        inline_calls.append("history")
        return "committed"

    async def detached_memory(_turn):
        detached_done.set()
        return "committed"

    async def run():
        store = ReplyCommitOutboxStore(path)
        service = ReplyCommitService(store)
        result = await service.enqueue(
            None,
            _committed_turn(),
            consumers={"history": inline_history, "memory": detached_memory},
            inline_consumer_names=("history",),
        )
        assert result.consumer_status == {
            "history": "committed",
            "memory": "pending",
        }
        await asyncio.wait_for(detached_done.wait(), timeout=1.0)
        for _ in range(20):
            if await store.get(_committed_turn().commit_id) is None:
                break
            await asyncio.sleep(0)
        remaining = await store.get(_committed_turn().commit_id)
        await service.stop()
        return remaining

    assert asyncio.run(run()) is None
    assert inline_calls == ["history"]


def test_reply_commit_without_outbox_does_not_await_consumer():
    started = asyncio.Event()
    release = asyncio.Event()

    class Event:
        def __init__(self):
            self.extra = {}

        def set_extra(self, key, value):
            self.extra[key] = value

    async def never_finishes(_turn):
        started.set()
        await release.wait()
        return "committed"

    async def run():
        service = ReplyCommitService()
        event = Event()
        result = await asyncio.wait_for(
            service.enqueue(
                event,
                _committed_turn(),
                consumers={"memory": never_finishes},
                inline_consumer_names=("memory",),
            ),
            timeout=0.2,
        )
        assert result.consumer_status == {"memory": "pending"}
        assert event.extra["astrmai_post_send_status"] == "post_send_degraded"
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await service.stop()

    asyncio.run(run())


def test_reply_commit_without_outbox_ignores_inline_hint_for_hanging_consumer():
    started = asyncio.Event()
    release = asyncio.Event()

    class Event:
        def __init__(self):
            self.extra = {}

        def set_extra(self, key, value):
            self.extra[key] = value

    async def hanging_history(_turn):
        started.set()
        await release.wait()
        return "committed"

    async def run():
        service = ReplyCommitService()
        result = await asyncio.wait_for(
            service.enqueue(
                Event(),
                _committed_turn(),
                consumers={"native_history": hanging_history},
                inline_consumer_names=("native_history",),
            ),
            timeout=0.2,
        )
        assert result.repair_scheduled is True
        await asyncio.wait_for(started.wait(), timeout=0.5)
        await service.stop()

    asyncio.run(run())


def _init_reply_outbox_db(path):
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 71")
        _run_migrations(db)
        db.commit()


def test_reply_commit_outbox_claim_is_exclusive_across_workers(tmp_path):
    path = tmp_path / "claim.db"
    _init_reply_outbox_db(path)
    committed = _committed_turn()

    async def run():
        first = ReplyCommitOutboxStore(path)
        second = ReplyCommitOutboxStore(path)
        await first.save(
            committed,
            repair_context={},
            consumer_status={"memory": "pending"},
            next_retry_at=100.0,
        )
        claims = await asyncio.gather(
            first.claim_due(now=100.0, lease_seconds=30.0),
            second.claim_due(now=100.0, lease_seconds=30.0),
        )
        return claims

    claims = asyncio.run(run())
    assert sorted(len(items) for items in claims) == [0, 1]


def test_reply_commit_outbox_expired_lease_can_be_reclaimed(tmp_path):
    path = tmp_path / "expiry.db"
    _init_reply_outbox_db(path)
    committed = _committed_turn()

    async def run():
        store = ReplyCommitOutboxStore(path)
        await store.save(
            committed,
            repair_context={},
            consumer_status={"memory": "pending"},
            next_retry_at=100.0,
        )
        first = await store.claim_due(now=100.0, lease_seconds=10.0)
        live = await store.claim_due(now=105.0, lease_seconds=10.0)
        reclaimed = await store.claim_due(now=111.0, lease_seconds=10.0)
        return first, live, reclaimed

    first, live, reclaimed = asyncio.run(run())
    assert len(first) == 1
    assert live == []
    assert len(reclaimed) == 1
    assert reclaimed[0][1] != first[0][1]


def test_reply_commit_outbox_delete_requires_matching_lease_token(tmp_path):
    path = tmp_path / "token.db"
    _init_reply_outbox_db(path)
    committed = _committed_turn()

    async def run():
        store = ReplyCommitOutboxStore(path)
        await store.save(
            committed,
            repair_context={},
            consumer_status={"memory": "pending"},
            next_retry_at=100.0,
        )
        claimed = await store.claim_due(now=100.0, lease_seconds=30.0)
        token = claimed[0][1]
        await store.delete(committed.commit_id, lease_token="wrong-token")
        remains = await store.get(committed.commit_id)
        await store.delete(committed.commit_id, lease_token=token)
        deleted = await store.get(committed.commit_id)
        return remains, deleted

    remains, deleted = asyncio.run(run())
    assert remains is not None
    assert deleted is None


def test_reply_commit_enqueue_initial_worker_holds_outbox_lease(tmp_path):
    path = tmp_path / "initial-worker.db"
    _init_reply_outbox_db(path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_consumer(_turn):
        started.set()
        await release.wait()
        return "committed"

    async def run():
        service = ReplyCommitService(ReplyCommitOutboxStore(path))
        await service.enqueue(
            None,
            _committed_turn(),
            consumers={"memory": slow_consumer},
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        competing = await ReplyCommitOutboxStore(path).claim_due()
        release.set()
        await service.stop()
        return competing

    assert asyncio.run(run()) == []


def test_reply_commit_stop_releases_initial_worker_lease_immediately(tmp_path):
    path = tmp_path / "cancel-release.db"
    _init_reply_outbox_db(path)
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def slow_consumer(_turn):
        started.set()
        await never_release.wait()
        return "committed"

    async def run():
        service = ReplyCommitService(ReplyCommitOutboxStore(path))
        await service.enqueue(
            None,
            _committed_turn(),
            consumers={"memory": slow_consumer},
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await service.stop()
        return await ReplyCommitOutboxStore(path).claim_due()

    claimed = asyncio.run(run())
    assert len(claimed) == 1
    assert claimed[0][0].consumer_status == {"memory": "pending"}


def test_reply_commit_outbox_stale_worker_cannot_update_reclaimed_row(tmp_path):
    path = tmp_path / "stale-worker.db"
    _init_reply_outbox_db(path)
    committed = _committed_turn()

    async def run():
        store = ReplyCommitOutboxStore(path)
        await store.save(
            committed,
            repair_context={},
            consumer_status={"memory": "pending"},
            next_retry_at=100.0,
        )
        first = (await store.claim_due(now=100.0, lease_seconds=10.0))[0]
        second = (await store.claim_due(now=111.0, lease_seconds=10.0))[0]
        stale_changed = await store.update_claimed(
            committed,
            repair_context={},
            consumer_status={"memory": "committed"},
            attempts=0,
            last_error="",
            lease_token=first[1],
        )
        current_changed = await store.update_claimed(
            committed,
            repair_context={},
            consumer_status={"memory": "committed"},
            attempts=0,
            last_error="",
            lease_token=second[1],
        )
        return stale_changed, current_changed

    assert asyncio.run(run()) == (False, True)
