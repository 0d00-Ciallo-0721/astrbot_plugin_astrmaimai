from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any


RETRIEVAL_PROFILE_VERSION = "retrieval-v1"


@dataclass(frozen=True, slots=True)
class LearningTurnCorrelation:
    turn_id: str = ""
    correlation_id: str = ""
    prompt_revision: str = ""
    scope_id: str = ""
    sender_id: str = ""
    query_fingerprint: str = ""

    @property
    def durable(self) -> bool:
        return bool(self.turn_id.strip() and self.correlation_id.strip())

    @classmethod
    def from_event(
        cls,
        event: Any,
        *,
        prompt_revision: str = "",
        query_text: str = "",
        scope_id: str = "",
        sender_id: str = "",
        allow_event_sender: bool = True,
    ) -> "LearningTurnCorrelation":
        get_extra = getattr(event, "get_extra", None)
        turn = get_extra("astrmai_turn_identity", None) if callable(get_extra) else None
        turn_id = str(getattr(turn, "turn_id", "") or "").strip()
        correlation_id = str(
            get_extra("astrmai_trace_id", "") if callable(get_extra) else ""
        ).strip()
        fingerprint = ""
        normalized_query = " ".join(str(query_text or "").split())
        if normalized_query:
            fingerprint = "sha256:v1:" + hashlib.sha256(
                normalized_query.encode("utf-8")
            ).hexdigest()
        resolved_scope = str(scope_id or getattr(event, "unified_msg_origin", "") or "").strip()
        resolved_sender = str(sender_id or "").strip()
        if not resolved_sender and allow_event_sender and hasattr(event, "get_sender_id"):
            try:
                resolved_sender = str(event.get_sender_id() or "").strip()
            except Exception:
                resolved_sender = ""
        return cls(
            turn_id=turn_id,
            correlation_id=correlation_id,
            prompt_revision=str(prompt_revision or "").strip(),
            scope_id=resolved_scope,
            sender_id=resolved_sender,
            query_fingerprint=fingerprint,
        )


@dataclass(frozen=True, slots=True)
class LearningFocusContext:
    source_event_id: str = ""
    scope_id: str = ""
    speaker_id: str = ""
    focus_message_fingerprint: str = ""
    synthetic: bool = False
    context_revision: int = 0

    @property
    def attribution_eligible(self) -> bool:
        return bool(
            not self.synthetic
            and self.source_event_id.strip()
            and self.scope_id.strip()
            and self.focus_message_fingerprint.strip()
            and type(self.context_revision) is int
            and self.context_revision >= 0
        )


@dataclass(frozen=True, slots=True)
class RetrievalEligibility:
    eligible: bool
    accepted_for_prompt: bool
    shadow_only: bool
    tier: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True, slots=True)
class LearningRetrievalCandidate:
    asset_id: str
    asset_revision: int
    candidate_revision: int
    admission_revision: int
    canonical_memory_id: str
    scope_id: str
    speaker_scope_id: str
    topic_scope_id: str
    fingerprint: str
    lifecycle_status: str
    membership_status: str
    generation: int
    relevance: float
    asset_weight: float
    support: float
    decay: float
    evidence_quality: float
    provenance_hash: str
    prompt_text: str = ""
    review_revision: int | None = None
    prompt_text_provenance: str = ""
    source_examples: tuple[str, ...] = ()
    source_example_ids: tuple[str, ...] = ()
    model_example_id: str = ""
    repetition_attempt: int = 0
    blocked_reason: str = ""
    tier: str = ""
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class LearningRetrievalSelection:
    profile_version: str = RETRIEVAL_PROFILE_VERSION
    selected: tuple[LearningRetrievalCandidate, ...] = ()
    blocked: tuple[LearningRetrievalCandidate, ...] = ()
    accepted_for_prompt: bool = False

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(item.asset_id for item in self.selected)


@dataclass(frozen=True, slots=True)
class LearningAssetProvenance:
    asset_id: str
    asset_revision: int
    generation: int
    candidate_revision: int
    review_revision: int
    admission_revision: int
    canonical_memory_id: str
    provenance_hash: str
    source_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LearningRetrievalEvent:
    correlation: LearningTurnCorrelation
    source_layer: str
    stage: str
    event_status: str
    policy_version: str
    asset_revision_ids: tuple[str, ...] = ()
    asset_provenance: tuple[LearningAssetProvenance, ...] = ()
    selected_ids: tuple[str, ...] = ()
    accepted_ids: tuple[str, ...] = ()
    visible_ids: tuple[str, ...] = ()
    candidate_revision: int | None = None
    review_revision: int | None = None
    admission_revision: int | None = None
    generation: int | None = None
    reason_code: str = ""
    trimmed_reason: str = ""
    budget_chars: int | None = None
    budget_tokens: int | None = None
    outcome: str = "unknown"
    reply_id: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    work_id: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class LearningRetrievalEventMutation:
    applied: bool
    conflict: bool
    idempotent: bool = False
    event_id: str = ""
    idempotency_key: str = ""
    failure_kind: str = ""


@dataclass(frozen=True, slots=True)
class LearningAssetVersion:
    asset_id: str
    asset_revision: int
    canonical_memory_id: str
    candidate_id: str
    candidate_revision: int
    admission_revision: int
    scope_id: str
    speaker_scope_id: str
    fingerprint: str
    fingerprint_version: int
    lifecycle_status: str
    provenance_hash: str
    revision: int = 0
    valid_at: float | None = None
    invalid_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class LearningIndexMembership:
    asset_id: str
    asset_revision: int
    generation: int
    resource_id: str
    mapping_ordinal: int
    vector_id: str
    mapping_hash: str
    index_hash: str
    membership_status: str
    revision: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class LearningPublishMutation:
    applied: bool
    conflict: bool
    idempotent: bool = False
    failure_stage: str = ""
    failure_kind: str = ""
    current_generation: int | None = None
    asset_revision: int | None = None
    membership_revision: int | None = None


@dataclass(frozen=True, slots=True)
class RepetitionGuardResult:
    blocked: bool
    regenerate: bool
    cosine_similarity: float
    trigram_overlap: float
    attempt: int
    profile_version: str = RETRIEVAL_PROFILE_VERSION
    reason_code: str = ""


__all__ = [
    "LearningFocusContext",
    "LearningAssetProvenance",
    "LearningAssetVersion",
    "LearningIndexMembership",
    "LearningPublishMutation",
    "LearningRetrievalCandidate",
    "LearningRetrievalEvent",
    "LearningRetrievalEventMutation",
    "LearningRetrievalSelection",
    "LearningTurnCorrelation",
    "RETRIEVAL_PROFILE_VERSION",
    "RepetitionGuardResult",
    "RetrievalEligibility",
]
