from __future__ import annotations

import time
from collections import Counter
from typing import Any

from astrbot.api import logger

from .agency_runtime import AgencyReflection, AgencyRuntimeStore


class AgencyReflectionBridge:
    """Promotes short-lived agency reflections into long-term cognitive feedback."""

    MIN_NEW_REFLECTIONS = 6
    PERIODIC_MIN_REFLECTIONS = 3
    PERIODIC_FLUSH_SECONDS = 20 * 60

    def __init__(self, memory_engine: Any):
        self.memory_engine = memory_engine
        self._last_flush_ts: dict[str, float] = {}
        self._last_reflection_ts: dict[str, float] = {}

    def _pending_items(self, runtime: AgencyRuntimeStore, chat_id: str) -> list[AgencyReflection]:
        last_ts = float(self._last_reflection_ts.get(chat_id, 0.0) or 0.0)
        return [item for item in runtime.recent(chat_id) if float(item.timestamp or 0.0) > last_ts]

    def should_flush(self, runtime: AgencyRuntimeStore, chat_id: str, *, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        items = self._pending_items(runtime, chat_id)
        if len(items) >= self.MIN_NEW_REFLECTIONS:
            return True
        last_flush = float(self._last_flush_ts.get(chat_id, 0.0) or 0.0)
        return bool(last_flush and len(items) >= self.PERIODIC_MIN_REFLECTIONS and now - last_flush >= self.PERIODIC_FLUSH_SECONDS)

    @staticmethod
    def _top(counter: Counter[str], default: str = "answer") -> str:
        if not counter:
            return default
        return counter.most_common(1)[0][0]

    @classmethod
    def build_feedback(cls, items: list[AgencyReflection]) -> tuple[str, str, list[str]]:
        if not items:
            return "", "", []
        intents = Counter(str(item.social_intent or item.reply_need or "answer") for item in items)
        tiers = Counter(str(item.action_tier or "none") for item in items)
        actions = Counter(str(item.action_taken or "none") for item in items)
        tag_counter: Counter[str] = Counter()
        for item in items:
            tag_counter.update(str(tag) for tag in item.cooldown_tags if str(tag).strip())

        top_intent = cls._top(intents)
        top_tier = cls._top(tiers, "none")
        top_action = cls._top(actions, "none")
        repeated_tags = sorted(tag for tag, count in tag_counter.items() if count >= 2)
        tags = sorted(tag_counter.keys())

        summary = (
            f"Recent agency pattern: {len(items)} turns, main_intent={top_intent}, "
            f"main_tier={top_tier}, main_action={top_action}."
        )
        if tags:
            summary += " Cooldowns observed: " + ", ".join(tags[:8]) + "."

        guidance_parts: list[str] = []
        if repeated_tags:
            guidance_parts.append("Avoid repeating recently used actions: " + ", ".join(repeated_tags[:6]) + ".")
        if tag_counter.get("long_reply", 0):
            guidance_parts.append("Prefer shorter replies unless the user explicitly asks for detail.")
        if tag_counter.get("sharp_reply", 0):
            guidance_parts.append("Do not continue sharp pushback unless there is another very clear direct attack.")
        if intents.get("wait", 0) + intents.get("ignore", 0) >= 2:
            guidance_parts.append("The chat recently needed restraint; observe before interrupting.")
        if not guidance_parts:
            guidance_parts.append("Keep the next response consistent with the recent agency pattern without repeating it.")

        return summary, " ".join(guidance_parts), tags

    async def maybe_flush(self, runtime: AgencyRuntimeStore, chat_id: str) -> bool:
        if not self.memory_engine or not hasattr(self.memory_engine, "record_cognitive_feedback"):
            return False
        if not self.should_flush(runtime, chat_id):
            return False
        items = self._pending_items(runtime, chat_id)
        summary, guidance, tags = self.build_feedback(items)
        if not (summary or guidance):
            return False
        try:
            await self.memory_engine.record_cognitive_feedback(
                session_id=chat_id,
                source="agency",
                summary=summary,
                guidance=guidance,
                tags=tags,
                importance=0.55,
            )
        except Exception as exc:
            logger.debug(f"[AgencyReflectionBridge] feedback write degraded: {exc}")
            return False
        self._last_flush_ts[chat_id] = time.time()
        self._last_reflection_ts[chat_id] = max(float(item.timestamp or 0.0) for item in items)
        return True


__all__ = ["AgencyReflectionBridge"]
