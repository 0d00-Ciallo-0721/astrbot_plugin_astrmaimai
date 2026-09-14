import asyncio
import time
import sqlite3

from astrmai.infrastructure.runtime.cross_session_handoff_store import (
    CrossSessionHandoff,
    CrossSessionHandoffStore,
)
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations


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
        for index in range(3):
            claim = await store.claim_for_recipient(
                "default", "recipient", owner=f"turn-{index}"
            )
            snapshots.append(claim)
            assert claim is not None
            assert await store.acknowledge(
                handoff.handoff_id,
                lease_token=claim.lease_token,
                expected_revision=claim.revision,
            )
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


def test_expired_handoff_is_retained_as_expired_for_diagnostics(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)

    async def _run():
        store = CrossSessionHandoffStore(db_path)
        handoff = _handoff(expires_at=time.time() + 10.0)
        await store.put(handoff)
        async with store._lock:
            store._handoffs[("default", "recipient")][-1].expires_at = time.time() - 1.0
        assert await store.peek_for_recipient("default", "recipient") is None
        return await store.lookup_for_recipient("default", "recipient")

    expired = asyncio.run(_run())
    assert expired is not None
    assert expired.status == "expired"
    assert expired.failure_stage == "lookup"
    assert expired.failure_kind == "handoff_expired"
    with sqlite3.connect(db_path) as db:
        assert db.execute(
            "SELECT status FROM cross_session_handoff WHERE handoff_id=?",
            (expired.handoff_id,),
        ).fetchone() == ("expired",)


def test_handoff_claim_is_unique_and_acknowledge_requires_matching_lease():
    async def _run():
        store = CrossSessionHandoffStore()
        handoff = _handoff()
        await store.put(handoff)
        first = await store.claim_for_recipient(
            "default", "recipient", owner="turn-a", lease_seconds=30
        )
        second = await store.claim_for_recipient(
            "default", "recipient", owner="turn-b", lease_seconds=30
        )
        wrong_ack = await store.acknowledge(
            handoff.handoff_id,
            lease_token="wrong",
            expected_revision=first.revision,
        )
        right_ack = await store.acknowledge(
            handoff.handoff_id,
            lease_token=first.lease_token,
            expected_revision=first.revision,
        )
        return first, second, wrong_ack, right_ack

    first, second, wrong_ack, right_ack = asyncio.run(_run())
    assert first is not None
    assert first.status == "claimed"
    assert first.lease_token
    assert second is None
    assert wrong_ack is False
    assert right_ack is True


def test_handoff_expiry_race_blocks_claim_after_lookup():
    async def _run():
        store = CrossSessionHandoffStore()
        handoff = _handoff(expires_at=time.time() + 10)
        await store.put(handoff)
        snapshot = await store.peek_for_recipient("default", "recipient")
        async with store._lock:
            store._handoffs[("default", "recipient")][-1].expires_at = time.time() - 1
        claim = await store.claim_for_recipient(
            "default", "recipient", owner="turn-a", expected_revision=snapshot.revision
        )
        decision = await store.lookup_for_recipient("default", "recipient")
        return claim, decision

    claim, decision = asyncio.run(_run())
    assert claim is None
    assert decision is not None
    assert decision.status == "expired"


def test_handoff_stale_revision_cannot_claim():
    async def _run():
        store = CrossSessionHandoffStore()
        await store.put(_handoff())
        return await store.claim_for_recipient(
            "default", "recipient", owner="turn-a", expected_revision=0
        )

    assert asyncio.run(_run()) is None


def test_claim_survives_store_recreation_and_blocks_duplicate(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 129")
        _run_migrations(db)

    async def _run():
        first_store = CrossSessionHandoffStore(db_path)
        await first_store.put(_handoff())
        first = await first_store.claim_for_recipient(
            "default", "recipient", owner="turn-a", lease_seconds=30
        )
        duplicate = await CrossSessionHandoffStore(db_path).claim_for_recipient(
            "default", "recipient", owner="turn-b", lease_seconds=30
        )
        return first, duplicate

    first, duplicate = asyncio.run(_run())
    assert first is not None
    assert duplicate is None


def test_complete_for_recipient_removes_only_latest_handoff():
    async def _run():
        store = CrossSessionHandoffStore()
        first = _handoff()
        second = _handoff()
        await store.put(first)
        await store.put(second)
        claim = await store.claim_for_recipient(
            "default", "recipient", owner="turn-complete"
        )
        completed = await store.complete_for_recipient(
            "default",
            "recipient",
            handoff_id=claim.handoff_id,
            owner=claim.owner,
            lease_token=claim.lease_token,
            expected_revision=claim.revision,
        )
        remaining = await store.peek_for_recipient("default", "recipient")
        return completed, remaining

    completed, remaining = asyncio.run(_run())

    assert completed is True
    assert remaining is not None


def test_handoff_is_restored_after_store_recreation(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)

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


def _create_handoff_table(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute(
            """CREATE TABLE cross_session_handoff (
                handoff_id TEXT PRIMARY KEY, platform_id TEXT, source_umo TEXT,
                source_sender_id TEXT, source_sender_name TEXT, target_umo TEXT,
                target_id TEXT, target_name TEXT, outbound_message TEXT,
                context_summary TEXT, delivery_mode TEXT, observed_turns INTEGER,
                status TEXT, created_at REAL, expires_at REAL, updated_at REAL,
                owner TEXT DEFAULT '', lease_token TEXT DEFAULT '', lease_until REAL DEFAULT 0,
                revision INTEGER DEFAULT 1, failure_stage TEXT DEFAULT '',
                failure_kind TEXT DEFAULT '', error_type TEXT DEFAULT '', error_summary TEXT DEFAULT ''
            )"""
        )


def test_completed_handoff_is_not_restored(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)

    async def _run():
        store = CrossSessionHandoffStore(db_path)
        await store.put(_handoff())
        claim = await store.claim_for_recipient(
            "default", "recipient", owner="turn-complete"
        )
        assert claim is not None
        assert await store.complete_for_recipient(
            "default",
            "recipient",
            handoff_id=claim.handoff_id,
            owner=claim.owner,
            lease_token=claim.lease_token,
            expected_revision=claim.revision,
        ) is True
        return await CrossSessionHandoffStore(db_path).peek_for_recipient("default", "recipient")

    assert asyncio.run(_run()) is None


def _read_handoff_row(db_path, handoff_id):
    with sqlite3.connect(db_path) as db:
        return db.execute(
            """SELECT status, owner, lease_token, lease_until, revision,
                      observed_turns, failure_stage, failure_kind, error_summary
               FROM cross_session_handoff WHERE handoff_id=?""",
            (handoff_id,),
        ).fetchone()


def test_stale_instance_acknowledge_cannot_clear_new_lease(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)

    async def _run():
        stale_store = CrossSessionHandoffStore(db_path)
        handoff = _handoff()
        await stale_store.put(handoff)
        stale = await stale_store.peek_for_recipient("default", "recipient")
        current_store = CrossSessionHandoffStore(db_path)
        claimed = await current_store.claim_for_recipient(
            "default", "recipient", owner="current-turn"
        )
        async with stale_store._lock:
            cached = stale_store._handoffs[("default", "recipient")][-1]
            cached.status = "claimed"
            cached.owner = "current-turn"
            cached.lease_token = claimed.lease_token
            cached.lease_until = claimed.lease_until
        stale_ack = await stale_store.acknowledge(
            handoff.handoff_id,
            lease_token=claimed.lease_token,
            expected_revision=stale.revision,
        )
        return handoff, claimed, stale_ack

    handoff, claimed, stale_ack = asyncio.run(_run())
    assert stale_ack is False
    row = _read_handoff_row(db_path, handoff.handoff_id)
    assert row[:3] == ("claimed", "current-turn", claimed.lease_token)
    assert row[4:6] == (claimed.revision, 0)


def test_stale_instance_complete_cannot_overwrite_new_claim(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)

    async def _run():
        stale_store = CrossSessionHandoffStore(db_path)
        handoff = _handoff()
        await stale_store.put(handoff)
        stale = await stale_store.peek_for_recipient("default", "recipient")
        current_store = CrossSessionHandoffStore(db_path)
        claimed = await current_store.claim_for_recipient(
            "default", "recipient", owner="current-turn"
        )
        async with stale_store._lock:
            cached = stale_store._handoffs[("default", "recipient")][-1]
            cached.status = "claimed"
            cached.owner = "current-turn"
            cached.lease_token = claimed.lease_token
            cached.lease_until = claimed.lease_until
        completed = await stale_store.complete_for_recipient(
            "default",
            "recipient",
            handoff_id=handoff.handoff_id,
            owner="current-turn",
            lease_token=claimed.lease_token,
            expected_revision=stale.revision,
        )
        return handoff, claimed, completed

    handoff, claimed, completed = asyncio.run(_run())
    assert completed is False
    row = _read_handoff_row(db_path, handoff.handoff_id)
    assert row[:3] == ("claimed", "current-turn", claimed.lease_token)
    assert row[4] == claimed.revision


def test_stale_persist_cannot_overwrite_newer_revision_or_diagnostics(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)
    store = CrossSessionHandoffStore(db_path)
    handoff = _handoff()
    handoff.status = "claimed"
    handoff.owner = "current-turn"
    handoff.lease_token = "current-token"
    handoff.lease_until = time.time() + 30
    handoff.revision = 2
    handoff.failure_stage = "claim"
    handoff.failure_kind = "current-diagnostic"
    handoff.error_summary = "current error"
    assert store._persist_sync(handoff) is True

    stale = _handoff()
    stale.handoff_id = handoff.handoff_id
    stale.status = "active"
    stale.revision = 1
    stale.failure_stage = ""
    stale.failure_kind = ""
    assert store._persist_sync(stale) is False

    row = _read_handoff_row(db_path, handoff.handoff_id)
    assert row[:3] == ("claimed", "current-turn", "current-token")
    assert row[4] == 2
    assert row[6:] == ("claim", "current-diagnostic", "current error")


def test_same_revision_persist_is_idempotent_only_for_identical_state(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)
    store = CrossSessionHandoffStore(db_path)
    handoff = _handoff()
    assert store._persist_sync(handoff) is True
    assert store._persist_sync(handoff) is True

    conflicting = _handoff()
    conflicting.handoff_id = handoff.handoff_id
    conflicting.created_at = handoff.created_at
    conflicting.updated_at = handoff.updated_at
    conflicting.expires_at = handoff.expires_at
    conflicting.status = "failed"
    conflicting.failure_stage = "dispatch"
    conflicting.failure_kind = "conflicting_same_revision"
    assert conflicting.revision == handoff.revision
    assert store._persist_sync(conflicting) is False
    assert _read_handoff_row(db_path, handoff.handoff_id)[0] == "active"


def test_expired_lease_can_be_reclaimed_and_old_owner_cannot_acknowledge(tmp_path):
    db_path = tmp_path / "handoff.db"
    _create_handoff_table(db_path)

    async def _run():
        old_store = CrossSessionHandoffStore(db_path)
        handoff = _handoff()
        await old_store.put(handoff)
        old_claim = await old_store.claim_for_recipient(
            "default", "recipient", owner="old-turn"
        )
        expired_at = time.time() - 1
        with sqlite3.connect(db_path) as db:
            db.execute(
                "UPDATE cross_session_handoff SET lease_until=? WHERE handoff_id=?",
                (expired_at, handoff.handoff_id),
            )
        new_store = CrossSessionHandoffStore(db_path)
        new_claim = await new_store.claim_for_recipient(
            "default", "recipient", owner="new-turn"
        )
        old_ack = await old_store.acknowledge(
            handoff.handoff_id,
            lease_token=old_claim.lease_token,
            expected_revision=old_claim.revision,
        )
        return handoff, new_claim, old_ack

    handoff, new_claim, old_ack = asyncio.run(_run())
    assert new_claim is not None
    assert new_claim.owner == "new-turn"
    assert old_ack is False
    row = _read_handoff_row(db_path, handoff.handoff_id)
    assert row[:3] == ("claimed", "new-turn", new_claim.lease_token)
    assert row[4] == new_claim.revision


def test_claim_fails_closed_when_lease_schema_is_missing(tmp_path):
    db_path = tmp_path / "legacy.db"
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
        claim = await store.claim_for_recipient(
            "default", "recipient", owner="turn-a"
        )
        return store, claim

    store, claim = asyncio.run(_run())
    assert claim is None
    assert store.last_failure == {
        "failure_stage": "claim",
        "failure_kind": "lease_schema_unavailable",
        "error_type": "SchemaNotReady",
    }


def test_two_stores_concurrently_claim_at_most_once_for_twenty_rounds(tmp_path):
    async def _run():
        for round_index in range(20):
            db_path = tmp_path / f"handoff-{round_index}.db"
            _create_handoff_table(db_path)
            seed = CrossSessionHandoffStore(db_path)
            await seed.put(_handoff())
            first = CrossSessionHandoffStore(db_path)
            second = CrossSessionHandoffStore(db_path)
            claims = await asyncio.gather(
                first.claim_for_recipient("default", "recipient", owner="turn-a"),
                second.claim_for_recipient("default", "recipient", owner="turn-b"),
            )
            winners = [claim for claim in claims if claim is not None]
            assert len(winners) == 1
            winner = winners[0]
            winner_store = first if claims[0] is winner else second
            assert await winner_store.acknowledge(
                winner.handoff_id,
                lease_token=winner.lease_token,
                expected_revision=winner.revision,
            )

    asyncio.run(_run())
