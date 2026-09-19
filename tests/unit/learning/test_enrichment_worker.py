from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.persistence.persistence_schema import _MIGRATIONS, _run_migrations
from astrmai.infrastructure.runtime.runtime_contracts import ProviderRequestStartContext
from astrmai.learning.mining.expression_results import (
    ExpressionEnrichmentResult,
    PatternSaveReport,
)
from astrmai.learning.persistence.candidate_ledger import (
    CandidateEvidence,
    CandidateLedger,
    LearningCandidate,
    LearningSourceBatch,
    SourceDisposition,
)
from astrmai.learning.runtime.enrichment_worker import LearningEnrichmentWorker
from astrmai.memory.services.expression_pattern_service import ExpressionPatternService


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "worker.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 146")
        for version, ddl in _MIGRATIONS:
            if version in (145, 146):
                db.execute(ddl)
        _run_migrations(db)
        db.commit()
    return CandidateLedger(path)


async def _discover_expression(ledger, *, created_at=100.0):
    batch = LearningSourceBatch.build(
        pipeline_type="expression",
        scope_id="qq:group:42",
        cursor_before=10,
        cursor_after=11,
        source_ids=("row:11",),
        created_at=created_at,
    )
    await ledger.begin_source_batch(batch)
    candidate = LearningCandidate.discovered(
        first_discovered_batch_id=batch.batch_id,
        scope_id=batch.scope_id,
        candidate_family="expression",
        candidate_subtype="catchphrase",
        speaker_id="",
        speaker_scope_id="",
        fingerprint="expression:fingerprint",
        fingerprint_version=1,
        extractor_version="expression-v1",
        source_message_start=11,
        source_message_end=11,
        evidence_quality="direct",
        source_payload={"expression": "脱敏表达", "source_message_ids": ["m-11"]},
        created_at=created_at,
    )
    evidence = CandidateEvidence.source(
        candidate_id=candidate.candidate_id,
        batch_id=batch.batch_id,
        source_row_id=11,
        source_message_id="m-11",
        identity_source="messagelog.id",
        scope_id=batch.scope_id,
        source_type="user_said",
        evidence_quality="direct",
        eligible=True,
        eligibility_reason="eligible",
        payload={"text": "脱敏表达"},
        created_at=created_at,
    )
    candidate = replace(candidate, evidence=(evidence,))
    await ledger.upsert_discovered(candidate)
    await ledger.settle_source_batch(
        batch.batch_id,
        expected_revision=0,
        dispositions=(
            SourceDisposition.for_row(11, candidate_ids=(candidate.candidate_id,)),
        ),
        now=created_at + 1.0,
    )
    return candidate


class _ExpressionEnricher:
    def __init__(
        self, *, result=None, provider_ok=True, failure_kind="provider_timeout"
    ):
        self.result = result or ExpressionEnrichmentResult(
            status="completed",
            items=[{
                "candidate_id": "placeholder",
                "expression": "脱敏表达",
                "model_examples": ["模型生成示例"],
            }],
            input_count=1,
            returned_count=1,
            reason="all_candidates_resolved",
        )
        self.provider_ok = provider_ok
        self.failure_kind = failure_kind
        self.last_provider_attempt = None
        self.provider_calls = 0
        self.received_candidates = []

    async def enrich(self, _scope_id, candidates, *, provider_call_kwargs=None):
        self.received_candidates.append([dict(item) for item in candidates])
        kwargs = dict(provider_call_kwargs or {})
        start = await kwargs["on_provider_request_start"](
            ProviderRequestStartContext(
                gateway_call_id="gateway-1",
                provider_id="provider/model-a",
                provider_family="native_chat",
                model_id="provider/model-a",
                identity_source="chat_provider_id",
                fallback_used=False,
                started_at=101.0,
            )
        )
        if not start.applied:
            self.last_provider_attempt = SimpleNamespace(
                ok=False,
                failure_stage=start.failure_stage,
                failure_kind=start.failure_kind,
                retryable=True,
                provider_id="provider/model-a",
                model_id="provider/model-a",
            )
            return ExpressionEnrichmentResult(
                status="provider_error",
                input_count=1,
                missing_candidate_ids=[candidates[0]["candidate_id"]],
                retryable=True,
                reason=start.failure_kind,
            )
        self.provider_calls += 1
        self.last_provider_attempt = SimpleNamespace(
            ok=self.provider_ok,
            failure_stage="" if self.provider_ok else "provider_request",
            failure_kind="" if self.provider_ok else self.failure_kind,
            retryable=not self.provider_ok,
            provider_id="provider/model-a",
            model_id="provider/model-a",
            logical_queue_wait_ms=1.0,
            runtime_queue_wait_ms=2.0,
            background_semaphore_wait_ms=3.0,
            global_semaphore_wait_ms=4.0,
            provider_latency_ms=5.0,
        )
        result = self.result
        if result.items:
            result.items[0]["candidate_id"] = candidates[0]["candidate_id"]
        return result


class _CancellingEnricher(_ExpressionEnricher):
    async def enrich(self, _scope_id, _candidates, *, provider_call_kwargs=None):
        kwargs = dict(provider_call_kwargs or {})
        await kwargs["on_provider_request_start"](
            ProviderRequestStartContext(
                gateway_call_id="gateway-cancel",
                provider_id="provider/model-a",
                provider_family="native_chat",
                model_id="provider/model-a",
                identity_source="chat_provider_id",
                fallback_used=False,
                started_at=101.0,
            )
        )
        raise asyncio.CancelledError()


async def _save_patterns(items, **_kwargs):
    return PatternSaveReport(
        attempted=len(items),
        saved=len(items),
        memory_ids=["memory:expression:1"] if items else [],
    )


async def _save_jargons(*_args, **_kwargs):
    raise AssertionError("jargon persistence should not run")


def _worker(ledger, enricher, *, now=100.0, max_attempts=3, save_patterns=_save_patterns):
    return LearningEnrichmentWorker(
        ledger=ledger,
        expression_enricher=enricher,
        jargon_enricher=SimpleNamespace(),
        save_patterns=save_patterns,
        save_jargons=_save_jargons,
        owner="worker-1",
        max_attempts=max_attempts,
        clock=lambda: now,
        jitter=lambda: 0.0,
    )


@pytest.mark.asyncio
async def test_worker_marks_provider_at_fence_and_settles_canonical_id(ledger):
    candidate = await _discover_expression(ledger)
    enricher = _ExpressionEnricher()

    result = await _worker(ledger, enricher).run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)
    attempts = await ledger.list_attempts(candidate.candidate_id)
    evidence = await ledger.list_evidence(candidate.candidate_id)

    assert result.claimed == 1
    assert result.enriched == 1
    assert enricher.provider_calls == 1
    assert stored.status == "enriched"
    assert attempts[-1].candidate_work_attempt == 1
    assert attempts[-1].provider_attempt == 1
    assert attempts[-1].provider_request_started is True
    assert len(evidence) == 2
    assert sum(item.is_generated for item in evidence) == 1
    generated = next(item for item in evidence if item.is_generated)
    assert generated.eligible is False
    assert generated.source_type == "generated"
    with sqlite3.connect(ledger.db_path) as db:
        metrics = db.execute(
            """
            SELECT queue_wait_ms, logical_queue_wait_ms, runtime_budget_wait_ms,
                   gateway_background_wait_ms, gateway_global_wait_ms,
                   provider_latency_ms, input_count, output_count
            FROM learning_candidate_attempt
            WHERE candidate_id = ?
            """,
            (candidate.candidate_id,),
        ).fetchone()
    assert metrics == (10.0, 1.0, 2.0, 3.0, 4.0, 5.0, 1, 1)


@pytest.mark.asyncio
async def test_worker_passes_attribution_from_candidate_to_canonical_metadata(ledger):
    candidate = await _discover_expression(ledger)
    original = await ledger.get_candidate(candidate.candidate_id)
    with sqlite3.connect(ledger.db_path) as db:
        payload = dict(original.source_payload)
        payload.update({
            "situation": "test",
            "source_row_ids": [11],
            "source_attributions": [{"source_row_id": 11, "source_type": "user_said"}],
            "source_types": ["user_said"],
            "evidence_qualities": ["high"],
            "attribution_scope_ids": ["qq:group:42"],
            "attribution_speaker_scope_ids": ["qq:group:42:user-1"],
            "personal_attribution_eligible": True,
        })
        db.execute(
            "UPDATE learning_candidate SET source_payload_json = ? WHERE candidate_id = ?",
            (json.dumps(payload, ensure_ascii=False), candidate.candidate_id),
        )
        db.commit()

    class _PassThroughEnricher(_ExpressionEnricher):
        async def enrich(self, scope_id, candidates, *, provider_call_kwargs=None):
            result = await super().enrich(
                scope_id, candidates, provider_call_kwargs=provider_call_kwargs
            )
            result.items[0].update(candidates[0])
            return result

    class _CanonicalStore:
        async def get_by_dedup_key(self, _key, include_inactive=True):
            return None

        async def resolve_dedup_key(self, key):
            return key

    class _CanonicalWriter:
        request = None

        async def write(self, request):
            self.request = request
            return "memory:expression:1"

    canonical_writer = _CanonicalWriter()
    canonical = ExpressionPatternService(_CanonicalStore(), canonical_writer)

    async def save_patterns(items, **_kwargs):
        memory_id = await canonical.write_pattern(
            "qq:group:42", items[0], source="learning_candidate_worker"
        )
        return PatternSaveReport(attempted=1, saved=1, memory_ids=[memory_id])

    result = await _worker(
        ledger, _PassThroughEnricher(), save_patterns=save_patterns
    ).run_due_once()

    assert result.enriched == 1
    assert canonical_writer.request.metadata["source_row_ids"] == [11]
    assert canonical_writer.request.metadata["attribution_speaker_scope_ids"] == [
        "qq:group:42:user-1"
    ]


@pytest.mark.asyncio
async def test_long_provider_renews_lease_and_prevents_second_ledger_recovery(ledger):
    candidate = await _discover_expression(ledger)
    clock = SimpleNamespace(value=100.0)
    started = asyncio.Event()
    renewed = asyncio.Event()
    release = asyncio.Event()
    writes = []

    class _SlowEnricher(_ExpressionEnricher):
        async def enrich(self, scope_id, candidates, *, provider_call_kwargs=None):
            started.set()
            await release.wait()
            return await super().enrich(
                scope_id, candidates, provider_call_kwargs=provider_call_kwargs
            )

    async def _counting_writer(items, **kwargs):
        writes.append((list(items), dict(kwargs)))
        return await _save_patterns(items, **kwargs)

    original_renew = ledger.renew_enrichment_lease

    async def _observed_renew(**kwargs):
        result = await original_renew(**kwargs)
        if result:
            renewed.set()
        return result

    ledger.renew_enrichment_lease = _observed_renew

    slow_enricher = _SlowEnricher()
    worker = LearningEnrichmentWorker(
        ledger=ledger,
        expression_enricher=slow_enricher,
        jargon_enricher=SimpleNamespace(),
        save_patterns=_counting_writer,
        save_jargons=_save_jargons,
        owner="worker-slow",
        lease_seconds=3.0,
        lease_heartbeat_interval=0.01,
        clock=lambda: clock.value,
        jitter=lambda: 0.0,
    )
    task = asyncio.create_task(worker.run_due_once())
    await started.wait()
    clock.value = 102.0
    await renewed.wait()

    competitor = CandidateLedger(ledger.db_path)
    assert await competitor.recover_expired_enrichment(now=103.5) == 0
    assert await competitor.list_due_enrichment(now=103.5, limit=10) == ()

    release.set()
    result = await task
    assert result.enriched == 1
    assert slow_enricher.provider_calls == 1
    assert len(writes) == 1
    assert (await competitor.get_candidate(candidate.candidate_id)).status == "enriched"


@pytest.mark.asyncio
async def test_lease_renewal_failure_cancels_provider_and_skips_persistence(ledger):
    candidate = await _discover_expression(ledger)
    provider_started = asyncio.Event()
    writes = []

    class _BlockedProvider(_ExpressionEnricher):
        async def enrich(self, _scope_id, candidates, *, provider_call_kwargs=None):
            kwargs = dict(provider_call_kwargs or {})
            started = await kwargs["on_provider_request_start"](
                ProviderRequestStartContext(
                    gateway_call_id="gateway-renewal-failure",
                    provider_id="provider/model-a",
                    provider_family="native_chat",
                    model_id="provider/model-a",
                    identity_source="chat_provider_id",
                    fallback_used=False,
                    started_at=101.0,
                )
            )
            assert started.applied is True
            self.provider_calls += 1
            provider_started.set()
            await asyncio.Event().wait()

    async def _unexpected_writer(items, **kwargs):
        writes.append((items, kwargs))
        return await _save_patterns(items, **kwargs)

    async def _lose_lease(**_kwargs):
        await provider_started.wait()
        return False

    ledger.renew_enrichment_lease = _lose_lease
    enricher = _BlockedProvider()
    worker = LearningEnrichmentWorker(
        ledger=ledger,
        expression_enricher=enricher,
        jargon_enricher=SimpleNamespace(),
        save_patterns=_unexpected_writer,
        save_jargons=_save_jargons,
        owner="worker-renewal-failure",
        lease_seconds=3.0,
        lease_heartbeat_interval=0.01,
        clock=lambda: 100.0,
        jitter=lambda: 0.0,
    )

    result = await worker.run_due_once()

    assert result.conflicts == 1
    assert enricher.provider_calls == 1
    assert writes == []
    assert (await ledger.get_candidate(candidate.candidate_id)).status == "enriching"


@pytest.mark.asyncio
async def test_lease_loss_after_provider_return_skips_canonical_persistence(ledger):
    candidate = await _discover_expression(ledger)
    provider_returned = False
    writes = []

    class _ReturnedProvider(_ExpressionEnricher):
        async def enrich(self, scope_id, candidates, *, provider_call_kwargs=None):
            nonlocal provider_returned
            result = await super().enrich(
                scope_id, candidates, provider_call_kwargs=provider_call_kwargs
            )
            provider_returned = True
            return result

    async def _renew_until_provider_returns(**_kwargs):
        return not provider_returned

    async def _unexpected_writer(items, **kwargs):
        writes.append((items, kwargs))
        return await _save_patterns(items, **kwargs)

    ledger.renew_enrichment_lease = _renew_until_provider_returns
    enricher = _ReturnedProvider()
    worker = LearningEnrichmentWorker(
        ledger=ledger,
        expression_enricher=enricher,
        jargon_enricher=SimpleNamespace(),
        save_patterns=_unexpected_writer,
        save_jargons=_save_jargons,
        owner="worker-post-provider-lease-loss",
        lease_seconds=60.0,
        lease_heartbeat_interval=60.0,
        clock=lambda: 100.0,
        jitter=lambda: 0.0,
    )

    result = await worker.run_due_once()

    assert result.conflicts == 1
    assert enricher.provider_calls == 1
    assert writes == []
    assert (await ledger.get_candidate(candidate.candidate_id)).status == "enriching"


@pytest.mark.asyncio
async def test_new_evidence_uses_new_canonical_persistence_identity(ledger):
    candidate = await _discover_expression(ledger)
    enricher = _ExpressionEnricher()
    persistence_calls = []

    async def _capturing_writer(items, **kwargs):
        persistence_calls.append(dict(kwargs))
        return await _save_patterns(items, **kwargs)

    assert (
        await _worker(ledger, enricher, save_patterns=_capturing_writer).run_due_once()
    ).enriched == 1

    batch = LearningSourceBatch.build(
        pipeline_type="expression",
        scope_id=candidate.scope_id,
        cursor_before=11,
        cursor_after=12,
        source_ids=("row:12",),
        created_at=200.0,
    )
    await ledger.begin_source_batch(batch)
    rediscovered = replace(
        candidate,
        first_discovered_batch_id=batch.batch_id,
        source_payload={**dict(candidate.source_payload), "contexts": ["new context"]},
        evidence=(
            CandidateEvidence.source(
                candidate_id=candidate.candidate_id,
                batch_id=batch.batch_id,
                source_row_id=12,
                source_message_id="m-12",
                identity_source="messagelog.id",
                scope_id=candidate.scope_id,
                source_type="user_said",
                evidence_quality="direct",
                eligible=True,
                eligibility_reason="eligible",
                payload={"text": "new context"},
                created_at=200.0,
            ),
        ),
    )
    await ledger.upsert_discovered(rediscovered)
    await ledger.settle_source_batch(
        batch.batch_id,
        expected_revision=0,
        dispositions=(SourceDisposition.for_row(12, candidate_ids=(candidate.candidate_id,)),),
        now=201.0,
    )

    assert (
        await _worker(
            ledger, enricher, now=202.0, save_patterns=_capturing_writer
        ).run_due_once()
    ).enriched == 1
    assert len(persistence_calls) == 2
    persistence_keys = [
        str(call.get("mining_batch_id") or "") for call in persistence_calls
    ]
    assert persistence_keys[0] != persistence_keys[1]
    assert all(
        call["candidate_id"] == candidate.candidate_id
        and call["candidate_revision"] > 0
        and call["candidate_persistence_id"] == call["mining_batch_id"]
        for call in persistence_calls
    )
    assert persistence_calls[0]["candidate_revision"] < persistence_calls[1]["candidate_revision"]
    assert enricher.received_candidates[-1][0]["contexts"] == ["new context"]


@pytest.mark.asyncio
async def test_same_work_attempt_replay_does_not_call_provider_twice(ledger):
    candidate = await _discover_expression(ledger)
    stored = await ledger.get_candidate(candidate.candidate_id)
    lease = await ledger.claim_enrichment(
        candidate_id=candidate.candidate_id,
        expected_revision=stored.revision,
        owner="worker-1",
        run_id="run-1",
        task_name="learning.expression_enrichment",
        lease_seconds=60.0,
        now=100.0,
    )
    enricher = _ExpressionEnricher()
    worker = _worker(ledger, enricher)

    assert await worker._process(lease) == "enriched"
    assert await worker._process(lease) == "conflicts"
    assert enricher.provider_calls == 1


@pytest.mark.asyncio
async def test_provider_timeout_retries_then_quarantines_without_losing_evidence(ledger):
    candidate = await _discover_expression(ledger)
    timeout = ExpressionEnrichmentResult(
        status="provider_error",
        input_count=1,
        retryable=True,
        reason="provider_timeout",
    )
    enricher = _ExpressionEnricher(result=timeout, provider_ok=False)

    first = await _worker(ledger, enricher, max_attempts=2).run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)
    assert first.retry_wait == 1
    assert stored.status == "retry_wait"
    assert stored.next_retry_at == 105.0
    assert len(await ledger.list_evidence(candidate.candidate_id)) == 1

    worker = _worker(ledger, enricher, now=105.0, max_attempts=2)
    second = await worker.run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)
    assert second.quarantined == 1
    assert stored.status == "quarantined"
    assert stored.failure_kind == "retry_exhausted"
    assert len(await ledger.list_evidence(candidate.candidate_id)) == 1


@pytest.mark.asyncio
async def test_provider_unavailable_waits_for_retry_without_quality_quarantine(ledger):
    candidate = await _discover_expression(ledger)
    unavailable = ExpressionEnrichmentResult(
        status="provider_error",
        input_count=1,
        retryable=True,
        reason="provider_unavailable",
    )

    result = await _worker(
        ledger,
        _ExpressionEnricher(
            result=unavailable,
            provider_ok=False,
            failure_kind="provider_unavailable",
        ),
    ).run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)

    assert result.retry_wait == 1
    assert stored.status == "retry_wait"
    assert stored.failure_kind == "provider_unavailable"
    assert stored.retryable is True
    with sqlite3.connect(ledger.db_path) as db:
        diagnostic = db.execute(
            "SELECT stage, status, failure_kind FROM learning_stage_diagnostic"
        ).fetchone()
    assert diagnostic == ("provider", "retry_wait", "provider_unavailable")


@pytest.mark.asyncio
async def test_empty_persistence_id_is_retryable_and_never_enriched(ledger):
    candidate = await _discover_expression(ledger)

    async def _empty_id(items, **_kwargs):
        return PatternSaveReport(
            attempted=len(items),
            saved=0,
            failed=len(items),
            failures=["empty id"],
        )

    result = await _worker(
        ledger,
        _ExpressionEnricher(),
        save_patterns=_empty_id,
    ).run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)

    assert result.retry_wait == 1
    assert stored.status == "retry_wait"
    assert stored.failure_stage == "persistence"
    assert stored.failure_kind == "persistence_partial"


@pytest.mark.asyncio
async def test_persistence_database_lock_is_retryable(ledger):
    candidate = await _discover_expression(ledger)

    async def _locked_writer(_items, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    result = await _worker(
        ledger,
        _ExpressionEnricher(),
        save_patterns=_locked_writer,
    ).run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)

    assert result.retry_wait == 1
    assert stored.status == "retry_wait"
    assert stored.failure_stage == "persistence"
    assert stored.failure_kind == "persist_locked"


@pytest.mark.asyncio
async def test_all_rejected_is_terminal_without_persistence_or_retry(ledger):
    candidate = await _discover_expression(ledger)
    rejected = ExpressionEnrichmentResult(
        status="all_rejected",
        input_count=1,
        returned_count=1,
        rejected_count=1,
        reason="model_rejected_all_candidates",
    )

    result = await _worker(
        ledger,
        _ExpressionEnricher(result=rejected),
    ).run_due_once()
    stored = await ledger.get_candidate(candidate.candidate_id)

    assert result.rejected == 1
    assert stored.status == "rejected"
    assert stored.retryable is False
    assert stored.next_retry_at == 0


@pytest.mark.asyncio
async def test_cancellation_settles_retry_wait_and_leaves_no_active_worker_task(ledger):
    candidate = await _discover_expression(ledger)
    worker = _worker(ledger, _CancellingEnricher())

    with pytest.raises(asyncio.CancelledError):
        await worker.run_due_once()

    stored = await ledger.get_candidate(candidate.candidate_id)
    attempts = await ledger.list_attempts(candidate.candidate_id)
    assert stored.status == "retry_wait"
    assert stored.failure_kind == "cancelled"
    assert stored.lease_token == ""
    assert attempts[-1].status == "retry_wait"
    assert not worker._active


@pytest.mark.asyncio
async def test_cancellation_keeps_original_signal_when_settlement_is_locked(
    ledger, monkeypatch
):
    candidate = await _discover_expression(ledger)
    worker = _worker(ledger, _CancellingEnricher())

    async def _locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_settle_with_diagnostic", _locked)
    with pytest.raises(asyncio.CancelledError):
        await worker.run_due_once()

    stored = await ledger.get_candidate(candidate.candidate_id)
    assert stored.status == "enriching"
    assert await ledger.recover_expired_enrichment(now=200.0) == 1
    assert (await ledger.get_candidate(candidate.candidate_id)).status == "retry_wait"
