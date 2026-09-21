from __future__ import annotations

from dataclasses import replace
from collections import Counter
import math
from typing import Iterable

from ..contracts.learning_retrieval import (
    LearningRetrievalCandidate,
    LearningRetrievalSelection,
    RETRIEVAL_PROFILE_VERSION,
    RepetitionGuardResult,
)


class LearningRetrievalSelector:
    PROFILE_VERSION = RETRIEVAL_PROFILE_VERSION
    TOP_K = 3
    SAME_SCOPE_QUOTA = 2
    SAME_FINGERPRINT_QUOTA = 1
    COSINE_REPETITION_THRESHOLD = 0.80
    TRIGRAM_REPETITION_THRESHOLD = 0.50
    _TIER_ORDER = {
        "speaker_in_group": 0,
        "speaker": 1,
        "group": 2,
        "topic": 3,
        "global": 4,
    }

    @staticmethod
    def _strict_non_negative_int(value: object) -> bool:
        return type(value) is int and value >= 0

    @classmethod
    def _tier(
        cls,
        candidate: LearningRetrievalCandidate,
        *,
        scope_id: str,
        speaker_id: str,
    ) -> tuple[str, str]:
        candidate_scope = candidate.scope_id.strip()
        scope_parts = scope_id.split(":")
        if (
            len(scope_parts) != 3
            or not all(scope_parts)
            or scope_parts[1] not in {"group", "private"}
        ):
            return "", "scope_invalid"
        if not candidate_scope or not (
            candidate_scope == scope_id or candidate_scope == "global"
        ):
            return "", "scope_mismatch"
        speaker_scope = candidate.speaker_scope_id.strip()
        if speaker_scope:
            if not speaker_id:
                return "", "speaker_unknown"
            expected = f"{scope_id}:{speaker_id}"
            if speaker_scope != expected:
                return "", "speaker_scope_mismatch"
            return (
                "speaker_in_group" if scope_parts[1] == "group" else "speaker",
                "",
            )
        if candidate.topic_scope_id:
            if not candidate.topic_scope_id.startswith(f"{scope_id}:topic:"):
                return "", "topic_scope_mismatch"
            return "topic", ""
        if candidate_scope == "global":
            return "global", ""
        return "group", ""

    @classmethod
    def _block_reason(
        cls,
        candidate: LearningRetrievalCandidate,
        *,
        scope_id: str,
        speaker_id: str,
        current_generation: int,
    ) -> tuple[str, str]:
        tier, reason = cls._tier(candidate, scope_id=scope_id, speaker_id=speaker_id)
        if reason:
            return "", reason
        if candidate.lifecycle_status != "active":
            return tier, "asset_not_active"
        if candidate.membership_status != "current":
            return tier, "membership_not_current"
        if not cls._strict_non_negative_int(candidate.generation):
            return tier, "generation_unknown"
        if candidate.generation != current_generation:
            return tier, "generation_mismatch"
        for name in (
            "asset_revision", "candidate_revision", "review_revision", "admission_revision",
        ):
            if not cls._strict_non_negative_int(getattr(candidate, name)):
                return tier, f"{name}_unknown"
        if not candidate.asset_id.strip() or not candidate.canonical_memory_id.strip():
            return tier, "asset_identity_unavailable"
        if not candidate.fingerprint.strip():
            return tier, "fingerprint_unavailable"
        if not candidate.provenance_hash.strip():
            return tier, "provenance_unavailable"
        return tier, ""

    @staticmethod
    def _score(candidate: LearningRetrievalCandidate) -> float:
        parts = (
            (0.45, candidate.relevance),
            (0.25, candidate.asset_weight),
            (0.15, candidate.support),
            (0.10, candidate.decay),
            (0.05, candidate.evidence_quality),
        )
        values: list[float] = []
        for weight, raw in parts:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return -1.0
            numeric = float(raw)
            if not math.isfinite(numeric):
                return -1.0
            values.append(weight * min(max(numeric, 0.0), 1.0))
        score = sum(values)
        return round(score, 12) if math.isfinite(score) else -1.0

    def select(
        self,
        candidates: Iterable[LearningRetrievalCandidate],
        *,
        scope_id: str,
        speaker_id: str,
        current_generation: int,
        shadow_enabled: bool,
        prompt_injection_enabled: bool = False,
    ) -> LearningRetrievalSelection:
        blocked: list[LearningRetrievalCandidate] = []
        eligible: list[LearningRetrievalCandidate] = []
        if not shadow_enabled:
            blocked = [replace(item, blocked_reason="retrieval_shadow_disabled") for item in candidates]
            return LearningRetrievalSelection(blocked=tuple(sorted(blocked, key=lambda item: item.asset_id)))
        if type(current_generation) is not int or current_generation < 0:
            blocked = [replace(item, blocked_reason="current_generation_unknown") for item in candidates]
            return LearningRetrievalSelection(blocked=tuple(sorted(blocked, key=lambda item: item.asset_id)))
        for item in candidates:
            tier, reason = self._block_reason(
                item,
                scope_id=scope_id.strip(),
                speaker_id=speaker_id.strip(),
                current_generation=current_generation,
            )
            score = self._score(item)
            if score < 0 and not reason:
                reason = "score_input_invalid"
            classified = replace(item, tier=tier, score=max(score, 0.0), blocked_reason=reason)
            (blocked if reason else eligible).append(classified)
        eligible.sort(key=lambda item: (self._TIER_ORDER.get(item.tier, 99), -item.score, item.asset_id))
        selected: list[LearningRetrievalCandidate] = []
        scope_counts: dict[str, int] = {}
        fingerprints: set[str] = set()
        for item in eligible:
            if len(selected) >= self.TOP_K:
                blocked.append(replace(item, blocked_reason="top_k_exceeded"))
                continue
            if item.fingerprint in fingerprints:
                blocked.append(replace(item, blocked_reason="fingerprint_quota"))
                continue
            if scope_counts.get(item.scope_id, 0) >= self.SAME_SCOPE_QUOTA:
                blocked.append(replace(item, blocked_reason="scope_quota"))
                continue
            selected.append(item)
            fingerprints.add(item.fingerprint)
            scope_counts[item.scope_id] = scope_counts.get(item.scope_id, 0) + 1
        return LearningRetrievalSelection(
            selected=tuple(selected),
            blocked=tuple(sorted(blocked, key=lambda item: item.asset_id)),
            accepted_for_prompt=bool(selected and prompt_injection_enabled),
        )

    @staticmethod
    def _normalized_characters(value: str) -> list[str]:
        return [character for character in "".join(str(value or "").lower().split()) if character]

    @classmethod
    def repetition_guard(
        cls,
        *,
        source_examples: Iterable[str],
        model_output: str,
        attempt: int,
    ) -> RepetitionGuardResult:
        if type(attempt) is not int or attempt < 0:
            return RepetitionGuardResult(
                True, False, 0.0, 0.0, 0, reason_code="attempt_invalid",
            )
        source = cls._normalized_characters("\n".join(str(item or "") for item in source_examples))
        output = cls._normalized_characters(model_output)
        source_counts = Counter(source)
        output_counts = Counter(output)
        common = set(source_counts) & set(output_counts)
        numerator = sum(source_counts[token] * output_counts[token] for token in common)
        source_norm = math.sqrt(sum(value * value for value in source_counts.values()))
        output_norm = math.sqrt(sum(value * value for value in output_counts.values()))
        cosine = numerator / (source_norm * output_norm) if source_norm and output_norm else 0.0

        def trigrams(items: list[str]) -> set[str]:
            return {"".join(items[index:index + 3]) for index in range(max(0, len(items) - 2))}

        source_trigrams = trigrams(source)
        output_trigrams = trigrams(output)
        overlap = (
            len(source_trigrams & output_trigrams) / len(output_trigrams)
            if output_trigrams
            else 0.0
        )
        exceeded = (
            cosine > cls.COSINE_REPETITION_THRESHOLD
            or overlap > cls.TRIGRAM_REPETITION_THRESHOLD
        )
        return RepetitionGuardResult(
            blocked=bool(exceeded and attempt >= 1),
            regenerate=bool(exceeded and attempt == 0),
            cosine_similarity=round(cosine, 12),
            trigram_overlap=round(overlap, 12),
            attempt=attempt,
            reason_code=(
                "repetition_threshold_exceeded"
                if exceeded
                else ""
            ),
        )


__all__ = ["LearningRetrievalSelector"]
