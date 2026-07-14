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

        try:
            active_states = list(self.state_engine.get_active_states())
        except Exception as exc:
            logger.debug(f"[Life] state decay listing degraded: {exc}")
            active_states = []
        for state in active_states:
            try:
                self.state_engine.apply_natural_decay(state)  # synchronous, no I/O
            except Exception as exc:
                logger.debug(f"[Life] state decay degraded: {exc}")

        try:
            active_profiles = list(self.state_engine.get_active_profiles())
        except Exception as exc:
            logger.debug(f"[Life] profile decay listing degraded: {exc}")
            active_profiles = []
        for profile in active_profiles:
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
                try:
                    await self.state_engine.update_social_score_from_fact(profile.user_id, delta)
                except Exception as exc:
                    logger.debug(f"[Life] profile social decay degraded: {exc}")

        enable_rel_engine = getattr(self.config.evolution, "enable_relationship_engine", True) if hasattr(self.config, "evolution") else True
        if enable_rel_engine and hasattr(self.state_engine, "relationship_engine"):
            self.state_engine.relationship_engine.apply_global_decay()

        if not self.memory_engine or not hasattr(self.memory_engine, "apply_daily_decay"):
            return
        if now - self._last_memory_decay < 86400:
            return
        memory_config = getattr(self.config, "memory", None)
        decay_rate = float(getattr(memory_config, "time_decay_rate", 0.01) or 0.0)
        try:
            await self.memory_engine.apply_daily_decay(decay_rate=decay_rate)
        except Exception as exc:
            logger.debug(f"[Life] memory daily decay degraded: {exc}")
        else:
            self._last_memory_decay = now


__all__ = ["DecayService"]
