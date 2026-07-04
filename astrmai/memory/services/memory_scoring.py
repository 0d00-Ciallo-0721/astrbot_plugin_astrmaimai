from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ..contracts.memory_query import MemoryCandidate


@dataclass(slots=True)
class MemoryScoringConfig:
    canonical_weight: float = 0.25
    hybrid_weight: float = 0.45
    importance_weight: float = 0.15
    recency_weight: float = 0.10
    confidence_weight: float = 0.05
    conflict_penalty: float = 0.20
    stale_penalty: float = 0.20
    search_weight: float = 0.45
    search_importance_weight: float = 0.20
    search_confidence_weight: float = 0.15
    search_recency_weight: float = 0.20
    search_stale_penalty: float = 0.25
    deep_temporal_alpha: float = 0.7
    deep_temporal_tau_seconds: float = 86400.0
    deep_temporal_lambda_default: float = 1.0
    deep_temporal_lambda_fact: float = 0.1
    deep_temporal_candidate_pool_factor: int = 4
    deep_temporal_candidate_pool_min: int = 20
    deep_temporal_llm_window: int = 8
    maintenance_hot_beta: float = 0.7
    maintenance_temporal_stale_hot_threshold: float = 0.35


DEFAULT_MEMORY_SCORING = MemoryScoringConfig()


def scoring_from_config(config=None) -> MemoryScoringConfig:
    base = MemoryScoringConfig()
    memory_cfg = getattr(config, "memory", None) if config else None
    if not memory_cfg:
        return base
    values = {}
    for field_name in MemoryScoringConfig.__dataclass_fields__:
        if hasattr(memory_cfg, field_name):
            values[field_name] = getattr(memory_cfg, field_name)
    return MemoryScoringConfig(**values)


def _normalized_tau_seconds(value: float) -> float:
    return max(float(value or 86400.0), 1.0)


def compute_temporal_boost(
    candidate: MemoryCandidate,
    *,
    now: float | None = None,
    config: MemoryScoringConfig | None = None,
) -> float:
    scoring = config or DEFAULT_MEMORY_SCORING
    alpha = min(max(float(scoring.deep_temporal_alpha or 0.0), 0.0), 1.0)
    if float(candidate.created_at or 0.0) <= 0.0:
        return alpha
    # ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic
    now_ts = float(now or time.time())
    age_seconds = max(0.0, now_ts - float(candidate.created_at or 0.0))
    decay_lambda = (
        float(scoring.deep_temporal_lambda_fact or 0.1)
        if str(candidate.kind or "").strip().lower() == "fact"
        else float(scoring.deep_temporal_lambda_default or 1.0)
    )
    decay = math.exp(-decay_lambda * age_seconds / _normalized_tau_seconds(scoring.deep_temporal_tau_seconds))
    return alpha + (1.0 - alpha) * decay


def rerank_candidates(
    candidates: list[MemoryCandidate],
    *,
    now: float | None = None,
    config: MemoryScoringConfig | None = None,
) -> list[MemoryCandidate]:
    if len(candidates) <= 1:
        return candidates
    scoring = config or DEFAULT_MEMORY_SCORING
    now_ts = float(now or time.time())
    ranked: list[tuple[float, int, MemoryCandidate]] = []
    for index, candidate in enumerate(candidates):
        base_score = float(candidate.relevance_score or 0.0)
        boost = compute_temporal_boost(candidate, now=now_ts, config=scoring)
        final_score = base_score * boost
        candidate.relevance_score = final_score
        ranked.append((final_score, -index, candidate))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]


def compute_hot_score(
    candidate: MemoryCandidate,
    *,
    now: float | None = None,
    config: MemoryScoringConfig | None = None,
) -> float:
    scoring = config or DEFAULT_MEMORY_SCORING
    beta = min(max(float(scoring.maintenance_hot_beta or 0.0), 0.0), 1.0)
    now_ts = float(now or time.time())
    last_touch = float(candidate.last_access_time or candidate.updated_at or candidate.created_at or 0.0)
    if last_touch > 0.0:
        age_since_last_access_seconds = max(0.0, now_ts - last_touch)
    else:
        age_since_last_access_seconds = _normalized_tau_seconds(scoring.deep_temporal_tau_seconds)
    tau = _normalized_tau_seconds(scoring.deep_temporal_tau_seconds)
    freshness = 1.0 / (1.0 + age_since_last_access_seconds / tau)
    return beta * freshness + (1.0 - beta) * math.log(max(0.0, float(candidate.access_count or 0)) + 1.0)


__all__ = [
    "DEFAULT_MEMORY_SCORING",
    "MemoryScoringConfig",
    "compute_hot_score",
    "compute_temporal_boost",
    "rerank_candidates",
    "scoring_from_config",
]
