from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


DECISIONS = frozenset({"approved", "rejected", "revision_needed", "quarantined"})
REASONS = frozenset({
    "evidence_supported", "evidence_conflict", "insufficient_evidence",
    "invalid_contract", "policy_sensitive", "reviewer_disagreement",
    "pair_order_unstable", "human_override",
})
REVIEWER_KINDS = frozenset({"model", "human", "rule"})
_REVIEWER_MODEL_IDENTITY_PREFIX = "reviewer-model-v1:"
_SENSITIVE_KEYS = frozenset({
    "authorization", "api_key", "cookie", "set_cookie", "password", "secret",
    "lease_token", "access_token", "refresh_token", "prompt", "system_prompt",
    "payload", "response", "content", "raw_message", "message_text",
})


def _strict_revision(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name}_invalid")
    return value


def reviewer_model_identity(
    *, provider_source_id: str, model_id: str, reviewer_profile_version: str,
) -> str:
    values = {
        "provider_source_id": provider_source_id,
        "model_id": model_id,
        "reviewer_profile_version": reviewer_profile_version,
    }
    normalized: dict[str, str] = {}
    for name, raw_value in values.items():
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise ValueError(f"{name}_required")
        normalized[name] = raw_value.strip()
    payload = json.dumps(
        normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    )
    return _REVIEWER_MODEL_IDENTITY_PREFIX + hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()


def is_reviewer_model_identity(value: str) -> bool:
    if not isinstance(value, str) or not value.startswith(_REVIEWER_MODEL_IDENTITY_PREFIX):
        return False
    digest = value[len(_REVIEWER_MODEL_IDENTITY_PREFIX):]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _clean_ids(values: tuple[str, ...], name: str, *, durable: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{name}_must_be_tuple")
    if any(not isinstance(value, str) for value in values):
        raise ValueError(f"{name}_invalid")
    cleaned = tuple(dict.fromkeys(value.strip() for value in values))
    if any(not value for value in cleaned):
        raise ValueError(f"{name}_invalid")
    if durable:
        for value in cleaned:
            valid = value.startswith(("row:", "event_id:", "platform_message_id:"))
            lowered = value.lower()
            if not valid or "synthetic" in lowered or "fallback" in lowered or lowered.startswith("event_id:evt_"):
                raise ValueError("source_evidence_identity_invalid")
    return cleaned


def sanitize_diagnostics(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    def clean(item: Any, key: str = "") -> Any:
        normalized_key = key.lower().replace("-", "_")
        if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith("_api_key"):
            return "[redacted]"
        if isinstance(item, Mapping):
            return {str(k): clean(v, str(k)) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(v) for v in item[:50]]
        if isinstance(item, str):
            return item if len(item) <= 256 else item[:253] + "..."
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)[:256]

    return MappingProxyType(clean(dict(value or {})))


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    decision_id: str
    candidate_id: str
    candidate_revision: int
    decision: str
    reason: str
    reviewer_id: str
    reviewer_kind: str
    reviewer_attempt_id: str
    rubric_version: str
    prompt_version: str
    model_identity: str
    source_evidence_ids: tuple[str, ...]
    source_example_ids: tuple[str, ...]
    model_example_ids: tuple[str, ...] = ()
    source_decision_ids: tuple[str, ...] = ()
    confidence: float | None = None
    expected_revision: int = 0
    pair_order: str = ""
    order_invariant: bool = False
    diagnostics: Mapping[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "decision_id", "candidate_id", "reviewer_id", "reviewer_attempt_id",
            "rubric_version", "prompt_version", "pair_order",
        ):
            raw_value = getattr(self, name)
            if not isinstance(raw_value, str):
                raise ValueError(f"{name}_required")
            value = raw_value.strip()
            if not value:
                raise ValueError(f"{name}_required")
            object.__setattr__(self, name, value)
        _strict_revision(self.candidate_revision, "candidate_revision")
        _strict_revision(self.expected_revision, "expected_revision")
        if self.candidate_revision != self.expected_revision:
            raise ValueError("revision_mismatch")
        if self.decision not in DECISIONS:
            raise ValueError("decision_invalid")
        if self.reason not in REASONS:
            raise ValueError("reason_invalid")
        if self.reviewer_kind not in REVIEWER_KINDS:
            raise ValueError("reviewer_kind_invalid")
        if self.pair_order not in {"ab", "ba", "human", "quorum"}:
            raise ValueError("pair_order_invalid")
        if not isinstance(self.order_invariant, bool):
            raise ValueError("order_invariant_invalid")
        if self.reviewer_kind == "model" and not str(self.model_identity or "").strip():
            raise ValueError("model_identity_required")
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
                raise ValueError("confidence_invalid")
            if not math.isfinite(float(self.confidence)) or not 0.0 <= float(self.confidence) <= 1.0:
                raise ValueError("confidence_invalid")
        evidence_ids = _clean_ids(self.source_evidence_ids, "source_evidence_ids", durable=True)
        if not evidence_ids:
            raise ValueError("source_evidence_required")
        source_examples = _clean_ids(self.source_example_ids, "source_example_ids", durable=True)
        model_examples = _clean_ids(self.model_example_ids, "model_example_ids")
        source_decisions = _clean_ids(self.source_decision_ids, "source_decision_ids")
        if set(source_examples) - set(evidence_ids):
            raise ValueError("source_example_not_in_evidence")
        if set(source_examples) & set(model_examples):
            raise ValueError("source_model_example_overlap")
        if not math.isfinite(float(self.created_at)) or self.created_at <= 0:
            raise ValueError("created_at_invalid")
        object.__setattr__(self, "source_evidence_ids", evidence_ids)
        object.__setattr__(self, "source_example_ids", source_examples)
        object.__setattr__(self, "model_example_ids", model_examples)
        object.__setattr__(self, "source_decision_ids", source_decisions)
        object.__setattr__(self, "diagnostics", sanitize_diagnostics(self.diagnostics))


@dataclass(frozen=True, slots=True)
class ReviewQuorumResult:
    candidate_id: str
    candidate_revision: int
    status: str
    decision: str | None
    decision_ids: tuple[str, ...]
    reviewer_ids: tuple[str, ...]
    reason: str
    order_invariant: bool = False
