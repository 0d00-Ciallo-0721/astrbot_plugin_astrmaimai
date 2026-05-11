from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List


@dataclass(slots=True)
class UserProfileSummary:
    user_id: str
    name: str
    nickname: str = ""
    persona_analysis: str = ""
    social_score: float = 0.0
    memory_points: List[Any] = field(default_factory=list)

    @classmethod
    def from_profile(cls, profile: Any) -> "UserProfileSummary":
        return cls(
            user_id=str(getattr(profile, "user_id", "") or ""),
            name=str(getattr(profile, "name", "") or ""),
            nickname=str(getattr(profile, "nickname", "") or ""),
            persona_analysis=str(getattr(profile, "persona_analysis", "") or ""),
            social_score=float(getattr(profile, "social_score", 0.0) or 0.0),
            memory_points=list(getattr(profile, "memory_points", []) or []),
        )
