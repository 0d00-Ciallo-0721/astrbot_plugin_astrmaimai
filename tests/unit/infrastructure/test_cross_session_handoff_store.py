import asyncio
import time
import sqlite3

from astrmai.infrastructure.runtime.cross_session_handoff_store import (
    CrossSessionHandoff,
    CrossSessionHandoffStore,
)


def _handoff(*, target_id: str = "recipient", expires_at: float = 0.0):
    return CrossSessionHandoff(
        platform_id="default",
        source_umo="default:FriendMessage:origin",
        source_sender_id="origin",
        source_sender_name="Alice",
        target_umo=f"default:FriendMessage:{target_id}",
        target_id=target_id,
        target_name="Bob",
        outbound_message="Alice让我转告你：明天见",
        context_summary="Alice 委托我通知 Bob 明天见。",
        delivery_mode="relay",
        expires_at=expires_at,
    )


def test_handoff_survives_three_observed_turns_then_is_consumed():
    async def _run():
        store = CrossSessionHandoffStore()
        handoff = _handoff()
        await store.put(handoff)
        snapshots = []
        for _ in range(3):
            snapshots.append(await store.peek_for_recipient("default", "recipient"))
            await store.acknowledge(handoff.handoff_id)
        return snapshots, await store.peek_for_recipient("default", "recipient")

    snapshots, remaining = asyncio.run(_run())

    assert all(item is not None for item in snapshots)
    assert remaining is None


def test_expired_handoff_is_not_returned():
    async def _run():
        store = CrossSessionHandoffStore()
        handoff = _handoff(expires_at=time.time() + 1.0)
        await store.put(handoff)
        async with store._lock:
            store._handoffs[("default", "recipient")][-1].expires_at = time.time() - 1.0
        return await store.peek_for_recipient("default", "recipient")

    assert asyncio.run(_run()) is None


def test_complete_for_recipient_removes_only_latest_handoff():
    async def _run():
        store = CrossSessionHandoffStore()
        first = _handoff()
        second = _handoff()
        await store.put(first)
        await store.put(second)
        completed = await store.complete_for_recipient("default", "recipient")
        remaining = await store.peek_for_recipient("default", "recipient")
        return completed, remaining

    completed, remaining = asyncio.run(_run())

    assert completed is True
    assert remaining is not None


def test_handoff_is_restored_after_store_recreation(tmp_path):
    db_path = tmp_path / "handoff.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE cross_session_handoff (
                handoff_id TEXT PRIMARY KEY, platform_id TEXT, source_umo TEXT,
                source_sender_id TEXT, source_sender_name TEXT, target_umo TEXT,
                target_id TEXT, target_name TEXT, outbound_message TEXT,
                context_summary TEXT, delivery_mode TEXT, observed_turns INTEGER,
                status TEXT, created_at REAL, expires_at REAL, updated_at REAL
            )"""
        )

    async def _run():
        first = CrossSessionHandoffStore(db_path)
        handoff = _handoff()
        await first.put(handoff)
        restored = await CrossSessionHandoffStore(db_path).peek_for_recipient("default", "recipient")
        return handoff, restored

    handoff, restored = asyncio.run(_run())
    assert restored is not None
    assert restored.handoff_id == handoff.handoff_id
    assert restored.context_summary == handoff.context_summary


def test_completed_handoff_is_not_restored(tmp_path):
    db_path = tmp_path / "handoff.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE cross_session_handoff (
                handoff_id TEXT PRIMARY KEY, platform_id TEXT, source_umo TEXT,
                source_sender_id TEXT, source_sender_name TEXT, target_umo TEXT,
                target_id TEXT, target_name TEXT, outbound_message TEXT,
                context_summary TEXT, delivery_mode TEXT, observed_turns INTEGER,
                status TEXT, created_at REAL, expires_at REAL, updated_at REAL
            )"""
        )

    async def _run():
        store = CrossSessionHandoffStore(db_path)
        await store.put(_handoff())
        assert await store.complete_for_recipient("default", "recipient") is True
        return await CrossSessionHandoffStore(db_path).peek_for_recipient("default", "recipient")

    assert asyncio.run(_run()) is None
