from __future__ import annotations


class ReviewRepository:
    def __init__(self, db_service):
        self.db = db_service

    def save_pattern(self, pattern):
        return self.db.save_pattern(pattern)

    async def save_pattern_async(self, pattern):
        return await self.db.save_pattern_async(pattern)

    def get_patterns(self, group_id: str, limit: int = 5):
        return self.db.get_patterns(group_id, limit=limit)

    async def get_patterns_async(self, group_id: str, limit: int = 5):
        return await self.db.get_patterns_async(group_id, limit=limit)

    def delete_pattern(self, pattern_id):
        return self.db.delete_pattern(pattern_id)

    def adjust_pattern_weight(self, group_id: str, situation: str, expression: str, delta: float):
        return self.db.adjust_pattern_weight(group_id, situation, expression, delta)

    async def adjust_pattern_weight_async(self, group_id: str, situation: str, expression: str, delta: float):
        return await self.db.adjust_pattern_weight_async(group_id, situation, expression, delta)

    def list_reviewable_patterns(self, group_id: str | None = None, limit: int = 20):
        return self.db.list_reviewable_patterns(group_id=group_id, limit=limit)

    async def list_reviewable_patterns_async(self, group_id: str | None = None, limit: int = 20):
        return await self.db.list_reviewable_patterns_async(group_id=group_id, limit=limit)

    def get_pattern_by_id(self, pattern_id: int):
        return self.db.get_pattern_by_id(pattern_id)

    async def get_pattern_by_id_async(self, pattern_id: int):
        return await self.db.get_pattern_by_id_async(pattern_id)

    def update_pattern_review(self, pattern_id: int, **kwargs):
        return self.db.update_pattern_review(pattern_id, **kwargs)

    async def update_pattern_review_async(self, pattern_id: int, **kwargs):
        return await self.db.update_pattern_review_async(pattern_id, **kwargs)

    def list_expression_reviews(self, group_id: str | None = None, statuses: list[str] | None = None, limit: int = 20):
        return self.db.list_expression_reviews(group_id=group_id, statuses=statuses, limit=limit)

    async def list_expression_reviews_async(self, group_id: str | None = None, statuses: list[str] | None = None, limit: int = 20):
        return await self.db.list_expression_reviews_async(group_id=group_id, statuses=statuses, limit=limit)
