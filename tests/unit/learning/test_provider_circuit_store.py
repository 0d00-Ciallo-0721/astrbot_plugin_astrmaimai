import asyncio
import sqlite3

import aiosqlite
import pytest

from astrmai.infrastructure.persistence.persistence_schema import (
    _run_migrations,
    _run_migrations_async,
)
from astrmai.learning.persistence.provider_circuit_store import (
    LearningProviderCircuitStore,
)


@pytest.fixture
def circuit_store(tmp_path):
    db_path = tmp_path / "circuit.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 144")
        _run_migrations(db)
        db.commit()
    return LearningProviderCircuitStore(db_path)


def test_provider_circuit_migration_is_additive_and_reaches_v146(tmp_path):
    db_path = tmp_path / "migration.db"
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA user_version = 144")
        _run_migrations(db)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='learning_provider_circuit'"
        ).fetchone()
        columns = {
            row[1]
            for row in db.execute("PRAGMA table_info(learning_provider_circuit)").fetchall()
        }
        settlement_table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='learning_provider_circuit_settlement'"
        ).fetchone()
    assert version == 146
    assert table == ("learning_provider_circuit",)
    assert settlement_table == ("learning_provider_circuit_settlement",)
    assert {"provider_key", "task_family", "revision", "half_open_token"} <= columns


@pytest.mark.asyncio
async def test_provider_circuit_async_migration_reaches_v146(tmp_path):
    db_path = tmp_path / "migration-async.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA user_version = 144")
        await _run_migrations_async(db)
        await db.commit()
        cursor = await db.execute("PRAGMA user_version")
        version_row = await cursor.fetchone()
        await cursor.close()
        cursor = await db.execute("PRAGMA table_info(learning_provider_circuit)")
        columns = {row[1] for row in await cursor.fetchall()}
        await cursor.close()
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='learning_provider_circuit_settlement'"
        )
        settlement_table = await cursor.fetchone()
        await cursor.close()

    assert version_row == (146,)
    assert settlement_table == ("learning_provider_circuit_settlement",)
    assert {"provider_key", "task_family", "revision", "half_open_token"} <= columns


@pytest.mark.asyncio
async def test_three_provider_failures_open_circuit_and_non_provider_failure_does_not(circuit_store):
    key = circuit_store.resolve_provider_key("provider-1", "openai")
    assert key == "id:provider-1"

    ignored = await circuit_store.record_failure(
        provider_key=key,
        task_family="learning.expression_enrichment",
        failure_kind="queue_timeout",
        now=100.0,
    )
    assert ignored.applied is False
    assert await circuit_store.get_state(key, "learning.expression_enrichment") is None

    for offset in range(3):
        result = await circuit_store.record_failure(
            provider_key=key,
            task_family="learning.expression_enrichment",
            failure_kind="provider_error",
            now=101.0 + offset,
        )
        assert result.applied is True

    state = await circuit_store.get_state(key, "learning.expression_enrichment")
    assert state is not None
    assert state.state == "open"
    assert state.failure_count == 3
    assert state.circuit_until == pytest.approx(1003.0)


@pytest.mark.asyncio
async def test_half_open_allows_one_probe_and_success_closes(circuit_store):
    key = "family:openai"
    family = "learning.jargon_enrichment"
    for offset in range(3):
        await circuit_store.record_failure(
            provider_key=key,
            task_family=family,
            failure_kind="provider_timeout",
            now=10.0 + offset,
        )

    first = await circuit_store.check_or_claim(
        provider_key=key,
        task_family=family,
        owner="worker-1",
        lease_seconds=30,
        now=913.0,
    )
    second = await circuit_store.check_or_claim(
        provider_key=key,
        task_family=family,
        owner="worker-2",
        lease_seconds=30,
        now=913.0,
    )
    assert first.allowed is True
    assert first.reason == "half_open_probe"
    assert first.lease_token
    assert second.allowed is False
    assert second.reason == "half_open_busy"

    closed = await circuit_store.record_success(
        provider_key=key,
        task_family=family,
        expected_revision=first.revision,
        lease_token=first.lease_token,
        now=914.0,
    )
    assert closed.applied is True
    assert closed.state.state == "closed"


@pytest.mark.asyncio
async def test_half_open_abort_requires_matching_owner_token_and_revision(circuit_store):
    key = "id:provider-abort"
    family = "learning.expression_enrichment"
    for offset in range(3):
        await circuit_store.record_failure(
            provider_key=key,
            task_family=family,
            failure_kind="provider_error",
            now=10.0 + offset,
        )

    claim = await circuit_store.check_or_claim(
        provider_key=key,
        task_family=family,
        owner="worker-1",
        lease_seconds=30,
        now=913.0,
    )
    stale = await circuit_store.abort_half_open(
        provider_key=key,
        task_family=family,
        owner="worker-2",
        lease_token=claim.lease_token,
        expected_revision=claim.revision,
        now=914.0,
    )
    assert stale.applied is False
    assert stale.conflict is True

    released = await circuit_store.abort_half_open(
        provider_key=key,
        task_family=family,
        owner="worker-1",
        lease_token=claim.lease_token,
        expected_revision=claim.revision,
        now=914.0,
    )
    assert released.applied is True
    assert released.state is not None
    assert released.state.state == "open"
    assert released.state.half_open_owner == ""
    assert released.state.half_open_token == ""
    assert released.state.lease_until == 0

    restarted = LearningProviderCircuitStore(circuit_store.db_path)
    next_claim = await restarted.check_or_claim(
        provider_key=key,
        task_family=family,
        owner="worker-2",
        lease_seconds=30,
        now=914.0,
    )
    assert next_claim.allowed is True
    assert next_claim.reason == "half_open_probe"

    late = await circuit_store.record_success(
        provider_key=key,
        task_family=family,
        expected_revision=claim.revision,
        lease_token=claim.lease_token,
        now=915.0,
    )
    assert late.applied is False
    assert late.conflict is True


@pytest.mark.asyncio
async def test_distinct_stale_revision_failures_merge_and_duplicate_is_idempotent(circuit_store):
    key = "id:provider-cas"
    family = "learning.expression_enrichment"
    initial = await circuit_store.record_failure(
        provider_key=key,
        task_family=family,
        failure_kind="provider_error",
        settlement_id="failure-initial",
        now=1.0,
    )
    assert initial.state is not None

    fresh = await circuit_store.record_failure(
        provider_key=key,
        task_family=family,
        failure_kind="provider_error",
        settlement_id="failure-a",
        expected_revision=initial.state.revision,
        now=2.0,
    )
    merged = await circuit_store.record_failure(
        provider_key=key,
        task_family=family,
        failure_kind="provider_error",
        settlement_id="failure-b",
        expected_revision=initial.state.revision,
        now=3.0,
    )
    assert fresh.applied is True
    assert merged.applied is True
    assert merged.conflict is False

    duplicate = await circuit_store.record_failure(
        provider_key=key,
        task_family=family,
        failure_kind="provider_error",
        settlement_id="failure-a",
        expected_revision=initial.state.revision,
        now=4.0,
    )
    assert duplicate.applied is False
    assert duplicate.conflict is False
    assert duplicate.idempotent is True
    assert duplicate.resulting_state == "closed"
    assert duplicate.resulting_revision == 2
    assert duplicate.current_state is not None
    assert duplicate.current_state.state == "open"
    assert duplicate.current_state.revision == 3

    restarted = LearningProviderCircuitStore(circuit_store.db_path)
    state = await restarted.get_state(key, family)
    assert state is not None
    assert state.revision == 3
    assert state.failure_count == 3
    assert state.state == "open"


@pytest.mark.asyncio
async def test_two_concurrent_failures_from_same_closed_revision_both_count(circuit_store):
    family = "learning.expression_enrichment"
    for round_index in range(20):
        key = f"id:provider-concurrent-{round_index}"
        initial = await circuit_store.record_failure(
            provider_key=key,
            task_family=family,
            failure_kind="provider_error",
            settlement_id=f"initial-{round_index}",
            now=1.0,
        )
        first_decision, second_decision = await asyncio.gather(
            circuit_store.check_or_claim(
                provider_key=key,
                task_family=family,
                owner="worker-a",
                lease_seconds=30,
                now=2.0,
            ),
            circuit_store.check_or_claim(
                provider_key=key,
                task_family=family,
                owner="worker-b",
                lease_seconds=30,
                now=2.0,
            ),
        )
        assert first_decision.revision == initial.state.revision
        assert second_decision.revision == initial.state.revision

        first, second = await asyncio.gather(
            circuit_store.record_failure(
                provider_key=key,
                task_family=family,
                failure_kind="provider_error",
                settlement_id=f"provider-request-a-{round_index}",
                expected_revision=first_decision.revision,
                now=3.0,
            ),
            circuit_store.record_failure(
                provider_key=key,
                task_family=family,
                failure_kind="provider_error",
                settlement_id=f"provider-request-b-{round_index}",
                expected_revision=second_decision.revision,
                now=3.0,
            ),
        )

        assert first.applied is True
        assert second.applied is True
        state = await circuit_store.get_state(key, family)
        assert state is not None
        assert state.failure_count == 3
        assert state.state == "open"


@pytest.mark.asyncio
async def test_concurrent_duplicate_failure_settlement_counts_once(circuit_store):
    key = "id:provider-duplicate"
    family = "learning.jargon_enrichment"
    first, duplicate = await asyncio.gather(
        circuit_store.record_failure(
            provider_key=key,
            task_family=family,
            failure_kind="provider_timeout",
            settlement_id="same-provider-request",
            now=1.0,
        ),
        circuit_store.record_failure(
            provider_key=key,
            task_family=family,
            failure_kind="provider_timeout",
            settlement_id="same-provider-request",
            now=1.0,
        ),
    )

    assert sum(int(item.applied) for item in (first, duplicate)) == 1
    assert sum(int(item.idempotent) for item in (first, duplicate)) == 1
    state = await circuit_store.get_state(key, family)
    assert state is not None
    assert state.failure_count == 1
    assert state.state == "closed"


@pytest.mark.asyncio
async def test_unknown_provider_never_creates_shared_circuit(circuit_store):
    assert circuit_store.resolve_provider_key("", "") == ""
    decision = await circuit_store.check_or_claim(
        provider_key="",
        task_family="learning.jargon_enrichment",
        owner="worker",
        lease_seconds=30,
        now=1.0,
    )
    assert decision.allowed is False
    assert decision.reason == "identity_unknown"


@pytest.mark.asyncio
async def test_half_open_claim_cas_has_no_stale_overwrite_in_twenty_rounds(tmp_path):
    for round_id in range(20):
        db_path = tmp_path / f"race-{round_id}.db"
        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA user_version = 144")
            _run_migrations(db)
            db.commit()
        store = LearningProviderCircuitStore(db_path)
        key = f"id:provider-{round_id}"
        family = "learning.expression_enrichment"
        for offset in range(3):
            await store.record_failure(
                provider_key=key,
                task_family=family,
                failure_kind="provider_error",
                now=float(offset),
            )
        decisions = await asyncio.gather(
            *(
                store.check_or_claim(
                    provider_key=key,
                    task_family=family,
                    owner=f"worker-{worker}",
                    lease_seconds=30,
                    now=903.0,
                )
                for worker in range(8)
            )
        )
        assert sum(decision.allowed for decision in decisions) == 1
        state = await store.get_state(key, family)
        assert state is not None
        assert state.revision == 4


@pytest.mark.asyncio
async def test_abort_and_late_settlement_have_no_stale_overwrite_in_twenty_rounds(tmp_path):
    for round_id in range(20):
        db_path = tmp_path / f"abort-race-{round_id}.db"
        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA user_version = 144")
            _run_migrations(db)
            db.commit()
        store = LearningProviderCircuitStore(db_path)
        key = f"id:provider-abort-{round_id}"
        family = "learning.expression_enrichment"
        for offset in range(3):
            await store.record_failure(
                provider_key=key,
                task_family=family,
                failure_kind="provider_error",
                now=float(offset),
            )
        claim = await store.check_or_claim(
            provider_key=key,
            task_family=family,
            owner="worker-abort",
            lease_seconds=30,
            now=903.0,
        )
        abort, late = await asyncio.gather(
            store.abort_half_open(
                provider_key=key,
                task_family=family,
                owner="worker-abort",
                lease_token=claim.lease_token,
                expected_revision=claim.revision,
                now=904.0,
            ),
            store.record_success(
                provider_key=key,
                task_family=family,
                expected_revision=claim.revision,
                lease_token=claim.lease_token,
                now=904.0,
            ),
        )
        assert int(abort.applied) + int(late.applied) == 1
        assert int(abort.conflict) + int(late.conflict) == 1
        state = await store.get_state(key, family)
        assert state is not None
        assert state.revision == claim.revision + 1
