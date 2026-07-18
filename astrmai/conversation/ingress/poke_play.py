from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PokePlayState:
    count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0


@dataclass(slots=True)
class PokePlayContext:
    intent: str
    intensity: str
    streak_count: int
    relationship_level: str
    should_counter_poke: bool
    should_force_meme: bool
    meme_tag: str
    cooldown_seconds: float
    narrative: str
    reply_hint: str
    social_signal: str
    group_focus_hint: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PokePlaybook:
    """Short-lived poke semantics for lightweight social play."""

    def __init__(self, *, window_seconds: float = 90.0, cooldown_seconds: float = 18.0):
        self.window_seconds = float(window_seconds or 90.0)
        self.cooldown_seconds = float(cooldown_seconds or 18.0)
        self._states: dict[str, PokePlayState] = {}

    def build(
        self,
        *,
        chat_id: str,
        sender_id: str,
        sender_name: str,
        target_id: str,
        target_name: str,
        target_is_bot: bool,
        social_score: float = 0.0,
        group_id: str = "",
        now: float | None = None,
    ) -> PokePlayContext:
        current = float(now or time.time())
        key = f"{chat_id or group_id or 'global'}:{sender_id}:{target_id}"
        state = self._states.get(key)
        if state is None or current - state.last_seen > self.window_seconds:
            state = PokePlayState(count=0, first_seen=current)
        state.count += 1
        state.last_seen = current
        self._states[key] = state
        self._prune(current)

        relationship_level = self._relationship_level(social_score)
        intensity = self._intensity(state.count)
        intent = self._intent(target_is_bot=target_is_bot, count=state.count, social_score=social_score)
        cooldown_seconds = self.cooldown_seconds if state.count >= 3 else 0.0
        should_counter_poke = target_is_bot and state.count <= 2 and social_score >= -20.0
        should_force_meme = target_is_bot and state.count in {2, 4}
        meme_tag = "happy" if social_score >= 0 else "curious"
        narrative = self._narrative(
            sender_name=sender_name,
            target_name=target_name,
            target_is_bot=target_is_bot,
            intent=intent,
            count=state.count,
        )
        reply_hint = self._reply_hint(
            target_is_bot=target_is_bot,
            intent=intent,
            count=state.count,
            relationship_level=relationship_level,
            cooldown_seconds=cooldown_seconds,
        )
        social_signal = "attention_request" if target_is_bot else "peer_play"
        group_focus_hint = ""
        if group_id and not target_is_bot:
            group_focus_hint = f"{sender_name} 正在和 {target_name} 进行群友之间的戳一戳互动，机器人只是旁观者。"

        return PokePlayContext(
            intent=intent,
            intensity=intensity,
            streak_count=state.count,
            relationship_level=relationship_level,
            should_counter_poke=should_counter_poke,
            should_force_meme=should_force_meme,
            meme_tag=meme_tag,
            cooldown_seconds=cooldown_seconds,
            narrative=narrative,
            reply_hint=reply_hint,
            social_signal=social_signal,
            group_focus_hint=group_focus_hint,
            metadata={
                "window_seconds": self.window_seconds,
                "key": key,
                "first_seen": state.first_seen,
                "last_seen": state.last_seen,
            },
        )

    def _prune(self, now: float) -> None:
        cutoff = now - max(self.window_seconds * 3, 300.0)
        stale = [key for key, state in self._states.items() if state.last_seen < cutoff]
        for key in stale:
            self._states.pop(key, None)

    @staticmethod
    def _relationship_level(score: float) -> str:
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        if score >= 50:
            return "close"
        if score >= 15:
            return "friendly"
        if score <= -20:
            return "distant"
        return "neutral"

    @staticmethod
    def _intensity(count: int) -> str:
        if count >= 4:
            return "poke_battle"
        if count >= 3:
            return "repeated"
        if count == 2:
            return "playful_repeat"
        return "single"

    @staticmethod
    def _intent(*, target_is_bot: bool, count: int, social_score: float) -> str:
        if not target_is_bot:
            return "peer_nudge"
        if count >= 4:
            return "poke_battle"
        if count >= 3:
            return "attention_spam"
        if social_score >= 35:
            return "affectionate_ping"
        if social_score <= -20:
            return "boundary_probe"
        return "attention_ping"

    @staticmethod
    def _narrative(
        *,
        sender_name: str,
        target_name: str,
        target_is_bot: bool,
        intent: str,
        count: int,
    ) -> str:
        if target_is_bot:
            if count >= 4:
                return f"{sender_name} 又戳了你一下，像是在把轻互动升级成戳戳大战。"
            if count >= 3:
                return f"{sender_name} 连续戳你，像是在催你注意到 ta。"
            if intent == "affectionate_ping":
                return f"{sender_name} 轻轻戳了你一下，像是在亲近地打招呼。"
            if intent == "boundary_probe":
                return f"{sender_name} 戳了你一下，语境更像试探边界。"
            return f"{sender_name} 戳了你一下，可能是在打招呼或求关注。"
        if count >= 3:
            return f"{sender_name} 连续戳了 {target_name}，当前群里出现一个轻微起哄焦点。"
        return f"{sender_name} 戳了 {target_name} 一下，这是群友之间的轻互动。"

    @staticmethod
    def _reply_hint(
        *,
        target_is_bot: bool,
        intent: str,
        count: int,
        relationship_level: str,
        cooldown_seconds: float,
    ) -> str:
        if not target_is_bot:
            return "这是群友之间的戳一戳，不是你被戳。默认可以忽略；若语境自然，只能以旁观者轻轻围观或选择戳回发起者/被戳者，禁止说成自己被戳。"
        if cooldown_seconds > 0:
            return "用一两句短回复表达被连续戳到的反应，可以调皮提醒别一直戳。"
        if relationship_level in {"close", "friendly"}:
            return "用亲近、调皮的一两句回应，可以像被叫到一样接住互动。"
        if intent == "boundary_probe":
            return "用克制的一两句回应，保持边界感，不要过分亲昵。"
        return "用一两句短回复接住戳一戳，不要展开大道理，也不要强行接旧话题。"


__all__ = ["PokePlayContext", "PokePlayState", "PokePlaybook"]
