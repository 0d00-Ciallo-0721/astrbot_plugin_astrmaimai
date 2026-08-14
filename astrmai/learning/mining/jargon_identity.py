from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Iterable

from ..dedup import normalize_jargon_term


def jargon_surface_similarity(left: Any, right: Any) -> float:
    first = normalize_jargon_term(str(left or ""))
    second = normalize_jargon_term(str(right or ""))
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    if (first in second or second in first) and abs(len(first) - len(second)) <= 2:
        return 0.96
    return SequenceMatcher(None, first, second).ratio()


def resolve_jargon_identity(
    term: str,
    records: Iterable[Any],
    *,
    threshold: float = 0.9,
) -> tuple[str, float]:
    normalized = normalize_jargon_term(term)
    if not normalized:
        return "", 0.0
    best_term = normalized
    best_score = 0.0
    for record in records or []:
        metadata = dict(getattr(record, "metadata", {}) or {})
        canonical = str(getattr(record, "content", "") or metadata.get("canonical_term") or "").strip()
        surfaces = [canonical, *(metadata.get("surface_forms") or []), *(metadata.get("aliases") or [])]
        score = max((jargon_surface_similarity(normalized, surface) for surface in surfaces), default=0.0)
        if score > best_score:
            best_term = canonical or normalized
            best_score = score
    if best_score < max(0.0, min(float(threshold), 1.0)):
        return normalized, best_score
    return best_term, best_score


__all__ = ["jargon_surface_similarity", "resolve_jargon_identity"]
