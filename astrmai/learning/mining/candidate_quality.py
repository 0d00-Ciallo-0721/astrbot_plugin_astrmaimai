from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import replace
from typing import Any, Iterable, Mapping

from ..dedup.normalization import normalize_expression_text
from ..quality.contracts import (
    CandidateQualityDecision,
    CandidateQualityFeatures,
    LearningQualityProfile,
)


DAY_SECONDS = 86_400.0
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_]{2,24}(?![A-Za-z0-9_])")
_PUNCTUATION = re.compile(r"[^A-Za-z0-9\u4e00-\u9fff]+")


class QualitySourceIdentityError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


def _valid_counts(*values: int | float) -> bool:
    return all(
        isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
        and float(value).is_integer()
        for value in values
    )


def compute_g2(a: int, b: int, c: int, d: int) -> float | None:
    if not _valid_counts(a, b, c, d):
        return None
    row1, row2 = a + b, c + d
    total = row1 + row2
    if row1 <= 0 or row2 <= 0 or total <= 0:
        return None
    col1, col2 = a + c, b + d
    observed = (float(a), float(b), float(c), float(d))
    expected = (
        row1 * col1 / total,
        row1 * col2 / total,
        row2 * col1 / total,
        row2 * col2 / total,
    )
    if any(value <= 0 or not math.isfinite(value) for value in expected):
        return None
    value = 2.0 * sum(
        item * math.log(item / target)
        for item, target in zip(observed, expected)
        if item > 0
    )
    return max(value, 0.0)


def compute_log2_effect(
    a: int,
    b: int,
    c: int,
    d: int,
    *,
    correction: float = 0.5,
) -> float | None:
    if not _valid_counts(a, b, c, d) or not math.isfinite(correction) or correction <= 0:
        return None
    speaker_total, other_total = a + b, c + d
    if speaker_total <= 0 or other_total <= 0:
        return None
    speaker_rate = (a + correction) / (speaker_total + 2.0 * correction)
    other_rate = (c + correction) / (other_total + 2.0 * correction)
    if speaker_rate <= 0 or other_rate <= 0:
        return None
    return math.log2(speaker_rate / other_rate)


compute_signed_log2_lift = compute_log2_effect


def g2_p_value_df1(g2: float | None) -> float | None:
    if g2 is None or not math.isfinite(g2) or g2 < 0:
        return None
    return min(max(math.erfc(math.sqrt(g2 / 2.0)), 0.0), 1.0)


def benjamini_hochberg(
    items: Iterable[tuple[str, float | None]],
) -> dict[str, float | None]:
    materialized = [(str(key), value) for key, value in items]
    valid = sorted(
        (
            (key, float(value))
            for key, value in materialized
            if value is not None and math.isfinite(float(value)) and 0 <= float(value) <= 1
        ),
        key=lambda item: (item[1], item[0]),
    )
    result = {key: None for key, _value in materialized}
    running = 1.0
    size = len(valid)
    for index in range(size - 1, -1, -1):
        key, p_value = valid[index]
        rank = index + 1
        running = min(running, p_value * size / rank, 1.0)
        result[key] = max(running, 0.0)
    return dict(sorted(result.items()))


def compute_pmi(
    term: str,
    document_frequencies: Mapping[str, int],
    total_documents: int,
) -> float | None:
    normalized = str(term or "")
    if total_documents <= 0 or len(normalized) < 2:
        return None
    term_df = int(document_frequencies.get(normalized, 0) or 0)
    if term_df < 0:
        return None
    probability = (term_df + 1.0) / (total_documents + 1.0)
    values: list[float] = []
    for index in range(1, len(normalized)):
        left, right = normalized[:index], normalized[index:]
        left_df = int(document_frequencies.get(left, 0) or 0)
        right_df = int(document_frequencies.get(right, 0) or 0)
        if left_df < 0 or right_df < 0:
            return None
        left_probability = (left_df + 1.0) / (total_documents + 1.0)
        right_probability = (right_df + 1.0) / (total_documents + 1.0)
        values.append(math.log2(probability / (left_probability * right_probability)))
    return min(values) if values else None


compute_min_split_pmi = compute_pmi


def shannon_entropy(neighbor_counts: Mapping[str, int]) -> float | None:
    if any(not _valid_counts(value) for value in neighbor_counts.values()):
        return None
    total = sum(int(value) for value in neighbor_counts.values())
    if total <= 0:
        return None
    return -sum(
        (count / total) * math.log2(count / total)
        for count in neighbor_counts.values()
        if count > 0
    )


def compute_burst_ratio(
    recent_df: int,
    recent_total: int,
    baseline_df: int,
    baseline_total: int,
) -> float | None:
    if not _valid_counts(recent_df, recent_total, baseline_df, baseline_total):
        return None
    if recent_total <= 0 or baseline_total <= 0:
        return None
    if recent_df > recent_total or baseline_df > baseline_total:
        return None
    recent_rate = (recent_df + 0.5) / (recent_total + 1.0)
    baseline_rate = (baseline_df + 0.5) / (baseline_total + 1.0)
    return recent_rate / baseline_rate


def _decision(
    features: CandidateQualityFeatures,
    decision: str,
    reasons: Iterable[str],
    *,
    retryable: bool = False,
) -> CandidateQualityDecision:
    legacy_eligible = decision not in {"blocked", "rejected_deterministic"}
    return CandidateQualityDecision(
        candidate_id=features.candidate_id,
        decision=decision,
        decision_version="candidate-quality-shadow-v1",
        threshold_version=features.profile_version,
        reasons=tuple(dict.fromkeys(reasons)),
        shadow_eligible_for_enrichment=legacy_eligible,
        shadow_eligible_for_review=legacy_eligible,
        retryable=retryable,
    )


def decide_expression_shadow(
    features: CandidateQualityFeatures,
    profile: LearningQualityProfile,
) -> CandidateQualityDecision:
    if features.profile_version != profile.version or features.profile_hash != profile.parameters_hash:
        return _decision(features, "blocked", ("unknown_profile",))
    if features.speaker_message_count is None or features.other_total is None:
        return _decision(features, "group_shadow_only", ("speaker_denominator_unavailable",))
    reasons: list[str] = []
    if features.speaker_message_count < int(profile.parameters["expression_min_speaker_messages"]):
        reasons.append("insufficient_speaker_messages")
    if (features.speaker_support or 0) < int(profile.parameters["expression_min_support"]):
        reasons.append("insufficient_candidate_support")
    if features.distinct_turns < int(profile.parameters["expression_min_distinct_turns"]):
        reasons.append("insufficient_turn_diversity")
    if features.other_total <= 0:
        reasons.append("empty_other_denominator")
    if features.log2_effect is None or features.g2 is None or features.fdr_q is None:
        reasons.append("statistics_unavailable")
    elif features.log2_effect < 0:
        return _decision(features, "group_shadow_only", ("negative_lift",))
    else:
        if features.log2_effect < float(profile.parameters["expression_min_log2_effect"]):
            reasons.append("effect_below_threshold")
        if features.g2 < float(profile.parameters["expression_min_g2"]):
            reasons.append("g2_below_threshold")
        if features.fdr_q > float(profile.parameters["expression_max_fdr_q"]):
            reasons.append("fdr_above_threshold")
    if reasons:
        return _decision(features, "insufficient_data", reasons)
    return _decision(features, "high_confidence_shadow", ("expression_high_confidence",))


def decide_jargon_shadow(
    features: CandidateQualityFeatures,
    profile: LearningQualityProfile,
) -> CandidateQualityDecision:
    if features.profile_version != profile.version or features.profile_hash != profile.parameters_hash:
        return _decision(features, "blocked", ("unknown_profile",))
    if features.support_count < int(profile.parameters["jargon_min_support"]):
        return _decision(features, "insufficient_data", ("insufficient_candidate_support",))
    if features.left_entropy_bits is None or features.right_entropy_bits is None:
        return _decision(features, "insufficient_data", ("insufficient_neighbors",))
    if (
        features.left_entropy_bits < float(profile.parameters["jargon_min_left_entropy_bits"])
        or features.right_entropy_bits < float(profile.parameters["jargon_min_right_entropy_bits"])
    ):
        return _decision(features, "low_confidence_shadow", ("boundary_entropy_below_threshold",))
    return _decision(features, "high_confidence_shadow", ("jargon_boundary_high_confidence",))


def _quality_id(
    candidate_id: str,
    candidate_revision: int,
    profile_version: str,
) -> str:
    payload = json.dumps(
        [candidate_id, candidate_revision, profile_version],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "quality:" + hashlib.sha256(payload).hexdigest()


def build_expression_features(
    *,
    candidate_id: str,
    candidate_revision: int,
    profile: LearningQualityProfile,
    window_end: float,
    a: int,
    b: int,
    c: int,
    d: int,
    eligible_message_count: int,
    unknown_message_count: int,
    distinct_turns: int,
    distinct_day_count: int,
    context_diversity: int,
    first_seen_at: float | None,
    last_seen_at: float | None,
    created_at: float,
) -> tuple[CandidateQualityFeatures | None, CandidateQualityDecision]:
    if not _valid_counts(
        a, b, c, d, eligible_message_count, unknown_message_count,
        distinct_turns, distinct_day_count, context_diversity,
    ) or not math.isfinite(window_end):
        return None, CandidateQualityDecision(
            candidate_id=str(candidate_id or "invalid-candidate"),
            decision="blocked",
            decision_version="candidate-quality-shadow-v1",
            threshold_version=profile.version,
            reasons=("invalid_feature",),
            shadow_eligible_for_enrichment=False,
            shadow_eligible_for_review=False,
            retryable=False,
        )
    g2 = compute_g2(a, b, c, d)
    effect = compute_log2_effect(a, b, c, d)
    p_value = g2_p_value_df1(g2)
    missing = tuple(
        reason
        for value, reason in (
            (g2, "g2_unavailable"),
            (effect, "log2_effect_unavailable"),
            (p_value, "p_value_unavailable"),
        )
        if value is None
    )
    features = CandidateQualityFeatures(
        quality_id=_quality_id(candidate_id, candidate_revision, profile.version),
        candidate_id=candidate_id,
        candidate_revision=candidate_revision,
        profile_version=profile.version,
        profile_hash=profile.parameters_hash,
        window_start=window_end - float(profile.parameters["expression_window_days"]) * DAY_SECONDS,
        window_end=window_end,
        eligible_message_count=eligible_message_count,
        unknown_message_count=unknown_message_count,
        support_count=a,
        speaker_support=a,
        speaker_message_count=a + b,
        group_support=a + c,
        group_message_count=a + b + c + d,
        other_support=c,
        other_total=c + d,
        distinct_turns=distinct_turns,
        distinct_turn_count=distinct_turns,
        distinct_day_count=distinct_day_count,
        context_diversity=context_diversity,
        g2=g2,
        log2_effect=effect,
        signed_log2_lift=effect,
        p_value=p_value,
        fdr_q=None,
        pmi=None,
        left_entropy_bits=None,
        right_entropy_bits=None,
        burst_ratio=None,
        first_seen_at=first_seen_at,
        last_seen_at=last_seen_at,
        feature_complete=not missing,
        missing_reasons=missing,
        confidence_tier="unknown" if missing else "low",
        reasons=(),
        created_at=created_at,
    )
    return features, decide_expression_shadow(features, profile)


def build_expression_quality_snapshots(
    candidates: Iterable[Mapping[str, Any]],
    messages: Iterable[Mapping[str, Any]],
    *,
    profile: LearningQualityProfile,
    window_end: float,
) -> tuple[CandidateQualityFeatures, ...]:
    if profile.version != "quality-v1" or not math.isfinite(window_end):
        return ()
    window_start = window_end - float(profile.parameters["expression_window_days"]) * DAY_SECONDS
    eligible: dict[str, dict[str, Any]] = {}
    seen_sources: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(messages):
        message = dict(raw)
        source_id = _normalized_source_id(message, index)
        if not source_id:
            raise QualitySourceIdentityError("source_identity_unavailable")
        try:
            timestamp = float(message.get("timestamp"))
        except (TypeError, ValueError):
            timestamp = None
        if timestamp is not None and not math.isfinite(timestamp):
            timestamp = None
        fact = {
            "timestamp": timestamp,
            "content": str(message.get("content") or ""),
            "eligible": bool(message.get("eligible", False)),
            "known_scope": bool(message.get("known_scope", False)),
            "speaker_scope_id": (
                str(message.get("speaker_scope_id") or "")
                if bool(message.get("eligible_for_speaker_stats", False))
                else ""
            ),
        }
        if source_id in seen_sources and seen_sources[source_id] != fact:
            raise QualitySourceIdentityError("source_identity_conflict")
        seen_sources[source_id] = fact
        if not fact["eligible"]:
            continue
        if timestamp is not None and not window_start <= timestamp < window_end:
            continue
        eligible[source_id] = fact
    known = {
        key: item
        for key, item in eligible.items()
        if item["known_scope"] and item["timestamp"] is not None
    }
    known_ids = set(known)
    unknown_timestamp_count = sum(
        item["timestamp"] is None for item in eligible.values()
    )
    pending_personal: dict[str, list[CandidateQualityFeatures]] = defaultdict(list)
    snapshots: list[CandidateQualityFeatures] = []
    for raw in sorted(
        (dict(item) for item in candidates),
        key=lambda item: str(item.get("candidate_id") or ""),
    ):
        candidate_id = str(raw.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        revision = int(raw.get("candidate_revision", 0) or 0)
        support_ids = {
            str(item).strip()
            for item in raw.get("source_message_ids", ())
            if str(item).strip()
        }
        supported = support_ids & known_ids
        timestamps = [float(known[key]["timestamp"]) for key in supported]
        group_support = len(supported)
        distinct_turns = group_support
        distinct_days = len({int(value // DAY_SECONDS) for value in timestamps})
        context_diversity = len(
            {
                str(known[key]["speaker_scope_id"])
                for key in supported
                if str(known[key]["speaker_scope_id"])
            }
        )
        timestamp_missing = ("timestamp_unavailable",) if unknown_timestamp_count else ()
        speaker_scope_id = str(raw.get("speaker_scope_id") or "").strip()
        if not speaker_scope_id:
            snapshots.append(
                CandidateQualityFeatures(
                    quality_id=_quality_id(candidate_id, revision, profile.version),
                    candidate_id=candidate_id,
                    candidate_revision=revision,
                    profile_version=profile.version,
                    profile_hash=profile.parameters_hash,
                    window_start=window_start,
                    window_end=window_end,
                    eligible_message_count=len(eligible),
                    unknown_message_count=len(eligible) - len(known),
                    support_count=group_support,
                    speaker_support=None,
                    speaker_message_count=None,
                    group_support=group_support,
                    group_message_count=len(known),
                    other_support=None,
                    other_total=None,
                    distinct_turns=distinct_turns,
                    distinct_turn_count=distinct_turns,
                    distinct_day_count=distinct_days,
                    context_diversity=context_diversity,
                    g2=None,
                    log2_effect=None,
                    signed_log2_lift=None,
                    p_value=None,
                    fdr_q=None,
                    pmi=None,
                    left_entropy_bits=None,
                    right_entropy_bits=None,
                    burst_ratio=None,
                    first_seen_at=min(timestamps) if timestamps else None,
                    last_seen_at=max(timestamps) if timestamps else None,
                    feature_complete=False,
                    missing_reasons=("speaker_denominator_unavailable", *timestamp_missing),
                    confidence_tier="unknown",
                    reasons=("speaker_denominator_unavailable",),
                    created_at=window_end,
                )
            )
            continue
        speaker_ids = {
            key for key, item in known.items() if item["speaker_scope_id"] == speaker_scope_id
        }
        a = len(supported & speaker_ids)
        b = len(speaker_ids) - a
        c = group_support - a
        d = len(known) - len(speaker_ids) - c
        features, _decision_value = build_expression_features(
            candidate_id=candidate_id,
            candidate_revision=revision,
            profile=profile,
            window_end=window_end,
            a=a,
            b=b,
            c=c,
            d=d,
            eligible_message_count=len(eligible),
            unknown_message_count=len(eligible) - len(known),
            distinct_turns=distinct_turns,
            distinct_day_count=distinct_days,
            context_diversity=context_diversity,
            first_seen_at=min(timestamps) if timestamps else None,
            last_seen_at=max(timestamps) if timestamps else None,
            created_at=window_end,
        )
        if features is not None:
            if timestamp_missing:
                features = replace(
                    features,
                    feature_complete=False,
                    missing_reasons=(*features.missing_reasons, *timestamp_missing),
                )
            pending_personal[speaker_scope_id].append(features)
    for _speaker, family in sorted(pending_personal.items()):
        q_values = benjamini_hochberg(
            (features.candidate_id, features.p_value) for features in family
        )
        for features in family:
            with_q = replace(features, fdr_q=q_values[features.candidate_id])
            decision = decide_expression_shadow(with_q, profile)
            snapshots.append(
                replace(
                    with_q,
                    feature_complete=with_q.feature_complete and with_q.fdr_q is not None,
                    confidence_tier=(
                        "high"
                        if decision.decision == "high_confidence_shadow"
                        else "insufficient"
                        if decision.decision == "insufficient_data"
                        else "low"
                    ),
                    reasons=decision.reasons,
                )
            )
    return tuple(sorted(snapshots, key=lambda item: item.candidate_id))


def build_jargon_quality_snapshots(
    candidates: Iterable[Mapping[str, Any]],
    scan: Mapping[str, Any],
    *,
    profile: LearningQualityProfile,
    created_at: float,
) -> tuple[CandidateQualityFeatures, ...]:
    report = dict(scan.get("report") or {})
    scanned = dict(scan.get("candidates") or {})
    if report.get("status") != "completed" or profile.version != "quality-v1":
        return ()
    eligible = int(report.get("eligible_documents", 0) or 0)
    unknown_timestamps = int(report.get("unknown_timestamps", 0) or 0)
    snapshots: list[CandidateQualityFeatures] = []
    for raw in sorted(
        (dict(item) for item in candidates),
        key=lambda item: str(item.get("candidate_id") or ""),
    ):
        candidate_id = str(raw.get("candidate_id") or "").strip()
        term = unicodedata.normalize(
            "NFKC",
            str(raw.get("canonical_form") or raw.get("content") or ""),
        ).strip().lower()
        item = scanned.get(term)
        if not candidate_id or not isinstance(item, Mapping):
            continue
        support = int(item.get("support_count", 0) or 0)
        missing = tuple(
            reason
            for value, reason in (
                (item.get("pmi"), "pmi_unavailable"),
                (item.get("left_entropy_bits"), "left_entropy_unavailable"),
                (item.get("right_entropy_bits"), "right_entropy_unavailable"),
                (item.get("burst_ratio"), "burst_unavailable"),
            )
            if value is None
        )
        if unknown_timestamps:
            missing = (*missing, "timestamp_unavailable")
        features = CandidateQualityFeatures(
            quality_id=_quality_id(
                candidate_id,
                int(raw.get("candidate_revision", 0) or 0),
                profile.version,
            ),
            candidate_id=candidate_id,
            candidate_revision=int(raw.get("candidate_revision", 0) or 0),
            profile_version=profile.version,
            profile_hash=profile.parameters_hash,
            window_start=float(report["window_start"]),
            window_end=float(report["window_end"]),
            eligible_message_count=eligible + unknown_timestamps,
            unknown_message_count=unknown_timestamps,
            support_count=support,
            speaker_support=None,
            speaker_message_count=None,
            group_support=support,
            group_message_count=eligible,
            other_support=None,
            other_total=None,
            distinct_turns=support,
            distinct_turn_count=support,
            distinct_day_count=0,
            context_diversity=0,
            g2=None,
            log2_effect=None,
            signed_log2_lift=None,
            p_value=None,
            fdr_q=None,
            pmi=None if item.get("pmi") is None else float(item["pmi"]),
            left_entropy_bits=(
                None
                if item.get("left_entropy_bits") is None
                else float(item["left_entropy_bits"])
            ),
            right_entropy_bits=(
                None
                if item.get("right_entropy_bits") is None
                else float(item["right_entropy_bits"])
            ),
            burst_ratio=(
                None
                if item.get("burst_ratio") is None
                else float(item["burst_ratio"])
            ),
            first_seen_at=(
                None
                if item.get("first_seen_at") is None
                else float(item["first_seen_at"])
            ),
            last_seen_at=(
                None
                if item.get("last_seen_at") is None
                else float(item["last_seen_at"])
            ),
            feature_complete=not missing,
            missing_reasons=missing,
            confidence_tier="unknown" if missing else "low",
            reasons=(),
            created_at=float(created_at),
        )
        decision = decide_jargon_shadow(features, profile)
        snapshots.append(
            replace(
                features,
                confidence_tier=(
                    "high"
                    if decision.decision == "high_confidence_shadow"
                    else "insufficient"
                    if decision.decision == "insufficient_data"
                    else "low"
                ),
                reasons=decision.reasons,
            )
        )
    return tuple(snapshots)


def build_expression_shadow_report(
    candidates: Iterable[Mapping[str, Any]],
    messages: Iterable[Mapping[str, Any]],
    *,
    profile: LearningQualityProfile,
    window_end: float,
) -> dict[str, Any]:
    if profile.version != "quality-v1" or not math.isfinite(window_end):
        return {
            "status": "blocked",
            "reason": "unknown_profile" if profile.version != "quality-v1" else "invalid_window",
            "provider_call_count": 0,
        }
    window_start = window_end - float(profile.parameters["expression_window_days"]) * DAY_SECONDS
    eligible: dict[str, dict[str, Any]] = {}
    unknown_timestamps = 0
    for index, raw in enumerate(messages):
        message = dict(raw)
        if not bool(message.get("eligible", False)):
            continue
        try:
            timestamp = float(message.get("timestamp"))
        except (TypeError, ValueError):
            unknown_timestamps += 1
            continue
        if not math.isfinite(timestamp):
            unknown_timestamps += 1
            continue
        if not window_start <= timestamp < window_end:
            continue
        source_id = _normalized_source_id(message, index)
        if not source_id:
            return {
                "status": "blocked",
                "reason": "source_identity_unavailable",
                "provider_call_count": 0,
            }
        current = eligible.get(source_id)
        normalized = {
            "timestamp": timestamp,
            "known_scope": bool(message.get("known_scope", False)),
            "speaker_scope_id": (
                str(message.get("speaker_scope_id") or "")
                if bool(message.get("eligible_for_speaker_stats", False))
                else ""
            ),
        }
        if current is not None and current != normalized:
            return {
                "status": "blocked",
                "reason": "source_identity_conflict",
                "provider_call_count": 0,
            }
        eligible[source_id] = normalized

    known = {key: item for key, item in eligible.items() if item["known_scope"]}
    speakers = sorted(
        {
            str(item["speaker_scope_id"])
            for item in known.values()
            if str(item["speaker_scope_id"])
        }
    )
    preliminary: list[tuple[str, str, CandidateQualityFeatures]] = []
    candidate_reports: dict[str, dict[str, Any]] = {}
    for raw_candidate in sorted(
        (dict(item) for item in candidates),
        key=lambda item: str(item.get("candidate_id") or ""),
    ):
        candidate_id = str(raw_candidate.get("candidate_id") or "").strip()
        if not candidate_id:
            continue
        support_ids = {
            str(item).strip()
            for item in raw_candidate.get("source_message_ids", ())
            if str(item).strip()
        }
        group_support = len(support_ids & set(known))
        report = {
            "candidate_id": candidate_id,
            "group_support": group_support,
            "group_message_count": len(known),
            "decision": "group_shadow_only",
            "personal": [],
        }
        candidate_reports[candidate_id] = report
        for speaker in speakers:
            speaker_ids = {
                key for key, item in known.items() if item["speaker_scope_id"] == speaker
            }
            a = len(support_ids & speaker_ids)
            b = len(speaker_ids) - a
            c = group_support - a
            d = len(known) - len(speaker_ids) - c
            timestamps = [
                float(known[key]["timestamp"])
                for key in support_ids & set(known)
            ]
            features, _decision_value = build_expression_features(
                candidate_id=candidate_id,
                candidate_revision=int(raw_candidate.get("candidate_revision", 0) or 0),
                profile=profile,
                window_end=window_end,
                a=a,
                b=b,
                c=c,
                d=d,
                eligible_message_count=len(eligible),
                unknown_message_count=len(eligible) - len(known),
                distinct_turns=len(support_ids & set(known)),
                distinct_day_count=len({int(value // DAY_SECONDS) for value in timestamps}),
                context_diversity=int(raw_candidate.get("distinct_contributor_count", 0) or 0),
                first_seen_at=min(timestamps) if timestamps else None,
                last_seen_at=max(timestamps) if timestamps else None,
                created_at=window_end,
            )
            if features is not None:
                preliminary.append((candidate_id, speaker, features))

    by_speaker: dict[str, list[tuple[str, CandidateQualityFeatures]]] = defaultdict(list)
    for candidate_id, speaker, features in preliminary:
        by_speaker[speaker].append((candidate_id, features))
    decision_counts: Counter[str] = Counter()
    for speaker, family in sorted(by_speaker.items()):
        q_values = benjamini_hochberg(
            (candidate_id, features.p_value) for candidate_id, features in family
        )
        speaker_key = hashlib.sha256(speaker.encode("utf-8")).hexdigest()[:16]
        for candidate_id, features in family:
            completed = replace(features, fdr_q=q_values[candidate_id])
            decision = decide_expression_shadow(completed, profile)
            decision_counts[decision.decision] += 1
            candidate_reports[candidate_id]["personal"].append(
                {
                    "speaker_key": speaker_key,
                    "support_count": completed.support_count,
                    "speaker_message_count": completed.speaker_message_count,
                    "other_total": completed.other_total,
                    "g2": completed.g2,
                    "log2_effect": completed.log2_effect,
                    "fdr_q": completed.fdr_q,
                    "decision": decision.decision,
                    "reasons": list(decision.reasons),
                }
            )
    return {
        "status": "completed",
        "profile_version": profile.version,
        "profile_hash": profile.parameters_hash,
        "window_start": window_start,
        "window_end": window_end,
        "eligible_message_count": len(eligible),
        "known_scope_message_count": len(known),
        "unknown_message_count": len(eligible) - len(known),
        "unknown_timestamp_count": unknown_timestamps,
        "candidate_count": len(candidate_reports),
        "personal_test_count": sum(decision_counts.values()),
        "decision_counts": dict(sorted(decision_counts.items())),
        "candidates": [candidate_reports[key] for key in sorted(candidate_reports)],
        "provider_call_count": 0,
    }


def _normalized_source_id(message: Mapping[str, Any], index: int) -> str:
    del index
    return str(message.get("source_id") or message.get("source_row_id") or "").strip()


def scan_jargon_shadow(
    messages: Iterable[Mapping[str, Any]],
    *,
    profile: LearningQualityProfile,
    window_end: float,
) -> dict[str, Any]:
    if profile.version != "quality-v1" or not math.isfinite(window_end):
        return {
            "candidates": {},
            "report": {
                "status": "blocked",
                "reason": "unknown_profile" if profile.version != "quality-v1" else "invalid_window",
                "scan_passes": 0,
                "provider_call_count": 0,
            },
        }
    unique: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(messages):
        message = dict(raw)
        source_id = _normalized_source_id(message, index)
        if not source_id:
            return {
                "candidates": {},
                "report": {
                    "status": "blocked",
                    "reason": "source_identity_unavailable",
                    "scan_passes": 0,
                    "provider_call_count": 0,
                },
            }
        try:
            timestamp = float(message.get("timestamp"))
        except (TypeError, ValueError):
            timestamp = None
        if timestamp is not None and not math.isfinite(timestamp):
            timestamp = None
        fact = {
            "source_id": source_id,
            "content": str(message.get("content") or ""),
            "timestamp": timestamp,
            "eligible": bool(message.get("eligible", True)),
        }
        if source_id in unique and unique[source_id] != fact:
            return {
                "candidates": {},
                "report": {
                    "status": "blocked",
                    "reason": "source_identity_conflict",
                    "scan_passes": 0,
                    "provider_call_count": 0,
                },
            }
        unique[source_id] = fact
    ordered = [unique[key] for key in sorted(unique)]
    document_frequency: Counter[str] = Counter()
    left_neighbors: dict[str, Counter[str]] = defaultdict(Counter)
    right_neighbors: dict[str, Counter[str]] = defaultdict(Counter)
    recent_frequency: Counter[str] = Counter()
    baseline_frequency: Counter[str] = Counter()
    first_seen: dict[str, float] = {}
    last_seen: dict[str, float] = {}
    recent_total = 0
    baseline_total = 0
    input_chars = 0
    enumerated = 0
    truncated_chars = 0
    eligible_documents = 0
    unknown_timestamps = 0
    for index, message in enumerate(ordered):
        if not bool(message.get("eligible", True)):
            continue
        content = unicodedata.normalize("NFKC", str(message.get("content") or ""))
        if not content:
            continue
        try:
            timestamp = float(message.get("timestamp"))
        except (TypeError, ValueError):
            unknown_timestamps += 1
            continue
        if not math.isfinite(timestamp) or not (window_end - 14 * DAY_SECONDS <= timestamp < window_end):
            if not math.isfinite(timestamp):
                unknown_timestamps += 1
            continue
        eligible_documents += 1
        in_recent = timestamp >= window_end - DAY_SECONDS
        if in_recent:
            recent_total += 1
        else:
            baseline_total += 1
        input_chars += len(content)
        terms_in_document: set[str] = set()
        for match in _CJK_RUN.finditer(content):
            original_run = match.group(0)
            run = original_run[: int(profile.parameters["jargon_max_cjk_run"])]
            truncated_chars += max(len(original_run) - len(run), 0)
            max_length = min(int(profile.parameters["jargon_cjk_max_length"]), len(run))
            all_subterms: set[str] = set()
            for length in range(1, max_length + 1):
                for start in range(0, len(run) - length + 1):
                    term = run[start : start + length]
                    all_subterms.add(term)
                    if length < int(profile.parameters["jargon_cjk_min_length"]):
                        continue
                    enumerated += 1
                    terms_in_document.add(term)
                    left_neighbors[term][run[start - 1] if start else "<BOS>"] += 1
                    end = start + length
                    right_neighbors[term][run[end] if end < len(run) else "<EOS>"] += 1
            terms_in_document.update(all_subterms)
        for match in _ASCII_TOKEN.finditer(content):
            term = match.group(0).lower()
            enumerated += 1
            terms_in_document.add(term)
            left_neighbors[term][content[match.start() - 1] if match.start() else "<BOS>"] += 1
            right_neighbors[term][content[match.end()] if match.end() < len(content) else "<EOS>"] += 1
        for term in terms_in_document:
            document_frequency[term] += 1
            (recent_frequency if in_recent else baseline_frequency)[term] += 1
            first_seen[term] = min(first_seen.get(term, timestamp), timestamp)
            last_seen[term] = max(last_seen.get(term, timestamp), timestamp)
    candidate_terms = sorted(
        term for term in document_frequency
        if len(term) >= 2 and (_CJK_RUN.fullmatch(term) or re.fullmatch(r"[A-Za-z0-9_]{2,24}", term))
    )
    max_candidates = int(profile.parameters["jargon_max_raw_candidates"])
    retained = candidate_terms[:max_candidates]
    candidates: dict[str, dict[str, Any]] = {}
    for term in retained:
        support = int(document_frequency[term])
        left_entropy = shannon_entropy(left_neighbors[term])
        right_entropy = shannon_entropy(right_neighbors[term])
        burst = compute_burst_ratio(
            recent_frequency[term], recent_total,
            baseline_frequency[term], baseline_total,
        )
        abbreviation = bool(re.fullmatch(r"[A-Za-z]{2,8}", term))
        candidates[term] = {
            "support_count": support,
            "pmi": compute_pmi(term, document_frequency, eligible_documents),
            "left_entropy_bits": left_entropy,
            "right_entropy_bits": right_entropy,
            "burst_ratio": burst,
            "first_seen_at": first_seen.get(term),
            "last_seen_at": last_seen.get(term),
            "abbreviation_shape": abbreviation,
            "expansion_status": "unknown" if abbreviation else "not_applicable",
            "provider_call_count": 0,
        }
    return {
        "candidates": candidates,
        "report": {
            "status": "completed",
            "profile_version": profile.version,
            "profile_hash": profile.parameters_hash,
            "window_start": window_end - 14 * DAY_SECONDS,
            "window_end": window_end,
            "scan_passes": 1,
            "input_messages": len(ordered),
            "eligible_documents": eligible_documents,
            "unknown_timestamps": unknown_timestamps,
            "input_chars": input_chars,
            "enumerated_ngrams": enumerated,
            "raw_candidate_count": len(candidate_terms),
            "retained_candidate_count": len(retained),
            "truncated_count": max(len(candidate_terms) - len(retained), 0),
            "truncated_chars": truncated_chars,
            "provider_call_count": 0,
        },
    }


def resolve_alias(
    aliases: Mapping[str, str | Iterable[str]],
    key: str,
) -> tuple[str | None, str]:
    current = str(key)
    visited: set[str] = set()
    while current in aliases:
        if current in visited:
            return None, "alias_cycle"
        visited.add(current)
        target = aliases[current]
        if isinstance(target, str):
            if not target:
                return None, "alias_target_empty"
            current = target
            continue
        targets = tuple(dict.fromkeys(str(item) for item in target if str(item)))
        if len(targets) != 1:
            return None, "alias_conflict"
        current = targets[0]
    return current, "resolved" if visited else "not_aliased"


def _ngrams(value: str, size: int = 2) -> set[str]:
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def duplicate_hint(
    left: str,
    right: str,
    *,
    embedding_similarity: float | None = None,
    embedding_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_left = normalize_expression_text(left)
    normalized_right = normalize_expression_text(right)
    if normalized_left == normalized_right and normalized_left:
        return {"method": "normalized_exact", "similarity": 1.0, "action": "idempotent"}
    compact_left = _PUNCTUATION.sub("", normalized_left)
    compact_right = _PUNCTUATION.sub("", normalized_right)
    if compact_left and compact_left == compact_right:
        return {"method": "punctuation_variant", "similarity": 1.0, "action": "hint_only"}
    if embedding_similarity is not None:
        required = {"provider_id", "model_id", "dimension", "generation"}
        if not embedding_identity or not required.issubset(embedding_identity):
            return {
                "method": "unavailable",
                "reason": "embedding_identity_unavailable",
                "action": "preserve",
                "provider_call_count": 0,
            }
        if not math.isfinite(embedding_similarity):
            return {"method": "unavailable", "reason": "embedding_similarity_invalid", "action": "preserve", "provider_call_count": 0}
        return {"method": "embedding_cosine", "similarity": embedding_similarity, "action": "hint_only", "provider_call_count": 0}
    left_grams, right_grams = _ngrams(compact_left), _ngrams(compact_right)
    if not left_grams or not right_grams:
        return {"method": "unavailable", "reason": "insufficient_ngrams", "action": "preserve", "provider_call_count": 0}
    similarity = len(left_grams & right_grams) / len(left_grams | right_grams)
    return {"method": "jaccard_2gram", "similarity": similarity, "action": "hint_only", "provider_call_count": 0}


def decay_suggestion(
    *,
    candidate_family: str,
    last_seen_at: float | None,
    window_end: float,
) -> dict[str, Any]:
    if candidate_family not in {"expression", "jargon"} or last_seen_at is None:
        return {"status": "unavailable", "reason": "last_seen_unavailable", "state_changed": False}
    if not math.isfinite(last_seen_at) or not math.isfinite(window_end) or last_seen_at > window_end:
        return {"status": "blocked", "reason": "invalid_temporal_window", "state_changed": False}
    days = (window_end - last_seen_at) / DAY_SECONDS
    rate = 0.10 if candidate_family == "expression" else 0.05
    return {
        "status": "completed",
        "days_without_evidence": days,
        "decay_rate": rate,
        "factor": (1.0 - rate) ** days,
        "suggested_status": "stale" if days >= 180 else "candidate",
        "lifecycle_owner": "MemoryV2Store.apply_decay",
        "state_changed": False,
    }


__all__ = [
    "QualitySourceIdentityError",
    "benjamini_hochberg",
    "build_expression_features",
    "build_expression_quality_snapshots",
    "build_expression_shadow_report",
    "build_jargon_quality_snapshots",
    "compute_burst_ratio",
    "compute_g2",
    "compute_log2_effect",
    "compute_min_split_pmi",
    "compute_pmi",
    "compute_signed_log2_lift",
    "decay_suggestion",
    "decide_expression_shadow",
    "decide_jargon_shadow",
    "duplicate_hint",
    "g2_p_value_df1",
    "resolve_alias",
    "scan_jargon_shadow",
    "shannon_entropy",
]
