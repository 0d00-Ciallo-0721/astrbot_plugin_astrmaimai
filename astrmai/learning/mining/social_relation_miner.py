from __future__ import annotations


class SocialRelationMiner:
    def __init__(self, state_engine=None):
        self.state_engine = state_engine

    def bind_state_engine(self, state_engine) -> None:
        self.state_engine = state_engine

    def is_available(self) -> bool:
        return bool(self.state_engine and hasattr(self.state_engine, 'update_social_score_from_fact'))

    async def record_affection_fact(self, user_id: str, impact_score: float) -> None:
        if not self.is_available() or not user_id:
            return
        try:
            score = float(impact_score or 0.0)
        except (TypeError, ValueError):
            return
        if score == 0.0:
            return
        await self.state_engine.update_social_score_from_fact(str(user_id), score)
