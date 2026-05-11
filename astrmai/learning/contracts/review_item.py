from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ReviewItem:
    pattern_id: int
    group_id: str
    situation: str
    expression: str
    review_status: str
    review_reason: str = ""
    review_suggestion: str = ""

    @classmethod
    def from_pattern(cls, pattern: Any) -> "ReviewItem":
        return cls(
            pattern_id=int(getattr(pattern, "id", 0) or 0),
            group_id=str(getattr(pattern, "group_id", "") or ""),
            situation=str(getattr(pattern, "situation", "") or ""),
            expression=str(getattr(pattern, "expression", "") or ""),
            review_status=str(getattr(pattern, "review_status", "pending") or "pending"),
            review_reason=str(getattr(pattern, "review_reason", "") or ""),
            review_suggestion=str(getattr(pattern, "review_suggestion", "") or ""),
        )
