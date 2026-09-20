from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Sequence

from ...infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from ..mining.diagnostics import LearningStageDiagnostic
from ..quality.contracts import CandidateQualityFeatures


_CANDIDATE_FAMILIES = frozenset({"expression", "jargon"})
_SOURCE_DISPOSITIONS = frozenset({"skipped", "no_candidate", "candidate_ids"})
_SETTLEMENT_STATUSES = frozenset(
    {
        "enriched",
        "rejected",
        "partial",
        "retry_wait",
        "quarantined",
        "blocked",
        "failed",
        "cancelled",
    }
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"value is not JSON-safe: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _merge_source_payload(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in incoming.items():
        current = merged.get(key)
        if isinstance(current, list) and isinstance(value, (list, tuple)):
            combined: list[Any] = []
            seen: set[str] = set()
            for item in [*current, *value]:
                marker = _canonical_json(item)
                if marker in seen:
                    continue
                seen.add(marker)
                combined.append(_json_value(item))
            merged[key] = combined
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)):
            merged[key] = max(current, value)
        elif key not in merged or current in (None, "", (), []):
            merged[key] = _json_value(value)
    return merged


def _source_key(*, source_row_id: int | None, source_message_id: str) -> str:
    if source_row_id is not None:
        return f"row:{int(source_row_id)}"
    message_id = str(source_message_id or "").strip()
    return f"message:{message_id}" if message_id else ""


@dataclass(frozen=True, slots=True)
class SourceDisposition:
    source_identity_type: str
    source_row_id: int | None = None
    source_message_id: str = ""
    identity_source: str = ""
    disposition: str = "candidate_ids"
    reason_code: str = ""
    candidate_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in _SOURCE_DISPOSITIONS:
            raise ValueError("invalid source disposition")
        if not self.source_key:
            raise ValueError("source disposition requires a durable source identity")
        candidate_ids = tuple(sorted({str(item).strip() for item in self.candidate_ids if str(item).strip()}))
        object.__setattr__(self, "candidate_ids", candidate_ids)
        if self.disposition == "candidate_ids" and not candidate_ids:
            raise ValueError("candidate disposition requires candidate_ids")
        if self.disposition != "candidate_ids" and candidate_ids:
            raise ValueError("non-candidate disposition cannot carry candidate_ids")
        if self.disposition in {"skipped", "no_candidate"} and not str(self.reason_code).strip():
            raise ValueError("skipped/no_candidate disposition requires reason_code")

    @property
    def source_key(self) -> str:
        return _source_key(
            source_row_id=self.source_row_id,
            source_message_id=self.source_message_id,
        )

    @classmethod
    def for_row(
        cls,
        source_row_id: int,
        *,
        disposition: str = "candidate_ids",
        reason_code: str = "",
        candidate_ids: Sequence[str] = (),
    ) -> SourceDisposition:
        return cls(
            source_identity_type="source_row_id",
            source_row_id=int(source_row_id),
            identity_source="messagelog.id",
            disposition=disposition,
            reason_code=reason_code,
            candidate_ids=tuple(candidate_ids),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_identity_type": self.source_identity_type,
            "source_row_id": self.source_row_id,
            "source_message_id": self.source_message_id,
            "identity_source": self.identity_source,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True, slots=True)
class LearningSourceBatch:
    batch_id: str
    pipeline_type: str
    scope_id: str
    cursor_before: int
    cursor_after: int
    source_ids: tuple[str, ...]
    source_ids_hash: str
    dispositions: tuple[SourceDisposition, ...] = ()
    source_count: int = 0
    created_at: float = 0.0
    status: str = "started"
    revision: int = 0

    @classmethod
    def build(
        cls,
        *,
        pipeline_type: str,
        scope_id: str,
        cursor_before: int,
        cursor_after: int,
        source_ids: Sequence[str],
        created_at: float,
    ) -> LearningSourceBatch:
        pipeline = str(pipeline_type or "").strip()
        scope = str(scope_id or "").strip()
        if pipeline not in _CANDIDATE_FAMILIES or not scope:
            raise ValueError("invalid source batch pipeline/scope")
        if int(cursor_after) < int(cursor_before):
            raise ValueError("cursor_after must not precede cursor_before")
        stable_ids = tuple(sorted({str(item).strip() for item in source_ids if str(item).strip()}))
        if not stable_ids or any(not item.startswith(("row:", "message:")) for item in stable_ids):
            raise ValueError("source_ids require durable row/message identities")
        ids_hash = _sha256({"source_ids": stable_ids, "version": 1})
        batch_id = f"batch:{_sha256({'pipeline_type': pipeline, 'scope_id': scope, 'cursor_before': int(cursor_before), 'cursor_after': int(cursor_after), 'source_ids': stable_ids, 'source_ids_hash': ids_hash, 'version': 1})}"
        return cls(
            batch_id=batch_id,
            pipeline_type=pipeline,
            scope_id=scope,
            cursor_before=int(cursor_before),
            cursor_after=int(cursor_after),
            source_ids=stable_ids,
            source_ids_hash=ids_hash,
            source_count=len(stable_ids),
            created_at=float(created_at),
        )


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    evidence_id: str
    candidate_id: str
    batch_id: str
    source_row_id: int | None = None
    source_message_id: str = ""
    event_id: str | None = None
    platform_message_id: str | None = None
    sender_id: str = ""
    scope_id: str = ""
    speaker_scope_id: str = ""
    pairwise_scope_id: str | None = None
    topic_epoch: int | None = None
    source_type: str = "unknown"
    evidence_quality: str = "unknown"
    attribution_confidence: float | None = None
    is_generated: bool = False
    eligible: bool = False
    eligibility_reason: str = ""
    identity_source: str = ""
    payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    created_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _mapping(self.payload))

    @classmethod
    def source(
        cls,
        *,
        candidate_id: str,
        batch_id: str,
        source_row_id: int | None = None,
        source_message_id: str = "",
        identity_source: str,
        sender_id: str = "",
        scope_id: str = "",
        speaker_scope_id: str = "",
        source_type: str = "unknown",
        evidence_quality: str = "unknown",
        eligible: bool,
        eligibility_reason: str,
        payload: Mapping[str, Any],
        created_at: float,
        event_id: str | None = None,
        platform_message_id: str | None = None,
        pairwise_scope_id: str | None = None,
        topic_epoch: int | None = None,
        attribution_confidence: float | None = None,
        is_generated: bool = False,
    ) -> CandidateEvidence:
        source_key = _source_key(
            source_row_id=source_row_id, source_message_id=source_message_id
        )
        if not source_key or not str(identity_source or "").strip():
            raise ValueError("source evidence requires identity and provenance")
        evidence_id = f"evidence:{_sha256({'candidate_id': candidate_id, 'source_identity': source_key, 'source_type': source_type, 'payload_hash': _sha256(payload), 'version': 1})}"
        return cls(
            evidence_id=evidence_id,
            candidate_id=str(candidate_id),
            batch_id=str(batch_id),
            source_row_id=source_row_id,
            source_message_id=str(source_message_id or ""),
            event_id=event_id,
            platform_message_id=platform_message_id,
            sender_id=str(sender_id or ""),
            scope_id=str(scope_id or ""),
            speaker_scope_id=str(speaker_scope_id or ""),
            pairwise_scope_id=pairwise_scope_id,
            topic_epoch=topic_epoch,
            source_type=str(source_type or "unknown"),
            evidence_quality=str(evidence_quality or "unknown"),
            attribution_confidence=attribution_confidence,
            is_generated=bool(is_generated),
            eligible=bool(eligible),
            eligibility_reason=str(eligibility_reason or ""),
            identity_source=str(identity_source),
            payload=_mapping(payload),
            created_at=float(created_at),
        )

    @classmethod
    def generated(
        cls,
        *,
        candidate_id: str,
        batch_id: str,
        payload: Mapping[str, Any],
        created_at: float,
    ) -> CandidateEvidence:
        evidence_id = f"evidence:{_sha256({'candidate_id': candidate_id, 'batch_id': batch_id, 'source_type': 'generated', 'payload_hash': _sha256(payload), 'version': 1})}"
        return cls(
            evidence_id=evidence_id,
            candidate_id=str(candidate_id),
            batch_id=str(batch_id),
            source_type="generated",
            evidence_quality="model",
            is_generated=True,
            eligible=False,
            eligibility_reason="generated_not_source_support",
            identity_source="model_generated",
            payload=_mapping(payload),
            created_at=float(created_at),
        )


@dataclass(frozen=True, slots=True)
class LearningCandidate:
    candidate_id: str
    first_discovered_batch_id: str
    scope_id: str
    candidate_family: str
    candidate_subtype: str
    speaker_id: str
    speaker_scope_id: str
    fingerprint: str
    fingerprint_version: int
    extractor_version: str
    source_message_start: int | None
    source_message_end: int | None
    evidence_quality: str
    source_payload: Mapping[str, Any]
    enrichment_payload: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    evidence: tuple[CandidateEvidence, ...] = ()
    status: str = "discovered"
    revision: int = 0
    attempt: int = 0
    lease_owner: str = ""
    lease_token: str = ""
    lease_until: float = 0.0
    next_retry_at: float = 0.0
    failure_stage: str = ""
    failure_kind: str = ""
    retryable: bool = False
    provider_id: str = ""
    model_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_payload", _mapping(self.source_payload))
        object.__setattr__(self, "enrichment_payload", _mapping(self.enrichment_payload))
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @classmethod
    def discovered(
        cls,
        *,
        first_discovered_batch_id: str,
        scope_id: str,
        candidate_family: str,
        candidate_subtype: str,
        speaker_id: str,
        speaker_scope_id: str,
        fingerprint: str,
        fingerprint_version: int,
        extractor_version: str,
        source_message_start: int | None,
        source_message_end: int | None,
        evidence_quality: str,
        source_payload: Mapping[str, Any],
        created_at: float,
    ) -> LearningCandidate:
        family = str(candidate_family or "").strip()
        scope = str(scope_id or "").strip()
        fingerprint_value = str(fingerprint or "").strip()
        if family not in _CANDIDATE_FAMILIES or not scope or not fingerprint_value:
            raise ValueError("invalid candidate identity")
        identity = {
            "scope_id": scope,
            "candidate_family": family,
            "speaker_id": str(speaker_id or ""),
            "fingerprint": fingerprint_value,
            "fingerprint_version": int(fingerprint_version),
            "version": 1,
        }
        payload = dict(source_payload or {})
        payload["candidate_subtype"] = str(candidate_subtype or "")
        return cls(
            candidate_id=f"candidate:{_sha256(identity)}",
            first_discovered_batch_id=str(first_discovered_batch_id),
            scope_id=scope,
            candidate_family=family,
            candidate_subtype=str(candidate_subtype or ""),
            speaker_id=str(speaker_id or ""),
            speaker_scope_id=str(speaker_scope_id or ""),
            fingerprint=fingerprint_value,
            fingerprint_version=int(fingerprint_version),
            extractor_version=str(extractor_version or ""),
            source_message_start=source_message_start,
            source_message_end=source_message_end,
            evidence_quality=str(evidence_quality or "unknown"),
            source_payload=_mapping(payload),
            created_at=float(created_at),
            updated_at=float(created_at),
        )


@dataclass(frozen=True, slots=True)
class SourceBatchWriteResult:
    batch_id: str
    inserted: bool = False
    idempotent: bool = False
    conflict: bool = False
    revision: int = 0


@dataclass(frozen=True, slots=True)
class SourceBatchSettlementResult:
    batch_id: str
    completed: bool = False
    idempotent: bool = False
    conflict: bool = False
    revision: int = 0
    diagnostics: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class CandidateWriteResult:
    candidate_id: str
    inserted: bool
    deduplicated: bool
    evidence_inserted: int
    revision: int
    status: str
    conflict: bool = False
    source: str = "candidate_ledger"
    diagnostics: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class WorkAttemptLease:
    attempt_id: str
    candidate_id: str
    run_id: str
    candidate_work_attempt: int
    owner: str
    lease_token: str
    lease_until: float
    expected_revision: int
    task_name: str
    scope_id: str
    candidate: LearningCandidate


@dataclass(frozen=True, slots=True)
class ProviderStartResult:
    applied: bool
    conflict: bool
    idempotent: bool
    attempt_id: str
    provider_attempt: int
    current_revision: int
    failure_stage: str = ""
    failure_kind: str = ""


@dataclass(frozen=True, slots=True)
class EnrichmentSettlement:
    status: str
    enrichment_payload: Mapping[str, Any]
    retryable: bool
    retry_at: float
    failure_stage: str
    failure_kind: str
    diagnostics: Mapping[str, Any]
    provider_id: str
    model_id: str
    canonical_ids: tuple[str, ...]
    generated_evidence: tuple[CandidateEvidence, ...]
    queue_wait_ms: float
    logical_queue_wait_ms: float
    runtime_budget_wait_ms: float
    gateway_background_wait_ms: float
    gateway_global_wait_ms: float
    provider_latency_ms: float
    elapsed_ms: float
    input_count: int
    output_count: int
    result_digest: str
    settlement_started_at: float
    finished_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "enrichment_payload", _mapping(self.enrichment_payload))
        object.__setattr__(self, "diagnostics", _mapping(self.diagnostics))
        object.__setattr__(self, "canonical_ids", tuple(self.canonical_ids))
        object.__setattr__(self, "generated_evidence", tuple(self.generated_evidence))

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "enrichment_payload": dict(self.enrichment_payload),
            "retryable": self.retryable,
            "retry_at": self.retry_at,
            "failure_stage": self.failure_stage,
            "failure_kind": self.failure_kind,
            "diagnostics": dict(self.diagnostics),
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "canonical_ids": list(self.canonical_ids),
            "generated_evidence_ids": [item.evidence_id for item in self.generated_evidence],
            "queue_wait_ms": self.queue_wait_ms,
            "logical_queue_wait_ms": self.logical_queue_wait_ms,
            "runtime_budget_wait_ms": self.runtime_budget_wait_ms,
            "gateway_background_wait_ms": self.gateway_background_wait_ms,
            "gateway_global_wait_ms": self.gateway_global_wait_ms,
            "provider_latency_ms": self.provider_latency_ms,
            "elapsed_ms": self.elapsed_ms,
            "input_count": self.input_count,
            "output_count": self.output_count,
            "settlement_started_at": self.settlement_started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def build(
        cls,
        *,
        status: str,
        enrichment_payload: Mapping[str, Any] | None = None,
        retryable: bool = False,
        retry_at: float = 0.0,
        failure_stage: str = "",
        failure_kind: str = "",
        diagnostics: Mapping[str, Any] | None = None,
        provider_id: str = "",
        model_id: str = "",
        canonical_ids: Sequence[str] = (),
        generated_evidence: Sequence[CandidateEvidence] = (),
        queue_wait_ms: float = 0.0,
        logical_queue_wait_ms: float = 0.0,
        runtime_budget_wait_ms: float = 0.0,
        gateway_background_wait_ms: float = 0.0,
        gateway_global_wait_ms: float = 0.0,
        provider_latency_ms: float = 0.0,
        elapsed_ms: float = 0.0,
        input_count: int = 0,
        output_count: int = 0,
        settlement_started_at: float,
        finished_at: float,
    ) -> EnrichmentSettlement:
        provisional = cls(
            status=str(status),
            enrichment_payload=_mapping(enrichment_payload),
            retryable=bool(retryable),
            retry_at=float(retry_at),
            failure_stage=str(failure_stage or ""),
            failure_kind=str(failure_kind or ""),
            diagnostics=_mapping(diagnostics),
            provider_id=str(provider_id or ""),
            model_id=str(model_id or ""),
            canonical_ids=tuple(str(item) for item in canonical_ids),
            generated_evidence=tuple(generated_evidence),
            queue_wait_ms=max(0.0, float(queue_wait_ms or 0.0)),
            logical_queue_wait_ms=max(0.0, float(logical_queue_wait_ms or 0.0)),
            runtime_budget_wait_ms=max(0.0, float(runtime_budget_wait_ms or 0.0)),
            gateway_background_wait_ms=max(0.0, float(gateway_background_wait_ms or 0.0)),
            gateway_global_wait_ms=max(0.0, float(gateway_global_wait_ms or 0.0)),
            provider_latency_ms=max(0.0, float(provider_latency_ms or 0.0)),
            elapsed_ms=max(0.0, float(elapsed_ms or 0.0)),
            input_count=max(0, int(input_count or 0)),
            output_count=max(0, int(output_count or 0)),
            result_digest="",
            settlement_started_at=float(settlement_started_at),
            finished_at=float(finished_at),
        )
        object.__setattr__(provisional, "result_digest", _sha256(provisional.payload()))
        return provisional


@dataclass(frozen=True, slots=True)
class CandidateSettlementResult:
    applied: bool
    conflict: bool
    idempotent: bool
    candidate: LearningCandidate | None
    result_digest: str
    failure_stage: str = ""
    failure_kind: str = ""


@dataclass(frozen=True, slots=True)
class CandidateAttemptRecord:
    attempt_id: str
    candidate_id: str
    run_id: str
    stage: str
    candidate_work_attempt: int
    provider_attempt: int
    revision: int
    owner: str
    lease_token: str
    lease_until: float
    task_name: str
    scope_id: str
    provider_request_started: bool
    status: str
    retry_at: float
    retryable: bool
    failure_stage: str
    failure_kind: str
    result_digest: str
    queue_wait_ms: float
    logical_queue_wait_ms: float
    runtime_budget_wait_ms: float
    gateway_background_wait_ms: float
    gateway_global_wait_ms: float
    provider_latency_ms: float
    elapsed_ms: float
    input_count: int
    output_count: int


@dataclass(frozen=True, slots=True)
class QualitySnapshotWriteResult:
    quality_id: str
    inserted: bool = False
    idempotent: bool = False
    conflict: bool = False
    failure_kind: str = ""


class CandidateLedger:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    async def schema_ready(self) -> bool:
        required_tables = {
            "learning_source_batch",
            "learning_candidate",
            "learning_candidate_evidence",
            "learning_candidate_attempt",
            "learning_stage_diagnostic",
            "learning_candidate_quality",
        }
        required_indexes = {
            "ix_learning_candidate_due",
            "ix_learning_candidate_scope",
            "ix_learning_candidate_fingerprint",
            "ix_learning_source_batch_prefix",
            "ix_learning_attempt_due",
            "ix_learning_diagnostic_run",
            "ix_learning_circuit_due",
            "ix_learning_quality_candidate",
        }
        async with self._db() as db:
            cursor = await db.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table','index')"
            )
            rows = await cursor.fetchall()
            await cursor.close()
        tables = {str(name) for name, kind in rows if kind == "table"}
        indexes = {str(name) for name, kind in rows if kind == "index"}
        return required_tables.issubset(tables) and required_indexes.issubset(indexes)

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[Any]:
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    _QUALITY_COLUMNS = """
        quality_id, candidate_id, candidate_revision, profile_version,
        profile_hash, window_start, window_end, eligible_message_count,
        unknown_message_count, support_count, speaker_support,
        speaker_message_count, group_support, group_message_count,
        other_support, other_total, distinct_turns, distinct_turn_count,
        distinct_day_count, context_diversity, g2, log2_effect,
        signed_log2_lift, p_value, fdr_q, pmi, left_entropy_bits,
        right_entropy_bits, burst_ratio, first_seen_at, last_seen_at,
        feature_complete, missing_reasons_json, confidence_tier,
        reasons_json, created_at
    """

    @staticmethod
    def _quality_values(features: CandidateQualityFeatures) -> tuple[Any, ...]:
        features.__post_init__()
        return (
            features.quality_id,
            features.candidate_id,
            features.candidate_revision,
            features.profile_version,
            features.profile_hash,
            features.window_start,
            features.window_end,
            features.eligible_message_count,
            features.unknown_message_count,
            features.support_count,
            features.speaker_support,
            features.speaker_message_count,
            features.group_support,
            features.group_message_count,
            features.other_support,
            features.other_total,
            features.distinct_turns,
            features.distinct_turn_count,
            features.distinct_day_count,
            features.context_diversity,
            features.g2,
            features.log2_effect,
            features.signed_log2_lift,
            features.p_value,
            features.fdr_q,
            features.pmi,
            features.left_entropy_bits,
            features.right_entropy_bits,
            features.burst_ratio,
            features.first_seen_at,
            features.last_seen_at,
            int(features.feature_complete),
            _canonical_json(features.missing_reasons),
            features.confidence_tier,
            _canonical_json(features.reasons),
            features.created_at,
        )

    @staticmethod
    def _quality_from_row(row: Sequence[Any]) -> CandidateQualityFeatures:
        try:
            missing_reasons = json.loads(str(row[32]))
            reasons = json.loads(str(row[34]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_quality_snapshot_json") from exc
        if not isinstance(missing_reasons, list) or not isinstance(reasons, list):
            raise ValueError("invalid_quality_snapshot_json")
        return CandidateQualityFeatures(
            quality_id=str(row[0]),
            candidate_id=str(row[1]),
            candidate_revision=int(row[2]),
            profile_version=str(row[3]),
            profile_hash=str(row[4]),
            window_start=float(row[5]),
            window_end=float(row[6]),
            eligible_message_count=int(row[7]),
            unknown_message_count=int(row[8]),
            support_count=int(row[9]),
            speaker_support=None if row[10] is None else int(row[10]),
            speaker_message_count=None if row[11] is None else int(row[11]),
            group_support=int(row[12]),
            group_message_count=int(row[13]),
            other_support=None if row[14] is None else int(row[14]),
            other_total=None if row[15] is None else int(row[15]),
            distinct_turns=int(row[16]),
            distinct_turn_count=int(row[17]),
            distinct_day_count=int(row[18]),
            context_diversity=int(row[19]),
            g2=None if row[20] is None else float(row[20]),
            log2_effect=None if row[21] is None else float(row[21]),
            signed_log2_lift=None if row[22] is None else float(row[22]),
            p_value=None if row[23] is None else float(row[23]),
            fdr_q=None if row[24] is None else float(row[24]),
            pmi=None if row[25] is None else float(row[25]),
            left_entropy_bits=None if row[26] is None else float(row[26]),
            right_entropy_bits=None if row[27] is None else float(row[27]),
            burst_ratio=None if row[28] is None else float(row[28]),
            first_seen_at=None if row[29] is None else float(row[29]),
            last_seen_at=None if row[30] is None else float(row[30]),
            feature_complete=bool(row[31]),
            missing_reasons=tuple(str(item) for item in missing_reasons),
            confidence_tier=str(row[33]),
            reasons=tuple(str(item) for item in reasons),
            created_at=float(row[35]),
        )

    async def store_quality_snapshot(
        self, features: CandidateQualityFeatures
    ) -> QualitySnapshotWriteResult:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT revision FROM learning_candidate WHERE candidate_id = ?",
                    (features.candidate_id,),
                )
                candidate = await cursor.fetchone()
                await cursor.close()
                if candidate is None or int(candidate[0]) != features.candidate_revision:
                    await db.rollback()
                    return QualitySnapshotWriteResult(
                        quality_id=features.quality_id,
                        conflict=True,
                        failure_kind="candidate_revision_conflict",
                    )
                cursor = await db.execute(
                    f"""SELECT {self._QUALITY_COLUMNS}
                        FROM learning_candidate_quality
                        WHERE candidate_id = ? AND candidate_revision = ?
                          AND profile_version = ?""",
                    (
                        features.candidate_id,
                        features.candidate_revision,
                        features.profile_version,
                    ),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    try:
                        matches = self._quality_from_row(existing) == features
                    except ValueError:
                        matches = False
                    await db.rollback()
                    return QualitySnapshotWriteResult(
                        quality_id=features.quality_id,
                        idempotent=matches,
                        conflict=not matches,
                        failure_kind="" if matches else "quality_snapshot_conflict",
                    )
                await db.execute(
                    f"""INSERT INTO learning_candidate_quality({self._QUALITY_COLUMNS})
                        VALUES ({','.join('?' for _ in range(36))})""",
                    self._quality_values(features),
                )
                await db.commit()
                return QualitySnapshotWriteResult(
                    quality_id=features.quality_id,
                    inserted=True,
                )
            except sqlite3.IntegrityError:
                await db.rollback()
                return QualitySnapshotWriteResult(
                    quality_id=features.quality_id,
                    conflict=True,
                    failure_kind="quality_identity_conflict",
                )
            except BaseException:
                await db.rollback()
                raise

    async def load_quality_snapshot(
        self,
        candidate_id: str,
        candidate_revision: int,
        profile_version: str,
    ) -> CandidateQualityFeatures | None:
        async with self._db() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUALITY_COLUMNS}
                    FROM learning_candidate_quality
                    WHERE candidate_id = ? AND candidate_revision = ?
                      AND profile_version = ?""",
                (str(candidate_id), int(candidate_revision), str(profile_version)),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return None if row is None else self._quality_from_row(row)

    @staticmethod
    def _batch_identity(batch: LearningSourceBatch) -> tuple[Any, ...]:
        return (
            batch.pipeline_type,
            batch.scope_id,
            batch.cursor_before,
            batch.cursor_after,
            _canonical_json(batch.source_ids),
            batch.source_ids_hash,
            batch.source_count,
        )

    async def begin_source_batch(self, batch: LearningSourceBatch) -> SourceBatchWriteResult:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT pipeline_type, scope_id, cursor_before, cursor_after,
                           source_ids_json, source_ids_hash, source_count, revision
                    FROM learning_source_batch WHERE batch_id = ?
                    """,
                    (batch.batch_id,),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing is not None:
                    matches = tuple(existing[:7]) == self._batch_identity(batch)
                    await db.commit()
                    return SourceBatchWriteResult(
                        batch_id=batch.batch_id,
                        idempotent=matches,
                        conflict=not matches,
                        revision=int(existing[7]),
                    )
                cursor = await db.execute(
                    """
                    SELECT batch_id FROM learning_source_batch
                    WHERE pipeline_type = ? AND scope_id = ?
                      AND cursor_before = ? AND cursor_after = ?
                      AND source_ids_hash <> ?
                    LIMIT 1
                    """,
                    (
                        batch.pipeline_type,
                        batch.scope_id,
                        batch.cursor_before,
                        batch.cursor_after,
                        batch.source_ids_hash,
                    ),
                )
                range_conflict = await cursor.fetchone()
                await cursor.close()
                if range_conflict is not None:
                    await db.rollback()
                    return SourceBatchWriteResult(batch.batch_id, conflict=True)
                await db.execute(
                    """
                    INSERT INTO learning_source_batch(
                        batch_id, pipeline_type, scope_id, cursor_before, cursor_after,
                        source_ids_json, source_ids_hash, source_dispositions_json,
                        source_count, eligible_count, skipped_count, no_candidate_count,
                        candidate_count, status, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 0, 0, 0,
                              'started', 0, ?, ?)
                    """,
                    (
                        batch.batch_id,
                        batch.pipeline_type,
                        batch.scope_id,
                        batch.cursor_before,
                        batch.cursor_after,
                        _canonical_json(batch.source_ids),
                        batch.source_ids_hash,
                        batch.source_count,
                        batch.source_count,
                        batch.created_at,
                        batch.created_at,
                    ),
                )
                await db.commit()
                return SourceBatchWriteResult(batch.batch_id, inserted=True)
            except Exception:
                await db.rollback()
                raise

    @staticmethod
    def _candidate_identity(candidate: LearningCandidate) -> tuple[Any, ...]:
        return (
            candidate.scope_id,
            candidate.candidate_family,
            candidate.speaker_id,
            candidate.fingerprint,
            candidate.fingerprint_version,
        )

    @staticmethod
    def _evidence_values(item: CandidateEvidence) -> tuple[Any, ...]:
        payload = dict(item.payload)
        if item.identity_source:
            payload.setdefault("identity_source", item.identity_source)
        return (
            item.candidate_id,
            item.evidence_id,
            item.batch_id,
            item.source_row_id,
            item.source_message_id,
            item.event_id,
            item.platform_message_id,
            item.sender_id,
            item.scope_id,
            item.speaker_scope_id,
            item.pairwise_scope_id,
            item.topic_epoch,
            item.source_type,
            item.evidence_quality,
            item.attribution_confidence,
            int(item.is_generated),
            int(item.eligible),
            item.eligibility_reason,
            _canonical_json(payload),
            item.created_at,
        )

    async def _insert_evidence(self, db, item: CandidateEvidence) -> tuple[bool, bool]:
        cursor = await db.execute(
            """
            SELECT candidate_id, evidence_id, batch_id, source_row_id,
                   source_message_id, event_id, platform_message_id, sender_id,
                   scope_id, speaker_scope_id, pairwise_scope_id, topic_epoch,
                   source_type, evidence_quality, attribution_confidence,
                   is_generated, eligible, eligibility_reason, payload_json, created_at
            FROM learning_candidate_evidence
            WHERE candidate_id = ? AND evidence_id = ?
            """,
            (item.candidate_id, item.evidence_id),
        )
        existing = await cursor.fetchone()
        await cursor.close()
        values = self._evidence_values(item)
        if existing is not None:
            return False, tuple(existing) != values
        await db.execute(
            """
            INSERT INTO learning_candidate_evidence(
                candidate_id, evidence_id, batch_id, source_row_id,
                source_message_id, event_id, platform_message_id, sender_id,
                scope_id, speaker_scope_id, pairwise_scope_id, topic_epoch,
                source_type, evidence_quality, attribution_confidence,
                is_generated, eligible, eligibility_reason, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return True, False

    @staticmethod
    def _source_evidence_complete(evidence: Sequence[CandidateEvidence]) -> bool:
        return any(
            item.eligible
            and not item.is_generated
            and bool(_source_key(source_row_id=item.source_row_id, source_message_id=item.source_message_id))
            and bool(item.identity_source)
            for item in evidence
        )

    async def upsert_discovered(self, candidate: LearningCandidate) -> CandidateWriteResult:
        if candidate.candidate_family not in _CANDIDATE_FAMILIES:
            raise ValueError("invalid candidate family")
        if any(item.candidate_id != candidate.candidate_id for item in candidate.evidence):
            raise ValueError("evidence candidate identity mismatch")
        complete = self._source_evidence_complete(candidate.evidence)
        target_status = "enrichment_pending" if complete else "blocked"
        diagnostics = {} if complete else {"failure_kind": "source_evidence_incomplete"}
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT candidate_id, first_discovered_batch_id, scope_id,
                           candidate_type, speaker_id, fingerprint,
                           fingerprint_version, source_payload_json, status, revision,
                           lease_owner, lease_token
                    FROM learning_candidate
                    WHERE scope_id = ? AND candidate_type = ? AND speaker_id = ?
                      AND fingerprint = ? AND fingerprint_version = ?
                    """,
                    self._candidate_identity(candidate),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                inserted = existing is None
                if existing is not None:
                    if str(existing[0]) != candidate.candidate_id:
                        await db.rollback()
                        return CandidateWriteResult(
                            candidate.candidate_id, False, False, 0,
                            int(existing[9]), str(existing[8]), conflict=True,
                            diagnostics=MappingProxyType({"failure_kind": "candidate_replay_conflict"}),
                        )
                else:
                    await db.execute(
                        """
                        INSERT INTO learning_candidate(
                            candidate_id, first_discovered_batch_id, scope_id,
                            candidate_type, speaker_id, speaker_scope_id, fingerprint,
                            fingerprint_version, extractor_version, status, revision,
                            attempt, source_message_start, source_message_end,
                            evidence_quality, source_payload_json,
                            enrichment_payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', 0, 0,
                                  ?, ?, ?, ?, '{}', ?, ?)
                        """,
                        (
                            candidate.candidate_id,
                            candidate.first_discovered_batch_id,
                            candidate.scope_id,
                            candidate.candidate_family,
                            candidate.speaker_id,
                            candidate.speaker_scope_id,
                            candidate.fingerprint,
                            candidate.fingerprint_version,
                            candidate.extractor_version,
                            candidate.source_message_start,
                            candidate.source_message_end,
                            candidate.evidence_quality,
                            _canonical_json(candidate.source_payload),
                            candidate.created_at,
                            candidate.updated_at,
                        ),
                    )
                evidence_inserted = 0
                for item in candidate.evidence:
                    was_inserted, conflict = await self._insert_evidence(db, item)
                    if conflict:
                        await db.rollback()
                        return CandidateWriteResult(
                            candidate.candidate_id, False, not inserted, evidence_inserted,
                            int(existing[9]) if existing else 0,
                            str(existing[8]) if existing else "discovered",
                            conflict=True,
                            diagnostics=MappingProxyType({"failure_kind": "evidence_replay_conflict"}),
                        )
                    evidence_inserted += int(was_inserted)
                if inserted:
                    revision = 1
                    await db.execute(
                        """
                        UPDATE learning_candidate
                        SET status = ?, revision = ?, failure_stage = ?,
                            failure_kind = ?, retryable = 0, updated_at = ?
                        WHERE candidate_id = ? AND revision = 0
                        """,
                        (
                            target_status,
                            revision,
                            "" if complete else "discovery",
                            "" if complete else "source_evidence_incomplete",
                            candidate.updated_at,
                            candidate.candidate_id,
                        ),
                    )
                else:
                    revision = int(existing[9])
                    target_status = str(existing[8])
                    if evidence_inserted:
                        previous_revision = revision
                        revision += 1
                        existing_payload = json.loads(str(existing[7] or "{}"))
                        merged_payload = _merge_source_payload(
                            existing_payload, candidate.source_payload
                        )
                        if target_status == "enriching":
                            cursor = await db.execute(
                                """
                                UPDATE learning_candidate_attempt
                                SET status = 'retry_wait', finished_at = ?, retry_at = ?,
                                    retryable = 1, failure_stage = 'discovery',
                                    failure_kind = 'candidate_revision_superseded'
                                WHERE candidate_id = ? AND revision = ?
                                  AND owner = ? AND lease_token = ?
                                  AND status = 'running'
                                """,
                                (
                                    candidate.updated_at,
                                    candidate.updated_at,
                                    candidate.candidate_id,
                                    previous_revision,
                                    str(existing[10] or ""),
                                    str(existing[11] or ""),
                                ),
                            )
                            if cursor.rowcount != 1:
                                await db.rollback()
                                return CandidateWriteResult(
                                    candidate.candidate_id,
                                    False,
                                    True,
                                    0,
                                    previous_revision,
                                    target_status,
                                    conflict=True,
                                    diagnostics=MappingProxyType(
                                        {"failure_kind": "active_attempt_cas_conflict"}
                                    ),
                                )
                            cursor = await db.execute(
                                """
                                UPDATE learning_candidate
                                SET revision = ?, status = 'retry_wait',
                                    source_payload_json = ?,
                                    source_message_start = CASE
                                        WHEN source_message_start IS NULL THEN ?
                                        WHEN ? IS NULL THEN source_message_start
                                        ELSE MIN(source_message_start, ?)
                                    END,
                                    source_message_end = CASE
                                        WHEN source_message_end IS NULL THEN ?
                                        WHEN ? IS NULL THEN source_message_end
                                        ELSE MAX(source_message_end, ?)
                                    END,
                                    lease_owner = '', lease_token = '', lease_until = 0,
                                    next_retry_at = ?, failure_stage = 'discovery',
                                    failure_kind = 'candidate_revision_superseded',
                                    retryable = 1, updated_at = ?
                                WHERE candidate_id = ? AND revision = ?
                                  AND status = 'enriching'
                                  AND lease_owner = ? AND lease_token = ?
                                """,
                                (
                                    revision,
                                    _canonical_json(merged_payload),
                                    candidate.source_message_start,
                                    candidate.source_message_start,
                                    candidate.source_message_start,
                                    candidate.source_message_end,
                                    candidate.source_message_end,
                                    candidate.source_message_end,
                                    candidate.updated_at,
                                    candidate.updated_at,
                                    candidate.candidate_id,
                                    previous_revision,
                                    str(existing[10] or ""),
                                    str(existing[11] or ""),
                                ),
                            )
                            if cursor.rowcount != 1:
                                await db.rollback()
                                return CandidateWriteResult(
                                    candidate.candidate_id,
                                    False,
                                    True,
                                    0,
                                    previous_revision,
                                    target_status,
                                    conflict=True,
                                    diagnostics=MappingProxyType(
                                        {"failure_kind": "candidate_revision_conflict"}
                                    ),
                                )
                            next_status = "retry_wait"
                        else:
                            next_status = (
                                "enrichment_pending"
                                if complete
                                and target_status
                                in {
                                    "discovered",
                                    "blocked",
                                    "enriched",
                                    "rejected",
                                    "partial",
                                    "failed",
                                    "cancelled",
                                }
                                else target_status
                            )
                            cursor = await db.execute(
                                """
                                UPDATE learning_candidate
                                SET revision = ?, status = ?, source_payload_json = ?,
                                    source_message_start = CASE
                                        WHEN source_message_start IS NULL THEN ?
                                        WHEN ? IS NULL THEN source_message_start
                                        ELSE MIN(source_message_start, ?)
                                    END,
                                    source_message_end = CASE
                                        WHEN source_message_end IS NULL THEN ?
                                        WHEN ? IS NULL THEN source_message_end
                                        ELSE MAX(source_message_end, ?)
                                    END,
                                    next_retry_at = CASE WHEN ? = 'enrichment_pending' THEN 0 ELSE next_retry_at END,
                                    failure_stage = CASE WHEN ? = 'enrichment_pending' THEN '' ELSE failure_stage END,
                                    failure_kind = CASE WHEN ? = 'enrichment_pending' THEN '' ELSE failure_kind END,
                                    retryable = CASE WHEN ? = 'enrichment_pending' THEN 0 ELSE retryable END,
                                    updated_at = ?
                                WHERE candidate_id = ? AND revision = ?
                                """,
                                (
                                    revision,
                                    next_status,
                                    _canonical_json(merged_payload),
                                    candidate.source_message_start,
                                    candidate.source_message_start,
                                    candidate.source_message_start,
                                    candidate.source_message_end,
                                    candidate.source_message_end,
                                    candidate.source_message_end,
                                    next_status,
                                    next_status,
                                    next_status,
                                    next_status,
                                    candidate.updated_at,
                                    candidate.candidate_id,
                                    revision - 1,
                                ),
                            )
                            if cursor.rowcount != 1:
                                await db.rollback()
                                return CandidateWriteResult(
                                    candidate.candidate_id,
                                    False,
                                    True,
                                    0,
                                    previous_revision,
                                    target_status,
                                    conflict=True,
                                    diagnostics=MappingProxyType(
                                        {"failure_kind": "candidate_revision_conflict"}
                                    ),
                                )
                        target_status = next_status
                await db.commit()
                return CandidateWriteResult(
                    candidate.candidate_id,
                    inserted,
                    not inserted,
                    evidence_inserted,
                    revision,
                    target_status,
                    diagnostics=MappingProxyType(diagnostics),
                )
            except Exception:
                await db.rollback()
                raise

    async def settle_source_batch(
        self,
        batch_id: str,
        *,
        expected_revision: int,
        dispositions: Sequence[SourceDisposition],
        now: float,
    ) -> SourceBatchSettlementResult:
        ordered = tuple(sorted(dispositions, key=lambda item: item.source_key))
        payload_json = _canonical_json([item.to_dict() for item in ordered])
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT source_ids_json, source_ids_hash, source_count, status,
                           revision, source_dispositions_json
                    FROM learning_source_batch WHERE batch_id = ?
                    """,
                    (batch_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await db.rollback()
                    return SourceBatchSettlementResult(batch_id, conflict=True)
                if str(row[3]) == "completed":
                    same = str(row[5]) == payload_json
                    await db.commit()
                    return SourceBatchSettlementResult(
                        batch_id,
                        completed=same,
                        idempotent=same,
                        conflict=not same,
                        revision=int(row[4]),
                    )
                source_ids = tuple(json.loads(str(row[0])))
                keys = tuple(item.source_key for item in ordered)
                failure = ""
                if int(row[4]) != int(expected_revision):
                    failure = "revision_conflict"
                elif len(keys) != len(set(keys)) or tuple(sorted(keys)) != tuple(sorted(source_ids)):
                    failure = "source_disposition_incomplete"
                elif _sha256({"source_ids": tuple(sorted(source_ids)), "version": 1}) != str(row[1]):
                    failure = "source_ids_hash_mismatch"
                if not failure:
                    for item in ordered:
                        if item.disposition != "candidate_ids":
                            continue
                        for candidate_id in item.candidate_ids:
                            cursor = await db.execute(
                                """
                                SELECT 1 FROM learning_candidate c
                                JOIN learning_candidate_evidence e
                                  ON e.candidate_id = c.candidate_id
                                WHERE c.candidate_id = ? AND e.batch_id = ?
                                  AND ((e.source_row_id IS NOT NULL AND 'row:' || e.source_row_id = ?)
                                    OR (e.source_row_id IS NULL AND 'message:' || e.source_message_id = ?))
                                LIMIT 1
                                """,
                                (candidate_id, batch_id, item.source_key, item.source_key),
                            )
                            durable = await cursor.fetchone()
                            await cursor.close()
                            if durable is None:
                                failure = "candidate_evidence_not_durable"
                                break
                        if failure:
                            break
                if failure:
                    await db.rollback()
                    return SourceBatchSettlementResult(
                        batch_id,
                        conflict=True,
                        revision=int(row[4]),
                        diagnostics=MappingProxyType({"failure_kind": failure}),
                    )
                skipped = sum(item.disposition == "skipped" for item in ordered)
                no_candidate = sum(item.disposition == "no_candidate" for item in ordered)
                candidate_ids = {
                    candidate_id
                    for item in ordered
                    for candidate_id in item.candidate_ids
                }
                eligible = len(ordered) - skipped
                cursor = await db.execute(
                    """
                    UPDATE learning_source_batch
                    SET source_dispositions_json = ?, eligible_count = ?,
                        skipped_count = ?, no_candidate_count = ?, candidate_count = ?,
                        status = 'completed', revision = revision + 1,
                        failure_stage = '', failure_kind = '', updated_at = ?
                    WHERE batch_id = ? AND revision = ? AND status = 'started'
                    """,
                    (
                        payload_json,
                        eligible,
                        skipped,
                        no_candidate,
                        len(candidate_ids),
                        float(now),
                        batch_id,
                        int(expected_revision),
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return SourceBatchSettlementResult(batch_id, conflict=True)
                await db.commit()
                return SourceBatchSettlementResult(
                    batch_id, completed=True, revision=int(expected_revision) + 1
                )
            except Exception:
                await db.rollback()
                raise

    async def highest_contiguous_completed_cursor(
        self, *, pipeline_type: str, scope_id: str, cursor_before: int
    ) -> int:
        async with self._db() as db:
            cursor = await db.execute(
                """
                SELECT cursor_before, cursor_after
                FROM learning_source_batch
                WHERE pipeline_type = ? AND scope_id = ? AND status = 'completed'
                  AND cursor_after >= ?
                ORDER BY cursor_before, cursor_after
                """,
                (pipeline_type, scope_id, int(cursor_before)),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        by_before: dict[int, set[int]] = {}
        for before, after in rows:
            by_before.setdefault(int(before), set()).add(int(after))
        current = int(cursor_before)
        visited: set[int] = set()
        while current not in visited:
            visited.add(current)
            targets = by_before.get(current, set())
            if len(targets) != 1:
                break
            target = next(iter(targets))
            if target < current:
                break
            current = target
        return current

    @staticmethod
    def _candidate_from_row(row, evidence=()) -> LearningCandidate | None:
        if row is None:
            return None
        source_payload = json.loads(str(row[14] or "{}"))
        subtype = str(source_payload.get("candidate_subtype", ""))
        return LearningCandidate(
            candidate_id=str(row[0]),
            first_discovered_batch_id=str(row[1]),
            scope_id=str(row[2]),
            candidate_family=str(row[3]),
            candidate_subtype=subtype,
            speaker_id=str(row[4]),
            speaker_scope_id=str(row[5]),
            fingerprint=str(row[6]),
            fingerprint_version=int(row[7]),
            extractor_version=str(row[8]),
            status=str(row[9]),
            revision=int(row[10]),
            attempt=int(row[11]),
            source_message_start=row[12],
            source_message_end=row[13],
            evidence_quality=str(row[16]),
            source_payload=_mapping(source_payload),
            enrichment_payload=_mapping(json.loads(str(row[15] or "{}"))),
            evidence=tuple(evidence),
            lease_owner=str(row[17]),
            lease_token=str(row[18]),
            lease_until=float(row[19]),
            next_retry_at=float(row[20]),
            failure_stage=str(row[21]),
            failure_kind=str(row[22]),
            retryable=bool(row[23]),
            provider_id=str(row[24]),
            model_id=str(row[25]),
            created_at=float(row[26]),
            updated_at=float(row[27]),
        )

    _CANDIDATE_COLUMNS = (
        "candidate_id, first_discovered_batch_id, scope_id, candidate_type, "
        "speaker_id, speaker_scope_id, fingerprint, fingerprint_version, "
        "extractor_version, status, revision, attempt, source_message_start, "
        "source_message_end, source_payload_json, enrichment_payload_json, "
        "evidence_quality, lease_owner, lease_token, lease_until, next_retry_at, "
        "failure_stage, failure_kind, retryable, provider_id, model_id, "
        "created_at, updated_at"
    )

    async def get_candidate(self, candidate_id: str) -> LearningCandidate | None:
        async with self._db() as db:
            cursor = await db.execute(
                f"SELECT {self._CANDIDATE_COLUMNS} FROM learning_candidate WHERE candidate_id = ?",
                (candidate_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._candidate_from_row(row)

    @staticmethod
    def _evidence_from_row(row) -> CandidateEvidence:
        payload = json.loads(str(row[18] or "{}"))
        identity_source = str(payload.pop("identity_source", ""))
        return CandidateEvidence(
            candidate_id=str(row[0]),
            evidence_id=str(row[1]),
            batch_id=str(row[2]),
            source_row_id=row[3],
            source_message_id=str(row[4]),
            event_id=row[5],
            platform_message_id=row[6],
            sender_id=str(row[7]),
            scope_id=str(row[8]),
            speaker_scope_id=str(row[9]),
            pairwise_scope_id=row[10],
            topic_epoch=row[11],
            source_type=str(row[12]),
            evidence_quality=str(row[13]),
            attribution_confidence=row[14],
            is_generated=bool(row[15]),
            eligible=bool(row[16]),
            eligibility_reason=str(row[17]),
            identity_source=identity_source,
            payload=_mapping(payload),
            created_at=float(row[19]),
        )

    async def list_evidence(self, candidate_id: str) -> tuple[CandidateEvidence, ...]:
        async with self._db() as db:
            cursor = await db.execute(
                """
                SELECT candidate_id, evidence_id, batch_id, source_row_id,
                       source_message_id, event_id, platform_message_id, sender_id,
                       scope_id, speaker_scope_id, pairwise_scope_id, topic_epoch,
                       source_type, evidence_quality, attribution_confidence,
                       is_generated, eligible, eligibility_reason, payload_json, created_at
                FROM learning_candidate_evidence
                WHERE candidate_id = ? ORDER BY evidence_id
                """,
                (candidate_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return tuple(self._evidence_from_row(row) for row in rows)

    async def list_due_enrichment(
        self, *, now: float, limit: int
    ) -> tuple[LearningCandidate, ...]:
        async with self._db() as db:
            cursor = await db.execute(
                f"""
                SELECT {self._CANDIDATE_COLUMNS}
                FROM learning_candidate AS c
                WHERE c.status IN ('enrichment_pending','retry_wait')
                  AND next_retry_at <= ? AND (lease_until = 0 OR lease_until <= ?)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM learning_candidate_evidence AS e
                    JOIN learning_source_batch AS b ON b.batch_id = e.batch_id
                    WHERE e.candidate_id = c.candidate_id
                      AND e.is_generated = 0 AND e.eligible = 1
                      AND b.status <> 'completed'
                  )
                ORDER BY next_retry_at, updated_at, candidate_id LIMIT ?
                """,
                (float(now), float(now), max(0, int(limit))),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return tuple(self._candidate_from_row(row) for row in rows)

    async def claim_enrichment(
        self,
        *,
        candidate_id: str,
        expected_revision: int,
        owner: str,
        run_id: str,
        task_name: str,
        lease_seconds: float,
        now: float,
        config_revision: int = 0,
    ) -> WorkAttemptLease | None:
        lease_token = uuid.uuid4().hex
        attempt_id = f"work-attempt:{uuid.uuid4().hex}"
        lease_until = float(now) + max(0.1, float(lease_seconds))
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    f"SELECT {self._CANDIDATE_COLUMNS} FROM learning_candidate WHERE candidate_id = ?",
                    (candidate_id,),
                )
                row = await cursor.fetchone()
                await cursor.close()
                candidate = self._candidate_from_row(row)
                cursor = await db.execute(
                    """
                    SELECT 1
                    FROM learning_candidate_evidence AS e
                    JOIN learning_source_batch AS b ON b.batch_id = e.batch_id
                    WHERE e.candidate_id = ?
                      AND e.is_generated = 0 AND e.eligible = 1
                      AND b.status <> 'completed'
                    LIMIT 1
                    """,
                    (candidate_id,),
                )
                incomplete_batch = await cursor.fetchone()
                await cursor.close()
                if (
                    candidate is None
                    or candidate.revision != int(expected_revision)
                    or candidate.status not in {"enrichment_pending", "retry_wait"}
                    or candidate.next_retry_at > float(now)
                    or candidate.lease_until > float(now)
                    or incomplete_batch is not None
                ):
                    await db.rollback()
                    return None
                new_revision = candidate.revision + 1
                work_attempt = candidate.attempt + 1
                cursor = await db.execute(
                    """
                    UPDATE learning_candidate
                    SET status = 'enriching', revision = ?, attempt = ?,
                        lease_owner = ?, lease_token = ?, lease_until = ?,
                        failure_stage = '', failure_kind = '', retryable = 0,
                        updated_at = ?
                    WHERE candidate_id = ? AND revision = ?
                      AND status IN ('enrichment_pending','retry_wait')
                      AND next_retry_at <= ? AND (lease_until = 0 OR lease_until <= ?)
                      AND NOT EXISTS (
                        SELECT 1
                        FROM learning_candidate_evidence AS e
                        JOIN learning_source_batch AS b ON b.batch_id = e.batch_id
                        WHERE e.candidate_id = learning_candidate.candidate_id
                          AND e.is_generated = 0 AND e.eligible = 1
                          AND b.status <> 'completed'
                      )
                    """,
                    (
                        new_revision,
                        work_attempt,
                        str(owner),
                        lease_token,
                        lease_until,
                        float(now),
                        candidate_id,
                        int(expected_revision),
                        float(now),
                        float(now),
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return None
                await db.execute(
                    """
                    INSERT INTO learning_candidate_attempt(
                        attempt_id, candidate_id, run_id, stage, attempt,
                        candidate_work_attempt, provider_attempt, revision,
                        owner, lease_token, lease_until, task_name, scope_id,
                        config_revision, started_at, created_at, status
                    ) VALUES (?, ?, ?, 'enrichment', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
                    """,
                    (
                        attempt_id,
                        candidate_id,
                        str(run_id),
                        work_attempt,
                        work_attempt,
                        new_revision,
                        str(owner),
                        lease_token,
                        lease_until,
                        str(task_name),
                        candidate.scope_id,
                        int(config_revision),
                        float(now),
                        float(now),
                    ),
                )
                await db.commit()
                claimed = await self.get_candidate(candidate_id)
                return WorkAttemptLease(
                    attempt_id=attempt_id,
                    candidate_id=candidate_id,
                    run_id=str(run_id),
                    candidate_work_attempt=work_attempt,
                    owner=str(owner),
                    lease_token=lease_token,
                    lease_until=lease_until,
                    expected_revision=new_revision,
                    task_name=str(task_name),
                    scope_id=candidate.scope_id,
                    candidate=claimed,
                )
            except Exception:
                await db.rollback()
                raise

    async def mark_provider_started(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        owner: str,
        lease_token: str,
        expected_revision: int,
        provider_id: str,
        provider_family: str,
        model_id: str,
        identity_source: str,
        gateway_call_id: str,
        provider_request_id: str,
        started_at: float,
        now: float | None = None,
    ) -> ProviderStartResult:
        checked_at = float(started_at if now is None else now)
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT a.provider_attempt, a.provider_request_started,
                           a.provider_id, a.provider_family, a.model_id,
                           a.identity_source, a.gateway_call_id,
                           a.provider_request_id, a.started_at,
                           c.revision, c.lease_until, a.status,
                           c.status, c.lease_owner, c.lease_token,
                           a.revision, a.lease_until
                    FROM learning_candidate_attempt a
                    JOIN learning_candidate c ON c.candidate_id = a.candidate_id
                    WHERE a.attempt_id = ? AND a.candidate_id = ?
                      AND a.owner = ? AND a.lease_token = ?
                    """,
                    (attempt_id, candidate_id, owner, lease_token),
                )
                row = await cursor.fetchone()
                await cursor.close()
                if row is None:
                    await db.rollback()
                    return ProviderStartResult(False, True, False, attempt_id, 0, 0, "provider_start_fence", "cas_conflict")
                if bool(row[1]):
                    identity = tuple(str(value or "") for value in row[2:8])
                    requested = (
                        str(provider_id), str(provider_family), str(model_id),
                        str(identity_source), str(gateway_call_id), str(provider_request_id),
                    )
                    current_lease = (
                        int(row[9]) == int(expected_revision)
                        and float(row[10]) >= checked_at
                        and str(row[11]) == "running"
                        and str(row[12]) == "enriching"
                        and str(row[13]) == str(owner)
                        and str(row[14]) == str(lease_token)
                        and int(row[15]) == int(expected_revision)
                        and float(row[16]) >= checked_at
                    )
                    same_identity = (
                        identity == requested
                        and float(row[8]) == float(started_at)
                    )
                    matches = same_identity and current_lease
                    await db.commit()
                    return ProviderStartResult(
                        False, not matches, matches, attempt_id, int(row[0]), int(row[9]),
                        "" if matches else "provider_start_fence",
                        "" if matches else (
                            "cas_conflict" if same_identity else "conflicting_replay"
                        ),
                    )
                if (
                    int(row[9]) != int(expected_revision)
                    or float(row[10]) < checked_at
                    or str(row[11]) != "running"
                    or str(row[12]) != "enriching"
                    or str(row[13]) != str(owner)
                    or str(row[14]) != str(lease_token)
                    or int(row[15]) != int(expected_revision)
                    or float(row[16]) < checked_at
                ):
                    await db.rollback()
                    return ProviderStartResult(False, True, False, attempt_id, 0, int(row[9]), "provider_start_fence", "cas_conflict")
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(provider_attempt), 0) FROM learning_candidate_attempt WHERE candidate_id = ? AND provider_request_started = 1",
                    (candidate_id,),
                )
                provider_attempt = int((await cursor.fetchone())[0]) + 1
                await cursor.close()
                cursor = await db.execute(
                    """
                    UPDATE learning_candidate_attempt
                    SET provider_attempt = ?, provider_request_started = 1,
                        provider_id = ?, provider_family = ?, model_id = ?,
                        identity_source = ?, gateway_call_id = ?,
                        provider_request_id = ?, started_at = ?
                    WHERE attempt_id = ? AND candidate_id = ? AND owner = ?
                      AND lease_token = ? AND revision = ?
                      AND provider_attempt = 0 AND provider_request_started = 0
                      AND status = 'running' AND lease_until >= ?
                    """,
                    (
                        provider_attempt,
                        provider_id,
                        provider_family,
                        model_id,
                        identity_source,
                        gateway_call_id,
                        provider_request_id,
                        checked_at,
                        attempt_id,
                        candidate_id,
                        owner,
                        lease_token,
                        int(expected_revision),
                        float(started_at),
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return ProviderStartResult(False, True, False, attempt_id, 0, int(row[9]), "provider_start_fence", "cas_conflict")
                await db.commit()
                return ProviderStartResult(True, False, False, attempt_id, provider_attempt, int(row[9]))
            except sqlite3.OperationalError:
                await db.rollback()
                raise
            except Exception:
                await db.rollback()
                raise

    async def renew_enrichment_lease(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        owner: str,
        lease_token: str,
        expected_revision: int,
        lease_until: float,
        now: float,
    ) -> bool:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    UPDATE learning_candidate
                    SET lease_until = ?, updated_at = ?
                    WHERE candidate_id = ? AND revision = ? AND status = 'enriching'
                      AND lease_owner = ? AND lease_token = ? AND lease_until >= ?
                    """,
                    (lease_until, now, candidate_id, expected_revision, owner, lease_token, now),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                cursor = await db.execute(
                    """
                    UPDATE learning_candidate_attempt SET lease_until = ?
                    WHERE attempt_id = ? AND candidate_id = ? AND revision = ?
                      AND owner = ? AND lease_token = ? AND status = 'running'
                    """,
                    (lease_until, attempt_id, candidate_id, expected_revision, owner, lease_token),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return False
                await db.commit()
                return True
            except Exception:
                await db.rollback()
                raise

    async def settle_enrichment(
        self,
        *,
        candidate_id: str,
        attempt_id: str,
        owner: str,
        lease_token: str,
        expected_revision: int,
        result: EnrichmentSettlement,
    ) -> CandidateSettlementResult:
        canonical_payload = _canonical_json(result.payload())
        digest = _sha256(result.payload())
        if result.status not in _SETTLEMENT_STATUSES or digest != result.result_digest:
            return CandidateSettlementResult(False, True, False, await self.get_candidate(candidate_id), digest, "settlement", "invalid_result_digest")
        if result.status == "enriched" and (
            not result.canonical_ids
            or any(not str(item).strip() for item in result.canonical_ids)
        ):
            return CandidateSettlementResult(False, True, False, await self.get_candidate(candidate_id), digest, "persistence", "empty_persistence_id")
        if result.status == "retry_wait" and (not result.retryable or result.retry_at <= 0):
            return CandidateSettlementResult(False, True, False, await self.get_candidate(candidate_id), digest, "settlement", "invalid_retry_contract")
        if result.finished_at < result.settlement_started_at:
            return CandidateSettlementResult(False, True, False, await self.get_candidate(candidate_id), digest, "settlement", "invalid_settlement_time")
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT a.result_digest, a.status, a.owner, a.lease_token,
                           a.revision, a.lease_until, a.finished_at,
                           c.revision, c.status, c.lease_owner,
                           c.lease_token, c.lease_until
                    FROM learning_candidate_attempt AS a
                    JOIN learning_candidate AS c
                      ON c.candidate_id = a.candidate_id
                    WHERE a.attempt_id = ? AND a.candidate_id = ?
                    """,
                    (attempt_id, candidate_id),
                )
                attempt_row = await cursor.fetchone()
                await cursor.close()
                if attempt_row is None:
                    await db.rollback()
                    return CandidateSettlementResult(False, True, False, None, digest, "settlement", "attempt_missing")
                if str(attempt_row[0] or ""):
                    expected_attempt_status = (
                        "completed" if result.status == "enriched" else result.status
                    )
                    same_digest = str(attempt_row[0]) == digest
                    same_cas_identity = (
                        str(attempt_row[2]) == str(owner)
                        and str(attempt_row[3]) == str(lease_token)
                        and int(attempt_row[4]) == int(expected_revision)
                    )
                    settled_state_is_current = (
                        str(attempt_row[1]) == expected_attempt_status
                        and float(attempt_row[5]) >= float(result.finished_at)
                        and float(attempt_row[6]) == float(result.finished_at)
                        and int(attempt_row[7]) == int(expected_revision) + 1
                        and str(attempt_row[8]) == result.status
                        and str(attempt_row[9]) == ""
                        and str(attempt_row[10]) == ""
                        and float(attempt_row[11]) == 0.0
                    )
                    matches = (
                        same_digest
                        and same_cas_identity
                        and settled_state_is_current
                    )
                    await db.commit()
                    return CandidateSettlementResult(
                        False, not matches, matches,
                        await self.get_candidate(candidate_id), digest,
                        "" if matches else "settlement",
                        "" if matches else (
                            "conflicting_replay" if not same_digest else "cas_conflict"
                        ),
                    )
                for item in result.generated_evidence:
                    if item.candidate_id != candidate_id or not item.is_generated:
                        await db.rollback()
                        return CandidateSettlementResult(False, True, False, None, digest, "settlement", "invalid_generated_evidence")
                    _inserted, conflict = await self._insert_evidence(db, item)
                    if conflict:
                        await db.rollback()
                        return CandidateSettlementResult(False, True, False, None, digest, "settlement", "evidence_replay_conflict")
                cursor = await db.execute(
                    """
                    UPDATE learning_candidate
                    SET status = ?, revision = revision + 1,
                        lease_owner = '', lease_token = '', lease_until = 0,
                        next_retry_at = ?, enrichment_payload_json = ?,
                        failure_stage = ?, failure_kind = ?, retryable = ?,
                        provider_id = ?, model_id = ?, updated_at = ?
                    WHERE candidate_id = ? AND revision = ? AND status = 'enriching'
                      AND lease_owner = ? AND lease_token = ? AND lease_until >= ?
                    """,
                    (
                        result.status,
                        result.retry_at,
                        _canonical_json(result.enrichment_payload),
                        result.failure_stage,
                        result.failure_kind,
                        int(result.retryable),
                        result.provider_id,
                        result.model_id,
                        result.finished_at,
                        candidate_id,
                        int(expected_revision),
                        owner,
                        lease_token,
                        result.finished_at,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return CandidateSettlementResult(False, True, False, await self.get_candidate(candidate_id), digest, "settlement", "cas_conflict")
                attempt_status = "completed" if result.status == "enriched" else result.status
                cursor = await db.execute(
                    """
                    UPDATE learning_candidate_attempt
                    SET status = ?, finished_at = ?, retry_at = ?, retryable = ?,
                        provider_id = CASE WHEN ? <> '' THEN ? ELSE provider_id END,
                        model_id = CASE WHEN ? <> '' THEN ? ELSE model_id END,
                        failure_stage = ?, failure_kind = ?, diagnostics_json = ?,
                        canonical_ids_json = ?, result_digest = ?, settlement_payload_json = ?,
                        queue_wait_ms = ?, logical_queue_wait_ms = ?,
                        runtime_budget_wait_ms = ?, gateway_background_wait_ms = ?,
                        gateway_global_wait_ms = ?, provider_latency_ms = ?,
                        elapsed_ms = ?, input_count = ?, output_count = ?
                    WHERE attempt_id = ? AND candidate_id = ? AND revision = ?
                      AND owner = ? AND lease_token = ? AND status = 'running'
                    """,
                    (
                        attempt_status,
                        result.finished_at,
                        result.retry_at,
                        int(result.retryable),
                        result.provider_id,
                        result.provider_id,
                        result.model_id,
                        result.model_id,
                        result.failure_stage,
                        result.failure_kind,
                        _canonical_json(result.diagnostics),
                        _canonical_json(result.canonical_ids),
                        digest,
                        canonical_payload,
                        result.queue_wait_ms,
                        result.logical_queue_wait_ms,
                        result.runtime_budget_wait_ms,
                        result.gateway_background_wait_ms,
                        result.gateway_global_wait_ms,
                        result.provider_latency_ms,
                        result.elapsed_ms,
                        result.input_count,
                        result.output_count,
                        attempt_id,
                        candidate_id,
                        int(expected_revision),
                        owner,
                        lease_token,
                    ),
                )
                if cursor.rowcount != 1:
                    await db.rollback()
                    return CandidateSettlementResult(False, True, False, None, digest, "settlement", "attempt_cas_conflict")
                await db.commit()
                return CandidateSettlementResult(
                    True, False, False, await self.get_candidate(candidate_id), digest
                )
            except Exception:
                await db.rollback()
                raise

    async def recover_expired_enrichment(self, *, now: float) -> int:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    """
                    SELECT candidate_id, revision, lease_owner, lease_token
                    FROM learning_candidate
                    WHERE status = 'enriching' AND lease_until > 0 AND lease_until <= ?
                    """,
                    (float(now),),
                )
                rows = await cursor.fetchall()
                await cursor.close()
                recovered = 0
                for candidate_id, revision, owner, token in rows:
                    cursor = await db.execute(
                        """
                        UPDATE learning_candidate
                        SET status = 'retry_wait', revision = revision + 1,
                            lease_owner = '', lease_token = '', lease_until = 0,
                            next_retry_at = ?, retryable = 1,
                            failure_stage = 'worker', failure_kind = 'lease_expired',
                            updated_at = ?
                        WHERE candidate_id = ? AND revision = ? AND status = 'enriching'
                          AND lease_owner = ? AND lease_token = ? AND lease_until <= ?
                        """,
                        (now, now, candidate_id, revision, owner, token, now),
                    )
                    if cursor.rowcount != 1:
                        continue
                    recovered += 1
                    await db.execute(
                        """
                        UPDATE learning_candidate_attempt
                        SET status = 'retry_wait', finished_at = ?, retry_at = ?,
                            retryable = 1, failure_stage = 'worker',
                            failure_kind = 'lease_expired'
                        WHERE candidate_id = ? AND revision = ? AND owner = ?
                          AND lease_token = ? AND status = 'running'
                        """,
                        (now, now, candidate_id, revision, owner, token),
                    )
                await db.commit()
                return recovered
            except Exception:
                await db.rollback()
                raise

    async def append_stage_diagnostic(
        self,
        *,
        run_id: str,
        candidate_id: str,
        stage: str,
        status: str,
        failure_stage: str = "",
        failure_kind: str = "",
        retryable: bool = False,
        attempt: int = 0,
        provider_id: str = "",
        model_id: str = "",
        input_count: int = 0,
        output_count: int = 0,
        diagnostics: Mapping[str, Any] | None = None,
        created_at: float,
    ) -> str:
        diagnostic = LearningStageDiagnostic(
            stage=str(stage),
            status=str(status),
            attempt=int(attempt),
            finished_at=float(created_at),
            input_count=int(input_count),
            output_count=int(output_count),
            failure_stage=str(failure_stage),
            failure_kind=str(failure_kind),
            retryable=bool(retryable),
            provider_id=str(provider_id),
            model_id=str(model_id),
            diagnostics=dict(diagnostics or {}),
        ).to_report()
        payload = {
            "run_id": str(run_id),
            "candidate_id": str(candidate_id),
            "stage": diagnostic["stage"],
            "status": diagnostic["status"],
            "failure_stage": diagnostic["failure_stage"],
            "failure_kind": diagnostic["failure_kind"],
            "attempt": diagnostic["attempt"],
            "diagnostics": diagnostic["diagnostics"],
        }
        diagnostic_id = f"diagnostic:{_sha256(payload)}"
        async with self._db() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO learning_stage_diagnostic(
                    diagnostic_id, run_id, candidate_id, stage, status,
                    failure_stage, failure_kind, retryable, attempt,
                    provider_id, model_id, input_count, output_count,
                    diagnostics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    diagnostic_id,
                    str(run_id),
                    str(candidate_id),
                    diagnostic["stage"],
                    diagnostic["status"],
                    diagnostic["failure_stage"],
                    diagnostic["failure_kind"],
                    int(diagnostic["retryable"]),
                    diagnostic["attempt"],
                    diagnostic["provider_id"],
                    diagnostic["model_id"],
                    diagnostic["input_count"],
                    diagnostic["output_count"],
                    _canonical_json(diagnostic["diagnostics"]),
                    float(created_at),
                ),
            )
            await db.commit()
        return diagnostic_id

    @staticmethod
    def _attempt_from_row(row) -> CandidateAttemptRecord:
        return CandidateAttemptRecord(
            attempt_id=str(row[0]),
            candidate_id=str(row[1]),
            run_id=str(row[2]),
            stage=str(row[3]),
            candidate_work_attempt=int(row[4]),
            provider_attempt=int(row[5]),
            revision=int(row[6]),
            owner=str(row[7]),
            lease_token=str(row[8]),
            lease_until=float(row[9]),
            task_name=str(row[10]),
            scope_id=str(row[11]),
            provider_request_started=bool(row[12]),
            status=str(row[13]),
            retry_at=float(row[14] or 0),
            retryable=bool(row[15]),
            failure_stage=str(row[16]),
            failure_kind=str(row[17]),
            result_digest=str(row[18]),
            queue_wait_ms=float(row[19] or 0),
            logical_queue_wait_ms=float(row[20] or 0),
            runtime_budget_wait_ms=float(row[21] or 0),
            gateway_background_wait_ms=float(row[22] or 0),
            gateway_global_wait_ms=float(row[23] or 0),
            provider_latency_ms=float(row[24] or 0),
            elapsed_ms=float(row[25] or 0),
            input_count=int(row[26] or 0),
            output_count=int(row[27] or 0),
        )

    async def list_attempts(self, candidate_id: str) -> tuple[CandidateAttemptRecord, ...]:
        async with self._db() as db:
            cursor = await db.execute(
                """
                SELECT attempt_id, candidate_id, run_id, stage,
                       candidate_work_attempt, provider_attempt, revision,
                       owner, lease_token, lease_until, task_name, scope_id,
                       provider_request_started, status, retry_at, retryable,
                       failure_stage, failure_kind, result_digest,
                       queue_wait_ms, logical_queue_wait_ms,
                       runtime_budget_wait_ms, gateway_background_wait_ms,
                       gateway_global_wait_ms, provider_latency_ms, elapsed_ms,
                       input_count, output_count
                FROM learning_candidate_attempt
                WHERE candidate_id = ? ORDER BY candidate_work_attempt, created_at
                """,
                (candidate_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return tuple(self._attempt_from_row(row) for row in rows)


__all__ = [
    "CandidateAttemptRecord",
    "CandidateEvidence",
    "CandidateLedger",
    "CandidateSettlementResult",
    "CandidateWriteResult",
    "EnrichmentSettlement",
    "LearningCandidate",
    "LearningSourceBatch",
    "ProviderStartResult",
    "QualitySnapshotWriteResult",
    "SourceBatchSettlementResult",
    "SourceBatchWriteResult",
    "SourceDisposition",
    "WorkAttemptLease",
]
