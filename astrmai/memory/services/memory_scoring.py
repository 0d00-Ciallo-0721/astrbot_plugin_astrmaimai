from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MemoryScoringConfig:
    canonical_weight: float = 0.25
    hybrid_weight: float = 0.45
    importance_weight: float = 0.15
    recency_weight: float = 0.10
    confidence_weight: float = 0.05
    stale_penalty: float = 0.20
    search_weight: float = 0.45
    search_importance_weight: float = 0.20
    search_confidence_weight: float = 0.15
    search_recency_weight: float = 0.10
    search_stale_penalty: float = 0.25


DEFAULT_MEMORY_SCORING = MemoryScoringConfig()
