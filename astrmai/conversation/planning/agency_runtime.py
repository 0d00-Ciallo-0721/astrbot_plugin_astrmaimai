from __future__ import annotations

from time import monotonic
from dataclasses import dataclass, field
from typing import Iterable


@dataclass(slots=True)
class AgencyReflection:
    timestamp: float
    chat_id: str
    reply_need: str
    social_intent: str
    action_tier: str
    action_taken: str
    reply_preview: str
    note: str
    actor_id: str = ""
    cooldown_tags: list[str] = field(default_factory=list)


class AgencyRuntimeStore:
    """Short-lived, in-memory self-continuity notes for planner agency."""

    MAX_REFLECTIONS_PER_CHAT = 12
    REFLECTION_TTL_SECONDS = 30 * 60
    SUMMARY_LIMIT = 3

    def __init__(self):
        self._by_scope: dict[tuple[str, str], list[AgencyReflection]] = {}

    @staticmethod
    def _scope_key(chat_id: str, actor_id: str = "") -> tuple[str, str]:
        return str(chat_id or ""), str(actor_id or "")

    def _recent_for_scope(
        self,
        chat_id: str,
        actor_id: str,
        *,
        now: float,
    ) -> list[AgencyReflection]:
        key = self._scope_key(chat_id, actor_id)
        current = self._by_scope.get(key, [])
        kept = [
            item
            for item in current
            if now - float(item.timestamp or 0.0) <= self.REFLECTION_TTL_SECONDS
        ][-self.MAX_REFLECTIONS_PER_CHAT :]
        if kept != current:
            if kept:
                self._by_scope[key] = kept
            else:
                self._by_scope.pop(key, None)
        return kept

    def recent(
        self,
        chat_id: str,
        *,
        actor_id: str | None = None,
        now: float | None = None,
    ) -> list[AgencyReflection]:
        now = monotonic() if now is None else now
        if actor_id is not None:
            return self._recent_for_scope(chat_id, actor_id, now=now)

        combined: list[AgencyReflection] = []
        for scope_chat_id, scope_actor_id in list(self._by_scope):
            if scope_chat_id != str(chat_id or ""):
                continue
            combined.extend(
                self._recent_for_scope(
                    scope_chat_id,
                    scope_actor_id,
                    now=now,
                )
            )
        return sorted(combined, key=lambda item: item.timestamp)

    def cooldown_tags(self, chat_id: str, *, within_seconds: float = 10 * 60) -> set[str]:
        now = monotonic()
        tags: set[str] = set()
        for item in self.recent(chat_id, now=now):
            if now - item.timestamp <= within_seconds:
                tags.update(str(tag) for tag in item.cooldown_tags if str(tag).strip())
        return tags

    def summary(self, chat_id: str, *, actor_id: str | None = None) -> str:
        recent_items = self.recent(chat_id, actor_id=actor_id)[-self.SUMMARY_LIMIT :]
        if not recent_items:
            return ""
        lines: list[str] = []
        for item in recent_items:
            tags = "、".join(item.cooldown_tags) if item.cooldown_tags else "无"
            lines.append(
                f"- 上轮姿态={item.social_intent or item.reply_need}，动作层={item.action_tier}，"
                f"结果={item.action_taken}，冷却={tags}"
            )
        return "最近我的短期行动残留：\n" + "\n".join(lines)

    def record(
        self,
        *,
        chat_id: str,
        reply_need: str,
        social_intent: str,
        action_tier: str,
        action_taken: str,
        reply_preview: str,
        note: str = "",
        actor_id: str = "",
        cooldown_tags: Iterable[str] = (),
    ) -> AgencyReflection:
        item = AgencyReflection(
            timestamp=monotonic(),
            chat_id=chat_id,
            reply_need=str(reply_need or ""),
            social_intent=str(social_intent or ""),
            action_tier=str(action_tier or ""),
            action_taken=str(action_taken or ""),
            reply_preview=str(reply_preview or "")[:160],
            note=str(note or "")[:160],
            actor_id=str(actor_id or ""),
            cooldown_tags=[str(tag) for tag in cooldown_tags if str(tag).strip()],
        )
        key = self._scope_key(chat_id, actor_id)
        items = [
            *self.recent(chat_id, actor_id=str(actor_id or "")),
            item,
        ][-self.MAX_REFLECTIONS_PER_CHAT :]
        self._by_scope[key] = items
        return item


__all__ = ["AgencyReflection", "AgencyRuntimeStore"]
