from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from .rhythm import evaluate_proactive_rhythm


CompletionCallback = Callable[[bool, str], Awaitable[None] | None]


def append_proactive_stage(event: Any, stage: str, status: str, reason: str = "") -> None:
    """Append lifecycle telemetry without allowing observability to affect delivery."""
    try:
        ledger = event.get_extra("astrmai_proactive_stage_ledger", None)
        if isinstance(ledger, list):
            ledger.append({"stage": str(stage), "status": str(status), "reason": str(reason or ""), "at": time.time()})
    except Exception:
        logger.debug("[ProactiveDispatcher] lifecycle telemetry degraded", exc_info=True)


@dataclass(slots=True)
class ProactiveMessageIntent:
    chat_id: str
    source: str
    reason: str
    guidance: str
    suggested_social_intent: str = "join"
    suggested_action_tier: str = "chat"
    urgency: float = 0.0
    cost: float = 0.0
    cooldown: float = 0.0
    created_at: float = 0.0
    intent_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProactiveDispatchDecision:
    intent_id: str
    chat_id: str
    source: str
    timestamp: float
    allowed: bool = False
    blocked_reason: str = ""
    safety_checks: dict[str, Any] = field(default_factory=dict)
    synthetic_event_queued: bool = False
    reply_sent: bool = False
    status: str = "blocked"
    reply_preview: str = ""
    completion_reason: str = ""
    stage_ledger: list[dict[str, Any]] = field(default_factory=list)


class ProactiveDispatcher:
    """Routes proactive candidates back through the normal attention/planning chain."""

    HISTORY_LIMIT = 200

    def __init__(
        self,
        *,
        attention_gate: Any = None,
        runtime_coordinator: Any = None,
        state_engine: Any = None,
        config: Any = None,
    ) -> None:
        self.attention_gate = attention_gate
        self.runtime_coordinator = runtime_coordinator
        self.state_engine = state_engine
        self.config = config
        self._history: list[dict[str, Any]] = []
        self._cooldowns: dict[str, float] = {}
        self._callbacks: dict[str, CompletionCallback] = {}
        self._completion_watchdogs: dict[str, asyncio.Task] = {}
        self._terminal_intents: set[str] = set()
        self._dispatch_lock = asyncio.Lock()

    def _completion_timeout_seconds(self, intent: ProactiveMessageIntent) -> float:
        metadata_value = intent.metadata.get("completion_timeout_sec", None)
        timing = getattr(self.config, "timing", None)
        configured = getattr(timing, "proactive_completion_timeout_sec", 180.0)
        try:
            value = float(metadata_value if metadata_value is not None else configured)
        except (TypeError, ValueError):
            value = 180.0
        return min(3600.0, max(5.0, value))

    async def _watch_completion(
        self,
        intent_id: str,
        timeout_sec: float,
        decision: ProactiveDispatchDecision,
        completion: CompletionCallback,
    ) -> None:
        try:
            await asyncio.sleep(timeout_sec)
        except asyncio.CancelledError:
            return
        if decision.reply_sent or decision.status != "queued":
            return
        decision.blocked_reason = "completion_timeout"
        decision.completion_reason = "completion_timeout"
        decision.stage_ledger.append(
            {
                "stage": "proactive.completion_watchdog",
                "status": "timeout",
                "reason": "completion_timeout",
                "at": time.time(),
                "timeout_sec": timeout_sec,
            }
        )
        await self._sync_history_for_dispatch(intent_id, decision)
        try:
            result = completion(False, "")
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning(
                f"[ProactiveDispatcher] completion watchdog settle failed "
                f"intent={intent_id}: {type(exc).__name__}"
            )
        finally:
            self._completion_watchdogs.pop(intent_id, None)

    def _arm_completion_watchdog(
        self,
        intent: ProactiveMessageIntent,
        decision: ProactiveDispatchDecision,
        completion: CompletionCallback,
    ) -> None:
        timeout_sec = self._completion_timeout_seconds(intent)
        decision.stage_ledger.append(
            {
                "stage": "proactive.completion_watchdog",
                "status": "armed",
                "reason": "",
                "at": time.time(),
                "timeout_sec": timeout_sec,
            }
        )
        task = asyncio.create_task(
            self._watch_completion(intent.intent_id, timeout_sec, decision, completion),
            name=f"proactive-completion-watchdog:{intent.intent_id}",
        )
        self._completion_watchdogs[intent.intent_id] = task

    def _cancel_completion_watchdog(self, intent_id: str) -> None:
        task = self._completion_watchdogs.pop(intent_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    @staticmethod
    def _preview(text: Any, limit: int = 160) -> str:
        cleaned = " ".join(str(text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)] + "..."

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _new_intent_id(self, intent: ProactiveMessageIntent, now: float) -> str:
        if intent.intent_id:
            return intent.intent_id
        seed = abs(hash((intent.chat_id, intent.source, intent.reason, intent.guidance, now)))
        return f"{intent.source}-{int(now * 1000)}-{seed % 1000000:06d}"

    async def _state_energy(self, chat_id: str) -> float | None:
        if not self.state_engine or not hasattr(self.state_engine, "get_state"):
            return None
        try:
            state = await self._maybe_await(self.state_engine.get_state(chat_id))
            return float(getattr(state, "energy", 0.0))
        except Exception as exc:
            logger.debug(f"[ProactiveDispatcher] state lookup degraded for {chat_id}: {exc}")
            return None

    async def _proactive_generation_current(self, intent: ProactiveMessageIntent) -> tuple[bool, int | None]:
        if not self.state_engine or not hasattr(self.state_engine, "is_proactive_generation_current"):
            return True, None
        captured = int(intent.metadata.get("captured_generation", 0) or 0)
        try:
            current = await self._maybe_await(
                self.state_engine.is_proactive_generation_current(intent.chat_id, captured)
            )
            return bool(current), captured
        except Exception as exc:
            logger.warning(f"[ProactiveDispatcher] generation check failed closed for {intent.chat_id}: {exc}")
            return False, captured

    async def _scheduling_snapshot(self, chat_id: str) -> dict[str, Any]:
        if not self.state_engine or not hasattr(self.state_engine, "get_state"):
            return {}
        try:
            state = await self._maybe_await(self.state_engine.get_state(chat_id))
        except Exception as exc:
            logger.debug(f"[ProactiveDispatcher] scheduling snapshot degraded for {chat_id}: {exc}")
            return {}
        return {
            "chat_kind": str(getattr(state, "chat_kind", "") or ""),
            "last_real_user_activity_at": float(
                getattr(state, "last_real_user_activity_at", 0.0) or 0.0
            ),
            "last_committed_bot_reply_at": float(
                getattr(state, "last_committed_bot_reply_at", 0.0) or 0.0
            ),
            "next_proactive_due_at": float(
                getattr(state, "next_proactive_due_at", 0.0) or 0.0
            ),
            "unanswered_proactive_count": int(
                getattr(state, "unanswered_proactive_count", 0) or 0
            ),
            "last_proactive_cancel_reason": str(
                getattr(state, "last_proactive_cancel_reason", "") or ""
            ),
        }

    async def _activity_snapshot(self, chat_id: str) -> dict[str, Any]:
        if not self.runtime_coordinator or not hasattr(self.runtime_coordinator, "get_activity_snapshot"):
            return {}
        try:
            snapshot = await self.runtime_coordinator.get_activity_snapshot(chat_id)
            return snapshot if isinstance(snapshot, dict) else {}
        except Exception as exc:
            logger.debug(f"[ProactiveDispatcher] runtime snapshot degraded for {chat_id}: {exc}")
            return {}

    @staticmethod
    def _proactive_noise_block(snapshot: dict[str, Any], bot_id: str) -> str:
        preview = str(snapshot.get("latest_activity_preview", "") or "").strip()
        lowered = preview.lower()
        try:
            recent_60s = int(snapshot.get("recent_activity_count_60s", 0) or 0)
        except (TypeError, ValueError):
            recent_60s = 0
        if recent_60s >= 4:
            return "recent_group_burst"
        if not preview:
            return ""
        if "[](%7b%22version%22" in lowered or "[图片]" in preview or "[image]" in lowered:
            return "recent_media_or_card"
        command_like = bool(re.search(r"(^|\s)[/!！.。#＃][^\s]+", preview))
        at_targets = [item for item in re.findall(r"@[^()\s]+(?:\((\d+)\))?", preview) if item]
        if command_like and at_targets:
            if not bot_id or any(str(target) != str(bot_id) for target in at_targets):
                return "recent_other_bot_command"
        return ""

    async def _safety_check(self, intent: ProactiveMessageIntent, *, now: float) -> tuple[bool, str, dict[str, Any]]:
        rhythm = evaluate_proactive_rhythm(self.config, now=now)
        snapshot = await self._activity_snapshot(intent.chat_id)
        try:
            latest_ts = float(snapshot.get("latest_activity_ts", 0.0) or 0.0)
        except (TypeError, ValueError):
            latest_ts = 0.0
        active_age = max(0.0, now - latest_ts) if latest_ts > 0 else 0.0
        life = getattr(self.config, "life", None)
        scheduled_inactive_allowed = (
            intent.source == "scheduled_scenario"
            and bool(intent.metadata.get("allow_inactive_chat", False))
            and bool(getattr(life, "scheduled_scenarios_allow_inactive_chat", False))
        )
        active = (
            intent.source == "wakeup"
            or scheduled_inactive_allowed
            or (latest_ts > 0 and active_age <= 1800.0)
        )
        raw_wait_targets = snapshot.get("wait_targets", []) or []
        if not isinstance(raw_wait_targets, (list, tuple, set)):
            raw_wait_targets = [raw_wait_targets]
        wait_targets = [str(item).strip() for item in raw_wait_targets if str(item).strip()]
        try:
            executor_pending = int(snapshot.get("executor_pending", 0) or 0)
        except (TypeError, ValueError):
            executor_pending = 1
        try:
            recent_activity_count_60s = int(snapshot.get("recent_activity_count_60s", 0) or 0)
        except (TypeError, ValueError):
            recent_activity_count_60s = 0
        bot_id = str(getattr(self.state_engine, "bot_id", "") or "")
        noise_block = self._proactive_noise_block(snapshot, bot_id) if intent.source == "wakeup" else ""
        energy = await self._state_energy(intent.chat_id)
        min_energy = float(getattr(getattr(self.config, "life", None), "wakeup_min_energy", 0.0) or 0.0)
        talk_willingness = intent.metadata.get("talk_willingness", None)
        try:
            talk_value = float(talk_willingness) if talk_willingness is not None else None
        except (TypeError, ValueError):
            talk_value = None
        cooldown_until = float(self._cooldowns.get(intent.chat_id, 0.0) or 0.0)
        if cooldown_until and now >= cooldown_until:
            self._cooldowns.pop(intent.chat_id, None)
            cooldown_until = 0.0
        generation_current, captured_generation = await self._proactive_generation_current(intent)
        scheduling = await self._scheduling_snapshot(intent.chat_id)
        checks = {
            "has_attention_gate": bool(self.attention_gate and hasattr(self.attention_gate, "inject_external_event")),
            "chat_active": active,
            "scheduled_inactive_allowed": scheduled_inactive_allowed,
            "active_age_seconds": round(active_age, 2),
            "wait_targets_empty": not wait_targets,
            "executor_idle": executor_pending <= 0,
            "energy": energy,
            "min_energy": min_energy,
            "talk_willingness": talk_value,
            "cooldown_until": cooldown_until,
            "cooldown_clear": now >= cooldown_until,
            "source": intent.source,
            "quiet_hours": rhythm.quiet_hours,
            "time_bucket": rhythm.time_bucket,
            "proactive_quiet_hours": list(rhythm.quiet_ranges),
            "base_frequency": rhythm.base_frequency,
            "base_frequency_factor": rhythm.base_frequency_factor,
            "proactive_noise_block": noise_block,
            "latest_activity_preview": self._preview(snapshot.get("latest_activity_preview", ""), 80),
            "recent_activity_count_60s": recent_activity_count_60s,
            "captured_generation": captured_generation,
            "generation_current": generation_current,
            **scheduling,
        }
        if not checks["has_attention_gate"]:
            return False, "attention_gate_unavailable", checks
        if not intent.chat_id:
            return False, "missing_chat_id", checks
        if not generation_current:
            return False, "proactive_generation_superseded", checks
        if intent.source in {"wakeup", "heartflow", "scheduled_scenario"} and rhythm.quiet_hours:
            return False, "quiet_hours", checks
        if not active:
            return False, "chat_inactive", checks
        if wait_targets or executor_pending > 0:
            return False, "user_waiting", checks
        if noise_block:
            return False, noise_block, checks
        if cooldown_until and now < cooldown_until:
            return False, "cooldown", checks
        if energy is not None and min_energy > 0 and energy < min_energy:
            return False, "low_energy", checks
        if intent.source == "scheduled_scenario":
            unanswered = int(scheduling.get("unanswered_proactive_count", 0) or 0)
            max_unanswered = int(getattr(life, "proactive_max_unanswered", 2) or 0)
            if max_unanswered >= 0 and unanswered >= max_unanswered:
                return False, "max_unanswered", checks
        if intent.source == "heartflow" and talk_value is not None and talk_value < 0.25:
            return False, "low_talk_willingness", checks
        return True, "", checks

    def _intent_record(self, intent: ProactiveMessageIntent, decision: ProactiveDispatchDecision) -> dict[str, Any]:
        return {
            "created_at": decision.timestamp,
            "intent": asdict(intent),
            "decision": asdict(decision),
            "chat_id": intent.chat_id,
            "source": intent.source,
            "status": decision.status,
            "blocked_reason": decision.blocked_reason,
            "reply_sent": decision.reply_sent,
            "stage_ledger": [dict(entry) for entry in decision.stage_ledger],
        }

    def _remember(self, intent: ProactiveMessageIntent, decision: ProactiveDispatchDecision) -> None:
        self._history = [*self._history, self._intent_record(intent, decision)][-self.HISTORY_LIMIT :]

    async def _sync_history_for_dispatch(self, intent_id: str, decision: ProactiveDispatchDecision) -> None:
        for item in reversed(self._history):
            if str(item.get("decision", {}).get("intent_id", "")) != intent_id:
                continue
            item["decision"] = asdict(decision)
            item["status"] = decision.status
            item["blocked_reason"] = decision.blocked_reason
            item["reply_sent"] = decision.reply_sent
            item["stage_ledger"] = [dict(entry) for entry in decision.stage_ledger]
            return

    def list_intents(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), self.HISTORY_LIMIT))
        return list(self._history)[-limit:][::-1]

    def describe_status(self) -> dict[str, Any]:
        return {
            "available": bool(self.attention_gate and hasattr(self.attention_gate, "inject_external_event")),
            "history_size": len(self._history),
            "cooldown_chats": len(self._cooldowns),
            "completion_watchdogs": len(self._completion_watchdogs),
        }

    async def shutdown(self) -> None:
        """Cancel pending watchers and settle queued claims during teardown."""
        for intent_id in list(self._completion_watchdogs):
            self._cancel_completion_watchdog(intent_id)

        pending_ids: list[str] = []
        for item in self._history:
            payload = item.get("decision", {})
            if str(payload.get("status", "") or "") != "queued":
                continue
            intent_id = str(payload.get("intent_id", "") or "")
            if not intent_id:
                continue
            payload["blocked_reason"] = "dispatcher_shutdown"
            item["decision"] = payload
            item["blocked_reason"] = "dispatcher_shutdown"
            pending_ids.append(intent_id)
            self._terminal_intents.add(intent_id)

        for intent_id in pending_ids:
            await self.complete(
                intent_id,
                reply_sent=False,
                completion_reason="dispatcher_shutdown",
            )
            for item in reversed(self._history):
                payload = item.get("decision", {})
                if str(payload.get("intent_id", "") or "") != intent_id:
                    continue
                entries = list(payload.get("stage_ledger", []) or [])
                entries.append(
                    {
                        "stage": "proactive.dispatcher_shutdown",
                        "status": "settled",
                        "reason": "dispatcher_shutdown",
                        "at": time.time(),
                    }
                )
                payload["stage_ledger"] = entries
                item["decision"] = payload
                item["stage_ledger"] = [dict(entry) for entry in entries]
                break

    async def complete(
        self,
        intent_id: str,
        *,
        reply_sent: bool,
        reply_preview: str = "",
        synthetic_event_queued: bool | None = None,
        completion_reason: str = "",
    ) -> None:
        self._cancel_completion_watchdog(intent_id)
        callback = self._callbacks.pop(intent_id, None)
        decision = None
        cooldown_until = 0.0
        for item in reversed(self._history):
            payload = item.get("decision", {})
            if str(payload.get("intent_id", "")) == intent_id:
                if synthetic_event_queued is not None:
                    payload["synthetic_event_queued"] = bool(synthetic_event_queued)
                payload["reply_sent"] = bool(reply_sent)
                payload["reply_preview"] = self._preview(reply_preview, 120)
                if completion_reason:
                    payload["completion_reason"] = str(completion_reason)
                payload["status"] = (
                    "sent"
                    if reply_sent
                    else (
                        "timeout"
                        if completion_reason == "completion_timeout"
                        else (
                            "shutdown"
                            if completion_reason == "dispatcher_shutdown"
                            else ("queued" if payload.get("synthetic_event_queued") else "skipped")
                        )
                    )
                )
                item["decision"] = payload
                item["reply_sent"] = bool(reply_sent)
                item["status"] = payload["status"]
                if reply_sent:
                    try:
                        cooldown_seconds = float(item.get("intent", {}).get("cooldown", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        cooldown_seconds = 0.0
                    if cooldown_seconds > 0:
                        cooldown_until = time.time() + cooldown_seconds
                        self._cooldowns[str(item.get("chat_id", "") or "")] = cooldown_until
                decision = payload
                break
        if callback:
            result = callback(bool(reply_sent), str(reply_preview or ""))
            if inspect.isawaitable(result):
                await result
        if decision:
            logger.debug(f"[ProactiveDispatcher] completion updated: {intent_id} -> {decision.get('status')}")

    async def dispatch(
        self,
        intent: ProactiveMessageIntent,
        *,
        on_complete: CompletionCallback | None = None,
    ) -> ProactiveDispatchDecision:
        async with self._dispatch_lock:
            return await self._dispatch_locked(intent, on_complete=on_complete)

    async def _dispatch_locked(
        self,
        intent: ProactiveMessageIntent,
        *,
        on_complete: CompletionCallback | None = None,
    ) -> ProactiveDispatchDecision:
        now = time.time()
        stage_ledger: list[dict[str, Any]] = []

        def record_stage(stage: str, status: str, reason: str = "") -> None:
            stage_ledger.append({"stage": stage, "status": status, "reason": reason, "at": time.time()})

        record_stage("proactive.candidate", "started")
        intent.created_at = intent.created_at or now
        intent.intent_id = self._new_intent_id(intent, now)
        claim_token = str(intent.metadata.get("claim_token", "") or "")
        if claim_token:
            record_stage("proactive.claim", "success")
        allowed, blocked_reason, checks = await self._safety_check(intent, now=now)
        if allowed:
            record_stage("proactive.safety_check", "success")
        decision = ProactiveDispatchDecision(
            intent_id=intent.intent_id,
            chat_id=intent.chat_id,
            source=intent.source,
            timestamp=now,
            allowed=allowed,
            blocked_reason=blocked_reason,
            safety_checks=checks,
            status="queued" if allowed else "blocked",
            stage_ledger=stage_ledger,
        )
        self._remember(intent, decision)
        if not allowed:
            record_stage("proactive.safety_check", "blocked", blocked_reason)
            record_stage("proactive.dispatch", "blocked", blocked_reason)
            await self._sync_history_for_dispatch(intent.intent_id, decision)
            return decision

        if not claim_token:
            record_stage("proactive.claim", "not_required", "claim_token_absent")
        record_stage("proactive.sensor", "delegated", "attention_gate")

        if on_complete:
            self._callbacks[intent.intent_id] = on_complete

        async def _completion(reply_sent: bool, reply_preview: str = "") -> None:
            if intent.intent_id in self._terminal_intents or decision.status != "queued" or any(
                item.get("stage") == "proactive.reply_commit"
                for item in decision.stage_ledger
            ):
                return
            self._cancel_completion_watchdog(intent.intent_id)
            if not reply_sent and decision.blocked_reason == "completion_timeout":
                completion_reason = "completion_timeout"
                reply_commit_status = "timeout"
            else:
                completion_reason = decision.blocked_reason or ("reply_not_sent" if not reply_sent else "")
                reply_commit_status = "success" if reply_sent else "skipped"
            decision.reply_sent = bool(reply_sent)
            decision.reply_preview = self._preview(reply_preview, 120)
            decision.completion_reason = completion_reason
            decision.status = (
                "timeout"
                if completion_reason == "completion_timeout"
                else ("sent" if reply_sent else "skipped")
            )
            decision.stage_ledger.append(
                {
                    "stage": "proactive.reply_commit",
                    "status": reply_commit_status,
                    "reason": completion_reason,
                    "at": time.time(),
                }
            )
            await self._sync_history_for_dispatch(intent.intent_id, decision)
            try:
                await self.complete(
                    intent.intent_id,
                    reply_sent=reply_sent,
                    reply_preview=reply_preview,
                    synthetic_event_queued=decision.synthetic_event_queued,
                    completion_reason=completion_reason,
                )
            except asyncio.CancelledError:
                decision.stage_ledger.append(
                    {
                        "stage": "proactive.state_settle",
                        "status": "cancelled",
                        "reason": "completion_cancelled",
                        "at": time.time(),
                    }
                )
                await self._sync_history_for_dispatch(intent.intent_id, decision)
                raise
            except Exception as exc:
                decision.stage_ledger.append(
                    {
                        "stage": "proactive.state_settle",
                        "status": "error",
                        "reason": type(exc).__name__,
                        "at": time.time(),
                    }
                )
                await self._sync_history_for_dispatch(intent.intent_id, decision)
                raise
            decision.stage_ledger.append(
                {
                    "stage": "proactive.state_settle",
                    "status": "success",
                    "reason": "",
                    "at": time.time(),
                }
            )
            await self._sync_history_for_dispatch(intent.intent_id, decision)

        candidate_text = (
            "[主动开口候选]\n"
            "这不是用户消息，而是后台产生的一次主动加入候选。请先根据当前聊天窗口判断是否自然；"
            "如果当前语境不适合，请等待或忽略。\n"
            f"候选指引：{intent.guidance}"
        )
        event_data = {
            "message_str": candidate_text,
            "timestamp": now,
            "sender_id": "astrmai_proactive_candidate",
            "sender_name": "主动开口候选",
            "self_id": str(getattr(self.state_engine, "bot_id", "") or "astrmai"),
            "extra": {
                "astrmai_is_proactive_event": True,
                "astrmai_proactive_candidate": True,
                "astrmai_proactive_source": intent.source,
                "astrmai_proactive_reason": intent.reason,
                "astrmai_proactive_guidance": intent.guidance,
                "astrmai_proactive_intent_id": intent.intent_id,
                "astrmai_proactive_urgency": float(intent.urgency or 0.0),
                "astrmai_proactive_cost": float(intent.cost or 0.0),
                "astrmai_proactive_cooldown": float(intent.cooldown or 0.0),
                "astrmai_proactive_time_bucket": checks.get("time_bucket", ""),
                "astrmai_social_intent": intent.suggested_social_intent,
                "astrmai_action_tier": intent.suggested_action_tier,
                "astrmai_loop_source": "proactive_dispatcher",
                "astrmai_proactive_dispatch_decision": decision,
                "astrmai_proactive_completion_callback": _completion,
                "astrmai_proactive_generation": int(intent.metadata.get("captured_generation", 0) or 0),
                "astrmai_proactive_claim_token": str(intent.metadata.get("claim_token", "") or ""),
                "astrmai_proactive_chat_kind": str(intent.metadata.get("chat_kind", "") or ""),
                "astrmai_proactive_stage_ledger": stage_ledger,
                "astrmai_scheduled_scenario": str(intent.metadata.get("scenario", "") or ""),
                "astrmai_scheduled_delivery_key": str(intent.metadata.get("delivery_key", "") or ""),
                "astrmai_daily_schedule_slot": str(intent.metadata.get("schedule_slot", "") or ""),
                "astrmai_daily_schedule_source": str(intent.metadata.get("schedule_source", "") or ""),
                "astrmai_scheduled_festival": str(intent.metadata.get("festival", "") or ""),
                "astrmai_scheduled_weather_available": bool(intent.metadata.get("weather_available", False)),
            },
        }
        if str(intent.metadata.get("chat_kind", "") or "") == "group":
            event_data["group_id"] = str(intent.metadata.get("group_id", intent.chat_id) or "")

        async def _inject_event():
            record_stage("proactive.generation_recheck", "started")
            generation_current, _ = await self._proactive_generation_current(intent)
            if not generation_current:
                record_stage("proactive.generation_recheck", "blocked", "stale_generation")
                return False
            record_stage("proactive.generation_recheck", "success")
            record_stage("proactive.event_enqueue", "started")
            result = await self.attention_gate.inject_external_event(intent.chat_id, event_data)
            record_stage(
                "proactive.event_enqueue",
                "success" if result else "skipped",
                "" if result else "inject_rejected",
            )
            return result

        try:
            lock_getter = getattr(self.attention_gate, "get_proactive_lock", None)
            if callable(lock_getter):
                injection_lock = lock_getter(intent.chat_id)
                async with injection_lock:
                    result = await _inject_event()
            else:
                result = await _inject_event()
        except asyncio.CancelledError:
            record_stage("proactive.event_enqueue", "cancelled", "dispatch_cancelled")
            raise
        except Exception as exc:
            record_stage("proactive.event_enqueue", "error", type(exc).__name__)
            decision.blocked_reason = f"event_enqueue:{type(exc).__name__}"
            decision.status = "skipped"
            result = False
        decision.synthetic_event_queued = bool(result)
        if not decision.synthetic_event_queued and not decision.blocked_reason:
            if any(
                item.get("stage") == "proactive.generation_recheck"
                and item.get("status") == "blocked"
                for item in decision.stage_ledger
            ):
                decision.blocked_reason = "stale_generation"
            elif any(
                item.get("stage") == "proactive.event_enqueue"
                and item.get("status") == "skipped"
                for item in decision.stage_ledger
            ):
                decision.blocked_reason = "event_enqueue_rejected"
            else:
                decision.blocked_reason = "dispatch_rejected"
        record_stage("proactive.dispatch", "queued" if decision.synthetic_event_queued else "skipped")
        if decision.synthetic_event_queued:
            self._arm_completion_watchdog(intent, decision, _completion)
        if not decision.reply_sent:
            decision.status = "queued" if decision.synthetic_event_queued else "skipped"
        await self._sync_history_for_dispatch(intent.intent_id, decision)
        return decision


__all__ = [
    "ProactiveDispatchDecision",
    "ProactiveDispatcher",
    "ProactiveMessageIntent",
]
