from __future__ import annotations

import time

from astrbot.api import logger

from ..memory.contracts.memory_query import MemoryQuery
from .dispatcher import ProactiveMessageIntent
from .rhythm import evaluate_proactive_rhythm


class WakeupService:
    def __init__(self, context, state_engine, persistence, call_background_lane, config, dispatcher=None, memory_engine=None):
        self.context = context
        self.state_engine = state_engine
        self.persistence = persistence
        self._call_background_lane = call_background_lane
        self.config = config
        self.dispatcher = dispatcher
        self.memory_engine = memory_engine

    async def run_once(self):
        active_states = self.state_engine.get_active_states()
        now = time.time()
        silence_threshold = self.config.life.silence_threshold
        energy_threshold = self.config.life.wakeup_min_energy
        wakeup_cost = self.config.life.wakeup_cost
        wakeup_cooldown = self.config.life.wakeup_cooldown

        for state in active_states:
            if hasattr(state, "lock") and state.lock.locked():
                continue
            if not getattr(state, "chat_id", None):
                continue

            minutes_silent = 999
            if getattr(state, "last_reply_time", 0) > 0:
                minutes_silent = (now - state.last_reply_time) / 60

            if not (minutes_silent > silence_threshold and state.energy > energy_threshold and minutes_silent != 999):
                continue
            if now < getattr(state, "next_wakeup_timestamp", 0):
                continue

            intent = await self.build_wakeup_intent(state, wakeup_cost, wakeup_cooldown)
            if not intent:
                continue
            if not self.dispatcher:
                logger.debug("[Life] proactive wakeup skipped: dispatcher unavailable")
                continue

            async def _on_complete(reply_sent: bool, reply_preview: str, *, target_state=state) -> None:
                if not reply_sent:
                    logger.info(f"[Life] proactive wakeup skipped by planner: {getattr(target_state, 'chat_id', '')}")
                    return
                try:
                    await self.state_engine.consume_energy(target_state.chat_id, amount=wakeup_cost)
                except TypeError:
                    await self.state_engine.consume_energy(target_state.chat_id)
                target_state.next_wakeup_timestamp = time.time() + wakeup_cooldown
                logger.info(f"[Life] proactive wakeup sent via main chain: {str(reply_preview or '')[:80]}")

            try:
                decision = await self.dispatcher.dispatch(intent, on_complete=_on_complete)
                if not decision.allowed:
                    logger.debug(f"[Life] proactive wakeup blocked: {decision.blocked_reason}")
            except Exception as exc:
                logger.error(f"[Life] proactive wakeup dispatch failed: {exc}")

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
        try:
            result = await self.memory_engine.recall(query, session_id=chat_id, top_k=1)
        except TypeError:
            result = await self.memory_engine.recall(query, session_id=chat_id)
        except Exception as exc:
            logger.debug(f"[Life] proactive wakeup memory hint degraded: {exc}")
            return ""
        return " ".join(str(result or "").split())


__all__ = ["WakeupService"]
