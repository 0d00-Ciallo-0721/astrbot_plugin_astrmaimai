from __future__ import annotations

import time

from astrbot.api import logger

from .dispatcher import ProactiveMessageIntent


class WakeupService:
    def __init__(self, context, state_engine, persistence, call_background_lane, config, dispatcher=None):
        self.context = context
        self.state_engine = state_engine
        self.persistence = persistence
        self._call_background_lane = call_background_lane
        self.config = config
        self.dispatcher = dispatcher

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
        persona_id = getattr(self.config.persona, "persona_id", "") or "global"
        cache = self.persistence.load_persona_cache()
        persona_data = cache.get(persona_id, {})
        summary = persona_data.get("summary", "")
        style = persona_data.get("style", "")
        parts = [
            "The chat has been quiet for a while. Consider one short, natural opening only if it feels welcome.",
            "If it feels awkward or unrelated, staying quiet is acceptable.",
            "Do not mention systems, silence thresholds, schedules, or proactive logic.",
        ]
        if summary:
            parts.append(f"Persona tone reference: {str(summary)[:180]}")
        if style:
            parts.append(f"Style hint: {str(style)[:120]}")
        return "\n".join(parts)


__all__ = ["WakeupService"]
