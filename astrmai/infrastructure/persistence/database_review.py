import asyncio
import json
import time
from typing import List, Optional

from sqlmodel import desc, select

from .orm_models import ExpressionPattern


class ReviewPersistenceMixin:
    def delete_pattern(self, pattern_id: Optional[int]):
        if not pattern_id:
            return
        with self.get_session() as session:
            pattern = session.get(ExpressionPattern, pattern_id)
            if pattern:
                session.delete(pattern)
                session.commit()

    def adjust_pattern_weight(self, group_id: str, situation: str, expression: str, delta: float):
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(
                ExpressionPattern.group_id == group_id,
                ExpressionPattern.situation == situation,
                ExpressionPattern.expression == expression,
            )
            pattern = session.exec(statement).first()
            if not pattern:
                return
            pattern.weight = max(0.0, min(2.0, pattern.weight + delta))
            pattern.last_active_time = time.time()
            session.add(pattern)
            session.commit()

    def _merge_content_list(self, existing: ExpressionPattern, pattern: ExpressionPattern) -> None:
        try:
            old_contents = json.loads(existing.content_list or "[]")
        except Exception:
            old_contents = []
        try:
            new_contents = json.loads(getattr(pattern, "content_list", "[]") or "[]")
        except Exception:
            new_contents = []
        merged_contents = []
        for item in [*old_contents, *new_contents]:
            text = str(item).strip()
            if text and text not in merged_contents:
                merged_contents.append(text)
        existing.content_list = json.dumps(merged_contents[:12], ensure_ascii=False)

    def _merge_existing_pattern(self, existing: ExpressionPattern, pattern: ExpressionPattern) -> ExpressionPattern:
        existing.weight += max(float(getattr(pattern, "weight", 1.0) or 1.0), 0.1)
        existing.count = int(getattr(existing, "count", 1) or 1) + max(int(getattr(pattern, "count", 1) or 1), 1)
        existing.style = getattr(pattern, "style", "") or existing.style
        existing.modified_by = getattr(pattern, "modified_by", "") or existing.modified_by
        existing.source = getattr(pattern, "source", "") or existing.source
        existing.shared_scope = getattr(pattern, "shared_scope", "") or existing.shared_scope
        existing.think_level = max(
            int(getattr(existing, "think_level", 0) or 0),
            int(getattr(pattern, "think_level", 0) or 0),
        )
        existing.review_status = getattr(pattern, "review_status", "") or existing.review_status
        existing.review_reason = getattr(pattern, "review_reason", "") or existing.review_reason
        existing.review_suggestion = getattr(pattern, "review_suggestion", "") or existing.review_suggestion
        self._merge_content_list(existing, pattern)
        existing.last_active_time = time.time()
        return existing

    def save_pattern(self, pattern: ExpressionPattern):
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(
                ExpressionPattern.group_id == pattern.group_id,
                ExpressionPattern.situation == pattern.situation,
                ExpressionPattern.expression == pattern.expression,
            )
            existing = session.exec(statement).first()
            if existing:
                target = self._merge_existing_pattern(existing, pattern)
                session.add(target)
            else:
                if not getattr(pattern, "content_list", ""):
                    pattern.content_list = "[]"
                target = pattern
                session.add(target)
            session.commit()
            session.refresh(target)
            return ExpressionPattern.model_validate(target.model_dump())

    def get_patterns(self, group_id: str, limit: int = 5, only_checked: bool = False, include_rejected: bool = False, shared_scope: Optional[str] = None, think_level: Optional[int] = None, review_status: Optional[str] = None) -> List[ExpressionPattern]:
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(ExpressionPattern.group_id == group_id)
            if only_checked:
                statement = statement.where(ExpressionPattern.checked == True)
            if not include_rejected:
                statement = statement.where(ExpressionPattern.rejected == False)
            if shared_scope is not None:
                statement = statement.where((ExpressionPattern.shared_scope == "") | (ExpressionPattern.shared_scope == shared_scope))
            if think_level is not None:
                statement = statement.where(ExpressionPattern.think_level <= think_level)
            if review_status:
                statement = statement.where(ExpressionPattern.review_status == review_status)
            statement = statement.order_by(desc(ExpressionPattern.weight), desc(ExpressionPattern.count), desc(ExpressionPattern.last_active_time)).limit(limit)
            results = session.exec(statement).all()
            return [ExpressionPattern.model_validate(item.model_dump()) for item in results]

    def list_reviewable_patterns(self, group_id: Optional[str] = None, limit: int = 20) -> List[ExpressionPattern]:
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(
                ExpressionPattern.rejected == False,
                ExpressionPattern.review_status.in_(["pending", "revision_needed", "pending_human"]),
            )
            if group_id:
                statement = statement.where(ExpressionPattern.group_id == group_id)
            statement = statement.order_by(desc(ExpressionPattern.count), desc(ExpressionPattern.weight)).limit(limit)
            results = session.exec(statement).all()
            return [ExpressionPattern.model_validate(item.model_dump()) for item in results]

    def get_pattern_by_id(self, pattern_id: int) -> Optional[ExpressionPattern]:
        with self.get_session() as session:
            pattern = session.get(ExpressionPattern, pattern_id)
            return ExpressionPattern.model_validate(pattern.model_dump()) if pattern else None

    def update_pattern_review(self, pattern_id: int, *, checked: Optional[bool] = None, rejected: Optional[bool] = None, modified_by: Optional[str] = None, review_status: Optional[str] = None, review_reason: Optional[str] = None, review_suggestion: Optional[str] = None, weight_delta: float = 0.0, replacement_expression: Optional[str] = None, apply_replacement: bool = False, style: Optional[str] = None) -> Optional[ExpressionPattern]:
        with self.get_session() as session:
            pattern = session.get(ExpressionPattern, pattern_id)
            if not pattern:
                return None
            if checked is not None:
                pattern.checked = checked
            if rejected is not None:
                pattern.rejected = rejected
            if modified_by is not None:
                pattern.modified_by = modified_by
            if review_status is not None:
                pattern.review_status = review_status
            if review_reason is not None:
                pattern.review_reason = review_reason
            if review_suggestion is not None:
                pattern.review_suggestion = review_suggestion
            if replacement_expression and apply_replacement:
                pattern.expression = replacement_expression.strip()
                pattern.review_suggestion = ""
            if style is not None:
                pattern.style = style
            if weight_delta:
                pattern.weight = max(0.0, min(3.0, float(pattern.weight or 1.0) + weight_delta))
            pattern.last_review_time = time.time()
            pattern.last_active_time = time.time()
            session.add(pattern)
            session.commit()
            session.refresh(pattern)
            return ExpressionPattern.model_validate(pattern.model_dump())

    def list_expression_reviews(self, group_id: Optional[str] = None, statuses: Optional[List[str]] = None, limit: int = 50) -> List[ExpressionPattern]:
        with self.get_session() as session:
            statement = select(ExpressionPattern)
            if group_id:
                statement = statement.where(ExpressionPattern.group_id == group_id)
            if statuses:
                statement = statement.where(ExpressionPattern.review_status.in_(statuses))
            statement = statement.order_by(desc(ExpressionPattern.last_review_time), desc(ExpressionPattern.count), desc(ExpressionPattern.create_time)).limit(limit)
            results = session.exec(statement).all()
            return [ExpressionPattern.model_validate(item.model_dump()) for item in results]

    async def save_pattern_async(self, pattern: ExpressionPattern):
        return await self._run_blocking(self.save_pattern, pattern, with_lock=True)

    async def get_patterns_async(self, group_id: str, limit: int = 5, **kwargs):
        return await self._run_blocking(self.get_patterns, group_id, limit, **kwargs)

    async def adjust_pattern_weight_async(self, group_id: str, situation: str, expression: str, delta: float):
        async with self._db_lock:
            return await asyncio.to_thread(self.adjust_pattern_weight, group_id, situation, expression, delta)

    async def list_reviewable_patterns_async(self, group_id: Optional[str] = None, limit: int = 20) -> List[ExpressionPattern]:
        return await asyncio.to_thread(self.list_reviewable_patterns, group_id, limit)

    async def get_pattern_by_id_async(self, pattern_id: int) -> Optional[ExpressionPattern]:
        return await asyncio.to_thread(self.get_pattern_by_id, pattern_id)

    async def update_pattern_review_async(self, pattern_id: int, **kwargs) -> Optional[ExpressionPattern]:
        async with self._db_lock:
            return await asyncio.to_thread(self.update_pattern_review, pattern_id, **kwargs)

    async def list_expression_reviews_async(self, group_id: Optional[str] = None, statuses: Optional[List[str]] = None, limit: int = 50) -> List[ExpressionPattern]:
        return await asyncio.to_thread(self.list_expression_reviews, group_id, statuses, limit)