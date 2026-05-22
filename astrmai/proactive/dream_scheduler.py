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
        self.promotion_engine = None
        self._db_service = None
        self._last_dream_time = 0.0
        self._dream_interval = getattr(getattr(config, "life", None), "dream_interval_min", 30) * 60

    def bind_dependencies(self, dream_agent, dream_generator, db_service=None, promotion_engine=None):
        self.dream_agent = dream_agent
        self.dream_generator = dream_generator
        self.promotion_engine = promotion_engine
        self._db_service = db_service

    def should_run(self, now_ts: float) -> bool:
        return (
            self.dream_agent is not None
            and now_ts - self._last_dream_time >= self._dream_interval
            and self._within_dream_time_range()
        )

    def describe_session_eligibility(self, session_id: str, now_ts: float) -> dict:
        session_id = str(session_id or "").strip()
        if not session_id:
            return {
                "eligible": False,
                "reason": "missing_session_id",
                "session_id": "",
                "throttle_scope": "global",
            }
        if self.dream_agent is None or self.dream_generator is None:
            return {
                "eligible": False,
                "reason": "dependencies_unavailable",
                "session_id": session_id,
                "throttle_scope": "global",
            }
        if not self._within_dream_time_range():
            return {
                "eligible": False,
                "reason": "dream_window_closed",
                "session_id": session_id,
                "throttle_scope": "global",
            }
        if now_ts - self._last_dream_time < self._dream_interval:
            return {
                "eligible": False,
                "reason": "dream_global_cooldown",
                "session_id": session_id,
                "throttle_scope": "global",
            }
        return {
            "eligible": True,
            "reason": "eligible",
            "session_id": session_id,
            "throttle_scope": "global",
        }

    def should_run_for_session(self, session_id: str, now_ts: float) -> bool:
        return bool(self.describe_session_eligibility(session_id, now_ts).get("eligible", False))

    async def _run_for_session(self, session_id: str | None) -> dict:
        if not self.dream_agent or not self.dream_generator:
            return {"performed": False, "reason": "dependencies_unavailable", "session_id": str(session_id or ""), "throttle_scope": "global"}
        self._last_dream_time = time.time()
        min_events = getattr(self.config.life, "min_memory_events_to_dream", getattr(self.dream_agent, "MIN_EVENTS_TO_DREAM", 5))
        self.dream_agent.MIN_EVENTS_TO_DREAM = min_events
        async with self._bg_semaphore:
            dream_log = await self.dream_agent.run_dream_cycle(session_id=session_id)
            if not dream_log:
                return {"performed": False, "reason": "no_dream_log", "session_id": str(session_id or ""), "throttle_scope": "global"}

            session_id = getattr(self.dream_agent, "_last_session_id", "global")
            persona_name = getattr(getattr(self.config, "persona", None), "name", "Mai")
            dream_text = await self.dream_generator.generate(
                dream_log=dream_log,
                persona_name=persona_name,
                session_id=session_id,
            )
            maintenance = self.dream_generator.build_maintenance_result(dream_log, session_id=session_id)
            promotion_report = {}
            if self.promotion_engine is not None and hasattr(self.promotion_engine, "run_audit"):
                try:
                    promotion_report = await self.promotion_engine.run_audit(str(session_id or ""), maintenance)
                except Exception as exc:
                    logger.debug(f"[DreamScheduler] promotion audit degraded: {exc}")

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
            return {
                "performed": True,
                "session_id": str(session_id or ""),
                "dream_visible": bool(dream_text and self.dream_visible),
                "summary": str(maintenance.get("summary", "") or ""),
                "promotion_report": promotion_report,
                "throttle_scope": "global",
            }

    async def run_once(self):
        return await self._run_for_session(None)

    async def run_once_for_session(self, session_id: str) -> dict:
        eligibility = self.describe_session_eligibility(session_id, time.time())
        if not eligibility.get("eligible", False):
            return {
                "performed": False,
                "reason": str(eligibility.get("reason", "dream_global_cooldown") or "dream_global_cooldown"),
                "session_id": str(eligibility.get("session_id", session_id) or ""),
                "throttle_scope": "global",
            }
        return await self._run_for_session(session_id)

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
            "throttle_scope": "global",
        }


__all__ = ["DreamScheduler"]
