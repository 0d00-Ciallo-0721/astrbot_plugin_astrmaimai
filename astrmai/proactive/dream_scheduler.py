from __future__ import annotations

import time

from astrbot.api import logger
from astrbot.api.event import MessageChain


class DreamScheduler:
    def __init__(self, context, memory_engine, config, semaphore, dream_visible: bool = False):
        self.context = context
        self.memory_engine = memory_engine
        self.config = config
        self._bg_semaphore = semaphore
        self.dream_visible = dream_visible
        self.dream_agent = None
        self.dream_generator = None
        self._db_service = None
        self._last_dream_time = 0.0
        self._dream_interval = getattr(getattr(config, "life", None), "dream_interval_min", 30) * 60

    def bind_dependencies(self, dream_agent, dream_generator, db_service=None):
        self.dream_agent = dream_agent
        self.dream_generator = dream_generator
        self._db_service = db_service

    def should_run(self, now_ts: float) -> bool:
        return (
            self.dream_agent is not None
            and now_ts - self._last_dream_time >= self._dream_interval
            and self._within_dream_time_range()
        )

    async def run_once(self):
        if not self.dream_agent or not self.dream_generator:
            return
        self._last_dream_time = time.time()
        min_events = getattr(self.config.life, "min_memory_events_to_dream", getattr(self.dream_agent, "MIN_EVENTS_TO_DREAM", 5))
        self.dream_agent.MIN_EVENTS_TO_DREAM = min_events
        async with self._bg_semaphore:
            dream_log = await self.dream_agent.run_dream_cycle()
            if not dream_log:
                return

            session_id = getattr(self.dream_agent, "_last_session_id", "global")
            persona_name = getattr(getattr(self.config, "persona", None), "name", "Mai")
            dream_text = await self.dream_generator.generate(
                dream_log=dream_log,
                persona_name=persona_name,
                session_id=session_id,
            )
            maintenance = self.dream_generator.build_maintenance_result(dream_log, session_id=session_id)

            if self.memory_engine and hasattr(self.memory_engine, "record_cognitive_feedback"):
                try:
                    await self.memory_engine.record_cognitive_feedback(
                        session_id=str(session_id),
                        source="dream",
                        summary=str(maintenance.get("summary", "") or ""),
                        guidance=self._maintenance_guidance(maintenance.get("tags", [])),
                        tags=list(maintenance.get("tags", []) or []),
                        importance=0.6,
                    )
                except Exception as exc:
                    logger.debug(f"[DreamScheduler] feedback write-back degraded: {exc}")

            if dream_text and self.memory_engine and hasattr(self.memory_engine, "add_memory"):
                try:
                    await self.memory_engine.add_memory(
                        content=f"[梦境日记] {dream_text}",
                        session_id="__dream_diary__",
                        importance=0.5,
                    )
                    await self.memory_engine.add_memory(
                        content=f"[dream_maintenance] {maintenance['summary']}",
                        session_id=str(session_id),
                        importance=0.65,
                    )
                except Exception as exc:
                    logger.debug(f"[DreamScheduler] memory write-back degraded: {exc}")

            if dream_text and self.dream_visible:
                target = getattr(self.config.life, "dream_send_target", "") or session_id
                try:
                    await self.context.send_message(target, MessageChain().message(dream_text))
                except Exception as exc:
                    logger.warning(f"[DreamScheduler] dream push degraded: {exc}")

    def _within_dream_time_range(self) -> bool:
        time_ranges = getattr(self.config.life, "dream_time_ranges", []) if hasattr(self.config, "life") else []
        if not time_ranges:
            return True
        current = time.localtime()
        current_minutes = current.tm_hour * 60 + current.tm_min
        for item in time_ranges:
            if not isinstance(item, str) or "-" not in item:
                continue
            start_raw, end_raw = item.split("-", 1)
            try:
                start_hour, start_min = [int(part) for part in start_raw.split(":", 1)]
                end_hour, end_min = [int(part) for part in end_raw.split(":", 1)]
            except ValueError:
                continue
            start_minutes = start_hour * 60 + start_min
            end_minutes = end_hour * 60 + end_min
            if start_minutes <= end_minutes:
                if start_minutes <= current_minutes <= end_minutes:
                    return True
            else:
                if current_minutes >= start_minutes or current_minutes <= end_minutes:
                    return True
        return False

    @staticmethod
    def _maintenance_guidance(tags) -> str:
        tag_set = {str(tag or "").strip().lower() for tag in tags or [] if str(tag or "").strip()}
        guidance: list[str] = []
        if {"merge", "update"} & tag_set:
            guidance.append("Prefer the consolidated memory summary over older fragments.")
        if "delete" in tag_set:
            guidance.append("Do not revive memory fragments that maintenance treated as stale or noisy.")
        if "jargon_review" in tag_set:
            guidance.append("Use related jargon cautiously and confirm meaning when context is unclear.")
        if not guidance:
            guidance.append("Memory state is stable; do not force old topics into the next reply.")
        return " ".join(guidance)

    def describe_status(self) -> dict:
        return {
            "dream_visible": self.dream_visible,
            "interval_seconds": self._dream_interval,
            "last_dream_time": self._last_dream_time,
            "dream_agent_bound": self.dream_agent is not None,
            "dream_generator_bound": self.dream_generator is not None,
        }


__all__ = ["DreamScheduler"]
