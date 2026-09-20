from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any, Literal, Mapping


_QUALITY_V1_PARAMETERS: dict[str, int | float | str | bool] = {
    "expression_window_days": 7,
    "expression_min_speaker_messages": 20,
    "expression_min_support": 3,
    "expression_min_distinct_turns": 3,
    "expression_min_g2": 6.63,
    "expression_min_log2_effect": 1.0,
    "expression_max_fdr_q": 0.05,
    "jargon_min_support": 5,
    "jargon_min_left_entropy_bits": 1.5,
    "jargon_min_right_entropy_bits": 1.5,
    "jargon_burst_ratio": 2.0,
    "jargon_cjk_min_length": 2,
    "jargon_cjk_max_length": 12,
    "jargon_ascii_min_length": 2,
    "jargon_ascii_max_length": 24,
    "jargon_abbreviation_max_length": 8,
    "jargon_max_cjk_run": 64,
    "jargon_max_raw_candidates": 2000,
    "expression_decay_rate": 0.10,
    "jargon_decay_rate": 0.05,
    "stale_after_days": 180,
    "near_duplicate_jaccard": 0.70,
    "near_duplicate_cosine": 0.85,
    "threshold_source": "initial_shadow",
}


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value: float | None) -> bool:
    return value is None or math.isfinite(float(value))


@dataclass(frozen=True, slots=True)
class LearningQualityProfile:
    version: str
    parameters_hash: str
    parameters: Mapping[str, int | float | str | bool]

    def __post_init__(self) -> None:
        version = str(self.version or "").strip()
        if not version:
            raise ValueError("quality_profile_version_required")
        normalized = dict(self.parameters)
        if any(not isinstance(value, (str, int, float, bool)) for value in normalized.values()):
            raise TypeError("quality_profile_parameter_type_invalid")
        if any(isinstance(value, float) and not math.isfinite(value) for value in normalized.values()):
            raise ValueError("quality_profile_parameter_non_finite")
        expected = _canonical_hash(normalized)
        if self.parameters_hash != expected:
            raise ValueError("quality_profile_hash_mismatch")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "parameters", MappingProxyType(normalized))

    @classmethod
    def quality_v1(cls) -> "LearningQualityProfile":
        parameters = dict(_QUALITY_V1_PARAMETERS)
        return cls(
            version="quality-v1",
            parameters_hash=_canonical_hash(parameters),
            parameters=parameters,
        )


@dataclass(frozen=True, slots=True)
class CandidateQualityFeatures:
    quality_id: str
    candidate_id: str
    candidate_revision: int
    profile_version: str
    profile_hash: str
    window_start: float
    window_end: float
    eligible_message_count: int
    unknown_message_count: int
    support_count: int
    speaker_support: int | None
    speaker_message_count: int | None
    group_support: int
    group_message_count: int
    other_support: int | None
    other_total: int | None
    distinct_turns: int
    distinct_turn_count: int
    distinct_day_count: int
    context_diversity: int
    g2: float | None
    log2_effect: float | None
    signed_log2_lift: float | None
    p_value: float | None
    fdr_q: float | None
    pmi: float | None
    left_entropy_bits: float | None
    right_entropy_bits: float | None
    burst_ratio: float | None
    first_seen_at: float | None
    last_seen_at: float | None
    feature_complete: bool
    missing_reasons: tuple[str, ...]
    confidence_tier: Literal["high", "low", "insufficient", "unknown"]
    reasons: tuple[str, ...]
    created_at: float

    def __post_init__(self) -> None:
        for name in ("quality_id", "candidate_id", "profile_version", "profile_hash"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name}_required")
        count_names = (
            "candidate_revision",
            "eligible_message_count",
            "unknown_message_count",
            "support_count",
            "group_support",
            "group_message_count",
            "distinct_turns",
            "distinct_turn_count",
            "distinct_day_count",
            "context_diversity",
        )
        nullable_counts = (
            "speaker_support",
            "speaker_message_count",
            "other_support",
            "other_total",
        )
        if any(
            not isinstance(getattr(self, name), int)
            or isinstance(getattr(self, name), bool)
            for name in count_names
        ):
            raise ValueError("invalid_quality_count")
        if any(getattr(self, name) < 0 for name in count_names):
            raise ValueError("negative_quality_count")
        if any(
            getattr(self, name) is not None
            and (
                not isinstance(getattr(self, name), int)
                or isinstance(getattr(self, name), bool)
            )
            for name in nullable_counts
        ):
            raise ValueError("invalid_quality_count")
        if any(
            getattr(self, name) is not None and getattr(self, name) < 0
            for name in nullable_counts
        ):
            raise ValueError("negative_quality_count")
        if not math.isfinite(self.window_start) or not math.isfinite(self.window_end):
            raise ValueError("invalid_quality_window")
        if self.window_end <= self.window_start:
            raise ValueError("invalid_quality_window")
        if not math.isfinite(self.created_at):
            raise ValueError("invalid_created_at")
        numeric_names = (
            "g2",
            "log2_effect",
            "signed_log2_lift",
            "p_value",
            "fdr_q",
            "pmi",
            "left_entropy_bits",
            "right_entropy_bits",
            "burst_ratio",
            "first_seen_at",
            "last_seen_at",
        )
        if any(not _finite(getattr(self, name)) for name in numeric_names):
            raise ValueError("non_finite_quality_feature")
        if self.distinct_turns != self.distinct_turn_count:
            raise ValueError("distinct_turn_count_mismatch")
        if self.log2_effect != self.signed_log2_lift:
            raise ValueError("signed_log2_lift_mismatch")
        if self.support_count > self.eligible_message_count:
            raise ValueError("support_exceeds_eligible")
        if self.group_support > self.group_message_count:
            raise ValueError("group_support_exceeds_denominator")
        if self.group_message_count + self.unknown_message_count != self.eligible_message_count:
            raise ValueError("eligible_denominator_conservation_failed")
        speaker_values = (self.speaker_support, self.speaker_message_count)
        other_values = (self.other_support, self.other_total)
        contingency_values = (*speaker_values, *other_values)
        if any(value is None for value in contingency_values) and not all(
            value is None for value in contingency_values
        ):
            raise ValueError("incomplete_contingency_counts")
        if all(value is not None for value in contingency_values):
            assert self.speaker_support is not None
            assert self.speaker_message_count is not None
            assert self.other_support is not None
            assert self.other_total is not None
            if self.speaker_support + self.other_support != self.group_support:
                raise ValueError("support_conservation_failed")
            if self.speaker_message_count + self.other_total != self.group_message_count:
                raise ValueError("denominator_conservation_failed")
            if self.speaker_support > self.speaker_message_count:
                raise ValueError("speaker_support_exceeds_denominator")
            if self.other_support > self.other_total:
                raise ValueError("other_support_exceeds_denominator")
        if self.confidence_tier not in {"high", "low", "insufficient", "unknown"}:
            raise ValueError("invalid_confidence_tier")
        object.__setattr__(self, "missing_reasons", tuple(dict.fromkeys(self.missing_reasons)))
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    def to_mapping(self) -> dict[str, Any]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True, slots=True)
class CandidateQualityDecision:
    candidate_id: str
    decision: Literal[
        "high_confidence_shadow",
        "low_confidence_shadow",
        "group_shadow_only",
        "duplicate_hint",
        "insufficient_data",
        "rejected_deterministic",
        "blocked",
    ]
    decision_version: str
    threshold_version: str
    reasons: tuple[str, ...]
    shadow_eligible_for_enrichment: bool
    shadow_eligible_for_review: bool
    retryable: bool

    def __post_init__(self) -> None:
        if not str(self.candidate_id or "").strip():
            raise ValueError("candidate_id_required")
        if self.decision not in {
            "high_confidence_shadow",
            "low_confidence_shadow",
            "group_shadow_only",
            "duplicate_hint",
            "insufficient_data",
            "rejected_deterministic",
            "blocked",
        }:
            raise ValueError("invalid_quality_decision")
        if not self.decision_version or not self.threshold_version:
            raise ValueError("quality_decision_version_required")
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))


def candidate_quality_from_mapping(payload: Mapping[str, Any]) -> CandidateQualityFeatures:
    values = dict(payload)
    aliases = {
        "feature_version": "profile_version",
        "q_value": "fdr_q",
        "min_split_pmi": "pmi",
        "left_entropy": "left_entropy_bits",
        "right_entropy": "right_entropy_bits",
    }
    for alias, canonical in aliases.items():
        if canonical not in values and alias in values:
            values[canonical] = values[alias]
        values.pop(alias, None)
    values.setdefault("signed_log2_lift", values.get("log2_effect"))
    values.setdefault("distinct_turn_count", values.get("distinct_turns", 0))
    values["missing_reasons"] = tuple(values.get("missing_reasons") or ())
    values["reasons"] = tuple(values.get("reasons") or ())
    allowed = {item.name for item in fields(CandidateQualityFeatures)}
    return CandidateQualityFeatures(**{key: value for key, value in values.items() if key in allowed})


__all__ = [
    "CandidateQualityDecision",
    "CandidateQualityFeatures",
    "LearningQualityProfile",
    "candidate_quality_from_mapping",
]
