from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.persistence.architecture_migration_audit import (
    LATEST_ARCHITECTURE_SCHEMA_VERSION,
    inspect_architecture_migration,
)
from astrmai.infrastructure.persistence.persistence_schema import _MIGRATIONS, _run_migrations
from astrmai.learning.persistence.review_repository import LearningReviewRepository
from astrmai.learning.review.admission import (
    AdmissionRepository,
    AdmissionService,
    PublishProofVerification,
    VectorPublishProof,
)
from astrmai.learning.review.contracts import (
    ReviewDecision,
    is_reviewer_model_identity,
    reviewer_model_identity,
)
from astrmai.learning.review.orchestrator import ReviewOrchestrator, ReviewWorkRequest
from astrmai.learning.review.expression_auto_check_task import ExpressionAutoCheckTask
from astrmai.learning.review.jargon_auto_check_task import JargonAutoCheckTask
from astrmai.learning.review.review_service import ExpressionReviewService


def _database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        for version, ddl in _MIGRATIONS:
            if 145 <= version < LATEST_ARCHITECTURE_SCHEMA_VERSION:
                db.execute(ddl)
        db.execute(f"PRAGMA user_version = {LATEST_ARCHITECTURE_SCHEMA_VERSION - 1}")
        _run_migrations(db)
        db.commit()


def _candidate(path: Path, candidate_id: str = "candidate-1", revision: int = 7) -> None:
    with sqlite3.connect(path) as db:
        now = 10.0
        batch_id = f"batch-{candidate_id}"
        db.execute(
            """INSERT INTO learning_source_batch(
               batch_id, pipeline_type, scope_id, cursor_before, cursor_after,
               source_ids_json, source_ids_hash, source_count, eligible_count,
               skipped_count, no_candidate_count, candidate_count, status,
               created_at, updated_at
            ) VALUES (?, 'expression', 'group-1', 0, 1, '[1]', ?, 1, 1, 0, 0, 1, 'completed', ?, ?)""",
            (batch_id, f"hash-{candidate_id}", now, now),
        )
        db.execute(
            """INSERT INTO learning_candidate(
               candidate_id, first_discovered_batch_id, scope_id, candidate_type,
               speaker_id, speaker_scope_id, fingerprint, extractor_version,
               quality_profile_version, status, revision, evidence_quality,
               created_at, updated_at
            ) VALUES (?, ?, 'group-1', 'expression', 'speaker-1', 'group-1:speaker-1',
                      ?, 'extractor-v1', 'quality-v1', 'review_pending', ?, 'high', ?, ?)""",
            (candidate_id, batch_id, f"fp-{candidate_id}", revision, now, now),
        )
        db.execute(
            """INSERT INTO learning_candidate_evidence(
               candidate_id, evidence_id, batch_id, source_row_id, sender_id,
               scope_id, speaker_scope_id, source_type, evidence_quality,
               eligible, eligibility_reason, created_at
            ) VALUES (?, ?, ?, ?, 'speaker-1', 'group-1', 'group-1:speaker-1',
                      'direct', 'high', 1, 'eligible', ?)""",
            (candidate_id, f"evidence-{candidate_id}", batch_id, revision + 100, now),
        )
        db.execute(
            """INSERT INTO learning_candidate_quality(
               quality_id, candidate_id, candidate_revision, profile_version,
               profile_hash, window_start, window_end, eligible_message_count,
               unknown_message_count, support_count, speaker_support,
               speaker_message_count, group_support, group_message_count,
               other_support, other_total, distinct_turns, distinct_turn_count,
               distinct_day_count, context_diversity, g2, log2_effect,
               signed_log2_lift, p_value, fdr_q, pmi, left_entropy_bits,
               right_entropy_bits, burst_ratio, feature_complete,
               missing_reasons_json, confidence_tier, reasons_json, created_at
            ) VALUES (?, ?, ?, 'quality-v1', 'profile-hash', 1, 2, 3, 0, 3,
                      3, 3, 3, 3, 0, 0, 3, 3, 1, 2, 7, 2, 2, 0.01,
                      0.02, 2, 2, 2, 3, 1, '[]', 'high', '[]', ?)""",
            (f"quality-{candidate_id}", candidate_id, revision, now),
        )
        db.commit()


def _decision(
    *, reviewer: str, attempt: str, decision_id: str, revision: int = 7,
    row: int = 107, vote: str = "approved", pair_order: str = "ab",
    model_identity: str = "", reviewer_kind: str = "model",
    rubric_version: str = "rubric-v1",
) -> ReviewDecision:
    effective_model_identity = ""
    if reviewer_kind == "model":
        raw_model_id = model_identity or f"fake:{reviewer}"
        effective_model_identity = reviewer_model_identity(
            provider_source_id="fixture-provider",
            model_id=raw_model_id,
            reviewer_profile_version="fixture-reviewer-profile-v1",
        )
    return ReviewDecision(
        decision_id=decision_id, candidate_id="candidate-1", candidate_revision=revision,
        decision=vote, reason="evidence_supported" if vote == "approved" else "evidence_conflict",
        reviewer_id=reviewer, reviewer_kind=reviewer_kind, reviewer_attempt_id=attempt,
        rubric_version=rubric_version, prompt_version="prompt-v1",
        model_identity=effective_model_identity,
        source_evidence_ids=(f"row:{row}",), source_example_ids=(f"row:{row}",),
        model_example_ids=(f"model:{reviewer}",), confidence=0.9,
        expected_revision=revision, pair_order=pair_order,
        diagnostics={"authorization": "secret", "request_id": decision_id}, created_at=100.0,
    )


async def _settle_pair(
    repository: LearningReviewRepository, *, reviewer: str, vote: str = "approved",
    model_identity: str = "", prefix: str = "pair", start: float = 100.0,
    rows: tuple[int, int] = (107, 107), reviewer_kind: str = "model",
    reviewer_kinds: tuple[str, str] | None = None,
    rubric_version: str = "rubric-v1",
) -> tuple[str, str]:
    decision_ids: list[str] = []
    for offset, pair_order in enumerate(("ab", "ba")):
        effective_reviewer_kind = (
            reviewer_kinds[offset] if reviewer_kinds is not None else reviewer_kind
        )
        attempt = f"{prefix}-{pair_order}"
        owner = f"{reviewer}-{pair_order}"
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner=owner,
            reviewer_id=reviewer, reviewer_kind=effective_reviewer_kind,
            reviewer_attempt_id=attempt,
            now=start + offset * 10.0, lease_seconds=60.0,
        )
        assert claim.attempt is not None
        decision_id = f"{prefix}-decision-{pair_order}"
        settled = await repository.settle(
            attempt_id=claim.attempt.attempt_id, owner=owner,
            lease_token=claim.attempt.lease_token, expected_attempt_revision=1,
            finished_at=start + offset * 10.0 + 1.0,
            decision=_decision(
                reviewer=reviewer, attempt=attempt, decision_id=decision_id,
                vote=vote, pair_order=pair_order,
                model_identity=model_identity or (
                    f"fake:{reviewer}" if effective_reviewer_kind == "model" else ""
                ),
                row=rows[offset], reviewer_kind=effective_reviewer_kind,
                rubric_version=rubric_version,
            ),
        )
        assert settled.applied
        decision_ids.append(decision_id)
    return tuple(decision_ids)


class _AuthoritativePublishVerifier:
    def __init__(self, accepted_asset_id: str = "asset-1") -> None:
        self.accepted_asset_id = accepted_asset_id

    async def verify(self, proof, *, submitting_owner_id: str):
        verified = (
            proof.asset_id == self.accepted_asset_id
            and proof.owner_id == submitting_owner_id
            and proof.membership_id == "membership-1"
        )
        return PublishProofVerification(
            verified, "publish_asset_not_verified" if not verified else "",
            "e" * 64 if verified else "",
        )


def _publish_proof(admission, **overrides) -> VectorPublishProof:
    values = {
        "candidate_id": "candidate-1",
        "candidate_revision": 7,
        "admission_revision": admission.admission_revision,
        "review_decision_ids": admission.review_decision_ids,
        "canonical_revision": 7,
        "owner_id": "fixture-index-owner",
        "asset_id": "asset-1",
        "asset_revision": 7,
        "membership_id": "membership-1",
        "index_generation": "generation-1",
        "provider_id": "fake-provider",
        "model_id": "fake-model",
        "vector_dimension": 8,
        "vector_count": 1,
        "mapping_digest": "a" * 64,
        "index_hash": "b" * 64,
        "provenance_digest": admission.provenance_digest,
    }
    values.update(overrides)
    return VectorPublishProof.signed(**values)


def test_review_decision_contract_is_strict_and_redacts_diagnostics():
    identity = reviewer_model_identity(
        provider_source_id="provider-a",
        model_id="model-a",
        reviewer_profile_version="profile-v1",
    )
    assert is_reviewer_model_identity(identity)
    assert all(value not in identity for value in ("provider-a", "model-a", "profile-v1"))
    assert len({
        identity,
        reviewer_model_identity(
            provider_source_id="provider-b", model_id="model-a",
            reviewer_profile_version="profile-v1",
        ),
        reviewer_model_identity(
            provider_source_id="provider-a", model_id="model-b",
            reviewer_profile_version="profile-v1",
        ),
        reviewer_model_identity(
            provider_source_id="provider-a", model_id="model-a",
            reviewer_profile_version="profile-v2",
        ),
    }) == 4
    with pytest.raises(ValueError, match="provider_source_id_required"):
        reviewer_model_identity(
            provider_source_id="", model_id="model-a",
            reviewer_profile_version="profile-v1",
        )
    decision = _decision(reviewer="r1", attempt="a1", decision_id="d1")
    assert decision.diagnostics["authorization"] == "[redacted]"
    with pytest.raises(ValueError, match="candidate_revision_invalid"):
        _decision(reviewer="r1", attempt="a1", decision_id="d1", revision=True)
    with pytest.raises(ValueError, match="source_evidence_identity_invalid"):
        ReviewDecision(
            decision_id="d", candidate_id="c", candidate_revision=1,
            decision="approved", reason="evidence_supported", reviewer_id="r",
            reviewer_kind="model", reviewer_attempt_id="a", rubric_version="r1",
            prompt_version="p1", model_identity="fake", source_evidence_ids=("synthetic:x",),
            source_example_ids=(), expected_revision=1, pair_order="ab", created_at=1.0,
        )
    for revision in (True, 1.0, "1"):
        with pytest.raises(ValueError, match="candidate_revision_invalid"):
            _decision(reviewer="r1", attempt="a1", decision_id="d1", revision=revision)
    for confidence in (-0.1, 1.1, True):
        with pytest.raises(ValueError, match="confidence_invalid"):
            ReviewDecision(
                decision_id="d", candidate_id="c", candidate_revision=1,
                decision="approved", reason="evidence_supported", reviewer_id="r",
                reviewer_kind="model", reviewer_attempt_id="a", rubric_version="r1",
                prompt_version="p1", model_identity="fake", source_evidence_ids=("row:1",),
                source_example_ids=("row:1",), confidence=confidence,
                expected_revision=1, pair_order="ab", created_at=1.0,
            )
    with pytest.raises(ValueError, match="decision_id_required"):
        ReviewDecision(
            decision_id=1, candidate_id="c", candidate_revision=1,
            decision="approved", reason="evidence_supported", reviewer_id="r",
            reviewer_kind="model", reviewer_attempt_id="a", rubric_version="r1",
            prompt_version="p1", model_identity="fake", source_evidence_ids=("row:1",),
            source_example_ids=("row:1",), expected_revision=1,
            pair_order="ab", created_at=1.0,
        )


def test_review_claim_settlement_replay_and_quorum(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "review.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        assert await repository.schema_ready()

        claim_a = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker-a",
            reviewer_id="reviewer-a", reviewer_kind="model", reviewer_attempt_id="attempt-a",
            now=100.0, lease_seconds=60.0,
        )
        assert claim_a.applied and claim_a.attempt is not None
        first = _decision(reviewer="reviewer-a", attempt="attempt-a", decision_id="decision-a")
        settled_a = await repository.settle(
            attempt_id=claim_a.attempt.attempt_id, owner="worker-a",
            lease_token=claim_a.attempt.lease_token, expected_attempt_revision=1,
            finished_at=110.0, decision=first,
        )
        assert settled_a.applied
        replay = await repository.settle(
            attempt_id=claim_a.attempt.attempt_id, owner="worker-a",
            lease_token=claim_a.attempt.lease_token, expected_attempt_revision=1,
            finished_at=110.0, decision=first,
        )
        assert replay.idempotent and not replay.conflict
        wrong_owner = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="wrong-worker",
            reviewer_id="reviewer-a", reviewer_kind="model", reviewer_attempt_id="attempt-a",
            now=111.0, lease_seconds=60.0,
        )
        assert wrong_owner.conflict and not wrong_owner.idempotent

        claim_a_ba = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker-a-ba",
            reviewer_id="reviewer-a", reviewer_kind="model", reviewer_attempt_id="attempt-a-ba",
            now=112.0, lease_seconds=60.0,
        )
        assert claim_a_ba.attempt is not None
        assert (await repository.settle(
            attempt_id=claim_a_ba.attempt.attempt_id, owner="worker-a-ba",
            lease_token=claim_a_ba.attempt.lease_token, expected_attempt_revision=1,
            finished_at=113.0,
            decision=_decision(
                reviewer="reviewer-a", attempt="attempt-a-ba",
                decision_id="decision-a-ba", pair_order="ba",
            ),
        )).applied

        claim_b = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker-b",
            reviewer_id="reviewer-b", reviewer_kind="model", reviewer_attempt_id="attempt-b",
            now=120.0, lease_seconds=60.0,
        )
        assert claim_b.applied and claim_b.attempt is not None
        settled_b = await repository.settle(
            attempt_id=claim_b.attempt.attempt_id, owner="worker-b",
            lease_token=claim_b.attempt.lease_token, expected_attempt_revision=1,
            finished_at=130.0,
            decision=_decision(reviewer="reviewer-b", attempt="attempt-b", decision_id="decision-b"),
        )
        assert settled_b.applied
        await _settle_pair(
            repository, reviewer="reviewer-b", prefix="reviewer-b-extra", start=140.0,
        )
        quorum = await repository.quorum(
            "candidate-1", 7,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert quorum.status == "completed"
        assert quorum.decision == "approved"
        assert set(quorum.reviewer_ids) == {"reviewer-a", "reviewer-b"}
        with sqlite3.connect(path) as db:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute("UPDATE learning_review_decision SET reason='invalid_contract' WHERE decision_id='decision-a'")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                db.execute("DELETE FROM learning_review_decision WHERE decision_id='decision-a'")

    asyncio.run(run())


def test_review_lease_wrong_token_expiry_and_quorum_disagreement_fail_closed(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "lease.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="a1",
            now=10.0, lease_seconds=5.0,
        )
        assert claim.attempt is not None
        wrong_token = await repository.renew(
            attempt_id=claim.attempt.attempt_id, candidate_id="candidate-1",
            expected_candidate_revision=7, owner="worker", lease_token="wrong",
            expected_revision=1, now=11.0,
        )
        assert wrong_token.conflict
        late = await repository.settle(
            attempt_id=claim.attempt.attempt_id, owner="worker",
            lease_token=claim.attempt.lease_token, expected_attempt_revision=1,
            finished_at=16.0,
            decision=_decision(reviewer="r1", attempt="a1", decision_id="d1"),
        )
        assert late.conflict and late.failure_kind == "settlement_cas_conflict"
        assert await repository.recover_expired(now=16.0) == 1

        await _settle_pair(repository, reviewer="r2", vote="approved", prefix="r2", start=22.0)
        await _settle_pair(repository, reviewer="r3", vote="rejected", prefix="r3", start=52.0)
        quorum = await repository.quorum(
            "candidate-1", 7, expected_reviewer_ids=("r2", "r3"),
        )
        assert quorum.status == "human_review_pending"
        assert quorum.reason == "reviewer_disagreement"

    asyncio.run(run())


def test_human_edit_creates_new_candidate_revision_and_preserves_history(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "revision.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="reviewer", reviewer_kind="model", reviewer_attempt_id="attempt",
            now=10.0, lease_seconds=60.0,
        )
        assert claim.applied
        revised = await repository.create_revision(
            candidate_id="candidate-1", expected_revision=7,
            edited_payload={"expression": "edited"}, source_evidence_ids=("row:107",),
            editor_id="human:admin", reason="human_override", now=20.0,
        )
        assert revised.applied and revised.current_revision == 8
        assert revised.previous_digest != revised.current_digest
        assert claim.attempt is not None
        expired = await repository.get_attempt(claim.attempt.attempt_id)
        assert expired is not None and expired.status == "expired"
        with sqlite3.connect(path) as db:
            candidate = db.execute(
                "SELECT revision, status, source_payload_json FROM learning_candidate WHERE candidate_id='candidate-1'"
            ).fetchone()
        assert candidate[0:2] == (8, "review_pending")
        history = json.loads(candidate[2])["review_revision_history"]
        assert history[-1]["source_evidence_ids"] == ["row:107"]

    asyncio.run(run())


def test_orchestrator_uses_durable_provider_start_fence_and_invalid_output_does_not_vote(tmp_path: Path):
    class FakeResult:
        def __init__(self, value):
            self.ok = True
            self.value = value
            self.provider_id = "recorded-provider"
            self.model_id = "recorded-model"

        def to_report(self):
            return {"model_id": self.model_id, "authorization": "must-not-leak"}

    class FakeAdapter:
        def __init__(self, value):
            self.value = value
            self.calls = 0

        async def call(self, **kwargs):
            self.calls += 1
            started = await kwargs["on_provider_request_start"](SimpleNamespace(started_at=105.0))
            assert started.applied
            return FakeResult(self.value)

    async def run() -> None:
        path = tmp_path / "orchestrator.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        adapter = FakeAdapter({"decision": "approved", "reason": "evidence_supported", "confidence": 0.9})
        orchestrator = ReviewOrchestrator(repository, adapter)
        request = ReviewWorkRequest(
            candidate_id="candidate-1", candidate_revision=7, scope_id="group-1",
            reviewer_id="reviewer-a", reviewer_kind="model", reviewer_attempt_id="attempt-a",
            owner="worker-a", rubric_version="rubric-v1", prompt_version="prompt-v1",
            pair_order="ab", prompt="fixture", system_prompt="fixture",
            source_evidence_ids=("row:107",), source_example_ids=("row:107",),
            reviewer_profile_version="fixture-reviewer-profile-v1",
        )
        result = await orchestrator.run(request)
        assert result.status == "completed"
        assert adapter.calls == 1
        decisions = await repository.list_decisions("candidate-1", 7)
        assert len(decisions) == 1
        assert decisions[0].diagnostics["authorization"] == "[redacted]"

        duplicate = await orchestrator.run(request)
        assert duplicate.status == "blocked"
        assert adapter.calls == 1

        invalid_adapter = FakeAdapter({"decision": "allow", "reason": "free text"})
        invalid = ReviewOrchestrator(repository, invalid_adapter)
        invalid_result = await invalid.run(ReviewWorkRequest(
            candidate_id="candidate-1", candidate_revision=7, scope_id="group-1",
            reviewer_id="reviewer-b", reviewer_kind="model", reviewer_attempt_id="attempt-b",
            owner="worker-b", rubric_version="rubric-v1", prompt_version="prompt-v1",
            pair_order="ba", prompt="fixture", system_prompt="fixture",
            source_evidence_ids=("row:107",), source_example_ids=("row:107",),
            reviewer_profile_version="fixture-reviewer-profile-v1",
        ))
        assert invalid_result.status == "quarantined"
        assert len(await repository.list_decisions("candidate-1", 7)) == 1

    asyncio.run(run())


def test_orchestrator_cancellation_settles_retry_wait_and_rethrows(tmp_path: Path):
    class CancellingAdapter:
        async def call(self, **kwargs):
            started = await kwargs["on_provider_request_start"](
                SimpleNamespace(started_at=105.0)
            )
            assert started.applied
            raise asyncio.CancelledError()

    async def run() -> None:
        path = tmp_path / "cancel.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        orchestrator = ReviewOrchestrator(repository, CancellingAdapter())
        request = ReviewWorkRequest(
            candidate_id="candidate-1", candidate_revision=7, scope_id="group-1",
            reviewer_id="reviewer-a", reviewer_kind="model", reviewer_attempt_id="attempt-a",
            owner="worker-a", rubric_version="rubric-v1", prompt_version="prompt-v1",
            pair_order="ab", prompt="fixture", system_prompt="fixture",
            source_evidence_ids=("row:107",), source_example_ids=("row:107",),
            reviewer_profile_version="fixture-reviewer-profile-v1",
        )
        with pytest.raises(asyncio.CancelledError):
            await orchestrator.run(request)
        with sqlite3.connect(path) as db:
            row = db.execute(
                "SELECT status, failure_kind, retryable, provider_request_started "
                "FROM learning_review_attempt"
            ).fetchone()
        assert row == ("retry_wait", "cancelled", 1, 1)

    asyncio.run(run())


def test_expression_auto_check_routes_durable_candidate_to_shared_orchestrator():
    class PatternService:
        async def list_reviewable_patterns(self, **_kwargs):
            return [SimpleNamespace(
                id="memory-1", group_id="group-1", situation="chat",
                expression="hello", style="", content_list="[]", count=3,
                review_status="pending", metadata={"candidate_id": "candidate-1", "candidate_revision": 7},
            )]

    class Ledger:
        async def get_candidate(self, candidate_id):
            return SimpleNamespace(candidate_id=candidate_id, revision=7)

        async def list_evidence(self, _candidate_id):
            return [SimpleNamespace(
                source_row_id=107, platform_message_id="", event_id="",
                eligible=True, is_generated=False,
            )]

    class Orchestrator:
        def __init__(self):
            self.requests = []
            self.expected_reviewer_ids = ("reviewer-a", "reviewer-b")

        async def run_configured_quorum(self, request):
            self.requests.append(request)
            return SimpleNamespace(status="completed")

    class Gateway:
        config = SimpleNamespace(evolution=SimpleNamespace(review_runner_min_interval_sec=15, review_batch_size=10, review_min_count=2))

        async def call_data_process_task(self, **_kwargs):
            raise AssertionError("durable review must use the shared orchestrator")

    async def run() -> None:
        orchestrator = Orchestrator()
        task = ExpressionAutoCheckTask(
            SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=PatternService())),
            Gateway(), review_orchestrator=orchestrator, candidate_ledger=Ledger(),
        )
        assert await task.run_once("group-1") == 1
        assert len(orchestrator.requests) == 1
        assert orchestrator.requests[0].source_evidence_ids == ("row:107",)
        assert orchestrator.requests[0].reviewer_id == "reviewer-a"

        maintenance = ExpressionAutoCheckTask(
            SimpleNamespace(memory_engine=None), Gateway(), maintenance_only=True,
        )
        assert await maintenance.run_once("group-1") == 0

    asyncio.run(run())


def test_jargon_auto_check_routes_durable_candidate_to_configured_quorum():
    class Ledger:
        async def get_candidate(self, candidate_id):
            return SimpleNamespace(candidate_id=candidate_id, revision=7)

        async def list_evidence(self, _candidate_id):
            return [SimpleNamespace(
                source_row_id=107, platform_message_id="", event_id="",
                eligible=True, is_generated=False,
            )]

    class Orchestrator:
        expected_reviewer_ids = ("reviewer-a", "reviewer-b")

        def __init__(self):
            self.requests = []

        async def run_configured_quorum(self, request):
            self.requests.append(request)
            return SimpleNamespace(status="completed")

    class Gateway:
        config = SimpleNamespace(evolution=SimpleNamespace())

    async def run() -> None:
        orchestrator = Orchestrator()
        task = JargonAutoCheckTask(
            SimpleNamespace(memory_engine=None), Gateway(),
            review_orchestrator=orchestrator, candidate_ledger=Ledger(),
        )
        candidate = SimpleNamespace(
            session_id="group-1", content="term", summary="meaning",
            metadata={
                "candidate_id": "candidate-1", "candidate_revision": 7,
                "meaning": "meaning", "scene": "chat", "examples": ["example"],
            },
        )
        outcome = await task._review_candidate_durable(candidate)
        assert outcome.status == "completed"
        assert len(orchestrator.requests) == 1
        assert orchestrator.requests[0].reviewer_id == "reviewer-a"
        assert orchestrator.requests[0].source_evidence_ids == ("row:107",)

    asyncio.run(run())


def test_review_claim_has_single_winner_for_twenty_independent_connection_rounds(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "race.db"
        _database(path)
        stale_overwrite = duplicate_approved = lost_history = 0
        successful_claims: list[int] = []
        for index in range(20):
            candidate_id = f"candidate-{index}"
            _candidate(path, candidate_id=candidate_id, revision=1)
            left = LearningReviewRepository(path)
            right = LearningReviewRepository(path)
            results = await asyncio.gather(
                left.claim(candidate_id=candidate_id, expected_revision=1, owner="left", reviewer_id="r-left", reviewer_kind="model", reviewer_attempt_id=f"left-{index}", now=100.0, lease_seconds=30.0),
                right.claim(candidate_id=candidate_id, expected_revision=1, owner="right", reviewer_id="r-right", reviewer_kind="model", reviewer_attempt_id=f"right-{index}", now=100.0, lease_seconds=30.0),
            )
            winners = [result for result in results if result.applied]
            successful_claims.append(len(winners))
            assert len(winners) == 1
            winner = winners[0].attempt
            assert winner is not None
            aborted = await left.abort(
                attempt_id=winner.attempt_id, candidate_id=candidate_id,
                candidate_revision=1, owner=winner.owner, lease_token=winner.lease_token,
                expected_revision=winner.revision, now=101.0,
            )
            assert aborted.applied
        assert successful_claims == [1] * 20
        assert stale_overwrite == duplicate_approved == lost_history == 0

    asyncio.run(run())


def test_admission_and_publish_proof_are_separate_from_candidate_state(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "admission.db"
        _database(path)
        _candidate(path)
        reviews = LearningReviewRepository(path)
        decision_ids = []
        decision_ids.extend(await _settle_pair(
            reviews, reviewer="reviewer-a", model_identity="model-a",
            prefix="reviewer-a", start=100.0,
        ))
        decision_ids.extend(await _settle_pair(
            reviews, reviewer="reviewer-b", model_identity="model-b",
            prefix="reviewer-b", start=130.0,
        ))

        service = AdmissionService(
            path,
            evaluation_enabled=True,
            automatic_quorum_enabled=True,
            mark_published_enabled=True,
            trusted_publish_owner_ids=frozenset({"fixture-index-owner"}),
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
            publish_proof_verifier=_AuthoritativePublishVerifier(),
        )
        evaluated = await service.evaluate("candidate-1", 7, now=150.0)
        assert evaluated.applied and evaluated.admission is not None
        assert evaluated.admission.pre_index_eligible
        assert not evaluated.admission.post_publish_eligible
        assert evaluated.admission.index_blocked
        assert evaluated.admission.blocked_kind == "vector_identity_unverified"
        decisions = await reviews.list_decisions("candidate-1", 7)
        final_quorum = next(item for item in decisions if item.reviewer_kind == "rule")
        assert set(final_quorum.source_decision_ids) == set(decision_ids)
        assert final_quorum.decision == "approved"
        assert final_quorum.order_invariant
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT status FROM learning_candidate WHERE candidate_id='candidate-1'").fetchone() == ("review_pending",)

        invalid = _publish_proof(evaluated.admission, vector_dimension=0)
        blocked = await service.repository.mark_published(
            invalid, submitting_owner_id="fixture-index-owner",
            expected_record_revision=evaluated.admission.revision, now=160.0,
        )
        assert blocked.conflict and blocked.failure_kind == "invalid_publish_proof"

        valid = _publish_proof(evaluated.admission)
        forged = replace(
            valid,
            mapping_digest="x",
            index_hash="x",
            publish_proof="x",
        )
        rejected_forgery = await service.repository.mark_published(
            forged, submitting_owner_id="fixture-index-owner",
            expected_record_revision=evaluated.admission.revision, now=165.0,
        )
        assert rejected_forgery.conflict
        assert rejected_forgery.failure_kind == "invalid_publish_proof"
        wrong_owner = await service.repository.mark_published(
            valid, submitting_owner_id="untrusted-owner",
            expected_record_revision=evaluated.admission.revision, now=166.0,
        )
        assert wrong_owner.conflict and wrong_owner.failure_kind == "invalid_publish_proof"
        published = await service.repository.mark_published(
            valid, submitting_owner_id="fixture-index-owner",
            expected_record_revision=evaluated.admission.revision, now=170.0,
        )
        assert published.applied and published.admission is not None
        assert published.admission.post_publish_eligible
        assert not published.admission.index_blocked
        assert published.admission.publish_proof_digest == "e" * 64
        assert published.admission.publish_proof_digest != valid.publish_proof

        with sqlite3.connect(path) as db:
            db.execute("UPDATE learning_candidate SET revision = 8 WHERE candidate_id = 'candidate-1'")
            db.commit()
        replay_after_revision = await service.repository.mark_published(
            valid, submitting_owner_id="fixture-index-owner",
            expected_record_revision=published.admission.revision, now=175.0,
        )
        assert replay_after_revision.conflict
        assert replay_after_revision.failure_kind == "candidate_revision_conflict"

        stale = _publish_proof(
            evaluated.admission,
            canonical_revision=8,
            asset_revision=8,
            index_generation="generation-2",
            mapping_digest="c" * 64,
            index_hash="d" * 64,
        )
        stale_result = await service.repository.mark_published(
            stale, submitting_owner_id="fixture-index-owner",
            expected_record_revision=published.admission.revision, now=180.0,
        )
        assert stale_result.conflict

    asyncio.run(run())


def test_stage07_migration_is_ready_and_has_no_cross_database_foreign_keys(tmp_path: Path):
    path = tmp_path / "migration.db"
    _database(path)
    _candidate(path)
    with sqlite3.connect(path) as db:
        report = inspect_architecture_migration(db)
        assert report.schema_version == LATEST_ARCHITECTURE_SCHEMA_VERSION
        assert not {"learning_review_attempt", "learning_review_decision", "learning_admission"} & set(report.missing_tables)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        admission_fks = db.execute("PRAGMA foreign_key_list(learning_admission)").fetchall()
        assert {row[2] for row in admission_fks} <= {"learning_candidate"}


def test_stage07_v168_database_upgrades_order_invariant_additively(tmp_path: Path):
    path = tmp_path / "v168-upgrade.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys = ON")
        for version, ddl in _MIGRATIONS:
            if 145 <= version <= 168:
                db.execute(ddl)
        db.execute("PRAGMA user_version = 168")
        db.commit()
        columns_before = {
            row[1] for row in db.execute("PRAGMA table_info(learning_review_decision)")
        }
        assert "order_invariant" not in columns_before
    _candidate(path)
    with sqlite3.connect(path) as db:
        db.execute(
            """INSERT INTO learning_review_decision(
                decision_id, candidate_id, candidate_revision, decision, reason,
                reviewer_id, reviewer_kind, reviewer_attempt_id, rubric_version,
                prompt_version, model_identity, source_evidence_ids_json,
                source_example_ids_json, expected_revision, pair_order, created_at
            ) VALUES (
                'legacy-decision', 'candidate-1', 7, 'approved', 'evidence_supported',
                'legacy-reviewer', 'model', 'legacy-attempt', 'rubric-v1',
                'prompt-v1', 'legacy-model', '[\"row:107\"]', '[\"row:107\"]',
                7, 'ab', 100
            )"""
        )
        db.commit()
        _run_migrations(db)
        columns_after = {
            row[1] for row in db.execute("PRAGMA table_info(learning_review_decision)")
        }
        assert "order_invariant" in columns_after
        assert db.execute("PRAGMA user_version").fetchone() == (
            LATEST_ARCHITECTURE_SCHEMA_VERSION,
        )
        assert db.execute(
            "SELECT order_invariant FROM learning_review_decision "
            "WHERE decision_id = 'legacy-decision'"
        ).fetchone() == (0,)


def test_claim_settle_and_admission_reject_non_reviewable_candidate(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "candidate-status.db"
        _database(path)
        _candidate(path)
        with sqlite3.connect(path) as db:
            db.execute("UPDATE learning_candidate SET status='blocked' WHERE candidate_id='candidate-1'")
            db.commit()
        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="a1",
            now=10.0,
        )
        assert claim.conflict and claim.failure_kind == "candidate_status_not_reviewable"
        admission = await AdmissionService(path, evaluation_enabled=True).evaluate(
            "candidate-1", 7, now=20.0,
        )
        assert admission.conflict and admission.failure_kind == "candidate_status_not_reviewable"
        assert admission.admission is None

    asyncio.run(run())


def test_settlement_requires_current_eligible_source_evidence(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "evidence.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="a1",
            now=10.0, lease_seconds=60.0,
        )
        assert claim.attempt is not None
        missing = await repository.settle(
            attempt_id=claim.attempt.attempt_id, owner="worker",
            lease_token=claim.attempt.lease_token, expected_attempt_revision=1,
            finished_at=20.0,
            decision=_decision(reviewer="r1", attempt="a1", decision_id="d1", row=999),
        )
        assert missing.conflict and missing.failure_kind == "review_evidence_missing"
        assert await repository.list_decisions("candidate-1", 7) == ()

    asyncio.run(run())


def test_settlement_rechecks_candidate_lifecycle_after_claim(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "settlement-status.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="a1", now=10.0,
        )
        assert claim.attempt is not None
        with sqlite3.connect(path) as db:
            db.execute("UPDATE learning_candidate SET status='quarantined' WHERE candidate_id='candidate-1'")
            db.commit()
        result = await repository.settle(
            attempt_id=claim.attempt.attempt_id, owner="worker",
            lease_token=claim.attempt.lease_token, expected_attempt_revision=1,
            finished_at=20.0,
            decision=_decision(reviewer="r1", attempt="a1", decision_id="d1"),
        )
        assert result.conflict and result.failure_kind == "candidate_status_not_reviewable"
        assert await repository.list_decisions("candidate-1", 7) == ()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("evidence_update", "decision_row"),
    (
        ("eligible = 0", 107),
        ("is_generated = 1", 107),
        ("event_id = 'authoritative-event'", 107),
    ),
)
def test_settlement_rejects_ineligible_generated_or_wrong_priority_identity(
    tmp_path: Path, evidence_update: str, decision_row: int,
):
    async def run() -> None:
        path = tmp_path / "evidence-boundary.db"
        _database(path)
        _candidate(path)
        with sqlite3.connect(path) as db:
            db.execute(
                f"UPDATE learning_candidate_evidence SET {evidence_update} "
                "WHERE candidate_id='candidate-1'"
            )
            db.commit()
        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="a1", now=10.0,
        )
        assert claim.attempt is not None
        result = await repository.settle(
            attempt_id=claim.attempt.attempt_id, owner="worker",
            lease_token=claim.attempt.lease_token, expected_attempt_revision=1,
            finished_at=20.0,
            decision=_decision(
                reviewer="r1", attempt="a1", decision_id="d1", row=decision_row,
            ),
        )
        assert result.conflict and result.failure_kind == "review_evidence_missing"
        assert await repository.list_decisions("candidate-1", 7) == ()

    asyncio.run(run())


def test_quorum_uses_all_reviewers_and_enforces_model_independence(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "quorum-all.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        votes = (("r1", "approved", "model-a"), ("r2", "approved", "model-b"), ("r3", "rejected", "model-c"))
        expected_ids: set[str] = set()
        for index, (reviewer, vote, model) in enumerate(votes, start=1):
            expected_ids.update(await _settle_pair(
                repository, reviewer=reviewer, vote=vote, model_identity=model,
                prefix=f"r{index}", start=10.0 + index * 30.0,
            ))
        result = await repository.quorum(
            "candidate-1", 7, expected_reviewer_ids=("r1", "r2", "r3"),
        )
        assert result.status == "human_review_pending"
        assert result.reason == "reviewer_disagreement"
        assert set(result.decision_ids) == expected_ids
        unconfigured = await repository.quorum("candidate-1", 7)
        assert unconfigured.status == "human_review_pending"
        assert unconfigured.reason == "reviewer_configuration_missing"

        other_path = tmp_path / "quorum-model.db"
        _database(other_path)
        _candidate(other_path)
        same_model = LearningReviewRepository(other_path)
        for index, reviewer in enumerate(("r1", "r2"), start=1):
            await _settle_pair(
                same_model, reviewer=reviewer, model_identity="same-model",
                prefix=f"same-{index}", start=10.0 + index * 30.0,
            )
        conflict = await same_model.quorum(
            "candidate-1", 7, expected_reviewer_ids=("r1", "r2"),
        )
        assert conflict.status == "human_review_pending"
        assert conflict.reason == "reviewer_identity_conflict"

    asyncio.run(run())


def test_retry_wait_allocates_new_durable_attempt(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "retry.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        first = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="durable-root",
            now=10.0,
        )
        assert first.attempt is not None
        assert (await repository.abort(
            attempt_id=first.attempt.attempt_id, candidate_id="candidate-1",
            candidate_revision=7, owner="worker", lease_token=first.attempt.lease_token,
            expected_revision=1, now=11.0, status="retry_wait", failure_kind="provider_timeout",
            retryable=True, retry_at=12.0,
        )).applied
        retry = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="durable-root",
            now=12.0,
        )
        assert retry.applied and retry.attempt is not None
        assert retry.attempt.attempt_id != first.attempt.attempt_id
        assert retry.attempt.review_work_attempt == first.attempt.review_work_attempt + 1
        assert retry.attempt.reviewer_attempt_id != first.attempt.reviewer_attempt_id

    asyncio.run(run())


def test_feature_flags_missing_candidate_publish_proof_and_shutdown_fail_closed(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "boundaries.db"
        _database(path)
        _candidate(path)
        disabled = await AdmissionService(path).evaluate("candidate-1", 7, now=10.0)
        assert disabled.conflict and disabled.failure_kind == "admission_evaluation_disabled"
        disabled_publish_repository = AdmissionRepository(path)
        placeholder = VectorPublishProof(
            candidate_id="candidate-1", candidate_revision=7, admission_revision=1,
            review_decision_ids=("decision",), canonical_revision=7,
            owner_id="owner", asset_id="asset", asset_revision=7,
            membership_id="membership", index_generation="generation-1",
            provider_id="provider", model_id="model", vector_dimension=8,
            vector_count=1, mapping_digest="a" * 64, index_hash="b" * 64,
            provenance_digest="c" * 64, publish_proof="d" * 64,
        )
        disabled_publish = await disabled_publish_repository.mark_published(
            placeholder, submitting_owner_id="owner", expected_record_revision=1, now=10.0,
        )
        assert disabled_publish.conflict
        assert disabled_publish.failure_kind == "mark_published_disabled"
        missing = await AdmissionService(path, evaluation_enabled=True).evaluate("missing", 1, now=10.0)
        assert missing.conflict and missing.failure_kind == "candidate_missing"

        repository = LearningReviewRepository(path)
        claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="worker",
            reviewer_id="r1", reviewer_kind="model", reviewer_attempt_id="a1", now=10.0,
        )
        assert claim.attempt is not None
        assert await repository.settle_running_for_shutdown(now=11.0) == 1
        stopped = await repository.get_attempt(claim.attempt.attempt_id)
        assert stopped is not None and stopped.status == "retry_wait"
        assert stopped.failure_kind == "shutdown"

        with sqlite3.connect(path) as db:
            db.execute("DROP INDEX ux_learning_review_active_claim")
            db.commit()
        assert not await repository.schema_ready()

    asyncio.run(run())


def test_legacy_review_entry_is_maintenance_only_for_durable_candidate():
    class PatternService:
        def __init__(self):
            self.calls = 0
            self.pattern = SimpleNamespace(
                id="memory-1", group_id="group-1", situation="chat",
                expression="hello", style="", count=1, checked=False,
                rejected=False, review_status="pending_human", review_reason="",
                review_suggestion="", shared_scope="group-1", think_level=0,
                weight=1.0, modified_by="", source="learning", content_list="[]",
                last_review_time=0.0, last_active_time=0.0, create_time=0.0,
                metadata={"candidate_id": "candidate-1", "candidate_revision": 7},
            )

        async def get_pattern(self, _pattern_id):
            return self.pattern

        async def update_review(self, *_args, **_kwargs):
            self.calls += 1
            return self.pattern

    async def run() -> None:
        patterns = PatternService()
        db = SimpleNamespace(memory_engine=SimpleNamespace(expression_pattern_service=patterns))
        result = await ExpressionReviewService(db).submit_review(
            "memory-1", "approved", "admin-1",
        )
        assert result is not None
        assert result["maintenance_only"] is True
        assert result["failure_kind"] == "durable_review_contract_required"
        assert result["review_status"] == "pending_human"
        assert patterns.calls == 0

    asyncio.run(run())


def test_configured_review_path_executes_every_reviewer_in_both_orders(tmp_path: Path):
    class FakeResult:
        ok = True
        value = {"decision": "approved", "reason": "evidence_supported", "confidence": 0.9}

        def __init__(self, model_id: str):
            self.provider_id = "recorded-provider"
            self.model_id = model_id

        def to_report(self):
            return {"model_id": self.model_id}

    class FakeAdapter:
        def __init__(self):
            self.calls = []

        async def call(self, **kwargs):
            self.calls.append(kwargs)
            started = await kwargs["on_provider_request_start"](
                SimpleNamespace(started_at=100.0 + len(self.calls))
            )
            assert started.applied
            return FakeResult("model-a" if len(self.calls) <= 2 else "model-b")

    async def run() -> None:
        path = tmp_path / "configured-review.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        adapter = FakeAdapter()
        orchestrator = ReviewOrchestrator(
            repository, adapter,
            automatic_quorum_enabled=True,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        result = await orchestrator.run_configured_quorum(ReviewWorkRequest(
            candidate_id="candidate-1", candidate_revision=7, scope_id="group-1",
            reviewer_id="", reviewer_kind="model", reviewer_attempt_id="matrix-root",
            owner="review-worker", rubric_version="rubric-v1", prompt_version="prompt-v1",
            pair_order="ab", prompt="fixture", system_prompt="fixture",
            source_evidence_ids=("row:107",), source_example_ids=("row:107",),
            reviewer_profile_version="fixture-reviewer-profile-v1",
        ))
        assert result.status == "completed"
        assert len(adapter.calls) == 4
        assert {item["task_name"] for item in adapter.calls} == {
            "learning.review:reviewer-a", "learning.review:reviewer-b",
        }
        decisions = await repository.list_decisions("candidate-1", 7)
        model_decisions = [item for item in decisions if item.reviewer_kind == "model"]
        assert {(item.reviewer_id, item.pair_order) for item in model_decisions} == {
            ("reviewer-a", "ab"), ("reviewer-a", "ba"),
            ("reviewer-b", "ab"), ("reviewer-b", "ba"),
        }
        assert all(is_reviewer_model_identity(item.model_identity) for item in model_decisions)
        assert len({item.model_identity for item in model_decisions}) == 2
        assert all(
            item.diagnostics["reviewer_profile_version"]
            == "fixture-reviewer-profile-v1"
            for item in model_decisions
        )
        final = next(item for item in decisions if item.reviewer_kind == "rule")
        assert final.order_invariant is True
        assert final.diagnostics["order_invariant"] is True

    asyncio.run(run())


def test_single_pair_order_never_reaches_quorum(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "single-order.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        for index, reviewer in enumerate(("reviewer-a", "reviewer-b"), start=1):
            claim = await repository.claim(
                candidate_id="candidate-1", expected_revision=7, owner=reviewer,
                reviewer_id=reviewer, reviewer_kind="model",
                reviewer_attempt_id=f"single-{index}", now=10.0 + index,
            )
            assert claim.attempt is not None
            assert (await repository.settle(
                attempt_id=claim.attempt.attempt_id, owner=reviewer,
                lease_token=claim.attempt.lease_token, expected_attempt_revision=1,
                finished_at=20.0 + index,
                decision=_decision(
                    reviewer=reviewer, attempt=f"single-{index}",
                    decision_id=f"single-decision-{index}", pair_order="ab",
                    model_identity=f"model-{index}",
                ),
            )).applied
        quorum = await repository.quorum(
            "candidate-1", 7,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert quorum.status == "human_review_pending"
        assert quorum.reason == "pair_order_incomplete"
        assert not quorum.order_invariant

    asyncio.run(run())


def test_quorum_finalize_fences_late_pair_completion(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "quorum-fence.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        await _settle_pair(
            repository, reviewer="reviewer-a", model_identity="model-a",
            prefix="a", start=10.0,
        )
        await _settle_pair(
            repository, reviewer="reviewer-b", model_identity="model-b",
            prefix="b", start=40.0,
        )
        late_claim = await repository.claim(
            candidate_id="candidate-1", expected_revision=7, owner="late",
            reviewer_id="reviewer-a", reviewer_kind="model",
            reviewer_attempt_id="late-ba", now=70.0,
        )
        assert late_claim.attempt is not None
        final = await repository.finalize_quorum(
            "candidate-1", 7, created_at=71.0,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert final is not None and final.order_invariant
        late = await repository.settle(
            attempt_id=late_claim.attempt.attempt_id, owner="late",
            lease_token=late_claim.attempt.lease_token, expected_attempt_revision=1,
            finished_at=72.0,
            decision=_decision(
                reviewer="reviewer-a", attempt="late-ba", decision_id="late-decision",
                vote="rejected", pair_order="ba", model_identity="model-a",
            ),
        )
        assert late.conflict
        assert late.failure_kind == "review_quorum_finalized"
        replayed = await repository.finalize_quorum(
            "candidate-1", 7, created_at=73.0,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert replayed == final

    asyncio.run(run())


@pytest.mark.parametrize(
    ("reviewer_a_rows", "reviewer_b_rows"),
    (
        ((107, 108), (107, 107)),
        ((108, 108), (107, 107)),
    ),
)
def test_quorum_rejects_mismatched_source_evidence_sets(
    tmp_path: Path, reviewer_a_rows: tuple[int, int],
    reviewer_b_rows: tuple[int, int],
):
    async def run() -> None:
        path = tmp_path / "quorum-evidence-mismatch.db"
        _database(path)
        _candidate(path)
        with sqlite3.connect(path) as db:
            db.execute(
                """INSERT INTO learning_candidate_evidence(
                   candidate_id, evidence_id, batch_id, source_row_id, sender_id,
                   scope_id, speaker_scope_id, source_type, evidence_quality,
                   eligible, eligibility_reason, created_at
                ) SELECT candidate_id, 'evidence-extra', batch_id, 108, sender_id,
                         scope_id, speaker_scope_id, source_type, evidence_quality,
                         eligible, eligibility_reason, created_at
                  FROM learning_candidate_evidence WHERE candidate_id = 'candidate-1'
                  LIMIT 1"""
            )
            db.commit()
        repository = LearningReviewRepository(path)
        await _settle_pair(
            repository, reviewer="reviewer-a", model_identity="model-a",
            prefix="evidence-a", start=10.0, rows=reviewer_a_rows,
        )
        await _settle_pair(
            repository, reviewer="reviewer-b", model_identity="model-b",
            prefix="evidence-b", start=40.0, rows=reviewer_b_rows,
        )
        quorum = await repository.quorum(
            "candidate-1", 7,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert quorum.status == "human_review_pending"
        assert quorum.reason == "review_input_mismatch"
        assert not quorum.order_invariant
        assert await repository.finalize_quorum(
            "candidate-1", 7, created_at=100.0,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        ) is None

    asyncio.run(run())


def test_quorum_rejects_mixed_reviewer_kinds(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "quorum-kind-mismatch.db"
        _database(path)
        _candidate(path)
        repository = LearningReviewRepository(path)
        await _settle_pair(
            repository, reviewer="reviewer-a", model_identity="model-a",
            prefix="kind-a", start=10.0, reviewer_kinds=("model", "human"),
        )
        await _settle_pair(
            repository, reviewer="reviewer-b", model_identity="model-b",
            prefix="kind-b", start=40.0,
        )
        quorum = await repository.quorum(
            "candidate-1", 7,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert quorum.status == "human_review_pending"
        assert quorum.reason == "reviewer_identity_conflict"
        assert not quorum.order_invariant

    asyncio.run(run())


def test_schema_readiness_rejects_order_invariant_without_check(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "review-schema-drift.db"
        _database(path)
        with sqlite3.connect(path) as db:
            original = db.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'learning_review_decision'"
            ).fetchone()[0]
            assert "CHECK(order_invariant IN (0,1))" in original
            db.execute("PRAGMA writable_schema = ON")
            db.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type = 'table' "
                "AND name = 'learning_review_decision'",
                (original.replace(" CHECK(order_invariant IN (0,1))", ""),),
            )
            schema_version = db.execute("PRAGMA schema_version").fetchone()[0]
            db.execute(f"PRAGMA schema_version = {schema_version + 1}")
            db.execute("PRAGMA writable_schema = OFF")
            db.commit()
        assert not await LearningReviewRepository(path).schema_ready()

    asyncio.run(run())


def test_admission_save_rechecks_candidate_revision(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "admission-cas.db"
        _database(path)
        _candidate(path)
        service = AdmissionService(path, evaluation_enabled=True)
        original = service.repository.save_evaluation

        async def race(**kwargs):
            with sqlite3.connect(path) as db:
                db.execute(
                    "UPDATE learning_candidate SET revision = 8 "
                    "WHERE candidate_id = 'candidate-1' AND revision = 7"
                )
                db.commit()
            return await original(**kwargs)

        service.repository.save_evaluation = race
        result = await service.evaluate("candidate-1", 7, now=100.0)
        assert result.conflict
        assert result.failure_kind == "candidate_revision_conflict"
        assert result.admission is None

    asyncio.run(run())


def test_publish_requires_authoritative_verifier_not_owner_checksum(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "publish-verifier.db"
        _database(path)
        _candidate(path)
        admission = await AdmissionRepository(path).save_evaluation(
            candidate_id="candidate-1", candidate_revision=7,
            review_decision_ids=("decision-1",), pre_index_eligible=True,
            blocked_reason="vector_identity_unverified", blocked_stage="index_identity",
            blocked_kind="vector_identity_unverified", provenance_digest="c" * 64,
            now=100.0,
        )
        assert admission.admission is not None
        proof = _publish_proof(admission.admission)
        repository = AdmissionRepository(
            path, mark_published_enabled=True,
            trusted_publish_owner_ids=frozenset({"fixture-index-owner"}),
        )
        unavailable = await repository.mark_published(
            proof, submitting_owner_id="fixture-index-owner",
            expected_record_revision=admission.admission.revision, now=101.0,
        )
        assert unavailable.conflict
        assert unavailable.failure_kind == "publish_verifier_unavailable"

        rejected_repository = AdmissionRepository(
            path, mark_published_enabled=True,
            trusted_publish_owner_ids=frozenset({"fixture-index-owner"}),
            publish_proof_verifier=_AuthoritativePublishVerifier("different-asset"),
        )
        rejected = await rejected_repository.mark_published(
            proof, submitting_owner_id="fixture-index-owner",
            expected_record_revision=admission.admission.revision, now=102.0,
        )
        assert rejected.conflict
        assert rejected.failure_kind == "publish_asset_not_verified"

    asyncio.run(run())


def test_human_admission_is_append_only_and_bound_to_durable_admission(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "human-admission-append-only.db"
        _database(path)
        _candidate(path)
        repository = AdmissionRepository(path)
        saved = await repository.save_evaluation(
            candidate_id="candidate-1", candidate_revision=7,
            review_decision_ids=("review-quorum:fixture",), pre_index_eligible=True,
            blocked_reason="vector_identity_unverified", blocked_stage="index_identity",
            blocked_kind="vector_identity_unverified", provenance_digest="a" * 64, now=10.0,
        )
        assert saved.admission is not None
        assert await repository.record_human_admission(
            admission_id="human-admission-1", candidate_id="candidate-1",
            candidate_revision=7, admission_revision=saved.admission.admission_revision,
            reviewer_identity="human:reviewer-1", decision="approved", reason="fixture",
            provenance_digest="a" * 64, now=11.0,
        )
        assert not await repository.record_human_admission(
            admission_id="human-admission-2", candidate_id="candidate-1",
            candidate_revision=7, admission_revision=saved.admission.admission_revision,
            reviewer_identity="human:reviewer-2", decision="approved", reason="duplicate",
            provenance_digest="a" * 64, now=12.0,
        )
        with sqlite3.connect(path) as db:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("UPDATE learning_human_admission SET reason='tampered'")
            with pytest.raises(sqlite3.IntegrityError):
                db.execute("DELETE FROM learning_human_admission")
            db.rollback()

    asyncio.run(run())


def test_human_admission_rejects_revision_and_provenance_drift(tmp_path: Path):
    async def run() -> None:
        path = tmp_path / "human-admission-contract.db"
        _database(path)
        _candidate(path)
        repository = AdmissionRepository(path)
        saved = await repository.save_evaluation(
            candidate_id="candidate-1", candidate_revision=7,
            review_decision_ids=("review-quorum:fixture",), pre_index_eligible=True,
            blocked_reason="vector_identity_unverified", blocked_stage="index_identity",
            blocked_kind="vector_identity_unverified", provenance_digest="b" * 64, now=10.0,
        )
        assert saved.admission is not None
        kwargs = dict(
            admission_id="human-admission-1", candidate_id="candidate-1",
            candidate_revision=7, admission_revision=saved.admission.admission_revision,
            reviewer_identity="human:reviewer-1", decision="approved", reason="fixture",
            provenance_digest="b" * 64, now=11.0,
        )
        assert await repository.record_human_admission(**{**kwargs, "admission_revision": 99}) is False
        assert await repository.record_human_admission(**{**kwargs, "provenance_digest": "c" * 64}) is False
        assert await repository.record_human_admission(**{**kwargs, "reviewer_identity": "model:reviewer"}) is False

    asyncio.run(run())
