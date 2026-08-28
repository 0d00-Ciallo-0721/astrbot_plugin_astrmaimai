from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from ..infrastructure.runtime.outbound_send_guard import outbound_send_allowed


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
        self._last_attempt_by_session: dict[str, float] = {}
        self._pending_completions: dict[str, dict] = {}
        self._dream_interval = getattr(getattr(config, "life", None), "dream_interval_min", 30) * 60

    def refresh_config(self, config) -> None:
        self.config = config
        life = getattr(config, "life", None)
        self._dream_interval = max(int(getattr(life, "dream_interval_min", 30) or 30), 1) * 60
        self.dream_visible = bool(getattr(life, "dream_visible", self.dream_visible))

    def bind_dependencies(self, dream_agent, dream_generator, db_service=None, promotion_engine=None):
        self.dream_agent = dream_agent
        self.dream_generator = dream_generator
        self.promotion_engine = promotion_engine
        self._db_service = db_service
        self._load_runtime_state()

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
        last_attempt = float(self._last_attempt_by_session.get(session_id, 0.0) or 0.0)
        if now_ts - last_attempt < self._dream_interval:
            return {
                "eligible": False,
                "reason": "dream_session_backoff",
                "session_id": session_id,
                "retry_after": last_attempt + self._dream_interval,
                "throttle_scope": "session",
            }
        return {
            "eligible": True,
            "reason": "eligible",
            "session_id": session_id,
            "throttle_scope": "global",
        }

    async def describe_session_eligibility_async(self, session_id: str, now_ts: float) -> dict:
        eligibility = self.describe_session_eligibility(session_id, now_ts)
        if not eligibility.get("eligible", False):
            return eligibility

        counter = getattr(self.dream_agent, "count_session_events", None)
        if not callable(counter):
            return eligibility

        min_events = max(
            int(
                getattr(
                    getattr(self.config, "life", None),
                    "min_memory_events_to_dream",
                    getattr(self.dream_agent, "MIN_EVENTS_TO_DREAM", 5),
                )
                or 1
            ),
            1,
        )
        event_count = int(await counter(session_id) or 0)
        if event_count < min_events:
            self._last_attempt_by_session[str(session_id)] = float(now_ts)
            await self._persist_runtime_state()
            return {
                "eligible": False,
                "reason": "insufficient_memory_events",
                "session_id": str(session_id),
                "event_count": event_count,
                "required_event_count": min_events,
                "retry_after": float(now_ts) + self._dream_interval,
                "throttle_scope": "session",
            }
        eligibility["event_count"] = event_count
        eligibility["required_event_count"] = min_events
        return eligibility

    def should_run_for_session(self, session_id: str, now_ts: float) -> bool:
        return bool(self.describe_session_eligibility(session_id, now_ts).get("eligible", False))

    async def _run_for_session(self, session_id: str | None) -> dict:
        if not self.dream_agent or not self.dream_generator:
            return {"performed": False, "reason": "dependencies_unavailable", "session_id": str(session_id or ""), "throttle_scope": "global"}
        async with self._bg_semaphore:
            # Keep the throttle check inside semaphore so concurrent sessions cannot bypass it.
            request_key = str(session_id or "__global__")
            pending = self._pending_completions.get(request_key)
            if pending is None and self._pending_completions:
                return {
                    "performed": False,
                    "reason": "dream_completion_pending",
                    "session_id": str(session_id or ""),
                    "throttle_scope": "global",
                }
            now_ts = time.time()
            if pending is None and now_ts - self._last_dream_time < self._dream_interval:
                return {"performed": False, "reason": "dream_global_cooldown", "session_id": str(session_id or ""), "throttle_scope": "global"}
            if pending is None and session_id:
                eligibility = await self.describe_session_eligibility_async(session_id, time.time())
                if not eligibility.get("eligible", False):
                    return {
                        "performed": False,
                        "reason": str(eligibility.get("reason", "dream_global_cooldown") or "dream_global_cooldown"),
                        "session_id": str(eligibility.get("session_id", session_id) or ""),
                        "throttle_scope": str(eligibility.get("throttle_scope", "global") or "global"),
                    }
            if pending is None:
                min_events = getattr(self.config.life, "min_memory_events_to_dream", getattr(self.dream_agent, "MIN_EVENTS_TO_DREAM", 5))
                original_min_events = self.dream_agent.MIN_EVENTS_TO_DREAM
                self._last_attempt_by_session[request_key] = time.time()
                await self._persist_runtime_state()
                try:
                    self.dream_agent.MIN_EVENTS_TO_DREAM = min_events
                    dream_log = await self.dream_agent.run_dream_cycle(session_id=session_id)
                finally:
                    self.dream_agent.MIN_EVENTS_TO_DREAM = original_min_events
                if not dream_log:
                    return {"performed": False, "reason": "no_dream_log", "session_id": str(session_id or ""), "throttle_scope": "global"}

                resolved_session_id = str(session_id or getattr(self.dream_agent, "_last_session_id", "global") or "global")
                persona_name = getattr(getattr(self.config, "persona", None), "name", "Mai")
                dream_text = await self.dream_generator.generate(
                    dream_log=dream_log,
                    persona_name=persona_name,
                    session_id=resolved_session_id,
                )
                maintenance = self.dream_generator.build_maintenance_result(dream_log, session_id=resolved_session_id)
                promotion_report = {}
                if self.promotion_engine is not None and hasattr(self.promotion_engine, "run_audit"):
                    try:
                        promotion_report = await self.promotion_engine.run_audit(resolved_session_id, maintenance)
                    except Exception as exc:
                        logger.debug(f"[DreamScheduler] promotion audit degraded: {exc}")
                pending = {
                    "session_id": resolved_session_id,
                    "dream_text": str(dream_text or ""),
                    "maintenance": maintenance,
                    "promotion_report": promotion_report,
                    "feedback_done": not bool(self.memory_engine and hasattr(self.memory_engine, "record_cognitive_feedback")),
                    "diary_memory_done": not bool(dream_text and self.memory_engine and hasattr(self.memory_engine, "add_memory")),
                    "maintenance_memory_done": not bool(dream_text and self.memory_engine and hasattr(self.memory_engine, "add_memory")),
                    "visible_send_done": not bool(dream_text and self.dream_visible),
                }
                self._pending_completions[request_key] = pending

            session_id = str(pending["session_id"])
            dream_text = str(pending["dream_text"] or "")
            maintenance = dict(pending["maintenance"] or {})
            failures: list[str] = []

            if not pending["feedback_done"]:
                try:
                    await self.memory_engine.record_cognitive_feedback(
                        session_id=session_id,
                        source="dream",
                        summary=str(maintenance.get("summary", "") or ""),
                        guidance=self._maintenance_guidance(maintenance.get("tags", [])),
                        tags=list(maintenance.get("tags", []) or []),
                        importance=0.6,
                    )
                    pending["feedback_done"] = True
                except Exception as exc:
                    failures.append("feedback")
                    logger.warning(f"[DreamScheduler] feedback write-back degraded: {exc}")

            if not pending["diary_memory_done"]:
                try:
                    await self.memory_engine.add_memory(
                        content=f"[梦境日记] {dream_text}",
                        session_id="__dream_diary__",
                        importance=0.5,
                    )
                    pending["diary_memory_done"] = True
                except Exception as exc:
                    failures.append("diary_memory")
                    logger.warning(f"[DreamScheduler] diary write-back degraded: {exc}")

            if not pending["maintenance_memory_done"]:
                try:
                    # OPT-15/ML-07: 运维摘要写入独立日记会话，不得进入真实会话的
                    # 可检索层（importance=0.65 的 active 记忆会被注入聊天 prompt，
                    # 用户可能看到 bot 提及"维护动作"）
                    await self.memory_engine.add_memory(
                        content=f"[dream_maintenance] {maintenance['summary']}",
                        session_id="__dream_diary__",
                        importance=0.65,
                    )
                    pending["maintenance_memory_done"] = True
                except Exception as exc:
                    failures.append("maintenance_memory")
                    logger.warning(f"[DreamScheduler] maintenance write-back degraded: {exc}")

            if not pending["visible_send_done"]:
                target = getattr(self.config.life, "dream_send_target", "") or session_id
                try:
                    if not outbound_send_allowed():
                        failures.append("visible_send_shutdown_rejected")
                        return {
                            "performed": False,
                            "degraded": True,
                            "reason": "shutdown_rejected",
                            "failures": failures,
                            "session_id": session_id,
                            "throttle_scope": "global",
                        }
                    await self.context.send_message(target, MessageChain().message(dream_text))
                    pending["visible_send_done"] = True
                except Exception as exc:
                    failures.append("visible_send")
                    logger.warning(f"[DreamScheduler] dream push degraded: {exc}")

            stage_status = {
                key: bool(pending[key])
                for key in ("feedback_done", "diary_memory_done", "maintenance_memory_done", "visible_send_done")
            }
            if not all(stage_status.values()):
                return {
                    "performed": False,
                    "degraded": True,
                    "reason": "dream_completion_incomplete",
                    "failures": failures,
                    "stage_status": stage_status,
                    "session_id": session_id,
                    "throttle_scope": "global",
                }

            self._pending_completions.pop(request_key, None)
            self._last_dream_time = time.time()
            await self._persist_runtime_state()
            return {
                "performed": True,
                "session_id": session_id,
                "dream_visible": bool(dream_text and self.dream_visible),
                "summary": str(maintenance.get("summary", "") or ""),
                "promotion_report": pending["promotion_report"],
                "stage_status": stage_status,
                "throttle_scope": "global",
            }

    async def run_once(self):
        return await self._run_for_session(None)

    async def run_once_for_session(self, session_id: str) -> dict:
        """Trigger dream for a specific session.

        Note: throttle is **global** — ``_last_dream_time`` is shared across
        all sessions.  ``session_id`` is only passed to the dream agent and
        does NOT affect the throttle decision.
        """
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

    def _runtime_state_path(self) -> Path | None:
        persistence = getattr(self._db_service, "persistence", None)
        cache_dir = getattr(persistence, "cache_dir", None)
        if cache_dir is None:
            return None
        return Path(cache_dir) / "dream_scheduler_state.json"

    def _load_runtime_state(self) -> None:
        path = self._runtime_state_path()
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            now_ts = time.time()
            self._last_dream_time = min(float(payload.get("last_dream_time", 0.0) or 0.0), now_ts)
            attempts = dict(payload.get("last_attempt_by_session", {}) or {})
            self._last_attempt_by_session = {
                str(key): min(float(value or 0.0), now_ts)
                for key, value in attempts.items()
                if str(key or "").strip() and now_ts - float(value or 0.0) < self._dream_interval * 2
            }
        except Exception as exc:
            logger.warning(f"[DreamScheduler] failed to restore scheduler state: {exc}")

    async def _persist_runtime_state(self) -> None:
        path = self._runtime_state_path()
        if path is None:
            return

        now_ts = time.time()
        attempts = {
            key: value
            for key, value in self._last_attempt_by_session.items()
            if now_ts - float(value or 0.0) < self._dream_interval * 2
        }
        payload = {
            "last_dream_time": self._last_dream_time,
            "last_attempt_by_session": attempts,
        }

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(path)

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:
            logger.warning(f"[DreamScheduler] failed to persist scheduler state: {exc}")

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
            "session_backoff_count": len(self._last_attempt_by_session),
            "dream_agent_bound": self.dream_agent is not None,
            "dream_generator_bound": self.dream_generator is not None,
            "pending_completions": len(self._pending_completions),
            "throttle_scope": "global",
        }


__all__ = ["DreamScheduler"]
