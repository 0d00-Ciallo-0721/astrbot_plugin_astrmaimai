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
        self._dispatch_lock = asyncio.Lock()

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
        active = intent.source == "wakeup" or (latest_ts > 0 and active_age <= 1800.0)
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
        checks = {
            "has_attention_gate": bool(self.attention_gate and hasattr(self.attention_gate, "inject_external_event")),
            "chat_active": active,
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
        }
        if not checks["has_attention_gate"]:
            return False, "attention_gate_unavailable", checks
        if not intent.chat_id:
            return False, "missing_chat_id", checks
        if intent.source in {"wakeup", "heartflow"} and rhythm.quiet_hours:
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
            return

    def list_intents(self, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), self.HISTORY_LIMIT))
        return list(self._history)[-limit:][::-1]

    def describe_status(self) -> dict[str, Any]:
        return {
            "available": bool(self.attention_gate and hasattr(self.attention_gate, "inject_external_event")),
            "history_size": len(self._history),
            "cooldown_chats": len(self._cooldowns),
        }

    async def complete(self, intent_id: str, *, reply_sent: bool, reply_preview: str = "", synthetic_event_queued: bool | None = None) -> None:
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
                payload["status"] = "sent" if reply_sent else ("queued" if payload.get("synthetic_event_queued") else "skipped")
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
        intent.created_at = intent.created_at or now
        intent.intent_id = self._new_intent_id(intent, now)
        allowed, blocked_reason, checks = await self._safety_check(intent, now=now)
        decision = ProactiveDispatchDecision(
            intent_id=intent.intent_id,
            chat_id=intent.chat_id,
            source=intent.source,
            timestamp=now,
            allowed=allowed,
            blocked_reason=blocked_reason,
            safety_checks=checks,
            status="queued" if allowed else "blocked",
        )
        self._remember(intent, decision)
        if not allowed:
            return decision

        if on_complete:
            self._callbacks[intent.intent_id] = on_complete

        async def _completion(reply_sent: bool, reply_preview: str = "") -> None:
            decision.reply_sent = bool(reply_sent)
            decision.reply_preview = self._preview(reply_preview, 120)
            decision.status = "sent" if reply_sent else "skipped"
            await self.complete(
                intent.intent_id,
                reply_sent=reply_sent,
                reply_preview=reply_preview,
                synthetic_event_queued=decision.synthetic_event_queued,
            )

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
            "group_id": str(intent.metadata.get("group_id", intent.chat_id) or ""),
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
            },
        }
        async def _inject_event():
            # ponytail: per-chat flag instead of global setattr to avoid blocking concurrent messages
            chat_id = intent.chat_id
            self.attention_gate._proactive_dispatching[chat_id] = True
            try:
                return await self.attention_gate.inject_external_event(intent.chat_id, event_data)
            finally:
                self.attention_gate._proactive_dispatching.pop(chat_id, None)
                if hasattr(self.attention_gate, "drain_deferred_messages"):
                    await self.attention_gate.drain_deferred_messages(chat_id)

        if not hasattr(self.attention_gate, "_proactive_dispatching"):
            self.attention_gate._proactive_dispatching = {}
        lock_getter = getattr(self.attention_gate, "get_proactive_lock", None)
        if callable(lock_getter):
            injection_lock = lock_getter(intent.chat_id)
            async with injection_lock:
                result = await _inject_event()
        else:
            result = await _inject_event()
        decision.synthetic_event_queued = bool(result)
        if not decision.reply_sent:
            decision.status = "queued" if decision.synthetic_event_queued else "skipped"
        await self._sync_history_for_dispatch(intent.intent_id, decision)
        return decision


__all__ = [
    "ProactiveDispatchDecision",
    "ProactiveDispatcher",
    "ProactiveMessageIntent",
]
