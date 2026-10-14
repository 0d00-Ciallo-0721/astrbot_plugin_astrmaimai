from __future__ import annotations

import time

from astrbot.api import logger


class DecayService:
    def __init__(self, state_engine, memory_engine, config):
        self.state_engine = state_engine
        self.memory_engine = memory_engine
        self.config = config
        self._last_memory_decay = 0.0

    async def has_due_work(self) -> bool:
        if self.state_engine.get_active_states() or self.state_engine.get_active_profiles():
            return True
        if not self.memory_engine or not hasattr(self.memory_engine, "apply_daily_decay"):
            return False
        last = self._last_memory_decay
        store = getattr(self.memory_engine, "v2_store", None)
        if store is not None:
            last = max(last, float(await store.get_meta("daily_decay_completed_at", "0")))
        return time.time() - last >= 86400

    async def run_once(self):
        now = time.time()
        report = {"processed": 0, "failed": 0}

        def outcome():
            return {**report, "status": "partial" if report["failed"] else "succeeded" if report["processed"] else "no_work"}

        try:
            active_states = list(self.state_engine.get_active_states())
        except Exception as exc:
            logger.debug(f"[Life] state decay listing degraded: {exc}")
            report["failed"] += 1
            active_states = []
        for state in active_states:
            try:
                self.state_engine.apply_natural_decay(state)  # synchronous, no I/O
                report["processed"] += 1
            except Exception as exc:
                logger.debug(f"[Life] state decay degraded: {exc}")
                report["failed"] += 1

        try:
            active_profiles = list(self.state_engine.get_active_profiles())
        except Exception as exc:
            logger.debug(f"[Life] profile decay listing degraded: {exc}")
            report["failed"] += 1
            active_profiles = []
        for profile in active_profiles:
            metadata = getattr(profile, "profile_metadata", None)
            if not isinstance(metadata, dict):
                metadata = {}
                profile.profile_metadata = metadata
            last_decay = float(metadata.get("last_relationship_decay_time", 0.0) or 0.0)
            last_activity = float(getattr(profile, "last_access_time", 0.0) or 0.0)
            if now - max(last_activity, last_decay) <= 86400:
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
                metadata["last_relationship_decay_time"] = now
                try:
                    await self.state_engine.update_social_score_from_fact(
                        profile.user_id,
                        delta,
                        touch_activity=False,
                    )
                    report["processed"] += 1
                except Exception as exc:
                    logger.debug(f"[Life] profile social decay degraded: {exc}")
                    report["failed"] += 1

        if not self.memory_engine or not hasattr(self.memory_engine, "apply_daily_decay"):
            return outcome()
        if now - self._last_memory_decay < 86400:
            return outcome()
        memory_config = getattr(self.config, "memory", None)
        decay_rate = float(getattr(memory_config, "time_decay_rate", 0.01) or 0.0)
        try:
            await self.memory_engine.apply_daily_decay(decay_rate=decay_rate, min_interval_sec=86400.0)
        except Exception as exc:
            logger.debug(f"[Life] memory daily decay degraded: {exc}")
            report["failed"] += 1
        else:
            self._last_memory_decay = now
            report["processed"] += 1
        return outcome()


__all__ = ["DecayService"]
