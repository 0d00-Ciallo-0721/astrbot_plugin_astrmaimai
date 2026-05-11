import asyncio
import unittest

from astrmai.learning.review.review_service import ExpressionReviewService
from astrmai.infrastructure.persistence import ExpressionPattern


class _FakeDB:
    def __init__(self):
        self.pattern = ExpressionPattern(
            id=11,
            group_id="group-1",
            situation="接梗时",
            expression="这波可以",
            review_status="pending_human",
            review_reason="表达太泛",
            review_suggestion="这波节奏挺对",
        )
        self.update_calls = []

    async def list_expression_reviews_async(self, group_id=None, statuses=None, limit=50):
        return [self.pattern]

    async def get_pattern_by_id_async(self, pattern_id):
        if pattern_id == self.pattern.id:
            return self.pattern
        return None

    async def update_pattern_review_async(self, pattern_id, **kwargs):
        self.update_calls.append((pattern_id, kwargs))
        for key, value in kwargs.items():
            if value is not None and hasattr(self.pattern, key):
                setattr(self.pattern, key, value)
        if kwargs.get("replacement_expression") and kwargs.get("apply_replacement"):
            self.pattern.expression = kwargs["replacement_expression"]
        return self.pattern


class ReviewServiceMigratedTests(unittest.TestCase):
    def test_list_pending_reviews_returns_json_ready_payload(self):
        service = ExpressionReviewService(_FakeDB())

        async def _run():
            return await service.list_pending_reviews("group-1", limit=10)

        result = asyncio.run(_run())
        self.assertEqual(result[0]["review_status"], "pending_human")
        self.assertEqual(result[0]["review_suggestion"], "这波节奏挺对")

    def test_submit_review_can_apply_replacement(self):
        db = _FakeDB()
        service = ExpressionReviewService(db)

        async def _run():
            return await service.submit_review(
                pattern_id=11,
                decision="revision_needed",
                reviewer_id="admin-1",
                replacement_expression="这波节奏挺对",
                reason="人工确认更自然",
            )

        result = asyncio.run(_run())
        self.assertEqual(result["expression"], "这波节奏挺对")
        self.assertEqual(result["review_status"], "approved")
        self.assertTrue(db.update_calls[0][1]["apply_replacement"])


__all__ = ["ReviewServiceMigratedTests"]
