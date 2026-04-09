from typing import Any, Dict, List, Optional

from ..infra.database import DatabaseService
from ..infra.datamodels import ExpressionPattern


class ExpressionReviewService:
    """供后端管理系统调用的表达审核接口层。"""

    def __init__(self, db_service: DatabaseService):
        self.db = db_service

    @staticmethod
    def _serialize_pattern(pattern: ExpressionPattern) -> Dict[str, Any]:
        return {
            "id": pattern.id,
            "group_id": pattern.group_id,
            "situation": pattern.situation,
            "expression": pattern.expression,
            "style": getattr(pattern, "style", ""),
            "count": getattr(pattern, "count", 1),
            "checked": getattr(pattern, "checked", False),
            "rejected": getattr(pattern, "rejected", False),
            "review_status": getattr(pattern, "review_status", "pending"),
            "review_reason": getattr(pattern, "review_reason", ""),
            "review_suggestion": getattr(pattern, "review_suggestion", ""),
            "shared_scope": getattr(pattern, "shared_scope", ""),
            "think_level": getattr(pattern, "think_level", 0),
            "weight": getattr(pattern, "weight", 1.0),
            "modified_by": getattr(pattern, "modified_by", ""),
            "source": getattr(pattern, "source", ""),
            "content_list": getattr(pattern, "content_list", "[]"),
            "last_review_time": getattr(pattern, "last_review_time", 0.0),
            "last_active_time": getattr(pattern, "last_active_time", 0.0),
            "create_time": getattr(pattern, "create_time", 0.0),
        }

    async def list_pending_reviews(self, group_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        patterns = await self.db.list_expression_reviews_async(
            group_id=group_id,
            statuses=["pending", "revision_needed", "pending_human"],
            limit=limit,
        )
        return [self._serialize_pattern(pattern) for pattern in patterns]

    async def list_recent_reviews(self, group_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        patterns = await self.db.list_expression_reviews_async(group_id=group_id, limit=limit)
        return [self._serialize_pattern(pattern) for pattern in patterns]

    async def get_review_detail(self, pattern_id: int) -> Optional[Dict[str, Any]]:
        pattern = await self.db.get_pattern_by_id_async(pattern_id)
        if not pattern:
            return None
        return self._serialize_pattern(pattern)

    async def submit_review(
        self,
        pattern_id: int,
        decision: str,
        reviewer_id: str,
        *,
        replacement_expression: str = "",
        style: Optional[str] = None,
        reason: str = "",
        weight_delta: float = 0.0,
    ) -> Optional[Dict[str, Any]]:
        normalized = str(decision or "").strip().lower()
        kwargs: Dict[str, Any] = {
            "modified_by": f"human:{reviewer_id}",
            "review_reason": reason or "",
            "style": style,
            "weight_delta": weight_delta,
        }
        if normalized == "approved":
            kwargs.update(
                {
                    "checked": True,
                    "rejected": False,
                    "review_status": "approved",
                    "review_suggestion": "",
                }
            )
        elif normalized == "rejected":
            kwargs.update(
                {
                    "checked": False,
                    "rejected": True,
                    "review_status": "rejected",
                    "review_suggestion": "",
                }
            )
        elif normalized in {"revision_needed", "revised", "replace"}:
            kwargs.update(
                {
                    "checked": True,
                    "rejected": False,
                    "review_status": "approved",
                    "replacement_expression": replacement_expression or None,
                    "apply_replacement": bool(replacement_expression),
                    "review_suggestion": "",
                }
            )
        else:
            return None

        updated = await self.db.update_pattern_review_async(pattern_id, **kwargs)
        if not updated:
            return None
        return self._serialize_pattern(updated)
