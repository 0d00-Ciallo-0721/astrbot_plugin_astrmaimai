from __future__ import annotations

import time
from time import monotonic
from collections import Counter
from typing import Any

from astrbot.api import logger

from .models import HeartflowPulse


class HeartflowFeedbackBridge:
    MIN_NEW_PULSES = 6
    PERIODIC_MIN_PULSES = 3
    PERIODIC_FLUSH_SECONDS = 20 * 60

    def __init__(self, memory_engine: Any):
        self.memory_engine = memory_engine
        self._last_flush_ts: dict[str, float] = {}
        self._last_pulse_ts: dict[str, float] = {}

    @property
    def last_feedback_time(self) -> float:
        if not self._last_flush_ts:
            return 0.0
        return max(self._last_flush_ts.values())

    def _pending_pulses(self, pulses_by_chat: dict[str, list[HeartflowPulse]], chat_id: str) -> list[HeartflowPulse]:
        last_ts = float(self._last_pulse_ts.get(chat_id, 0.0) or 0.0)
        return [pulse for pulse in pulses_by_chat.get(chat_id, []) if float(pulse.timestamp or 0.0) > last_ts]

    def should_flush(
        self,
        pulses_by_chat: dict[str, list[HeartflowPulse]],
        chat_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        now = monotonic() if now is None else now
        pending = self._pending_pulses(pulses_by_chat, chat_id)
        if len(pending) >= self.MIN_NEW_PULSES:
            return True
        last_flush = float(self._last_flush_ts.get(chat_id, 0.0) or 0.0)
        return bool(
            last_flush
            and len(pending) >= self.PERIODIC_MIN_PULSES
            and now - last_flush >= self.PERIODIC_FLUSH_SECONDS
        )

    @staticmethod
    def build_feedback(pulses: list[HeartflowPulse]) -> tuple[str, str, list[str]]:
        if not pulses:
            return "", "", []
        # ponytail: computed for future use — currently informational only.
        avg_interest = sum(float(getattr(item, "urgency", 0.0) or 0.0) for item in pulses) / len(pulses)
        impulses = Counter(str(item.suggested_social_intent or item.pulse_type or "observe") for item in pulses)
        tiers = Counter(str(item.suggested_action_tier or "none") for item in pulses)
        tag_counter: Counter[str] = Counter()
        for pulse in pulses:
            tag_counter.update(str(tag) for tag in getattr(pulse, "tags", []) if str(tag).strip())

        main_impulse = impulses.most_common(1)[0][0] if impulses else "observe"
        main_tier = tiers.most_common(1)[0][0] if tiers else "none"
        tags = sorted(tag_counter.keys())
        summary = (
            f"Recent heartflow pattern: {len(pulses)} pulses, "
            f"avg_urgency={avg_interest:.2f}, main_impulse={main_impulse}, main_tier={main_tier}."
        )
        if tags:
            summary += " Tags observed: " + ", ".join(tags[:8]) + "."

        guidance_parts: list[str] = []
        if tag_counter.get("low_talk", 0) >= 2 or main_impulse == "observe":
            guidance_parts.append("Prefer observing unless the user directly invites a response.")
        if tag_counter.get("high_interest", 0) >= 2:
            guidance_parts.append("Join only when the current clue is directly relevant.")
        if tag_counter.get("fatigue", 0):
            guidance_parts.append("Avoid full action chains while the chat state looks fatigued.")
        if tag_counter.get("proactive_hint", 0):
            guidance_parts.append("Do not force old topics; any proactive move must feel current.")
        if not guidance_parts:
            guidance_parts.append("Keep the next response aligned with this rhythm without repeating it.")
        return summary, " ".join(guidance_parts), tags

    async def maybe_flush(self, pulses_by_chat: dict[str, list[HeartflowPulse]], chat_id: str) -> bool:
        if not self.memory_engine or not hasattr(self.memory_engine, "record_cognitive_feedback"):
            return False
        if not self.should_flush(pulses_by_chat, chat_id):
            return False
        pending = self._pending_pulses(pulses_by_chat, chat_id)
        summary, guidance, tags = self.build_feedback(pending)
        if not (summary or guidance):
            return False
        try:
            await self.memory_engine.record_cognitive_feedback(
                session_id=chat_id,
                source="heartflow",
                summary=summary,
                guidance=guidance,
                tags=tags,
                importance=0.5,
            )
        except Exception as exc:
            logger.debug(f"[HeartflowFeedbackBridge] feedback write degraded: {exc}")
            return False
        self._last_flush_ts[chat_id] = time.time()
        self._last_pulse_ts[chat_id] = max(float(item.timestamp or 0.0) for item in pending)
        return True


__all__ = ["HeartflowFeedbackBridge"]
