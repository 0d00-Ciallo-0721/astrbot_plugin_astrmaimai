from __future__ import annotations

from datetime import datetime, timezone

import pytest

from astrmai.learning.evaluation.contracts import (
    annotation_status,
    cohen_kappa,
    metric_from_counts,
    wilson_interval,
    evaluate_stage10_readiness,
    evaluate_workflow_evidence,
    workflow_evidence_complete,
)


_NOW = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _workflow_evidence() -> dict[str, object]:
    return {
        "candidate_id": "candidate-1", "candidate_revision": 1,
        "source_evidence_ids": ["row:1"], "review_id": "review-1",
        "review_revision": 1, "review_candidate_revision": 1,
        "review_status": "completed", "reviewer_identity": "human:reviewer-1",
        "human_admission_id": "admission-1", "admission_revision": 1,
        "admission_candidate_revision": 1, "human_admission_decision": "approved",
        "admission_reviewer_identity": "human:admitter-1",
        "publish_proof_digest": "a" * 64, "asset_id": "asset-1",
        "canonical_memory_id": "canonical-1", "index_membership_id": "membership-1",
        "generation": 1, "retrieval_event_id": "event-1", "retrieval_generation": 1,
        "retrieval_provenance_digest": "b" * 64,
        "retrieval_provenance": {
            "candidate_id": "candidate-1", "asset_id": "asset-1",
            "canonical_memory_id": "canonical-1", "source_evidence_ids": ["row:1"],
        },
    }


def test_wilson_and_minimum_denominator_are_explicit():
    low, high = wilson_interval(90, 100)
    assert 0 < low < high < 1
    metric = metric_from_counts("candidate_precision", 0, 0, minimum_denominator=100, source="fixture", window_start=_NOW, window_end=_NOW)
    assert metric.status == "unavailable"
    assert metric.ci95_low is None
    assert "minimum_denominator_not_met" in metric.warnings


def test_unknown_values_do_not_change_kappa_or_metric_denominator():
    assert cohen_kappa([1, 1, "unknown"], [1, 0, "unknown"]) == pytest.approx(0.0)
    assert annotation_status(0.66) == "blocked"
    assert annotation_status(0.75) == "conditional"
    assert annotation_status(0.8) == "completed"
    metric = metric_from_counts("reply_outcome_known_rate", 1, 1, minimum_denominator=50, source="fixture", window_start=_NOW, window_end=_NOW, unknown_count=10)
    assert metric.denominator == 1
    assert metric.unknown_count == 10
    assert metric.status == "unavailable"


def test_metric_rejects_non_integer_counts():
    with pytest.raises(ValueError):
        metric_from_counts("x", 1.5, 2, minimum_denominator=1, source="fixture", window_start=_NOW, window_end=_NOW)


def test_metric_rejects_impossible_sample_and_unknown_counts():
    with pytest.raises(ValueError, match="sample_count"):
        metric_from_counts("x", 1, 2, minimum_denominator=1, source="fixture", window_start=_NOW, window_end=_NOW, sample_count=1)
    with pytest.raises(ValueError, match="unknown_count"):
        metric_from_counts("x", 1, 2, minimum_denominator=1, source="fixture", window_start=_NOW, window_end=_NOW, unknown_count=2, sample_count=3)


def test_missing_human_gold_keeps_semantic_metrics_unavailable():
    metric = metric_from_counts(
        "candidate_precision", 0, 0, minimum_denominator=100,
        source="ai_exploratory_or_no_human_gold", window_start=_NOW, window_end=_NOW,
        unavailable_reason="independent_human_gold_required",
    )
    assert metric.status == "unavailable"
    assert metric.unavailable_reason == "independent_human_gold_required"
    assert metric.ci95_low is None and metric.ci95_high is None


def test_candidate_to_retrieval_provenance_closes():
    evidence = _workflow_evidence()
    assert workflow_evidence_complete(evidence)


def test_rule_quorum_identity_is_distinct_from_human_admission():
    evidence = _workflow_evidence()
    evidence["reviewer_identity"] = "rule:review-quorum-v2"
    assert workflow_evidence_complete(evidence)


def test_list_retrieval_provenance_requires_complete_identity():
    evidence = _workflow_evidence()
    evidence.update({"asset_revision": 1, "provenance_hash": "c" * 64})
    evidence["retrieval_provenance"] = [{
        "candidate_id": "candidate-1", "candidate_revision": 1,
        "asset_id": "asset-1", "asset_revision": 1, "generation": 1,
        "admission_revision": 1, "canonical_memory_id": "canonical-1",
        "provenance_hash": "c" * 64, "source_evidence_ids": ["row:1"],
    }]
    assert workflow_evidence_complete(evidence)
    required = (
        "candidate_id", "candidate_revision", "asset_id", "asset_revision",
        "generation", "admission_revision", "canonical_memory_id",
        "provenance_hash", "source_evidence_ids",
    )
    for field in required:
        broken = dict(evidence)
        broken["retrieval_provenance"] = [dict(evidence["retrieval_provenance"][0])]
        broken["retrieval_provenance"][0].pop(field)
        assert not workflow_evidence_complete(broken), field


def test_admission_revision_mismatch_fails_closed():
    evidence = _workflow_evidence()
    evidence["admission_candidate_revision"] = 2
    assert not workflow_evidence_complete(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reviewer_identity", "model:reviewer"),
        ("review_status", "pending"),
        ("admission_revision", None),
        ("publish_proof_digest", "invalid"),
        ("asset_id", None),
        ("index_membership_id", None),
        ("retrieval_event_id", None),
        ("retrieval_generation", 2),
    ],
)
def test_workflow_evidence_required_fields_fail_closed(field, value):
    evidence = _workflow_evidence()
    if value is None:
        evidence.pop(field)
    else:
        evidence[field] = value
    result = evaluate_workflow_evidence(evidence)
    assert result.complete is False
    assert result.status == "blocked"
    assert result.failure_kind == "workflow_evidence_incomplete"


def test_workflow_evidence_requires_provenance_match():
    evidence = _workflow_evidence()
    evidence["retrieval_provenance"] = {"candidate_id": "other-candidate"}
    result = evaluate_workflow_evidence(evidence)
    assert result.complete is False
    assert "retrieval_candidate_id_mismatch" in result.conflicts


def _assert_workflow_field_blocked(field: str, value: object = None) -> None:
    evidence = _workflow_evidence()
    if value is None:
        evidence.pop(field, None)
    else:
        evidence[field] = value
    assert not evaluate_workflow_evidence(evidence).complete


def test_workflow_evidence_requires_human_reviewer_identity():
    _assert_workflow_field_blocked("reviewer_identity", "model:reviewer")


def test_workflow_evidence_requires_review_status():
    _assert_workflow_field_blocked("review_status", "pending")


def test_workflow_evidence_requires_admission_revision():
    _assert_workflow_field_blocked("admission_revision")


def test_workflow_evidence_requires_publish_proof():
    _assert_workflow_field_blocked("publish_proof_digest", "not-a-digest")


def test_workflow_evidence_requires_asset_and_index_identity():
    _assert_workflow_field_blocked("asset_id")
    _assert_workflow_field_blocked("index_membership_id")


def test_workflow_evidence_requires_retrieval_event():
    _assert_workflow_field_blocked("retrieval_event_id")


def test_workflow_evidence_requires_current_generation():
    _assert_workflow_field_blocked("retrieval_generation", 2)


def test_workflow_evidence_rejects_revision_mismatch():
    evidence = _workflow_evidence()
    evidence["review_candidate_revision"] = 2
    assert not workflow_evidence_complete(evidence)


def test_workflow_evidence_rejects_provenance_mismatch():
    evidence = _workflow_evidence()
    evidence["retrieval_provenance"] = {"source_evidence_ids": ["row:999"]}
    assert not workflow_evidence_complete(evidence)


def test_stage10_readiness_returns_structured_blocked_result():
    metric = metric_from_counts(
        "candidate_precision", 0, 0, minimum_denominator=100,
        source="ai_exploratory_or_no_human_gold", window_start=_NOW, window_end=_NOW,
        unavailable_reason="independent_human_gold_required",
    )
    readiness = evaluate_stage10_readiness(
        discovery_safety={"deterministic_replay": "passed", "provider_calls": 0, "network_calls": 0},
        workflow_evidence=(), semantic_metrics=(metric,), human_gold_status="pending",
    )
    assert readiness.discovery_replay_status == "passed"
    assert readiness.workflow_evidence_status == "incomplete"
    assert readiness.human_admission_status == "missing"
    assert readiness.business_gate_eligible is False
    assert readiness.stage10_authorized is False
    assert "workflow_evidence_incomplete" in readiness.blocked_reasons
    assert "human_gold_unavailable" not in readiness.blocked_reasons


def test_stage10_readiness_does_not_require_human_gold_for_discovery_safety():
    readiness = evaluate_stage10_readiness(
        discovery_safety={"deterministic_replay": "passed", "provider_calls": 0, "network_calls": 0},
        workflow_evidence=(), semantic_metrics=(), human_gold_status="pending",
    )
    assert readiness.discovery_replay_status == "passed"
    assert readiness.semantic_quality_status == "unavailable_without_human_gold"
    assert readiness.stage10_authorized is False


def test_stage10_readiness_never_uses_ai_exploratory_as_gate():
    readiness = evaluate_stage10_readiness(
        discovery_safety={"deterministic_replay": "passed", "provider_calls": 0, "network_calls": 0},
        workflow_evidence=(_workflow_evidence(),), semantic_metrics=(), human_gold_status="ai_exploratory",
    )
    assert readiness.business_gate_eligible is True
    assert readiness.stage10_authorized is True


def test_full_learning_pipeline_requires_workflow_evidence():
    metric = metric_from_counts(
        "candidate_precision", 90, 100, minimum_denominator=100,
        source="human_gold", window_start=_NOW, window_end=_NOW,
    )
    readiness = evaluate_stage10_readiness(
        discovery_safety={"deterministic_replay": "passed", "provider_calls": 0, "network_calls": 0},
        workflow_evidence=(_workflow_evidence(),), semantic_metrics=(metric,), human_gold_status="complete",
    )
    assert readiness.workflow_evidence_status == "complete"
    assert readiness.human_admission_status == "complete"
    assert readiness.stage10_authorized is True


@pytest.mark.parametrize(
    "failure_field",
    [
        "recording_conflicts", "pipeline_failure", "snapshot_integrity_failure",
        "migration_failure", "workflow_rebuild_failure",
    ],
)
def test_stage10_readiness_is_bound_to_current_replay_failures(failure_field):
    readiness = evaluate_stage10_readiness(
        discovery_safety={
            "deterministic_replay": "passed", "provider_calls": 0, "network_calls": 0,
            "replay_status": "partial", failure_field: True,
        },
        workflow_evidence=(_workflow_evidence(),), semantic_metrics=(), human_gold_status="pending",
    )
    assert readiness.stage10_authorized is False
    assert readiness.business_gate_eligible is False
    assert failure_field in readiness.blocked_reasons
