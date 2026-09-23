from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Iterable, Mapping


_SHA256 = re.compile(r"^(?:sha256(?::v1)?:)?[0-9a-fA-F]{64}$")
_UTC_SUFFIX = re.compile(r"(?:Z|[+-]00:00)$")


def _strict_non_negative(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a strict non-negative integer")
    return value


def _sha256(value: object, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be a SHA-256 string")
    return value


def _utc_iso(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be an UTC ISO-8601 string")
    text = value.strip()
    if not _UTC_SUFFIX.search(text):
        raise ValueError(f"{name} must include UTC timezone")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{name} must be UTC")
    return text


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationUnit:
    sample_id: str
    source_row_ids: tuple[int, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    identity_source: str = "unknown"
    candidate_id: str | None = None
    candidate_family: str = "uncertain"
    scope_id: str = "unknown"
    speaker_id: str = "unknown"
    evidence_quality: str = "unknown"
    source_type: str = "unknown"
    pipeline_version: str = ""
    extractor_version: str = ""
    snapshot_hash: str = ""
    primary_stratum: str = "legacy relation-poor"
    secondary_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.sample_id or not re.fullmatch(r"[0-9a-fA-F]{64}", self.sample_id):
            raise ValueError("sample_id must be a SHA-256 hex digest")
        if any(type(item) is not int or item <= 0 for item in self.source_row_ids):
            raise ValueError("source_row_ids must contain strict positive integers")
        if self.candidate_family not in {"expression", "jargon", "none", "uncertain"}:
            raise ValueError("unknown candidate_family")
        if self.evidence_quality not in {"high", "medium", "low", "unknown"}:
            raise ValueError("unknown evidence_quality")
        _sha256(self.snapshot_hash, "snapshot_hash")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source_row_ids"] = list(self.source_row_ids)
        value["source_message_ids"] = list(self.source_message_ids)
        value["secondary_tags"] = list(self.secondary_tags)
        return value


@dataclass(frozen=True, slots=True)
class GoldLabel:
    sample_id: str
    is_valid_candidate: str
    candidate_family: str
    attribution_scope: str
    evidence_quality: str
    definition_supported: str
    safety: str
    duplicate_group_id: str | None
    annotator_id: str
    rubric_version: str = "gold-v1"
    reason_codes: tuple[str, ...] = ()
    label_timestamp: str = ""

    def __post_init__(self) -> None:
        _sha256(self.sample_id, "sample_id")
        if self.is_valid_candidate not in {"yes", "no", "uncertain"}:
            raise ValueError("unknown validity label")
        if self.candidate_family not in {"expression", "jargon", "none", "uncertain"}:
            raise ValueError("unknown candidate family")
        if self.attribution_scope not in {"speaker_in_group", "speaker", "group", "pairwise_interaction", "topic", "global", "unknown"}:
            raise ValueError("unknown attribution scope")
        if self.evidence_quality not in {"high", "medium", "low", "unknown"}:
            raise ValueError("unknown evidence quality")
        if self.definition_supported not in {"yes", "no", "uncertain"} or self.safety not in {"allow", "reject", "uncertain"}:
            raise ValueError("unknown gold label enum")
        if not self.annotator_id or not self.label_timestamp:
            raise ValueError("annotator_id and label_timestamp are required")
        _utc_iso(self.label_timestamp, "label_timestamp")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["reason_codes"] = list(self.reason_codes)
        return value


@dataclass(frozen=True, slots=True)
class GoldSetManifest:
    seed: int
    target_size: int
    units: tuple[EvaluationUnit, ...]
    strata: Mapping[str, Mapping[str, int]]
    unavailable_strata: tuple[str, ...]
    snapshot_hash: str
    pipeline_version: str
    extractor_version: str
    manifest_hash: str = ""
    manifest_kind: str = "exploratory_sample"
    annotation_status: str = "not_collected"
    human_gold_status: str = "not_required_for_discovery"
    business_gate_eligible: bool = False

    def __post_init__(self) -> None:
        _strict_non_negative(self.seed, "seed")
        _strict_non_negative(self.target_size, "target_size")
        _sha256(self.snapshot_hash, "snapshot_hash")
        if self.manifest_kind not in {"exploratory_sample", "human_gold"}:
            raise ValueError("unknown manifest_kind")
        if self.annotation_status not in {"ai_exploratory", "not_collected", "human_gold"}:
            raise ValueError("unknown annotation_status")
        if self.human_gold_status not in {"not_required_for_discovery", "pending", "complete"}:
            raise ValueError("unknown human_gold_status")
        if type(self.business_gate_eligible) is not bool:
            raise ValueError("business_gate_eligible must be bool")
        if self.annotation_status == "human_gold" and self.human_gold_status != "complete":
            raise ValueError("human_gold annotation requires complete human_gold_status")
        if self.annotation_status != "human_gold" and self.business_gate_eligible:
            raise ValueError("exploratory manifests cannot be business-gate eligible")
        computed = canonical_hash({
            "seed": self.seed,
            "target_size": self.target_size,
            "units": [item.to_dict() for item in self.units],
            "strata": self.strata,
            "unavailable_strata": list(self.unavailable_strata),
            "snapshot_hash": self.snapshot_hash,
            "pipeline_version": self.pipeline_version,
            "extractor_version": self.extractor_version,
            "manifest_kind": self.manifest_kind,
            "annotation_status": self.annotation_status,
            "human_gold_status": self.human_gold_status,
            "business_gate_eligible": self.business_gate_eligible,
        })
        if self.manifest_hash and self.manifest_hash != computed:
            raise ValueError("manifest_hash does not match manifest content")
        object.__setattr__(self, "manifest_hash", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "target_size": self.target_size,
            "units": [item.to_dict() for item in self.units],
            "strata": {key: dict(value) for key, value in sorted(self.strata.items())},
            "unavailable_strata": list(self.unavailable_strata),
            "snapshot_hash": self.snapshot_hash,
            "pipeline_version": self.pipeline_version,
            "extractor_version": self.extractor_version,
            "manifest_kind": self.manifest_kind,
            "annotation_status": self.annotation_status,
            "human_gold_status": self.human_gold_status,
            "business_gate_eligible": self.business_gate_eligible,
            "manifest_hash": self.manifest_hash,
        }


@dataclass(frozen=True, slots=True)
class ReplayManifest:
    run_id: str
    mode: str
    source_snapshot_hash: str
    source_path_is_read_only: bool
    output_root: str
    source_schema_version: int
    execution_schema_version: int
    migration_path: tuple[str, ...]
    pipeline_version: str
    extractor_version: str
    fingerprint_version: str
    prompt_version: str
    provider_id: str
    model_id: str
    request_fixture_hash: str | None
    seed: int
    network_policy: str
    sample_manifest_hash: str
    created_at: str
    result_hash: str | None = None
    source_snapshot_path: str | None = None
    sample_manifest_path: str | None = None
    recordings_path: str | None = None
    provider_allowlist: tuple[str, ...] = ()
    memory_v2_snapshot_hash: str | None = None
    memory_v2_snapshot_path: str | None = None
    memory_v2_schema_version: int | None = None

    VALID_MODES: ClassVar[frozenset[str]] = frozenset({"deterministic", "recorded", "staging"})

    def __post_init__(self) -> None:
        if self.mode not in self.VALID_MODES:
            raise ValueError("unknown replay mode")
        # Staging remains a valid manifest value so the runner can return a
        # structured blocked result (and the CLI can use its documented exit
        # code) without executing any staging work.
        if not self.source_path_is_read_only:
            raise ValueError("source_path_is_read_only must be true")
        for name in ("source_snapshot_hash", "sample_manifest_hash"):
            _sha256(getattr(self, name), name)
        _sha256(self.request_fixture_hash, "request_fixture_hash", nullable=True)
        _sha256(self.memory_v2_snapshot_hash, "memory_v2_snapshot_hash", nullable=True)
        if self.memory_v2_schema_version is not None:
            _strict_non_negative(self.memory_v2_schema_version, "memory_v2_schema_version")
        if self.result_hash is not None:
            _sha256(self.result_hash, "result_hash")
        _strict_non_negative(self.seed, "seed")
        _strict_non_negative(self.source_schema_version, "source_schema_version")
        _strict_non_negative(self.execution_schema_version, "execution_schema_version")
        if self.network_policy not in {"loopback-only", "staging-allowlist"}:
            raise ValueError("unknown network policy")
        _utc_iso(self.created_at, "created_at")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["migration_path"] = list(self.migration_path)
        value["provider_allowlist"] = list(self.provider_allowlist)
        return value


@dataclass(frozen=True, slots=True)
class MetricResult:
    metric_name: str
    status: str
    source: str
    window_start: str
    window_end: str
    numerator: int
    denominator: int
    unknown_count: int
    sample_count: int
    warnings: tuple[str, ...] = ()
    ci95_low: float | None = None
    ci95_high: float | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "partial", "unavailable", "blocked", "failed"}:
            raise ValueError("unknown metric status")
        for name in ("numerator", "denominator", "unknown_count", "sample_count"):
            _strict_non_negative(getattr(self, name), name)
        if self.numerator > self.denominator and self.denominator >= 0:
            raise ValueError("numerator cannot exceed denominator")
        if self.sample_count < self.denominator:
            raise ValueError("sample_count cannot be less than denominator")
        if self.unknown_count > self.sample_count - self.denominator:
            raise ValueError("unknown_count exceeds excluded sample count")
        _utc_iso(self.window_start, "window_start")
        _utc_iso(self.window_end, "window_end")
        if self.ci95_low is not None and not math.isfinite(self.ci95_low):
            raise ValueError("ci95_low must be finite")
        if self.ci95_high is not None and not math.isfinite(self.ci95_high):
            raise ValueError("ci95_high must be finite")
        if self.status == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable metrics require unavailable_reason")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    status: str
    provider_calls: int
    source_integrity_before: Mapping[str, Any]
    source_integrity_after: Mapping[str, Any]
    artifacts: Mapping[str, str] = field(default_factory=dict)
    funnel: Mapping[str, Any] = field(default_factory=dict)
    metrics: tuple[MetricResult, ...] = ()
    unknowns: tuple[Mapping[str, Any], ...] = ()
    blocked_reasons: tuple[str, ...] = ()
    result_hash: str | None = None
    memory_v2_integrity_before: Mapping[str, Any] = field(default_factory=dict)
    memory_v2_integrity_after: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"completed", "partial", "unavailable", "blocked", "failed"}:
            raise ValueError("unknown replay result status")
        _strict_non_negative(self.provider_calls, "provider_calls")
        if self.result_hash is not None:
            _sha256(self.result_hash, "result_hash")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "provider_calls": self.provider_calls,
            "source_integrity_before": dict(self.source_integrity_before),
            "source_integrity_after": dict(self.source_integrity_after),
            "artifacts": dict(self.artifacts),
            "funnel": dict(self.funnel),
            "metrics": [item.to_dict() for item in self.metrics],
            "unknowns": [dict(item) for item in self.unknowns],
            "blocked_reasons": list(self.blocked_reasons),
            "memory_v2_integrity_before": dict(self.memory_v2_integrity_before),
            "memory_v2_integrity_after": dict(self.memory_v2_integrity_after),
            "result_hash": self.result_hash,
        }


def wilson_interval(numerator: int, denominator: int, z: float = 1.96) -> tuple[float | None, float | None]:
    _strict_non_negative(numerator, "numerator")
    _strict_non_negative(denominator, "denominator")
    if denominator == 0:
        return None, None
    if numerator > denominator or not math.isfinite(z) or z <= 0:
        raise ValueError("invalid Wilson interval input")
    p = numerator / denominator
    denominator_term = 1 + z * z / denominator
    centre = (p + z * z / (2 * denominator)) / denominator_term
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * denominator)) / denominator) / denominator_term
    return max(0.0, centre - spread), min(1.0, centre + spread)


def metric_from_counts(
    metric_name: str,
    numerator: int,
    denominator: int,
    *,
    minimum_denominator: int,
    source: str,
    window_start: str,
    window_end: str,
    unknown_count: int = 0,
    sample_count: int | None = None,
    warnings: tuple[str, ...] = (),
    unavailable_reason: str | None = None,
) -> MetricResult:
    _strict_non_negative(minimum_denominator, "minimum_denominator")
    if denominator < minimum_denominator:
        status = "unavailable"
        low = high = None
    else:
        low, high = wilson_interval(numerator, denominator)
        status = "partial" if unknown_count else "completed"
    return MetricResult(
        metric_name=metric_name,
        status=status,
        source=source,
        window_start=window_start,
        window_end=window_end,
        numerator=numerator,
        denominator=denominator,
        unknown_count=unknown_count,
        sample_count=denominator + unknown_count if sample_count is None else sample_count,
        warnings=warnings + (("minimum_denominator_not_met",) if denominator < minimum_denominator else ()),
        ci95_low=low,
        ci95_high=high,
        unavailable_reason=(unavailable_reason or "minimum_denominator_not_met") if status == "unavailable" else None,
    )


def cohen_kappa(left: list[object] | tuple[object, ...], right: list[object] | tuple[object, ...]) -> float | None:
    """Compute binary Cohen kappa while excluding explicit unknown values."""
    if len(left) != len(right):
        raise ValueError("annotation vectors must have equal length")
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None and a != "unknown" and b != "unknown"]
    if not pairs:
        return None
    observed = sum(a == b for a, b in pairs) / len(pairs)
    categories = sorted({value for pair in pairs for value in pair}, key=str)
    expected = sum(
        (sum(pair[0] == category for pair in pairs) / len(pairs))
        * (sum(pair[1] == category for pair in pairs) / len(pairs))
        for category in categories
    )
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def annotation_status(kappa: float | None) -> str:
    """Map the stage-09 agreement gate to an auditable status."""
    if kappa is None or not math.isfinite(kappa) or kappa < 0.67:
        return "blocked"
    if kappa < 0.80:
        return "conditional"
    return "completed"


@dataclass(frozen=True, slots=True)
class WorkflowEvidenceResult:
    complete: bool
    status: str
    missing_fields: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    failure_kind: str = ""
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Stage10Readiness:
    discovery_replay_status: str
    semantic_quality_status: str
    workflow_evidence_status: str
    human_admission_status: str
    business_gate_eligible: bool
    stage10_authorized: bool
    blocked_reasons: tuple[str, ...] = ()
    missing_evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _first_value(evidence: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = evidence.get(name)
        if value not in (None, "", (), [], {}):
            return value
    return None


def _human_identity(value: Any) -> bool:
    if isinstance(value, Mapping):
        return str(value.get("kind") or "").lower() == "human" and bool(str(value.get("id") or "").strip())
    text = str(value or "").strip().lower()
    return text.startswith("human:") and len(text) > len("human:")


def _reviewer_identity(value: Any) -> bool:
    if isinstance(value, Mapping):
        kind = str(value.get("kind") or "").lower()
        return kind in {"human", "rule"} and bool(str(value.get("id") or "").strip())
    text = str(value or "").strip().lower()
    return (text.startswith("human:") or text.startswith("rule:")) and len(text.split(":", 1)[-1]) > 0


def evaluate_workflow_evidence(evidence: Mapping[str, Any]) -> WorkflowEvidenceResult:
    """Validate durable stage-10 provenance without mutating business state."""
    if not isinstance(evidence, Mapping):
        return WorkflowEvidenceResult(False, "blocked", failure_kind="evidence_not_mapping")
    missing: list[str] = []
    conflicts: list[str] = []

    candidate_id = _first_value(evidence, "candidate_id")
    candidate_revision = _first_value(evidence, "candidate_revision")
    source_ids = _first_value(evidence, "source_evidence_ids")
    review_id = _first_value(evidence, "review_id", "review_revision")
    review_revision = _first_value(evidence, "review_revision")
    review_candidate_revision = _first_value(evidence, "review_candidate_revision")
    review_status = _first_value(evidence, "review_status", "effective_review_decision", "review_decision")
    review_identity = _first_value(evidence, "reviewer_identity", "review_reviewer_identity")
    admission_id = _first_value(evidence, "human_admission_id", "admission_id", "admission_revision")
    admission_revision = _first_value(evidence, "admission_revision")
    admission_candidate_revision = _first_value(evidence, "admission_candidate_revision")
    admission_decision = _first_value(evidence, "human_admission_decision", "admission_decision")
    admission_identity = _first_value(evidence, "admission_reviewer_identity", "human_reviewer_identity")
    publish_proof = _first_value(evidence, "publish_proof_digest")
    asset_id = _first_value(evidence, "asset_id")
    asset_revision = _first_value(evidence, "asset_revision")
    canonical_id = _first_value(evidence, "canonical_memory_id", "canonical_asset_id")
    index_id = _first_value(evidence, "index_membership_id", "membership_id", "vector_id")
    generation = _first_value(evidence, "generation", "current_generation")
    retrieval_id = _first_value(evidence, "retrieval_event_id", "event_id")
    retrieval_generation = _first_value(evidence, "retrieval_generation")
    retrieval_provenance = _first_value(evidence, "retrieval_provenance_digest", "retrieval_evidence_digest")

    required_values = {
        "candidate_id": candidate_id, "candidate_revision": candidate_revision,
        "source_evidence_ids": source_ids, "review_id_or_revision": review_id,
        "review_revision": review_revision, "review_candidate_revision": review_candidate_revision,
        "review_status": review_status,
        "reviewer_identity": review_identity, "human_admission_id_or_revision": admission_id,
        "admission_revision": admission_revision, "admission_candidate_revision": admission_candidate_revision,
        "human_admission_decision": admission_decision,
        "admission_reviewer_identity": admission_identity,
        "publish_proof_digest": publish_proof, "asset_id": asset_id,
        "canonical_memory_id": canonical_id, "index_membership_id": index_id,
        "generation": generation, "retrieval_event_id": retrieval_id,
        "retrieval_generation": retrieval_generation,
        "retrieval_provenance_digest": retrieval_provenance,
    }
    missing.extend(name for name, value in required_values.items() if value in (None, "", (), [], {}))
    revisions = {
        "candidate_revision": candidate_revision,
        "review_revision": review_revision,
        "admission_revision": admission_revision,
        "review_candidate_revision": review_candidate_revision,
        "admission_candidate_revision": admission_candidate_revision,
        "generation": generation,
        "retrieval_generation": retrieval_generation,
    }
    for name, value in revisions.items():
        if value is not None and (isinstance(value, bool) or type(value) is not int or value <= 0):
            conflicts.append(f"{name}_invalid")
    if not missing and candidate_revision != review_candidate_revision:
        conflicts.append("review_candidate_revision_mismatch")
    if not missing and candidate_revision != admission_candidate_revision:
        conflicts.append("admission_candidate_revision_mismatch")
    if not missing and generation != retrieval_generation:
        conflicts.append("retrieval_generation_mismatch")
    if not isinstance(source_ids, (list, tuple)) or not source_ids or any(
        not isinstance(item, str)
        or not item.strip()
        or not item.startswith(("row:", "event_id:", "platform_message_id:"))
        or "fallback" in item.lower()
        or "synthetic" in item.lower()
        for item in (source_ids or ())
    ):
        conflicts.append("source_evidence_ids_invalid")
    if not _reviewer_identity(review_identity):
        conflicts.append("reviewer_identity_invalid")
    if not _human_identity(admission_identity):
        conflicts.append("admission_reviewer_identity_not_human")
    if str(review_status or "").lower() not in {"completed", "approved", "effective", "accepted"}:
        conflicts.append("review_status_invalid")
    if str(admission_decision or "").lower() not in {"approved", "admitted"}:
        conflicts.append("human_admission_decision_invalid")
    if not isinstance(publish_proof, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", publish_proof):
        conflicts.append("publish_proof_digest_invalid")
    if not all(isinstance(value, str) and value.strip() for value in (asset_id, canonical_id, index_id, retrieval_id)):
        conflicts.append("provenance_identity_invalid")
    if not isinstance(retrieval_provenance, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", retrieval_provenance):
        conflicts.append("retrieval_provenance_digest_invalid")
    provenance = evidence.get("retrieval_provenance")
    if provenance is not None:
        if isinstance(provenance, Mapping):
            for name, expected in (("candidate_id", candidate_id), ("asset_id", asset_id), ("canonical_memory_id", canonical_id)):
                if name in provenance and provenance.get(name) != expected:
                    conflicts.append(f"retrieval_{name}_mismatch")
            if "source_evidence_ids" in provenance and set(provenance.get("source_evidence_ids") or ()) != set(source_ids or ()):
                conflicts.append("retrieval_source_evidence_mismatch")
        elif isinstance(provenance, (list, tuple)):
            matching = [
                item for item in provenance
                if isinstance(item, Mapping) and item.get("asset_id") == asset_id
            ]
            if len(matching) != 1:
                conflicts.append("retrieval_asset_provenance_invalid")
            else:
                item = matching[0]
                expected_fields = (
                    ("candidate_id", candidate_id),
                    ("candidate_revision", candidate_revision),
                    ("asset_id", asset_id),
                    ("asset_revision", asset_revision),
                    ("generation", generation),
                    ("admission_revision", admission_revision),
                    ("canonical_memory_id", canonical_id),
                    ("source_evidence_ids", source_ids),
                )
                for name, expected in expected_fields:
                    value = item.get(name)
                    if value in (None, "", (), [], {}):
                        conflicts.append(f"retrieval_{name}_missing")
                    elif name == "source_evidence_ids":
                        if not isinstance(value, (list, tuple)) or tuple(sorted(str(entry) for entry in value)) != tuple(sorted(str(entry) for entry in (expected or ()) )):
                            conflicts.append("retrieval_source_evidence_mismatch")
                    elif value != expected:
                        conflicts.append(f"retrieval_{name}_mismatch")
                provenance_hash = item.get("provenance_hash")
                if not isinstance(provenance_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", provenance_hash):
                    conflicts.append("retrieval_provenance_hash_missing")
        else:
            conflicts.append("retrieval_provenance_invalid")
    if missing or conflicts:
        return WorkflowEvidenceResult(
            False, "blocked", tuple(sorted(set(missing))), tuple(sorted(set(conflicts))),
            failure_kind="workflow_evidence_incomplete", retryable=False,
        )
    return WorkflowEvidenceResult(True, "complete")


def workflow_evidence_complete(evidence: Mapping[str, Any]) -> bool:
    return evaluate_workflow_evidence(evidence).complete


def evaluate_stage10_readiness(
    *,
    discovery_safety: Mapping[str, Any],
    workflow_evidence: Iterable[Any],
    semantic_metrics: Iterable[MetricResult],
    human_gold_status: str,
    replay_safety: Mapping[str, Any] | None = None,
) -> Stage10Readiness:
    """Combine discovery safety and stage-10 evidence without side effects."""
    if not isinstance(discovery_safety, Mapping):
        discovery_safety = {}
    safety_status = str(discovery_safety.get("discovery_replay_status") or "").lower()
    if not safety_status:
        try:
            provider_calls = discovery_safety.get("provider_calls", 0)
            network_calls = discovery_safety.get("network_calls", 0)
            safe_counts = type(provider_calls) is int and type(network_calls) is int
        except AttributeError:
            safe_counts = False
            provider_calls = network_calls = 1
        safety_status = "passed" if (
            safe_counts
            and discovery_safety.get("deterministic_replay") in {"passed", "completed"}
            and provider_calls == 0 and network_calls == 0
        ) else "blocked"
    safety = dict(discovery_safety)
    if isinstance(replay_safety, Mapping):
        safety.update(replay_safety)
    replay_status = safety.get("replay_status")
    replay_blocked = tuple(safety.get("replay_blocked_reasons") or ())
    if replay_status is not None and str(replay_status).lower() not in {"completed"}:
        safety_status = "blocked"
    if replay_blocked:
        safety_status = "blocked"
    failure_flags = {
        "recording_conflicts": bool(safety.get("recording_conflicts")),
        "pipeline_failure": bool(safety.get("pipeline_failure")),
        "snapshot_integrity_failure": bool(safety.get("snapshot_integrity_failure")),
        "migration_failure": bool(safety.get("migration_failure")),
        "workflow_rebuild_failure": bool(safety.get("workflow_rebuild_failure")),
    }
    for name, failed in failure_flags.items():
        if failed:
            safety_status = "blocked"
    raw_evidence = tuple(workflow_evidence or ())
    evidence_results = tuple(
        evaluate_workflow_evidence(item) if isinstance(item, Mapping)
        else WorkflowEvidenceResult(False, "blocked", failure_kind="evidence_not_mapping")
        for item in raw_evidence
    )
    complete_workflow = bool(evidence_results) and all(item.complete for item in evidence_results)
    semantic = tuple(semantic_metrics)
    semantic_complete = human_gold_status == "complete" and bool(semantic) and all(item.status == "completed" for item in semantic)
    semantic_status = "completed" if semantic_complete else "unavailable_without_human_gold"
    reasons: list[str] = []
    missing: list[str] = []
    if safety_status not in {"passed", "partial"}:
        reasons.append("discovery_replay_not_passed")
    reasons.extend(name for name, failed in failure_flags.items() if failed)
    if not complete_workflow:
        reasons.append("workflow_evidence_incomplete")
        if not evidence_results:
            missing.append("workflow_evidence")
        for result in evidence_results:
            missing.extend(result.missing_fields)
            reasons.extend(result.conflicts)
    admission_status = "complete" if complete_workflow else ("partial" if evidence_results else "missing")
    # Human Gold gates semantic KPI interpretation, not the mechanical
    # admission/workflow gate for stage 10. Unavailable metrics remain
    # visible in the report and cannot be presented as quality evidence.
    authorized = not reasons
    return Stage10Readiness(
        discovery_replay_status=safety_status,
        semantic_quality_status=semantic_status,
        workflow_evidence_status="complete" if complete_workflow else "incomplete",
        human_admission_status=admission_status,
        business_gate_eligible=authorized,
        stage10_authorized=authorized,
        blocked_reasons=tuple(sorted(set(reasons))),
        missing_evidence=tuple(sorted(set(missing))),
    )
