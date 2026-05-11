from __future__ import annotations

from ..dto import ReviewDecisionRequest


async def list_pending_reviews(facade, group_id: str = "", limit: int = 50):
    return await facade.list_pending_expression_reviews(group_id=group_id, limit=limit)


async def get_review_detail(facade, pattern_id: int):
    return await facade.get_expression_review_detail(pattern_id)


async def submit_review(facade, request: ReviewDecisionRequest):
    return await facade.submit_expression_review(
        pattern_id=request.pattern_id,
        decision=request.decision,
        reviewer_id=request.reviewer_id,
        replacement_expression=request.replacement_expression,
        style=request.style,
        reason=request.reason,
        weight_delta=request.weight_delta,
    )


__all__ = ["ReviewDecisionRequest", "get_review_detail", "list_pending_reviews", "submit_review"]
