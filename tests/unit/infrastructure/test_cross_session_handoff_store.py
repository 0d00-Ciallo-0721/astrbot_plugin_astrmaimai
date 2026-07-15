import asyncio
import time

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
