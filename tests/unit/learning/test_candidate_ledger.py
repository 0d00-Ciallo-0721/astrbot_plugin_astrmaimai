from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.learning.persistence.candidate_ledger import (
    CandidateLedger,
    CandidateEvidence,
    EnrichmentSettlement,
    LearningCandidate,
    LearningSourceBatch,
    SourceDisposition,
)


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "candidate-ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 146")
        for version, ddl in __import__(
            "astrmai.infrastructure.persistence.persistence_schema",
            fromlist=["_MIGRATIONS"],
        )._MIGRATIONS:
            if version in (145, 146):
                db.execute(ddl)
        _run_migrations(db)
        db.commit()
    return CandidateLedger(path)


def _batch(*, cursor_before=10, cursor_after=12, rows=(11, 12), created_at=100.0):
    return LearningSourceBatch.build(
        pipeline_type="jargon",
        scope_id="qq:group:42",
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        source_ids=tuple(f"row:{row}" for row in rows),
        created_at=created_at,
    )


def _candidate(batch, *, fingerprint="sha256:phrase-1", evidence=()):
    candidate = LearningCandidate.discovered(
        first_discovered_batch_id=batch.batch_id,
        scope_id=batch.scope_id,
        candidate_family="jargon",
        candidate_subtype="phrase",
        speaker_id="user-7",
        speaker_scope_id="qq:group:42:user-7",
        fingerprint=fingerprint,
        fingerprint_version=1,
        extractor_version="jargon-extractor-v1",
        source_message_start=batch.cursor_before,
        source_message_end=batch.cursor_after,
        evidence_quality="direct",
        source_payload={"surface": "脱敏短语"},
        created_at=101.0,
    )
    if evidence:
        return replace(candidate, evidence=tuple(evidence))
    return candidate


def _source_evidence(candidate, batch, *, row=11, payload=None):
    return CandidateEvidence.source(
        candidate_id=candidate.candidate_id,
        batch_id=batch.batch_id,
        source_row_id=row,
        source_message_id=f"platform-message-{row}",
        identity_source="messagelog.id",
        sender_id="user-7",
        scope_id=batch.scope_id,
        speaker_scope_id="qq:group:42:user-7",
        source_type="user_said",
        evidence_quality="direct",
        eligible=True,
        eligibility_reason="learning_evidence_eligible",
        payload=payload or {"role": "source"},
        created_at=102.0,
    )


async def _discover(
    ledger, *, batch=None, fingerprint="sha256:phrase-1", settle_batch=True
):
    batch = batch or _batch()
    await ledger.begin_source_batch(batch)
    candidate = _candidate(batch, fingerprint=fingerprint)
    evidence = _source_evidence(candidate, batch)
    candidate = replace(candidate, evidence=(evidence,))
    written = await ledger.upsert_discovered(candidate)
    if settle_batch:
        dispositions = tuple(
            SourceDisposition.for_row(
                row,
                candidate_ids=(candidate.candidate_id,) if row == 11 else (),
                disposition="candidate_ids" if row == 11 else "no_candidate",
                reason_code="" if row == 11 else "no_candidate",
            )
            for row in (int(item.split(":", 1)[1]) for item in batch.source_ids)
        )
        await ledger.settle_source_batch(
            batch.batch_id,
            expected_revision=0,
            dispositions=dispositions,
            now=103.0,
        )
    return batch, candidate, evidence, written


def test_stable_ids_use_canonical_identity_not_position():
    first_batch = _batch(rows=(12, 11))
    replay_batch = _batch(rows=(11, 12), created_at=999.0)
    first_candidate = _candidate(first_batch)
    replay_candidate = _candidate(replay_batch)
    first_evidence = _source_evidence(first_candidate, first_batch)
    replay_evidence = _source_evidence(replay_candidate, replay_batch)

    assert first_batch.batch_id == replay_batch.batch_id
    assert first_batch.source_ids_hash == replay_batch.source_ids_hash
    assert first_candidate.candidate_id == replay_candidate.candidate_id
    assert first_evidence.evidence_id == replay_evidence.evidence_id


@pytest.mark.asyncio
async def test_begin_batch_and_candidate_evidence_replay_are_idempotent(ledger):
    batch, candidate, evidence, first = await _discover(ledger)
    batch_replay = await ledger.begin_source_batch(batch)
    replay = await ledger.upsert_discovered(candidate)

    assert batch_replay.idempotent is True
    assert first.inserted is True
    assert first.evidence_inserted == 1
    assert first.status == "enrichment_pending"
    assert replay.inserted is False
    assert replay.deduplicated is True
    assert replay.evidence_inserted == 0
    assert replay.candidate_id == first.candidate_id
    assert await ledger.list_evidence(candidate.candidate_id) == (evidence,)


@pytest.mark.asyncio
async def test_evidence_replay_with_different_payload_bytes_conflicts(ledger):
    _batch_value, candidate, evidence, _first = await _discover(ledger)

    replay = await ledger.upsert_discovered(
        replace(candidate, evidence=(replace(evidence, created_at=999.0),))
    )

    assert replay.conflict is True
    assert replay.deduplicated is True
    assert replay.evidence_inserted == 0


@pytest.mark.asyncio
async def test_new_batch_can_extend_candidate_evidence_without_resetting_status(ledger):
    _first_batch, candidate, _evidence, first = await _discover(ledger)
    current = await ledger.get_candidate(candidate.candidate_id)
    lease = await ledger.claim_enrichment(
        candidate_id=candidate.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-first-enrichment",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=110.0,
    )
    settled = await ledger.settle_enrichment(
        candidate_id=lease.candidate_id,
        attempt_id=lease.attempt_id,
        owner=lease.owner,
        lease_token=lease.lease_token,
        expected_revision=lease.expected_revision,
        result=EnrichmentSettlement.build(
            status="enriched",
            enrichment_payload={"meaning": "first"},
            canonical_ids=("memory:first",),
            settlement_started_at=111.0,
            finished_at=112.0,
        ),
    )
    assert settled.applied is True
    second_batch = _batch(
        cursor_before=12,
        cursor_after=13,
        rows=(13,),
        created_at=200.0,
    )
    await ledger.begin_source_batch(second_batch)
    rediscovered = replace(
        _candidate(second_batch),
        source_payload={"surface": "脱敏短语", "contexts": ["new context"]},
    )
    second_evidence = _source_evidence(
        rediscovered,
        second_batch,
        row=13,
        payload={"role": "source", "context": "new context"},
    )

    extended = await ledger.upsert_discovered(
        replace(rediscovered, evidence=(second_evidence,))
    )

    assert extended.conflict is False
    assert extended.deduplicated is True
    assert extended.evidence_inserted == 1
    assert extended.revision == settled.candidate.revision + 1
    assert extended.status == "enrichment_pending"
    stored = await ledger.get_candidate(candidate.candidate_id)
    assert stored.source_payload["contexts"] == ["new context"]
    assert len(await ledger.list_evidence(candidate.candidate_id)) == 2


@pytest.mark.asyncio
async def test_new_evidence_atomically_supersedes_running_attempt(ledger):
    _first_batch, candidate, _evidence, _written = await _discover(ledger)
    current = await ledger.get_candidate(candidate.candidate_id)
    lease = await ledger.claim_enrichment(
        candidate_id=candidate.candidate_id,
        expected_revision=current.revision,
        owner="worker-old",
        run_id="run-old",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=110.0,
    )
    second_batch = _batch(
        cursor_before=12,
        cursor_after=13,
        rows=(13,),
        created_at=120.0,
    )
    await ledger.begin_source_batch(second_batch)
    rediscovered = replace(
        _candidate(second_batch),
        source_payload={"surface": "脱敏短语", "contexts": ["new context"]},
    )
    second_evidence = _source_evidence(
        rediscovered,
        second_batch,
        row=13,
        payload={"role": "source", "context": "new context"},
    )

    extended = await ledger.upsert_discovered(
        replace(rediscovered, evidence=(second_evidence,))
    )
    stored = await ledger.get_candidate(candidate.candidate_id)
    attempts = await ledger.list_attempts(candidate.candidate_id)

    assert extended.conflict is False
    assert extended.status == "retry_wait"
    assert extended.revision == lease.expected_revision + 1
    assert stored.status == "retry_wait"
    assert stored.lease_owner == ""
    assert stored.lease_token == ""
    assert stored.lease_until == 0.0
    assert attempts[-1].revision == lease.expected_revision
    assert attempts[-1].status == "retry_wait"
    assert attempts[-1].failure_kind == "candidate_revision_superseded"
    assert await ledger.recover_expired_enrichment(now=200.0) == 0
    assert (await ledger.list_attempts(candidate.candidate_id))[-1].status == "retry_wait"


@pytest.mark.asyncio
async def test_candidate_is_not_due_until_all_evidence_batches_are_completed(ledger):
    batch = _batch()
    await ledger.begin_source_batch(batch)
    candidate = _candidate(batch)
    evidence = _source_evidence(candidate, batch)
    written = await ledger.upsert_discovered(replace(candidate, evidence=(evidence,)))

    assert written.status == "enrichment_pending"
    assert await ledger.list_due_enrichment(now=110.0, limit=10) == ()
    assert await ledger.claim_enrichment(
        candidate_id=candidate.candidate_id,
        expected_revision=written.revision,
        owner="worker-a",
        run_id="run-before-batch-complete",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=110.0,
    ) is None

    await ledger.settle_source_batch(
        batch.batch_id,
        expected_revision=0,
        dispositions=(
            SourceDisposition.for_row(11, candidate_ids=(candidate.candidate_id,)),
            SourceDisposition.for_row(
                12, disposition="no_candidate", reason_code="no_candidate"
            ),
        ),
        now=109.0,
    )
    due = await ledger.list_due_enrichment(now=110.0, limit=10)
    assert [item.candidate_id for item in due] == [candidate.candidate_id]


@pytest.mark.asyncio
async def test_same_batch_id_with_different_content_conflicts(ledger):
    batch = _batch()
    assert (await ledger.begin_source_batch(batch)).inserted is True
    conflicting = replace(batch, scope_id="qq:group:other")
    result = await ledger.begin_source_batch(conflicting)

    assert result.conflict is True
    assert result.inserted is False


@pytest.mark.asyncio
async def test_incomplete_source_evidence_never_becomes_enrichment_pending(ledger):
    batch = _batch()
    await ledger.begin_source_batch(batch)
    candidate = _candidate(batch)
    generated = CandidateEvidence.generated(
        candidate_id=candidate.candidate_id,
        batch_id=batch.batch_id,
        payload={"example": "generated"},
        created_at=102.0,
    )
    result = await ledger.upsert_discovered(replace(candidate, evidence=(generated,)))

    assert result.status == "blocked"
    assert result.diagnostics["failure_kind"] == "source_evidence_incomplete"


@pytest.mark.asyncio
async def test_source_batch_completeness_and_contiguous_cursor(ledger):
    first, candidate, _evidence, _written = await _discover(
        ledger, settle_batch=False
    )
    disposition_first = (
        SourceDisposition.for_row(11, candidate_ids=(candidate.candidate_id,)),
        SourceDisposition.for_row(12, disposition="no_candidate", reason_code="none"),
    )
    settled_first = await ledger.settle_source_batch(
        first.batch_id,
        expected_revision=0,
        dispositions=disposition_first,
        now=110.0,
    )
    gap = _batch(cursor_before=14, cursor_after=15, rows=(15,), created_at=111.0)
    await ledger.begin_source_batch(gap)
    await ledger.settle_source_batch(
        gap.batch_id,
        expected_revision=0,
        dispositions=(
            SourceDisposition.for_row(15, disposition="skipped", reason_code="policy"),
        ),
        now=112.0,
    )

    assert settled_first.completed is True
    assert await ledger.highest_contiguous_completed_cursor(
        pipeline_type="jargon", scope_id=first.scope_id, cursor_before=10
    ) == 12
    invalid = await ledger.settle_source_batch(
        first.batch_id,
        expected_revision=1,
        dispositions=(
            SourceDisposition.for_row(11, candidate_ids=(candidate.candidate_id,)),
        ),
        now=113.0,
    )
    assert invalid.conflict is True


@pytest.mark.asyncio
async def test_due_query_does_not_increment_attempt_and_claim_is_durable(ledger):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    before = await ledger.get_candidate(written.candidate_id)
    due = await ledger.list_due_enrichment(now=110.0, limit=10)
    after = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=after.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=111.0,
    )

    assert before.attempt == after.attempt == 0
    assert [item.candidate_id for item in due] == [written.candidate_id]
    assert claim is not None
    assert claim.attempt_id
    assert claim.candidate_work_attempt == 1
    assert claim.expected_revision == after.revision + 1
    attempts = await ledger.list_attempts(written.candidate_id)
    assert len(attempts) == 1
    assert attempts[0].provider_attempt == 0


@pytest.mark.asyncio
async def test_provider_start_is_single_cas_and_preserves_attempt_denominators(ledger):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=111.0,
    )
    started = await ledger.mark_provider_started(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        provider_id="provider-a",
        provider_family="fake",
        model_id="model-a",
        identity_source="fixture",
        gateway_call_id="gateway-1",
        provider_request_id="request-1",
        started_at=112.0,
    )
    replay = await ledger.mark_provider_started(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        provider_id="provider-a",
        provider_family="fake",
        model_id="model-a",
        identity_source="fixture",
        gateway_call_id="gateway-1",
        provider_request_id="request-1",
        started_at=112.0,
    )

    assert started.applied is True
    assert started.provider_attempt == 1
    assert replay.applied is False
    assert replay.idempotent is True
    assert replay.provider_attempt == 1


@pytest.mark.asyncio
async def test_provider_start_idempotent_replay_requires_current_revision_and_lease(
    ledger,
):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=5.0,
        now=111.0,
    )
    kwargs = {
        "candidate_id": claim.candidate_id,
        "attempt_id": claim.attempt_id,
        "owner": claim.owner,
        "lease_token": claim.lease_token,
        "expected_revision": claim.expected_revision,
        "provider_id": "provider-a",
        "provider_family": "fake",
        "model_id": "model-a",
        "identity_source": "fixture",
        "gateway_call_id": "gateway-1",
        "provider_request_id": "request-1",
        "started_at": 112.0,
        "now": 112.0,
    }

    started = await ledger.mark_provider_started(**kwargs)
    stale_revision = await ledger.mark_provider_started(
        **{**kwargs, "expected_revision": claim.expected_revision - 1}
    )
    expired = await ledger.mark_provider_started(**{**kwargs, "now": 117.0})

    assert started.applied is True
    assert stale_revision.conflict is True
    assert stale_revision.idempotent is False
    assert expired.conflict is True
    assert expired.idempotent is False


@pytest.mark.asyncio
async def test_expired_lease_recovery_blocks_stale_owner_settlement(ledger):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    first = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=5.0,
        now=111.0,
    )
    assert await ledger.recover_expired_enrichment(now=117.0) == 1
    recovered = await ledger.get_candidate(written.candidate_id)
    second = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=recovered.revision,
        owner="worker-b",
        run_id="run-b",
        task_name="learning.jargon_enrichment",
        lease_seconds=20.0,
        now=118.0,
    )
    stale_result = EnrichmentSettlement.build(
        status="enriched",
        enrichment_payload={"definition": "stale"},
        settlement_started_at=112.0,
        finished_at=119.0,
    )
    stale = await ledger.settle_enrichment(
        candidate_id=first.candidate_id,
        attempt_id=first.attempt_id,
        owner=first.owner,
        lease_token=first.lease_token,
        expected_revision=first.expected_revision,
        result=stale_result,
    )

    assert second is not None
    assert second.candidate_work_attempt == 2
    assert stale.applied is False
    assert stale.conflict is True


@pytest.mark.asyncio
async def test_settlement_byte_equivalent_replay_and_conflicting_replay(ledger):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=111.0,
    )
    result = EnrichmentSettlement.build(
        status="enriched",
        enrichment_payload={"definition": "stable"},
        provider_id="provider-a",
        model_id="model-a",
        canonical_ids=("memory-1",),
        settlement_started_at=112.0,
        finished_at=113.0,
    )
    first = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=result,
    )
    replay = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=result,
    )
    conflict = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=replace(result, enrichment_payload={"definition": "different"}),
    )

    assert first.applied is True
    assert replay.idempotent is True
    assert conflict.conflict is True
    assert (await ledger.get_candidate(claim.candidate_id)).enrichment_payload == {
        "definition": "stable"
    }


@pytest.mark.asyncio
async def test_settlement_idempotent_replay_requires_original_cas_identity(ledger):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=111.0,
    )
    result = EnrichmentSettlement.build(
        status="enriched",
        enrichment_payload={"definition": "stable"},
        canonical_ids=("memory-1",),
        settlement_started_at=112.0,
        finished_at=113.0,
    )
    first = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=result,
    )
    wrong_token = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token="stale-token",
        expected_revision=claim.expected_revision,
        result=result,
    )
    wrong_owner = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner="stale-worker",
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=result,
    )
    stale_revision = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision - 1,
        result=result,
    )

    assert first.applied is True
    assert wrong_token.conflict is True
    assert wrong_token.idempotent is False
    assert wrong_owner.conflict is True
    assert wrong_owner.idempotent is False
    assert stale_revision.conflict is True
    assert stale_revision.idempotent is False


@pytest.mark.asyncio
async def test_enriched_settlement_requires_nonempty_canonical_ids_at_ledger_boundary(
    ledger,
):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-empty-canonical",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=111.0,
    )

    settled = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=EnrichmentSettlement.build(
            status="enriched",
            canonical_ids=(),
            settlement_started_at=112.0,
            finished_at=113.0,
        ),
    )

    assert settled.applied is False
    assert settled.conflict is True
    assert settled.failure_kind == "empty_persistence_id"
    assert (await ledger.get_candidate(claim.candidate_id)).status == "enriching"


@pytest.mark.asyncio
async def test_settlement_rejects_lease_that_expired_before_finished_at(ledger):
    _batch_value, _candidate_value, _evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-expired-at-finish",
        task_name="learning.jargon_enrichment",
        lease_seconds=5.0,
        now=111.0,
    )

    settled = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=EnrichmentSettlement.build(
            status="enriched",
            canonical_ids=("memory-1",),
            settlement_started_at=112.0,
            finished_at=117.0,
        ),
    )

    assert settled.applied is False
    assert settled.conflict is True
    assert settled.failure_kind == "cas_conflict"
    assert (await ledger.get_candidate(claim.candidate_id)).status == "enriching"


@pytest.mark.asyncio
async def test_retry_wait_due_and_generated_evidence_remain_separate(ledger):
    batch, _candidate_value, source_evidence, written = await _discover(ledger)
    current = await ledger.get_candidate(written.candidate_id)
    claim = await ledger.claim_enrichment(
        candidate_id=written.candidate_id,
        expected_revision=current.revision,
        owner="worker-a",
        run_id="run-a",
        task_name="learning.jargon_enrichment",
        lease_seconds=30.0,
        now=111.0,
    )
    generated = CandidateEvidence.generated(
        candidate_id=claim.candidate_id,
        batch_id=batch.batch_id,
        payload={"example": "model output"},
        created_at=112.0,
    )
    result = EnrichmentSettlement.build(
        status="retry_wait",
        retryable=True,
        retry_at=130.0,
        failure_stage="provider_request",
        failure_kind="provider_timeout",
        generated_evidence=(generated,),
        settlement_started_at=112.0,
        finished_at=113.0,
    )
    settled = await ledger.settle_enrichment(
        candidate_id=claim.candidate_id,
        attempt_id=claim.attempt_id,
        owner=claim.owner,
        lease_token=claim.lease_token,
        expected_revision=claim.expected_revision,
        result=result,
    )

    assert settled.applied is True
    assert await ledger.list_due_enrichment(now=129.0, limit=10) == ()
    assert len(await ledger.list_due_enrichment(now=130.0, limit=10)) == 1
    evidence = await ledger.list_evidence(claim.candidate_id)
    assert source_evidence in evidence
    assert generated in evidence
    assert sum(item.eligible and not item.is_generated for item in evidence) == 1


@pytest.mark.asyncio
async def test_twenty_round_two_connection_claim_race_has_single_winner(tmp_path):
    for round_index in range(20):
        path = tmp_path / f"race-{round_index}.db"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version = 146")
            for version, ddl in __import__(
                "astrmai.infrastructure.persistence.persistence_schema",
                fromlist=["_MIGRATIONS"],
            )._MIGRATIONS:
                if version in (145, 146):
                    db.execute(ddl)
            _run_migrations(db)
            db.commit()
        setup = CandidateLedger(path)
        _batch_value, _candidate_value, _evidence, written = await _discover(setup)
        current = await setup.get_candidate(written.candidate_id)
        first = CandidateLedger(path)
        second = CandidateLedger(path)
        claims = await asyncio.gather(
            first.claim_enrichment(
                candidate_id=written.candidate_id,
                expected_revision=current.revision,
                owner="worker-a",
                run_id="run-a",
                task_name="learning.jargon_enrichment",
                lease_seconds=30.0,
                now=111.0,
            ),
            second.claim_enrichment(
                candidate_id=written.candidate_id,
                expected_revision=current.revision,
                owner="worker-b",
                run_id="run-b",
                task_name="learning.jargon_enrichment",
                lease_seconds=30.0,
                now=111.0,
            ),
        )
        assert sum(claim is not None for claim in claims) == 1
        assert len(await setup.list_attempts(written.candidate_id)) == 1
