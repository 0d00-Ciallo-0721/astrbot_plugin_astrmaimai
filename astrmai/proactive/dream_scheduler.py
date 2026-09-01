from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain
from ..infrastructure.runtime.outbound_send_guard import outbound_send_allowed
from ..infrastructure.runtime.background_task_ledger import (
    BackgroundTaskLedger,
    TaskLease,
    settle_task_lease,
)
from ..infrastructure.persistence.dream_completion_outbox import DreamCompletionOutboxStore


class DreamScheduler:
    _TASK_LEASE_PAYLOAD_KEY = "_background_task_lease"
    FAILURE_RETRY_BASE_SECONDS = 300.0
    FAILURE_RETRY_MAX_SECONDS = 3600.0

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
        self._task_ledger = None
        self._completion_outbox = None
        self._completion_outbox_loaded = False
        self._completion_lease_tokens: dict[str, str] = {}
        self._last_dream_time = 0.0
        self._dream_retry_at = 0.0
        self._dream_failure_count = 0
        self._dream_reservation_owner = ""
        self._dream_reservation_until = 0.0
        self._last_attempt_by_session: dict[str, float] = {}
        self._pending_completions: dict[str, dict] = {}
        self._recent_runs: list[dict] = []
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
        db_path = getattr(getattr(db_service, "persistence", None), "db_path", None)
        self._task_ledger = BackgroundTaskLedger(db_path) if db_path else None
        self._completion_outbox = DreamCompletionOutboxStore(db_path) if db_path else None
        self._load_runtime_state()

    async def _restore_pending_completions(self) -> None:
        if self._completion_outbox is None:
            return
        self._completion_outbox_loaded = True
        try:
            claim_pending = getattr(self._completion_outbox, "claim_pending", None)
            entries = (
                await claim_pending(limit=20, lease_seconds=300.0)
                if callable(claim_pending)
                else [
                    (request_key, payload, "")
                    for request_key, payload in await self._completion_outbox.list_pending(limit=20)
                ]
            )
            for request_key, payload, lease_token in entries:
                if request_key and isinstance(payload, dict):
                    self._pending_completions.setdefault(request_key, payload)
                    if lease_token:
                        self._completion_lease_tokens[request_key] = lease_token
        except Exception:
            logger.debug("[DreamScheduler] durable completion restore degraded", exc_info=True)

    async def _save_pending_completion(self, request_key: str, payload: dict) -> bool:
        if self._completion_outbox is None:
            return True
        try:
            lease_token = str(self._completion_lease_tokens.get(request_key, "") or "")
            update_claimed = getattr(self._completion_outbox, "update_claimed", None)
            if lease_token and callable(update_claimed):
                return bool(
                    await update_claimed(
                        request_key,
                        payload,
                        lease_token=lease_token,
                        lease_seconds=300.0,
                    )
                )
            save_claimed = getattr(self._completion_outbox, "save_claimed", None)
            if callable(save_claimed):
                lease_token = await save_claimed(
                    request_key,
                    payload,
                    lease_seconds=300.0,
                )
                if not lease_token:
                    return False
                self._completion_lease_tokens[request_key] = lease_token
                return True
            await self._completion_outbox.save(request_key, payload)
            return True
        except Exception:
            logger.debug("[DreamScheduler] durable completion checkpoint degraded", exc_info=True)
            return False

    async def _delete_pending_completion(self, request_key: str) -> bool:
        if self._completion_outbox is None:
            return True
        try:
            deleted = await self._completion_outbox.delete(
                request_key,
                lease_token=str(
                    self._completion_lease_tokens.get(request_key, "") or ""
                ),
            )
            if deleted:
                self._completion_lease_tokens.pop(request_key, None)
            return bool(deleted)
        except Exception:
            logger.debug("[DreamScheduler] durable completion cleanup degraded", exc_info=True)
            return False

    @staticmethod
    def _serialize_task_lease(lease: TaskLease) -> dict:
        return {
            "task_id": lease.task_id,
            "task_family": lease.task_family,
            "scope_id": lease.scope_id,
            "lease_token": lease.lease_token,
            "lease_until": lease.lease_until,
            "retry_count": lease.retry_count,
        }

    def _restore_task_lease(self, pending: dict | None) -> TaskLease | None:
        payload = dict((pending or {}).get(self._TASK_LEASE_PAYLOAD_KEY) or {})
        if not payload.get("task_id") or not payload.get("lease_token"):
            return None
        try:
            return TaskLease(
                task_id=str(payload["task_id"]),
                task_family=str(payload.get("task_family") or "dream"),
                scope_id=str(payload.get("scope_id") or "__global__"),
                lease_token=str(payload["lease_token"]),
                lease_until=float(payload.get("lease_until", 0.0) or 0.0),
                retry_count=int(payload.get("retry_count", 0) or 0),
            )
        except (TypeError, ValueError):
            return None

    async def _claim_task_lease(
        self,
        request_key: str,
        pending: dict | None = None,
    ) -> TaskLease | None:
        restored = self._restore_task_lease(pending)
        if restored is not None:
            return restored
        if self._task_ledger is None:
            return None
        fingerprint = str((pending or {}).get("task_input_fingerprint") or request_key)
        lease = await self._task_ledger.claim(
            task_family="dream",
            scope_id="__global__",
            input_fingerprint=fingerprint,
            lease_seconds=max(300.0, self._dream_interval),
            min_interval_seconds=0.0 if pending is not None else max(0.0, float(self._dream_interval)),
            payload={"request_key": request_key, "run_id": str((pending or {}).get("run_id") or "")},
        )
        if lease is not None and pending is not None:
            pending["task_input_fingerprint"] = fingerprint
            pending[self._TASK_LEASE_PAYLOAD_KEY] = self._serialize_task_lease(lease)
        return lease

    async def _finish_task_lease(
        self,
        request_key: str,
        pending: dict | None,
        lease: TaskLease | None,
        *,
        status: str,
        error: str = "",
        retry_after_seconds: float = 0.0,
    ) -> bool:
        if self._task_ledger is None:
            return True
        active_lease = lease or self._restore_task_lease(pending)
        if active_lease is None:
            active_lease = await self._claim_task_lease(request_key, pending)
        if active_lease is None:
            return False
        run_id = str((pending or {}).get("run_id") or "").strip()
        if not run_id:
            run_id = f"dream_{request_key}_{active_lease.task_id}"
        finished = await settle_task_lease(
            self._task_ledger,
            active_lease,
            run_id=run_id,
            status=status,
            error=error,
            retry_after_seconds=retry_after_seconds,
        )
        if finished and pending is not None:
            pending.pop(self._TASK_LEASE_PAYLOAD_KEY, None)
        return bool(finished)

    def should_run(self, now_ts: float) -> bool:
        return (
            self.dream_agent is not None
            and now_ts - self._last_dream_time >= self._dream_interval
            and now_ts >= self._dream_retry_at
            and self._within_dream_time_range()
        )

    def _failure_retry_delay(self) -> float:
        return min(
            self.FAILURE_RETRY_MAX_SECONDS,
            self.FAILURE_RETRY_BASE_SECONDS
            * (2 ** max(0, int(self._dream_failure_count or 1) - 1)),
        )

    async def _record_generation_failure(
        self,
        request_key: str,
        lease: TaskLease | None,
        error: str,
    ) -> None:
        self._dream_failure_count = max(0, int(self._dream_failure_count)) + 1
        retry_delay = self._failure_retry_delay()
        self._dream_retry_at = time.time() + retry_delay
        self._dream_reservation_owner = request_key
        self._dream_reservation_until = self._dream_retry_at
        self._last_attempt_by_session.pop(request_key, None)
        await self._finish_task_lease(
            request_key,
            None,
            lease,
            status="retry_wait",
            error=error,
            retry_after_seconds=retry_delay,
        )
        await self._persist_runtime_state()

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
        reservation_until = max(
            self._dream_reservation_until,
            self._dream_retry_at,
            self._last_dream_time + self._dream_interval if self._last_dream_time else 0.0,
        )
        if now_ts < reservation_until:
            if self._dream_reservation_owner == session_id:
                return {
                    "eligible": False,
                    "reason": "dream_session_backoff",
                    "session_id": session_id,
                    "retry_after": reservation_until,
                    "throttle_scope": "session",
                }
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
        await self._restore_pending_completions()
        async with self._bg_semaphore:
            # Keep the throttle check inside semaphore so concurrent sessions cannot bypass it.
            request_key = str(session_id or "__global__")
            pending = self._pending_completions.get(request_key)
            if pending is None and self._pending_completions:
                request_key, pending = min(
                    self._pending_completions.items(),
                    key=lambda item: float(item[1].get("started_at", 0.0) or 0.0),
                )
            if (
                pending is not None
                and self._completion_outbox is not None
                and not self._completion_lease_tokens.get(request_key)
            ):
                if not await self._save_pending_completion(request_key, pending):
                    return {
                        "performed": False,
                        "reason": "dream_completion_lease_busy",
                        "session_id": str(session_id or ""),
                        "throttle_scope": "global",
                    }
            now_ts = time.time()
            reservation_until = max(
                self._dream_reservation_until,
                self._dream_retry_at,
                self._last_dream_time + self._dream_interval if self._last_dream_time else 0.0,
            )
            if pending is None and now_ts < reservation_until:
                reason = "dream_session_backoff" if self._dream_reservation_owner == request_key else "dream_global_cooldown"
                return {"performed": False, "reason": reason, "session_id": str(session_id or ""), "throttle_scope": "session" if reason == "dream_session_backoff" else "global"}
            if pending is None and session_id:
                eligibility = await self.describe_session_eligibility_async(session_id, time.time())
                if not eligibility.get("eligible", False):
                    return {
                        "performed": False,
                        "reason": str(eligibility.get("reason", "dream_global_cooldown") or "dream_global_cooldown"),
                        "session_id": str(eligibility.get("session_id", session_id) or ""),
                        "throttle_scope": str(eligibility.get("throttle_scope", "global") or "global"),
                    }
            lease = None
            if pending is not None:
                lease = await self._claim_task_lease(request_key, pending)
                if self._task_ledger is not None and lease is None:
                    return {
                        "performed": False,
                        "reason": "dream_lease_busy",
                        "session_id": str(pending.get("session_id") or session_id or ""),
                        "throttle_scope": "global",
                    }
                if lease is not None and not await self._save_pending_completion(request_key, pending):
                    return {
                        "performed": False,
                        "reason": "dream_completion_lease_lost",
                        "session_id": str(pending.get("session_id") or session_id or ""),
                        "throttle_scope": "global",
                    }
            if pending is None:
                lease = await self._claim_task_lease(request_key)
                if self._task_ledger is not None and lease is None:
                    return {"performed": False, "reason": "dream_lease_busy", "session_id": str(session_id or ""), "throttle_scope": "global"}
                min_events = getattr(self.config.life, "min_memory_events_to_dream", getattr(self.dream_agent, "MIN_EVENTS_TO_DREAM", 5))
                original_min_events = self.dream_agent.MIN_EVENTS_TO_DREAM
                self._last_attempt_by_session[request_key] = time.time()
                self._dream_reservation_owner = request_key
                self._dream_reservation_until = time.time() + max(
                    self.FAILURE_RETRY_BASE_SECONDS,
                    float(getattr(self.dream_agent, "TIMEOUT_SEC", 90.0) or 90.0),
                )
                await self._persist_runtime_state()
                try:
                    self.dream_agent.MIN_EVENTS_TO_DREAM = min_events
                    dream_log = await self.dream_agent.run_dream_cycle(session_id=session_id)
                except asyncio.CancelledError:
                    await asyncio.shield(
                        self._record_generation_failure(
                            request_key,
                            lease,
                            "dream_generation_cancelled",
                        )
                    )
                    raise
                except Exception as exc:
                    await self._record_generation_failure(
                        request_key,
                        lease,
                        str(exc),
                    )
                    raise
                finally:
                    self.dream_agent.MIN_EVENTS_TO_DREAM = original_min_events
                if not dream_log:
                    await self._finish_task_lease(
                        request_key,
                        None,
                        lease,
                        status="succeeded",
                    )
                if not dream_log:
                    self._last_dream_time = time.time()
                    self._dream_retry_at = 0.0
                    self._dream_failure_count = 0
                    self._dream_reservation_until = self._last_dream_time + self._dream_interval
                    await self._persist_runtime_state()
                    return {"performed": False, "reason": "no_dream_log", "session_id": str(session_id or ""), "throttle_scope": "global"}

                resolved_session_id = str(session_id or getattr(self.dream_agent, "_last_session_id", "global") or "global")
                persona_name = getattr(getattr(self.config, "persona", None), "name", "Mai")
                try:
                    dream_text = await self.dream_generator.generate(
                        dream_log=dream_log,
                        persona_name=persona_name,
                        session_id=resolved_session_id,
                    )
                    maintenance = self.dream_generator.build_maintenance_result(dream_log, session_id=resolved_session_id)
                except asyncio.CancelledError:
                    await asyncio.shield(
                        self._record_generation_failure(
                            request_key,
                            lease,
                            "dream_generation_cancelled",
                        )
                    )
                    raise
                except Exception as exc:
                    await self._record_generation_failure(
                        request_key,
                        lease,
                        str(exc),
                    )
                    raise
                promotion_report = {}
                if self.promotion_engine is not None and hasattr(self.promotion_engine, "run_audit"):
                    try:
                        promotion_report = await self.promotion_engine.run_audit(resolved_session_id, maintenance)
                    except Exception as exc:
                        logger.debug(f"[DreamScheduler] promotion audit degraded: {exc}")
                pending = {
                    "run_id": f"dream_{uuid.uuid4().hex[:16]}",
                    "session_id": resolved_session_id,
                    "started_at": time.time(),
                    "dream_text": str(dream_text or ""),
                    "maintenance": maintenance,
                    "promotion_report": promotion_report,
                    "feedback_done": not bool(self.memory_engine and hasattr(self.memory_engine, "record_cognitive_feedback")),
                    "diary_memory_done": not bool(dream_text and self.memory_engine and hasattr(self.memory_engine, "add_memory")),
                    "maintenance_memory_done": not bool(dream_text and self.memory_engine and hasattr(self.memory_engine, "add_memory")),
                    "visible_send_done": not bool(dream_text and self.dream_visible),
                    "task_input_fingerprint": str(request_key),
                }
                if lease is not None:
                    pending[self._TASK_LEASE_PAYLOAD_KEY] = self._serialize_task_lease(lease)
                self._pending_completions[request_key] = pending
                await self._persist_dream_run(pending, status="running")
                if not await self._save_pending_completion(request_key, pending):
                    return {
                        "performed": False,
                        "reason": "dream_completion_lease_busy",
                        "session_id": resolved_session_id,
                        "throttle_scope": "global",
                    }
                await self._persist_runtime_state()

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
                        kind="dream_diary",
                        source="dream_diary",
                        metadata={"dream_run_id": str(pending.get("run_id") or "")},
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
                        kind="dream_maintenance",
                        source="dream_maintenance",
                        metadata={"dream_run_id": str(pending.get("run_id") or "")},
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
                    else:
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
                pending["last_stage_status"] = stage_status
                pending["last_failures"] = list(failures)
                await self._persist_dream_run(
                    pending,
                    status="incomplete",
                    error=", ".join(failures),
                )
                ledger_settled = await self._finish_task_lease(
                    request_key,
                    pending,
                    lease,
                    status="retry_wait",
                    error=", ".join(failures),
                    retry_after_seconds=300.0,
                )
                checkpointed = await self._save_pending_completion(request_key, pending)
                await self._persist_runtime_state()
                if not checkpointed:
                    return {
                        "performed": False,
                        "degraded": True,
                        "reason": "dream_completion_lease_lost",
                        "failures": failures,
                        "stage_status": stage_status,
                        "session_id": session_id,
                        "throttle_scope": "global",
                    }
                if not ledger_settled:
                    return {
                        "performed": False,
                        "degraded": True,
                        "reason": "dream_ledger_settlement_failed",
                        "failures": failures,
                        "stage_status": stage_status,
                        "session_id": session_id,
                        "throttle_scope": "global",
                    }
                return {
                    "performed": False,
                    "degraded": True,
                    "reason": "dream_completion_incomplete",
                    "failures": failures,
                    "stage_status": stage_status,
                    "session_id": session_id,
                    "throttle_scope": "global",
                }

            ledger_settled = await self._finish_task_lease(
                request_key,
                pending,
                lease,
                status="succeeded",
            )
            if not ledger_settled:
                await self._save_pending_completion(request_key, pending)
                return {
                    "performed": False,
                    "degraded": True,
                    "reason": "dream_ledger_settlement_failed",
                    "session_id": session_id,
                    "throttle_scope": "global",
                }
            if not await self._delete_pending_completion(request_key):
                return {
                    "performed": False,
                    "degraded": True,
                    "reason": "dream_completion_lease_lost",
                    "session_id": session_id,
                    "throttle_scope": "global",
                }
            self._pending_completions.pop(request_key, None)
            self._last_dream_time = time.time()
            self._dream_retry_at = 0.0
            self._dream_failure_count = 0
            self._dream_reservation_until = self._last_dream_time + self._dream_interval
            self._recent_runs.append(
                {
                    "run_id": str(pending.get("run_id") or ""),
                    "session_id": session_id,
                    "completed_at": self._last_dream_time,
                    "status": "completed",
                    "dream_text_hash": hashlib.sha256(dream_text.encode("utf-8")).hexdigest()[:16] if dream_text else "",
                    "maintenance_action_count": len(list(maintenance.get("actions", []) or [])),
                    "promotion_report": dict(pending.get("promotion_report") or {}),
                    "stage_status": stage_status,
                }
            )
            await self._persist_dream_run(
                pending,
                status="completed",
                completed_at=self._last_dream_time,
            )
            self._recent_runs = self._recent_runs[-50:]
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
            self._dream_retry_at = min(
                float(payload.get("dream_retry_at", 0.0) or 0.0),
                now_ts + self.FAILURE_RETRY_MAX_SECONDS,
            )
            self._dream_failure_count = max(
                0, int(payload.get("dream_failure_count", 0) or 0)
            )
            self._dream_reservation_owner = str(payload.get("dream_reservation_owner", "") or "")
            self._dream_reservation_until = min(
                float(payload.get("dream_reservation_until", 0.0) or 0.0),
                now_ts + self._dream_interval,
            )
            attempts = dict(payload.get("last_attempt_by_session", {}) or {})
            self._last_attempt_by_session = {
                str(key): min(float(value or 0.0), now_ts)
                for key, value in attempts.items()
                if str(key or "").strip() and now_ts - float(value or 0.0) < self._dream_interval * 2
            }
            self._recent_runs = [
                dict(item)
                for item in list(payload.get("recent_runs", []) or [])[-50:]
                if isinstance(item, dict)
            ]
            pending = payload.get("pending_completions", {})
            if isinstance(pending, dict):
                self._pending_completions = {
                    str(key): dict(value)
                    for key, value in pending.items()
                    if isinstance(value, dict) and str(value.get("run_id") or "").strip()
                }
        except Exception as exc:
            logger.warning(f"[DreamScheduler] failed to restore scheduler state: {exc}")

    async def _persist_dream_run(
        self,
        pending: dict,
        *,
        status: str,
        error: str = "",
        completed_at: float = 0.0,
    ) -> None:
        db_service = self._db_service
        recorder = getattr(db_service, "record_dream_run_async", None) if db_service is not None else None
        if not callable(recorder):
            return
        maintenance = dict(pending.get("maintenance") or {})
        dream_text = str(pending.get("dream_text") or "")
        try:
            await recorder(
                {
                    "run_id": str(pending.get("run_id") or ""),
                    "session_id": str(pending.get("session_id") or ""),
                    "status": str(status or ""),
                    "attempt": int(pending.get("attempt", 1) or 1),
                    "maintenance_summary": str(maintenance.get("summary") or ""),
                    "maintenance_actions": list(maintenance.get("actions") or []),
                    "dream_text_hash": hashlib.sha256(dream_text.encode("utf-8")).hexdigest()[:16] if dream_text else "",
                    "promotion_report": dict(pending.get("promotion_report") or {}),
                    "stage_status": {
                        key: bool(pending.get(key))
                        for key in ("feedback_done", "diary_memory_done", "maintenance_memory_done", "visible_send_done")
                    },
                    "error": str(error or "")[:1000],
                    "started_at": float(pending.get("started_at") or time.time()),
                    "completed_at": float(completed_at or 0.0),
                }
            )
        except Exception as exc:
            logger.debug(f"[DreamScheduler] dream run ledger degraded: {exc}")

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
            "dream_retry_at": self._dream_retry_at,
            "dream_failure_count": self._dream_failure_count,
            "dream_reservation_owner": self._dream_reservation_owner,
            "dream_reservation_until": self._dream_reservation_until,
            "last_attempt_by_session": attempts,
            "recent_runs": list(self._recent_runs[-50:]),
            "pending_completions": dict(self._pending_completions),
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
            "dream_retry_at": self._dream_retry_at,
            "dream_failure_count": self._dream_failure_count,
            "dream_success_cooldown_until": (
                self._last_dream_time + self._dream_interval
                if self._last_dream_time
                else 0.0
            ),
            "session_backoff_count": len(self._last_attempt_by_session),
            "dream_agent_bound": self.dream_agent is not None,
            "dream_generator_bound": self.dream_generator is not None,
            "pending_completions": len(self._pending_completions),
            "recent_runs": list(self._recent_runs[-10:]),
            "throttle_scope": "global",
        }


__all__ = ["DreamScheduler"]
