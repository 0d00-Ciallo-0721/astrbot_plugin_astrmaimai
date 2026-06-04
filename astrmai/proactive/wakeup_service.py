from __future__ import annotations

import inspect
import time

from astrbot.api import logger

from ..infrastructure.context_economy import PromptTemplateId
from ..memory.contracts.memory_query import MemoryQuery
from .dispatcher import ProactiveMessageIntent
from .rhythm import evaluate_proactive_rhythm


class WakeupService:
    def __init__(self, context, state_engine, persistence, call_background_lane, config, dispatcher=None, memory_engine=None, prompt_registry=None):
        self.context = context
        self.state_engine = state_engine
        self.persistence = persistence
        self._call_background_lane = call_background_lane
        self.config = config
        self.dispatcher = dispatcher
        self.memory_engine = memory_engine
        self.prompt_registry = prompt_registry

    async def build_signal(self, chat_id: str, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        state = None
        if self.state_engine is not None and hasattr(self.state_engine, "get_state"):
            try:
                state = self.state_engine.get_state(chat_id)
                if inspect.isawaitable(state):
                    state = await state
            except Exception as exc:
                logger.debug(f"[Life] proactive wakeup state lookup degraded for {chat_id}: {exc}")
        if state is None and self.state_engine is not None and hasattr(self.state_engine, "get_active_states"):
            try:
                for candidate in self.state_engine.get_active_states() or []:
                    if str(getattr(candidate, "chat_id", "") or "") == str(chat_id or ""):
                        state = candidate
                        break
            except Exception as exc:
                logger.debug(f"[Life] proactive wakeup active state scan degraded for {chat_id}: {exc}")
        if state is None:
            return {"eligible": False, "reason": "state_unavailable", "chat_id": str(chat_id or "")}
        if hasattr(state, "lock") and getattr(state, "lock").locked():
            return {"eligible": False, "reason": "state_locked", "chat_id": str(chat_id or ""), "state": state}
        if not getattr(state, "chat_id", None):
            return {"eligible": False, "reason": "missing_chat_id", "chat_id": str(chat_id or ""), "state": state}

        silence_threshold = self.config.life.silence_threshold
        energy_threshold = self.config.life.wakeup_min_energy
        wakeup_cost = self.config.life.wakeup_cost
        wakeup_cooldown = self.config.life.wakeup_cooldown
        next_wakeup_timestamp = float(getattr(state, "next_wakeup_timestamp", 0.0) or 0.0)
        minutes_silent = 999.0
        if getattr(state, "last_reply_time", 0) > 0:
            minutes_silent = (now - float(getattr(state, "last_reply_time", 0) or 0.0)) / 60.0
        if minutes_silent == 999.0:
            return {
                "eligible": False,
                "candidate_present": False,
                "reason": "never_replied",
                "chat_id": str(chat_id or ""),
                "state": state,
                "next_wakeup_timestamp": next_wakeup_timestamp,
            }
        if minutes_silent <= silence_threshold:
            return {
                "eligible": False,
                "candidate_present": False,
                "reason": "silence_threshold_not_reached",
                "chat_id": str(chat_id or ""),
                "state": state,
                "minutes_silent": minutes_silent,
                "next_wakeup_timestamp": next_wakeup_timestamp,
            }
        if float(getattr(state, "energy", 0.0) or 0.0) <= float(energy_threshold or 0.0):
            return {
                "eligible": False,
                "candidate_present": False,
                "reason": "energy_too_low",
                "chat_id": str(chat_id or ""),
                "state": state,
                "minutes_silent": minutes_silent,
                "energy": float(getattr(state, "energy", 0.0) or 0.0),
                "next_wakeup_timestamp": next_wakeup_timestamp,
            }
        if now < next_wakeup_timestamp:
            return {
                "eligible": False,
                "candidate_present": True,
                "reason": "cooldown",
                "chat_id": str(chat_id or ""),
                "state": state,
                "minutes_silent": minutes_silent,
                "energy": float(getattr(state, "energy", 0.0) or 0.0),
                "wakeup_cost": float(wakeup_cost or 0.0),
                "wakeup_cooldown": float(wakeup_cooldown or 0.0),
                "next_wakeup_timestamp": next_wakeup_timestamp,
            }
        return {
            "eligible": True,
            "candidate_present": True,
            "reason": "silence_threshold_reached",
            "chat_id": str(chat_id or ""),
            "state": state,
            "minutes_silent": minutes_silent,
            "energy": float(getattr(state, "energy", 0.0) or 0.0),
            "wakeup_cost": float(wakeup_cost or 0.0),
            "wakeup_cooldown": float(wakeup_cooldown or 0.0),
            "next_wakeup_timestamp": next_wakeup_timestamp,
        }

    async def run_for_chat(self, chat_id: str, signal: dict | None = None, now: float | None = None) -> dict:
        now = time.time() if now is None else float(now)
        signal = dict(signal or await self.build_signal(chat_id, now=now))
        if not signal.get("eligible", False):
            return {
                "chat_id": str(chat_id or ""),
                "performed": False,
                "allowed": False,
                "reason": str(signal.get("reason", "") or "ineligible"),
            }
        state = signal.get("state")
        wakeup_cost = float(signal.get("wakeup_cost", getattr(self.config.life, "wakeup_cost", 0.0)) or 0.0)
        wakeup_cooldown = float(signal.get("wakeup_cooldown", getattr(self.config.life, "wakeup_cooldown", 0.0)) or 0.0)
        intent = await self.build_wakeup_intent(state, wakeup_cost, wakeup_cooldown)
        if not intent:
            return {"chat_id": str(chat_id or ""), "performed": False, "allowed": False, "reason": "intent_unavailable"}
        if not self.dispatcher:
            logger.debug("[Life] proactive wakeup skipped: dispatcher unavailable")
            return {"chat_id": str(chat_id or ""), "performed": False, "allowed": False, "reason": "dispatcher_unavailable"}

        async def _on_complete(reply_sent: bool, reply_preview: str, *, target_state=state) -> None:
            if not reply_sent:
                logger.info(f"[Life] proactive wakeup skipped by planner: {getattr(target_state, 'chat_id', '')}")
                return
            try:
                await self.state_engine.consume_energy(target_state.chat_id, amount=wakeup_cost)
            except TypeError:
                await self.state_engine.consume_energy(target_state.chat_id)
            target_state.next_wakeup_timestamp = time.time() + wakeup_cooldown
            target_state.is_dirty = True
            await self.persistence.save_chat_state(target_state)
            logger.info(f"[Life] proactive wakeup sent via main chain: {str(reply_preview or '')[:80]}")

        try:
            decision = await self.dispatcher.dispatch(intent, on_complete=_on_complete)
            if not decision.allowed:
                logger.debug(f"[Life] proactive wakeup blocked: {decision.blocked_reason}")
            return {
                "chat_id": str(chat_id or ""),
                "performed": bool(decision.allowed),
                "allowed": bool(decision.allowed),
                "reason": str(decision.blocked_reason or signal.get("reason", "")),
                "intent_id": str(getattr(decision, "intent_id", "") or ""),
            }
        except Exception as exc:
            logger.error(f"[Life] proactive wakeup dispatch failed: {exc}")
            return {"chat_id": str(chat_id or ""), "performed": False, "allowed": False, "reason": "dispatch_failed"}

    async def run_once(self):
        active_states = self.state_engine.get_active_states()
        now = time.time()
        for state in active_states:
            chat_id = str(getattr(state, "chat_id", "") or "")
            if not chat_id:
                continue
            signal = await self.build_signal(chat_id, now=now)
            if signal.get("state") is None:
                signal["state"] = state
            await self.run_for_chat(chat_id, signal=signal, now=now)

    async def build_wakeup_intent(self, state, wakeup_cost: float, wakeup_cooldown: float) -> ProactiveMessageIntent | None:
        chat_id = str(getattr(state, "chat_id", "") or "")
        if not chat_id:
            return None
        guidance = await self.generate_opening_line(chat_id)
        if not guidance:
            return None
        return ProactiveMessageIntent(
            chat_id=chat_id,
            source="wakeup",
            reason="silence_threshold_reached",
            guidance=guidance,
            suggested_social_intent="join",
            suggested_action_tier="chat",
            urgency=0.58,
            cost=float(wakeup_cost or 0.0),
            cooldown=float(wakeup_cooldown or 0.0),
            metadata={"group_id": chat_id},
        )

    async def generate_opening_line(self, chat_id: str) -> str:
        rhythm = evaluate_proactive_rhythm(self.config)
        persona_id = getattr(self.config.persona, "persona_id", "") or "global"
        cache = self.persistence.load_persona_cache()
        persona_data = cache.get(persona_id, {})
        summary = persona_data.get("summary", "")
        style = persona_data.get("style", "")
        memory_hint = await self._recall_light_memory(chat_id, "recent quiet chat")
        parts = [
            f"Time tone: {rhythm.time_bucket}.",
            "Consider one short natural line only if it would feel welcome.",
            "Make it easy to ignore; no @ mentions, no presence-check questions, no repeated questions.",
            "Prefer a soft continuation or a tiny everyday observation over a new heavy topic.",
            "Do not explain why you spoke.",
        ]
        if rhythm.time_bucket == "evening":
            parts.append("Evening tone: quieter, lower-pressure, and brief.")
        if summary:
            parts.append(f"Persona tone reference: {str(summary)[:180]}")
        if style:
            parts.append(f"Style hint: {str(style)[:120]}")
        if memory_hint:
            parts.append(f"Optional private memory hint, do not quote directly: {memory_hint[:180]}")
        if self.prompt_registry is not None:
            envelope = self.prompt_registry.render_template(
                PromptTemplateId.PROACTIVE_WAKEUP_OPENING,
                {
                    "time_bucket": rhythm.time_bucket,
                    "persona_summary": summary,
                    "persona_style": style,
                    "memory_hint": memory_hint,
                    "guidance_text": "\n".join(parts),
                },
            )
            return envelope.prompt
        return "\n".join(parts)

    async def _recall_light_memory(self, chat_id: str, query: str) -> str:
        if not self.memory_engine:
            return ""
        retrieval = getattr(self.memory_engine, "retrieval_service", None)
        if retrieval and hasattr(retrieval, "retrieve"):
            try:
                memory_query = MemoryQuery(query=str(query or ""), session_id=str(chat_id or ""), top_k=1)
                candidates = await retrieval.retrieve(memory_query)
                if hasattr(retrieval, "render_recall"):
                    return " ".join(str(retrieval.render_recall(memory_query, candidates) or "").split())
                return " ".join(str(getattr(candidates[0], "summary", "") or getattr(candidates[0], "content", "")).split()) if candidates else ""
            except Exception as exc:
                logger.debug(f"[Life] proactive wakeup v2 memory hint degraded: {exc}")
        return ""


__all__ = ["WakeupService"]
