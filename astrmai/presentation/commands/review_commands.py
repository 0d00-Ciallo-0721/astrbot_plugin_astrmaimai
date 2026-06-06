from __future__ import annotations

from astrbot.api import logger

from ..dto import ReviewDecisionRequest

VALID_DECISIONS = frozenset({"approved", "rejected", "revision_needed", "revised", "replace"})


async def list_pending_reviews(facade, group_id: str = "", limit: int = 50):
    limit = max(1, min(200, int(limit)))
    return await facade.list_pending_expression_reviews(group_id=group_id, limit=limit)


async def get_review_detail(facade, pattern_id: str):
    if not pattern_id or not pattern_id.strip():
        raise ValueError("pattern_id must not be empty")
    return await facade.get_expression_review_detail(pattern_id)


async def submit_review(facade, request: ReviewDecisionRequest):
    if not request.pattern_id or not request.pattern_id.strip():
        raise ValueError("pattern_id must be a non-empty string")
    decision = str(request.decision or "").strip().lower()
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}, got {request.decision!r}")
    weight_delta = max(-1.0, min(1.0, request.weight_delta))
    replacement_expression = request.replacement_expression or ""
    if len(replacement_expression) > 1000:
        logger.warning("[AstrMai] submit_review replacement_expression truncated from %d to 1000 chars", len(replacement_expression))
        replacement_expression = replacement_expression[:1000]
    style = request.style or ""
    if len(style) > 1000:
        logger.warning("[AstrMai] submit_review style truncated from %d to 1000 chars", len(style))
        style = style[:1000]
    reason = request.reason or ""
    if len(reason) > 1000:
        logger.warning("[AstrMai] submit_review reason truncated from %d to 1000 chars", len(reason))
        reason = reason[:1000]
    return await facade.submit_expression_review(
        pattern_id=request.pattern_id,
        decision=decision,
        reviewer_id=request.reviewer_id,
        replacement_expression=replacement_expression,
        style=style,
        reason=reason,
        weight_delta=weight_delta,
    )


__all__ = ["ReviewDecisionRequest", "get_review_detail", "list_pending_reviews", "submit_review"]
