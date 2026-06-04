from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable
import re


@dataclass(slots=True)
class ConversationTurnRecord:
    timestamp: float
    chat_id: str
    focus_preview: str
    goal_summary: str
    social_intent: str
    action_tier: str
    action_taken: str
    reply_preview: str
    reply_need: str = "reply"
    goal_status: str = ""


@dataclass(slots=True)
class ConversationContinuityState:
    chat_id: str
    current_topic: str = ""
    current_goal: str = ""
    goal_status: str = ""
    topic_started_at: float = 0.0
    last_goal_update_ts: float = 0.0
    turn_count: int = 0
    last_focus_preview: str = ""
    last_social_intent: str = ""
    last_action_taken: str = ""
    continuity_weight: str = ""
    turns: list[ConversationTurnRecord] = field(default_factory=list)


class ConversationContinuityStore:
    MAX_TURNS_PER_CHAT = 12
    TURN_TTL_SECONDS = 30 * 60
    SOFT_DECAY_SECONDS = 10 * 60
    TOPIC_SIMILARITY_THRESHOLD = 0.28
    WEAK_TOPIC_SIMILARITY_THRESHOLD = 0.45

    def __init__(self):
        self._states: dict[str, ConversationContinuityState] = {}

    def _state(self, chat_id: str) -> ConversationContinuityState:
        state = self._states.get(chat_id)
        if state is None:
            state = ConversationContinuityState(chat_id=chat_id)
            self._states[chat_id] = state
        return state

    @staticmethod
    def _normalize_topic_text(text: str) -> str:
        value = str(text or "").strip()
        if ":" in value:
            value = value.split(":", 1)[1]
        if "：" in value:
            value = value.split("：", 1)[1]
        return "".join(value.lower().split())

    @staticmethod
    def _ascii_word_tokens(text: str) -> set[str]:
        value = str(text or "").strip()
        if ":" in value:
            value = value.split(":", 1)[1]
        if "：" in value:
            value = value.split("：", 1)[1]
        return {token.lower() for token in re.findall(r"[A-Za-z0-9_]{3,}", value)}

    @classmethod
    def _topic_similarity(cls, left: str, right: str) -> float:
        left_tokens = cls._ascii_word_tokens(left)
        right_tokens = cls._ascii_word_tokens(right)
        if len(left_tokens) >= 2 and len(right_tokens) >= 2:
            return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        left_chars = set(cls._normalize_topic_text(left))
        right_chars = set(cls._normalize_topic_text(right))
        if not left_chars or not right_chars:
            return 0.0
        return len(left_chars & right_chars) / len(left_chars | right_chars)

    @classmethod
    def _is_same_topic(cls, left: str, right: str, *, threshold: float | None = None) -> bool:
        left_norm = cls._normalize_topic_text(left)
        right_norm = cls._normalize_topic_text(right)
        if not left_norm or not right_norm:
            return False
        shorter, longer = sorted((left_norm, right_norm), key=len)
        if len(shorter) >= 6 and shorter in longer:
            return True
        threshold = cls.TOPIC_SIMILARITY_THRESHOLD if threshold is None else threshold
        return cls._topic_similarity(left, right) >= threshold

    def _continuity_weight(self, state: ConversationContinuityState, now: float) -> str:
        last_update = float(state.last_goal_update_ts or 0.0)
        if not last_update:
            return ""
        if now - last_update > self.TURN_TTL_SECONDS:
            return ""
        return "weak" if now - last_update > self.SOFT_DECAY_SECONDS else "strong"

    def _expire_state_if_stale(self, state: ConversationContinuityState, now: float) -> bool:
        last_update = float(state.last_goal_update_ts or 0.0)
        if not last_update or now - last_update <= self.TURN_TTL_SECONDS:
            return False
        state.current_topic = ""
        state.current_goal = ""
        state.goal_status = ""
        state.topic_started_at = 0.0
        state.last_goal_update_ts = 0.0
        state.turn_count = 0
        state.last_focus_preview = ""
        state.last_social_intent = ""
        state.last_action_taken = ""
        state.continuity_weight = ""
        state.turns = []
        return True

    def recent(self, chat_id: str, *, now: float | None = None) -> list[ConversationTurnRecord]:
        now = time.time() if now is None else now
        state = self._state(chat_id)
        self._expire_state_if_stale(state, now)
        kept = [
            item
            for item in state.turns
            if now - float(item.timestamp or 0.0) <= self.TURN_TTL_SECONDS
        ]
        if kept != state.turns:
            state.turns = kept[-self.MAX_TURNS_PER_CHAT :]
        return state.turns

    def snapshot(self, chat_id: str, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        state = self._state(chat_id)
        self.recent(chat_id, now=now)
        return {
            "current_topic": state.current_topic,
            "current_goal": state.current_goal,
            "goal_status": state.goal_status,
            "continuity_weight": self._continuity_weight(state, now),
            "topic_started_at": float(state.topic_started_at or 0.0),
            "last_goal_update_ts": float(state.last_goal_update_ts or 0.0),
            "turn_count": int(state.turn_count or 0),
            "last_social_intent": state.last_social_intent,
            "last_action_taken": state.last_action_taken,
        }

    def summary(self, chat_id: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        state = self._state(chat_id)
        recent = self.recent(chat_id, now=now)[-3:]
        lines: list[str] = []
        if state.current_topic:
            lines.append(f"current_topic={state.current_topic[:120]}")
        if state.current_goal:
            lines.append(f"current_goal={state.current_goal[:160]}")
        if state.goal_status:
            lines.append(f"goal_status={state.goal_status}")
        continuity_weight = self._continuity_weight(state, now)
        if continuity_weight:
            lines.append(f"continuity_weight={continuity_weight}")
            if continuity_weight == "weak":
                lines.append("continuity_hint=weak_reference_only_do_not_force_old_topic")
        if state.turn_count:
            lines.append(f"turn_count={state.turn_count}")
        if state.last_social_intent or state.last_action_taken:
            lines.append(f"last_social_intent={state.last_social_intent or 'unknown'}")
            lines.append(f"last_action_taken={state.last_action_taken or 'unknown'}")
        for item in recent:
            detail = item.reply_preview or item.focus_preview
            if detail:
                lines.append(
                    f"- Recent turn: intent={item.social_intent or 'answer'}, "
                    f"action={item.action_taken or 'none'}, note={detail[:100]}"
                )
        if not lines:
            return ""
        return "Conversation continuity:\n" + "\n".join(lines)

    def record(
        self,
        *,
        chat_id: str,
        focus_preview: str = "",
        goal_summary: str = "",
        social_intent: str = "",
        action_tier: str = "",
        action_taken: str = "",
        reply_preview: str = "",
        reply_need: str = "reply",
        lightweight_event: bool = False,
        now: float | None = None,
    ) -> ConversationTurnRecord:
        now = time.time() if now is None else now
        state = self._state(chat_id)
        self.recent(chat_id, now=now)
        item = ConversationTurnRecord(
            timestamp=now,
            chat_id=chat_id,
            focus_preview=str(focus_preview or "")[:160],
            goal_summary=str(goal_summary or "")[:220],
            social_intent=str(social_intent or ""),
            action_tier=str(action_tier or ""),
            action_taken=str(action_taken or ""),
            reply_preview=str(reply_preview or "")[:160],
            reply_need=str(reply_need or "reply"),
        )
        # 设计意图：lightweight_event 和 wait/ignore 是非实质性轮次，
        # 不应推进对话主题/目标状态机。仅记录轮次，不更新 state.current_topic、
        # state.current_goal、state.goal_status、continuity_weight 等。
        # 此行为是有意设计，非 bug（参见 TECHNICAL-DEBT-INVENTORY D22）。
        if lightweight_event or item.reply_need in {"wait", "ignore"}:
            return item

        previous_focus = state.last_focus_preview or state.current_topic
        has_previous_topic = bool(state.current_topic and state.last_goal_update_ts)
        age_since_update = now - float(state.last_goal_update_ts or 0.0) if state.last_goal_update_ts else 0.0
        weak_continuity = bool(age_since_update > self.SOFT_DECAY_SECONDS)
        previous_closed = state.goal_status in {"redirected", "guarded", "observing"}
        similarity_threshold = (
            self.WEAK_TOPIC_SIMILARITY_THRESHOLD
            if weak_continuity or previous_closed
            else self.TOPIC_SIMILARITY_THRESHOLD
        )
        same_topic = self._is_same_topic(previous_focus, item.focus_preview, threshold=similarity_threshold)

        if item.social_intent == "redirect":
            goal_status = "redirected"
        elif item.social_intent == "boundary":
            goal_status = "guarded"
        elif item.social_intent == "observe":
            goal_status = "observing"
        elif has_previous_topic and same_topic:
            goal_status = "continuing"
        else:
            goal_status = "new"

        if item.focus_preview:
            if goal_status in {"new", "redirected"} or not state.current_topic:
                state.current_topic = item.focus_preview
            elif goal_status in {"guarded", "observing"} and not same_topic:
                state.current_topic = item.focus_preview
        if item.goal_summary:
            state.current_goal = item.goal_summary
        elif goal_status in {"new", "redirected"}:
            state.current_goal = ""
        state.goal_status = goal_status
        if goal_status in {"new", "redirected"} or not state.topic_started_at:
            state.topic_started_at = now
            state.turn_count = 1
        else:
            state.turn_count += 1
        state.last_goal_update_ts = now
        state.last_focus_preview = item.focus_preview or state.last_focus_preview
        state.last_social_intent = item.social_intent
        state.last_action_taken = item.action_taken
        state.continuity_weight = self._continuity_weight(state, now)
        item.goal_status = goal_status
        state.turns = [*self.recent(chat_id, now=now), item][-self.MAX_TURNS_PER_CHAT :]
        return item

    def clear(self, chat_id: str) -> None:
        self._states.pop(chat_id, None)

    def chats(self) -> Iterable[str]:
        return tuple(self._states)


__all__ = [
    "ConversationContinuityState",
    "ConversationContinuityStore",
    "ConversationTurnRecord",
]
