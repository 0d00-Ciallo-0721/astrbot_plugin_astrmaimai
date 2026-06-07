from __future__ import annotations

import time

from astrbot.api import logger


class DecayService:
    def __init__(self, state_engine, memory_engine, config):
        self.state_engine = state_engine
        self.memory_engine = memory_engine
        self.config = config
        self._last_memory_decay = 0.0

    async def run_once(self):
        now = time.time()

        for state in self.state_engine.get_active_states():
            self.state_engine.apply_natural_decay(state)  # synchronous, no I/O

        for profile in self.state_engine.get_active_profiles():
            if now - (getattr(profile, "last_access_time", 0) or 0) <= 86400:
                continue
            old_score = float(profile.social_score or 0.0)
            if old_score > 10:
                delta = -1
            elif old_score < -10:
                delta = 1
            elif old_score > 0:
                delta = -min(old_score, 1.0)
            elif old_score < 0:
                delta = min(abs(old_score), 1.0)
            else:
                delta = 0
            if delta != 0:
                profile.last_access_time = now
                await self.state_engine.update_social_score_from_fact(profile.user_id, delta)

        enable_rel_engine = getattr(self.config.evolution, "enable_relationship_engine", True) if hasattr(self.config, "evolution") else True
        if enable_rel_engine and hasattr(self.state_engine, "relationship_engine"):
            self.state_engine.relationship_engine.apply_global_decay()

        if not self.memory_engine or not hasattr(self.memory_engine, "apply_daily_decay"):
            return
        if now - self._last_memory_decay < 86400:
            return
        self._last_memory_decay = now
        try:
            await self.memory_engine.apply_daily_decay()
        except Exception as exc:
            logger.debug(f"[Life] memory daily decay degraded: {exc}")


__all__ = ["DecayService"]
