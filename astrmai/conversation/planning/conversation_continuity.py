from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..contracts.dialog_history_policy import DialogHistoryPolicy
from ..contracts.topic_attention_anchor import (
    ANCHOR_MAX_EVENT_IDS,
    ANCHOR_MAX_ID_CHARS,
    ANCHOR_MAX_OPEN_LOOP_CHARS,
    ANCHOR_MAX_PARTICIPANTS,
    ANCHOR_MAX_PROMPT_CHARS,
    ANCHOR_MAX_SERIALIZED_CHARS,
    ANCHOR_MAX_SOURCE_CHARS,
    ANCHOR_MAX_SUBJECT_CHARS,
    ANCHOR_SOURCE_VALUES,
    TopicAttentionAnchor,
)
from ..contracts.topic_bridge import BRIDGE_MAX_EVENT_IDS, BRIDGE_MAX_PROMPT_CHARS, BRIDGE_MAX_TURNS, BridgeDecision


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
    sender_id: str = ""
    source_event_id: str = ""
    assistant_outbound_ids: tuple[str, ...] = field(default_factory=tuple)
    topic_epoch: int = 0


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
    topic_confirmation_requested_at: float = 0.0
    topic_epoch: int = 0
    last_sender_id: str = ""
    last_event_id: str = ""
    topic_participants: set[str] = field(default_factory=set)
    topic_anchor: TopicAttentionAnchor = field(default_factory=TopicAttentionAnchor)
    turns: list[ConversationTurnRecord] = field(default_factory=list)
    bridge_anchor: TopicAttentionAnchor = field(default_factory=TopicAttentionAnchor)
    bridge_turns: list[ConversationTurnRecord] = field(default_factory=list)
    event_id_map: dict[str, str] = field(default_factory=dict)


class ConversationContinuityStore:
    MAX_TURNS_PER_CHAT = 12
    TURN_TTL_SECONDS = 30 * 60
    SOFT_DECAY_SECONDS = 10 * 60
    BRIDGE_TTL_SECONDS = 120
    BRIDGE_SAME_ACTOR_SECONDS = 90
    BRIDGE_MAX_PROMPT_CHARS = BRIDGE_MAX_PROMPT_CHARS
    TOPIC_SIMILARITY_THRESHOLD = 0.28
    WEAK_TOPIC_SIMILARITY_THRESHOLD = 0.45
    INTERNAL_TOPIC_ENVELOPE_RE = re.compile(
        r"\[事件=.*?\|\s*发言人=.*?\|\s*角色=.*?\|\s*类型=.*?\|\s*来源=",
        re.DOTALL,
    )
    NONSEMANTIC_TOPIC_RE = re.compile(r"^[\W_]+$", re.UNICODE)
    ANCHOR_MAX_PARTICIPANTS = ANCHOR_MAX_PARTICIPANTS
    ANCHOR_MAX_SUBJECT_CHARS = ANCHOR_MAX_SUBJECT_CHARS
    ANCHOR_MAX_OPEN_LOOP_CHARS = ANCHOR_MAX_OPEN_LOOP_CHARS
    ANCHOR_MAX_EVENT_IDS = ANCHOR_MAX_EVENT_IDS
    ANCHOR_MAX_PROMPT_CHARS = ANCHOR_MAX_PROMPT_CHARS
    ANCHOR_MAX_ID_CHARS = ANCHOR_MAX_ID_CHARS
    ANCHOR_MAX_SOURCE_CHARS = ANCHOR_MAX_SOURCE_CHARS
    ANCHOR_MAX_SERIALIZED_CHARS = ANCHOR_MAX_SERIALIZED_CHARS

    def __init__(self):
        self._states: dict[str, ConversationContinuityState] = {}
        self._topic_continuity_enabled = True
        self._topic_active_ttl_seconds = 900.0
        self._topic_confirm_after_seconds = 1800.0
        self._topic_confirmation_wait_seconds = 120.0
        self._topic_summary_max_chars = 300
        self._group_shared_history_enabled = True
        self._group_topic_active_ttl_seconds = 1200.0
        self._group_topic_confirm_after_seconds = 1800.0
        self._group_provider_topic_session_enabled = True

    @staticmethod
    def _anchor_event_value(event: Any, name: str, default: Any = "") -> Any:
        if event is None:
            return default
        if isinstance(event, Mapping):
            return event.get(name, default)
        return getattr(event, name, default)

    @classmethod
    def _anchor_append_unique(cls, values: Iterable[Any], value: Any, limit: int) -> tuple[str, ...]:
        result: list[str] = []
        for item in values:
            normalized = str(item or "").strip()
            if normalized and normalized not in result:
                result.append(normalized)
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
        return tuple(result[-max(1, int(limit or 1)) :])

    @classmethod
    def _normalize_evidence_ids(cls, values: Iterable[Any], limit: int) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            item = str(value or "").strip()
            if item and item not in normalized:
                normalized.append(item)
        return tuple(normalized[-max(1, int(limit or 1)) :])

    @classmethod
    def _anchor_is_question(cls, text: str) -> bool:
        value = cls._topic_message_text(text)
        return bool(value and (value.endswith(("?", "？")) or any(marker in value for marker in ("吗", "么", "是否", "怎么", "为什么"))))

    def _anchor_view(self, state: ConversationContinuityState, now: float) -> dict[str, Any]:
        anchor = state.topic_anchor
        if not anchor.subject_preview or not anchor.updated_at:
            return {}
        if (
            now - anchor.updated_at > self.TURN_TTL_SECONDS
            or anchor.updated_at - now > self.TURN_TTL_SECONDS
        ):
            return {}
        payload = anchor.as_dict()
        payload["age_seconds"] = max(0.0, now - anchor.updated_at)
        return payload

    @classmethod
    def _empty_anchor_view(cls) -> dict[str, Any]:
        return TopicAttentionAnchor().as_dict()

    def refresh_config(self, config: Any) -> None:
        """Refresh private-topic timing without changing existing conversation state."""
        private_config = getattr(config, "private_chat", None)
        if private_config is not None:
            self._topic_continuity_enabled = bool(
                getattr(private_config, "topic_continuity_enabled", True)
            )
            self._topic_active_ttl_seconds = max(
                600.0,
                float(getattr(private_config, "topic_active_ttl_sec", 900) or 900),
            )
            self._topic_confirm_after_seconds = max(
                1800.0,
                float(getattr(private_config, "topic_confirm_after_sec", 1800) or 1800),
            )
            self._topic_confirmation_wait_seconds = max(
                30.0,
                float(getattr(private_config, "topic_confirmation_wait_sec", 120) or 120),
            )
            self._topic_summary_max_chars = max(
                80,
                int(getattr(private_config, "topic_summary_max_chars", 300) or 300),
            )
        conversation_config = getattr(config, "conversation", None)
        if conversation_config is not None:
            self._group_shared_history_enabled = bool(
                getattr(conversation_config, "group_shared_history_enabled", True)
            )
            active = max(
                60.0,
                min(
                    86400.0,
                    float(getattr(conversation_config, "group_topic_active_ttl_sec", 1200) or 1200),
                ),
            )
            confirm = max(
                active,
                min(
                    172800.0,
                    float(getattr(conversation_config, "group_topic_confirm_after_sec", 1800) or 1800),
                ),
            )
            self._group_topic_active_ttl_seconds = active
            self._group_topic_confirm_after_seconds = confirm
            self._group_provider_topic_session_enabled = bool(
                getattr(conversation_config, "group_provider_topic_session_enabled", True)
            )

    def _state(self, chat_id: str) -> ConversationContinuityState:
        state = self._states.get(chat_id)
        if state is None:
            state = ConversationContinuityState(chat_id=chat_id)
            self._states[chat_id] = state
        return state

    def register_event_id_mapping(self, chat_id: str, *, canonical_id: str, platform_id: str) -> None:
        """Keep a bounded, explicit mapping between canonical and platform IDs."""
        canonical = str(canonical_id or "").strip()[: self.ANCHOR_MAX_ID_CHARS]
        platform = str(platform_id or "").strip()[: self.ANCHOR_MAX_ID_CHARS]
        if not canonical or not platform or canonical == platform:
            return
        state = self._state(str(chat_id or ""))
        state.event_id_map[canonical] = platform
        state.event_id_map[platform] = canonical
        if len(state.event_id_map) > self.ANCHOR_MAX_EVENT_IDS * 2:
            state.event_id_map = dict(list(state.event_id_map.items())[-self.ANCHOR_MAX_EVENT_IDS * 2 :])

    @staticmethod
    def _normalize_topic_text(text: str) -> str:
        value = str(text or "").strip()
        if ":" in value:
            value = value.split(":", 1)[-1]
        if "：" in value:
            value = value.split("：", 1)[-1]
        return "".join(value.lower().split())

    @staticmethod
    def _ascii_word_tokens(text: str) -> set[str]:
        value = str(text or "").strip()
        if ":" in value:
            value = value.split(":", 1)[-1]
        if "：" in value:
            value = value.split("：", 1)[-1]
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

    @staticmethod
    def _topic_message_text(text: str) -> str:
        return " ".join(str(text or "").strip().split())

    @classmethod
    def _sanitize_topic_preview(cls, text: str) -> str:
        normalized = cls._topic_message_text(text)
        if not normalized or cls.INTERNAL_TOPIC_ENVELOPE_RE.search(normalized):
            return ""
        return normalized

    @classmethod
    def _is_nonsemantic_short_input(cls, text: str) -> bool:
        normalized = cls._topic_message_text(text)
        if not normalized:
            return True
        if normalized in {"[图片]", "[表情包]", "图片", "表情包"}:
            return True
        return bool(cls.NONSEMANTIC_TOPIC_RE.fullmatch(normalized))

    @classmethod
    def _is_short_topic_followup(cls, text: str) -> bool:
        normalized = cls._normalize_topic_text(text)
        if not normalized or cls._is_nonsemantic_short_input(text):
            return False
        if len(normalized) <= 4:
            return True
        return any(
            marker in normalized
            for marker in (
                "然后呢",
                "接着呢",
                "后来呢",
                "后来",
                "那呢",
                "这个呢",
                "那个呢",
                "还记得吗",
                "继续吗",
                "什么意思",
                "怎么说",
                "为什么",
                "咋办",
                "怎么办",
                "why",
                "what about",
                "then",
            )
        )

    @classmethod
    def _has_explicit_topic_reference(cls, text: str) -> bool:
        normalized = cls._normalize_topic_text(text)
        if not normalized or cls._is_nonsemantic_short_input(text):
            return False
        return any(
            marker in normalized
            for marker in (
                "上面",
                "刚才",
                "前面",
                "那个",
                "这件事",
                "继续",
                "接着",
                "然后",
                "后来",
                "还记得",
                "之前",
            )
        )

    @classmethod
    def _is_explicit_topic_switch(cls, text: str) -> bool:
        normalized = cls._normalize_topic_text(text)
        return any(
            marker in normalized
            for marker in (
                "换个话题",
                "换一个话题",
                "先不说这个",
                "不聊这个了",
                "另外问个",
                "对了问个",
                "说点别的",
                "先说别的",
            )
        )

    @classmethod
    def _is_confirmation_yes(cls, text: str) -> bool:
        normalized = cls._normalize_topic_text(text)
        return normalized in {
            "继续",
            "继续聊",
            "继续这个",
            "是",
            "嗯",
            "嗯嗯",
            "对",
            "好",
            "好的",
            "可以",
            "当然",
            "记得",
            "接着说",
        }

    @classmethod
    def _is_confirmation_no(cls, text: str) -> bool:
        normalized = cls._normalize_topic_text(text)
        return normalized in {
            "不用",
            "不要",
            "不继续",
            "换话题",
            "换个话题",
            "不是",
            "算了",
            "先不聊了",
            "不记得",
        }

    def _topic_age_seconds(self, state: ConversationContinuityState, now: float) -> float:
        last_update = float(state.last_goal_update_ts or state.topic_started_at or 0.0)
        if not last_update:
            return 0.0
        return max(0.0, now - last_update)

    @classmethod
    def _is_explicit_history_recall(cls, text: str) -> bool:
        normalized = cls._normalize_topic_text(text)
        return any(
            marker in normalized
            for marker in (
                "昨天",
                "前天",
                "上次",
                "之前",
                "前几天",
                "以前",
                "还记得",
                "我们聊过",
                "刚才那个",
                "之前那个",
            )
        )

    def evaluate_group_message(
        self,
        chat_id: str,
        text: str,
        *,
        sender_id: str = "",
        has_reply_reference: bool = False,
        approved_event_ids: Iterable[str] = (),
        now: float | None = None,
    ) -> DialogHistoryPolicy:
        now = time.time() if now is None else float(now)
        normalized_chat_id = str(chat_id or "").strip()
        normalized_sender_id = str(sender_id or "").strip()
        thread_key = f"group:{normalized_chat_id}"
        if not self._group_shared_history_enabled:
            return DialogHistoryPolicy(
                history_mode="current_topic",
                group_id=normalized_chat_id,
                thread_key=thread_key,
                topic_epoch=0,
                current_sender_id=normalized_sender_id,
                approved_event_ids=tuple(str(item) for item in approved_event_ids if str(item)),
                allow_provider_session=True,
                rotation_reason="group_history_legacy_mode",
                continuity_evidence=("feature_disabled",),
            )

        state = self._state(normalized_chat_id)
        current_text = self._topic_message_text(text)
        explicit_recall = self._is_explicit_history_recall(current_text)
        explicit_switch = self._is_explicit_topic_switch(current_text)
        age_seconds = self._topic_age_seconds(state, now)
        has_topic = bool(state.current_topic and state.last_goal_update_ts)
        same_topic = bool(
            has_topic
            and self._is_same_topic(
                state.last_focus_preview or state.current_topic,
                current_text,
                threshold=(
                    self.TOPIC_SIMILARITY_THRESHOLD
                    if age_seconds <= self._group_topic_active_ttl_seconds
                    else self.WEAK_TOPIC_SIMILARITY_THRESHOLD
                ),
            )
        )
        followup_like = self._is_short_topic_followup(current_text)
        evidence: list[str] = []
        if has_reply_reference:
            evidence.append("reply_reference")
        if same_topic:
            evidence.append("topic_similarity")
        if followup_like:
            evidence.append("short_followup")
        if explicit_recall:
            evidence.append("explicit_recall")
        if normalized_sender_id and normalized_sender_id in state.topic_participants:
            evidence.append("known_participant")

        next_epoch = max(1, int(state.topic_epoch or 0))
        mode = "none"
        rotation_reason = ""
        allow_provider_session = False
        if explicit_recall:
            next_epoch = max(1, int(state.topic_epoch or 0) + 1)
            mode = "explicit_recall"
            rotation_reason = "explicit_history_recall"
        elif not has_topic and followup_like and not explicit_switch:
            next_epoch = max(1, int(state.topic_epoch or 0) + 1)
            mode = "current_topic"
            rotation_reason = "recent_history_bootstrap"
        elif not has_topic or explicit_switch:
            next_epoch = max(1, int(state.topic_epoch or 0) + 1)
            rotation_reason = "initial_topic" if not has_topic else "explicit_topic_switch"
        elif age_seconds <= self._group_topic_active_ttl_seconds:
            mode = "current_topic"
            allow_provider_session = self._group_provider_topic_session_enabled
        elif age_seconds <= self._group_topic_confirm_after_seconds and (
            has_reply_reference or same_topic or followup_like
        ):
            mode = "current_topic"
            allow_provider_session = self._group_provider_topic_session_enabled
            rotation_reason = "weak_window_evidence"
        else:
            next_epoch = max(1, int(state.topic_epoch or 0) + 1)
            rotation_reason = "topic_stale" if age_seconds > self._group_topic_confirm_after_seconds else "new_topic"

        return DialogHistoryPolicy(
            history_mode=mode,
            group_id=normalized_chat_id,
            thread_key=thread_key,
            topic_epoch=next_epoch,
            current_sender_id=normalized_sender_id,
            approved_event_ids=tuple(str(item) for item in approved_event_ids if str(item)),
            allow_provider_session=allow_provider_session,
            rotation_reason=rotation_reason,
            topic_age_seconds=age_seconds,
            continuity_evidence=tuple(evidence),
        )

    def _private_topic_summary(
        self,
        state: ConversationContinuityState,
        *,
        inherited: bool,
        age_seconds: float,
        status: str,
        current_text: str,
    ) -> str:
        topic = self._topic_message_text(state.current_topic)[: self._topic_summary_max_chars]
        if inherited and topic:
            return (
                "私聊话题承接（内部上下文）：\n"
                f"- 当前话题：{topic}\n"
                f"- 话题状态：{status}\n"
                f"- 距离上次实质性交流：约{int(age_seconds)}秒\n"
                "- 当前消息应优先理解为对该话题的继续追问；不要把其他会话或其他人的内容带入。"
            )
        if status == "new":
            return (
                "私聊话题状态（内部上下文）：\n"
                "- 当前消息视为新话题。\n"
                "- 之前的私聊话题只作背景，不要强行套用到当前回答。"
            )
        return ""

    def evaluate_private_message(
        self,
        chat_id: str,
        text: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Classify private-topic continuity before System 1/2 dispatch.

        This is intentionally deterministic and side-effect-light: normal topic
        state is still advanced by Planner after a real reply. The only state
        written here is the short-lived "waiting for confirmation" marker.
        """
        now = time.time() if now is None else float(now)
        normalized_chat_id = str(chat_id or "")
        current_text = self._topic_message_text(text)
        base = {
            "action": "new",
            "status": "new",
            "inherited": False,
            "requires_confirmation": False,
            "age_seconds": 0.0,
            "topic": "",
            "prompt_summary": "",
            "confirmation_text": "",
        }
        if not self._topic_continuity_enabled:
            return base

        state = self._states.get(normalized_chat_id)
        if state is None or not state.current_topic or not state.last_goal_update_ts:
            return base

        confirmation_at = float(state.topic_confirmation_requested_at or 0.0)
        if confirmation_at:
            if now - confirmation_at > self._topic_confirmation_wait_seconds:
                state.topic_confirmation_requested_at = 0.0
            elif self._is_confirmation_yes(current_text):
                state.topic_confirmation_requested_at = 0.0
                age_seconds = self._topic_age_seconds(state, now)
                base.update(
                    action="continue",
                    status="confirmed",
                    inherited=True,
                    age_seconds=age_seconds,
                    topic=state.current_topic,
                )
                base["prompt_summary"] = self._private_topic_summary(
                    state,
                    inherited=True,
                    age_seconds=age_seconds,
                    status="confirmed",
                    current_text=current_text,
                )
                return base
            elif self._is_confirmation_no(current_text) or self._is_explicit_topic_switch(current_text):
                state.topic_confirmation_requested_at = 0.0
                return base
            elif self._is_nonsemantic_short_input(current_text):
                return base
            else:
                base.update(
                    action="confirm",
                    status="awaiting_confirmation",
                    requires_confirmation=True,
                    age_seconds=self._topic_age_seconds(state, now),
                    topic=state.current_topic,
                    confirmation_text=(
                        f"我们刚才在聊“{self._topic_message_text(state.current_topic)[:120]}”，"
                        "还要继续这个话题吗？回复“继续”就好，想换话题直接告诉妃爱～"
                    ),
                )
                return base

        age_seconds = self._topic_age_seconds(state, now)
        explicit_switch = self._is_explicit_topic_switch(current_text)
        same_topic = self._is_same_topic(
            state.last_focus_preview or state.current_topic,
            current_text,
            threshold=self.TOPIC_SIMILARITY_THRESHOLD
            if age_seconds <= self._topic_active_ttl_seconds
            else self.WEAK_TOPIC_SIMILARITY_THRESHOLD,
        )
        followup_like = self._is_short_topic_followup(current_text) or self._has_explicit_topic_reference(
            current_text
        )
        stale_followup_like = same_topic or self._has_explicit_topic_reference(current_text)

        if (
            age_seconds > self._topic_confirm_after_seconds
            and not explicit_switch
            and stale_followup_like
        ):
            state.topic_confirmation_requested_at = now
            return {
                **base,
                "action": "confirm",
                "status": "stale_needs_confirmation",
                "requires_confirmation": True,
                "age_seconds": age_seconds,
                "topic": state.current_topic,
                "confirmation_text": (
                    f"我们之前在聊“{self._topic_message_text(state.current_topic)[:120]}”，"
                    "已经隔了一会儿了，还要接着聊吗？回复“继续”就好～"
                ),
            }

        if not explicit_switch and (same_topic or followup_like):
            status = "active" if age_seconds <= self._topic_active_ttl_seconds else "candidate"
            base.update(
                action="continue",
                status=status,
                inherited=True,
                age_seconds=age_seconds,
                topic=state.current_topic,
            )
            base["prompt_summary"] = self._private_topic_summary(
                state,
                inherited=True,
                age_seconds=age_seconds,
                status=status,
                current_text=current_text,
            )
            return base

        return {
            **base,
            "status": "new",
            "age_seconds": age_seconds,
            "topic": state.current_topic,
            "prompt_summary": self._private_topic_summary(
                state,
                inherited=False,
                age_seconds=age_seconds,
                status="new",
                current_text=current_text,
            ),
        }

    def _expire_state_if_stale(self, state: ConversationContinuityState, now: float) -> bool:
        last_update = float(state.last_goal_update_ts or 0.0)
        if (
            not last_update
            or now - last_update <= self.TURN_TTL_SECONDS
            or state.topic_confirmation_requested_at
        ):
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
        state.topic_confirmation_requested_at = 0.0
        state.last_sender_id = ""
        state.last_event_id = ""
        state.topic_participants.clear()
        state.topic_anchor = TopicAttentionAnchor()
        state.turns = []
        state.bridge_anchor = TopicAttentionAnchor()
        state.bridge_turns = []
        state.event_id_map.clear()
        return True

    def recent(self, chat_id: str, *, now: float | None = None) -> list[ConversationTurnRecord]:
        now = time.time() if now is None else now  # ponytail: M3 — use time.time() to match record()
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
            "topic_epoch": int(state.topic_epoch or 0),
            "last_sender_id": state.last_sender_id,
            "last_event_id": state.last_event_id,
            "topic_participants": sorted(state.topic_participants),
            "topic_anchor": self._anchor_view(state, now) or self._empty_anchor_view(),
        }

    def update_topic_anchor(
        self,
        chat_id: str,
        *,
        event: Any = None,
        subject_preview: str = "",
        reply_text: str = "",
        anchor_event: Any = None,
        event_id: str = "",
        actor_id: str = "",
        topic_epoch: int | None = None,
        open_loop: str = "",
        source: Iterable[str] = (),
        confidence: float = 0.7,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Update one bounded anchor without creating a second history store."""
        now = time.time() if now is None else float(now)
        state = self._state(str(chat_id or ""))
        if not self._anchor_view(state, now):
            state.topic_anchor = TopicAttentionAnchor()
        current = state.topic_anchor
        event = anchor_event or event
        canonical_event_id = self._anchor_event_value(event, "event_id", "")
        event_id = str(
            canonical_event_id
            or event_id
            or self._anchor_event_value(event, "platform_message_id", "")
            or ""
        ).strip()[: self.ANCHOR_MAX_ID_CHARS]
        actor_id = str(
            self._anchor_event_value(event, "actor_id", "") or actor_id or ""
        ).strip()[: self.ANCHOR_MAX_ID_CHARS]
        epoch = max(
            0,
            int(
                topic_epoch
                if topic_epoch is not None
                else self._anchor_event_value(event, "topic_epoch", 0) or state.topic_epoch or 0
            ),
        )
        subject = self._sanitize_topic_preview(subject_preview)
        if not subject:
            subject = self._sanitize_topic_preview(self._anchor_event_value(event, "visible_text", ""))
        subject = subject[: self.ANCHOR_MAX_SUBJECT_CHARS]
        if not subject and not event_id and not actor_id:
            return current.as_dict()
        current_subject = current.subject_preview
        epoch_changed = bool(current.topic_epoch and epoch and current.topic_epoch != epoch)
        changed = epoch_changed or bool(
            current_subject and subject and not self._is_same_topic(current_subject, subject)
        )
        if event_id and event_id in current.recent_event_ids and not changed:
            return current.as_dict()
        if changed:
            if current.subject_preview and current.updated_at and 0 <= now - current.updated_at < self.BRIDGE_TTL_SECONDS:
                state.bridge_anchor = current
                state.bridge_turns = [
                    turn for turn in state.turns
                    if turn.topic_epoch == current.topic_epoch
                    and 0 <= now - turn.timestamp < self.BRIDGE_TTL_SECONDS
                ][-BRIDGE_MAX_TURNS:]
            participant_ids: tuple[str, ...] = ()
            event_ids: tuple[str, ...] = ()
            sources: tuple[str, ...] = ("topic_transition",)
            current_open_loop = ""
        else:
            participant_ids = current.participants
            event_ids = current.recent_event_ids
            sources = current.source
            current_open_loop = current.open_loop
        participant_ids = self._anchor_append_unique(participant_ids, actor_id, self.ANCHOR_MAX_PARTICIPANTS)
        event_ids = self._anchor_append_unique(event_ids, event_id, self.ANCHOR_MAX_EVENT_IDS)
        for evidence_id in (
            self._anchor_event_value(event, "quote_event_id", ""),
            self._anchor_event_value(event, "reply_target_event_id", ""),
            self._anchor_event_value(event, "causal_parent_event_id", ""),
        ):
            event_ids = self._anchor_append_unique(
                event_ids,
                str(evidence_id or "").strip()[: self.ANCHOR_MAX_ID_CHARS],
                self.ANCHOR_MAX_EVENT_IDS,
            )
        if isinstance(source, (str, bytes)):
            source = (source,)
        incoming_sources = [
            str(item).strip()[: self.ANCHOR_MAX_SOURCE_CHARS]
            for item in source
            if str(item).strip() in ANCHOR_SOURCE_VALUES
        ]
        if event_id:
            incoming_sources.append("user_message" if not bool(self._anchor_event_value(event, "is_bot", False)) else "assistant_reply")
        platform_id = self._anchor_event_value(event, "platform_message_id", "")
        if event_id and platform_id:
            self.register_event_id_mapping(chat_id, canonical_id=event_id, platform_id=platform_id)
        if self._anchor_event_value(event, "quote_event_id", ""):
            incoming_sources.append("quote")
        if self._anchor_event_value(event, "reply_target_event_id", ""):
            incoming_sources.append("reply_target")
        if reply_text:
            incoming_sources.append("assistant_reply")
            if self._anchor_is_question(reply_text):
                current_open_loop = self._sanitize_topic_preview(reply_text)[: self.ANCHOR_MAX_OPEN_LOOP_CHARS]
                incoming_sources.append("explicit_question")
            else:
                current_open_loop = ""
        elif open_loop:
            current_open_loop = self._sanitize_topic_preview(open_loop)[: self.ANCHOR_MAX_OPEN_LOOP_CHARS]
        elif current_open_loop and subject and subject != current_subject:
            current_open_loop = ""
        elif current_open_loop and event is not None and not self._anchor_event_value(event, "is_bot", False):
            current_open_loop = ""
        for incoming_source in incoming_sources or ["user_message"]:
            sources = self._anchor_append_unique(sources, incoming_source, 8)
        try:
            bounded_confidence = max(0.0, min(1.0, float(confidence or 0.0)))
        except (TypeError, ValueError):
            bounded_confidence = 0.0
        state.topic_anchor = TopicAttentionAnchor(
            topic_epoch=epoch,
            participants=participant_ids,
            subject_preview=subject or current_subject,
            open_loop=current_open_loop,
            recent_event_ids=event_ids,
            updated_at=now,
            confidence=bounded_confidence,
            source=sources,
        )
        return state.topic_anchor.as_dict()

    def topic_anchor_view(self, chat_id: str, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else float(now)
        state = self._state(str(chat_id or ""))
        self.recent(chat_id, now=now)
        return self._anchor_view(state, now)

    def topic_anchor_prompt(self, chat_id: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else float(now)
        view = self.topic_anchor_view(chat_id, now=now)
        if not view:
            return ""
        lines = [
            "话题注意力锚点（动态上下文，非稳定系统规则）：",
            f"- subject={str(view.get('subject_preview', ''))[:self.ANCHOR_MAX_SUBJECT_CHARS]}",
            f"- participants={','.join(view.get('participants', [])[:self.ANCHOR_MAX_PARTICIPANTS])}",
            f"- open_loop={str(view.get('open_loop', ''))[:self.ANCHOR_MAX_OPEN_LOOP_CHARS] or 'none'}",
            f"- source={','.join(view.get('source', [])) or 'unknown'}; age_seconds={int(view.get('age_seconds', 0) or 0)}; confidence={float(view.get('confidence', 0.0) or 0.0):.2f}",
        ]
        return "\n".join(lines)[: self.ANCHOR_MAX_PROMPT_CHARS]

    def evaluate_topic_bridge(
        self,
        chat_id: str,
        *,
        event: Any,
        target_topic_epoch: int,
        rotation_reason: str = "",
        now: float | None = None,
    ) -> BridgeDecision:
        """Read the previous topic without changing its epoch, anchor or history."""
        now = time.time() if now is None else float(now)
        chat_key = str(chat_id or "").strip()
        source = self._states.get(chat_key)
        source_epoch = int(source.topic_epoch or 0) if source else 0
        target_epoch = max(0, int(target_topic_epoch or 0))

        def deny(reason: str) -> BridgeDecision:
            return BridgeDecision(
                reason=reason,
                source_chat_key=chat_key,
                target_chat_key=chat_key,
                source_topic_epoch=source_epoch,
                target_topic_epoch=target_epoch,
                created_at=now,
            )

        event_chat = str(self._anchor_event_value(event, "chat_id", "") or "").strip()
        if not chat_key or not event_chat or event_chat != chat_key:
            return deny("chat_mismatch")
        text = self._sanitize_topic_preview(self._anchor_event_value(event, "visible_text", ""))
        if rotation_reason == "explicit_topic_switch" or self._is_explicit_topic_switch(text) or self._is_confirmation_no(text) or "不聊了" in text:
            return deny("explicit_topic_switch")
        if source is None:
            return deny("no_source_anchor")
        source_anchor = source.topic_anchor
        source_turns = source.turns
        if source.bridge_anchor.topic_epoch and (
            source_anchor.topic_epoch != source_epoch
            or source.bridge_anchor.topic_epoch == target_epoch - 1
        ):
            source_anchor = source.bridge_anchor
            source_turns = source.bridge_turns
            source_epoch = source_anchor.topic_epoch
        if not source_anchor.subject_preview:
            return deny("no_source_anchor")
        if source_epoch == target_epoch:
            return deny("same_topic")
        if source_epoch <= 0 or target_epoch != source_epoch + 1:
            return deny("non_adjacent_topic")
        if source.goal_status in {"guarded", "redirected"} or source.last_social_intent in {"boundary", "redirect"}:
            return deny("closed_or_guarded_topic")
        anchor = source_anchor
        age = now - anchor.updated_at
        if age < 0 or age >= min(self.BRIDGE_TTL_SECONDS, self.TURN_TTL_SECONDS, self._group_topic_active_ttl_seconds):
            return deny("expired")
        recent_turns = tuple(
            turn for turn in source_turns
            if turn.topic_epoch == source_epoch and 0 <= now - turn.timestamp < self.BRIDGE_TTL_SECONDS
        )[-BRIDGE_MAX_TURNS:]
        actual_ids = tuple(
            dict.fromkeys(
                [
                    turn.source_event_id
                    for turn in recent_turns
                    if turn.source_event_id
                ]
                + [
                    outbound_id
                    for turn in recent_turns
                    for outbound_id in turn.assistant_outbound_ids
                    if outbound_id
                ]
            )
        )
        if not actual_ids:
            return deny("no_recent_evidence")
        current_event_id = str(self._anchor_event_value(event, "event_id", "") or "").strip()
        target_ids = tuple(dict.fromkeys(
            str(self._anchor_event_value(event, name, "") or "").strip()
            for name in ("reply_target_event_id", "quote_event_id", "causal_parent_event_id")
        ))
        target_ids = tuple(item for item in target_ids if item)
        mapped_target_ids = tuple(
            dict.fromkeys(
                item
                for target_id in target_ids
                for item in (target_id, source.event_id_map.get(target_id, ""))
                if item
            )
        )
        if current_event_id in actual_ids or any(item == current_event_id for item in mapped_target_ids):
            return deny("duplicate_event")
        if target_ids and not any(item in actual_ids for item in mapped_target_ids):
            return deny("unverified_reply_target")
        actor = str(self._anchor_event_value(event, "actor_id", "") or "").strip()
        same_actor = bool(actor and actor in anchor.participants and actor == source.last_sender_id)
        short_text = bool(text and len(text) <= 20)
        if target_ids:
            reason, confidence = "explicit_reply", 0.95
            evidence = tuple(item for item in actual_ids if item in mapped_target_ids)[:BRIDGE_MAX_EVENT_IDS]
        elif not same_actor:
            return deny("actor_mismatch")
        elif anchor.open_loop and short_text and (
            self._is_same_topic(text, anchor.open_loop)
            or self._normalize_topic_text(text) in {"是", "对", "好", "可以", "有空", "没有", "不行"}
        ):
            reason, confidence = "open_loop_answer", 0.85
            evidence = actual_ids[-BRIDGE_MAX_EVENT_IDS:]
        elif now - recent_turns[-1].timestamp <= self.BRIDGE_SAME_ACTOR_SECONDS and short_text and self._is_short_topic_followup(text):
            reason, confidence = "same_actor_followup", 0.75
            evidence = actual_ids[-BRIDGE_MAX_EVENT_IDS:]
        else:
            has_reference_word = any(word in text for word in ("他", "她", "它", "这个", "那个", "上面", "刚才", "继续", "然后"))
            return deny("insufficient_evidence" if has_reference_word else "unrelated_topic")
        turns = tuple(
            f"user: {turn.focus_preview[:120]} | assistant: {turn.reply_preview[:120]}"
            for turn in recent_turns if turn.focus_preview or turn.reply_preview
        )
        if not turns:
            return deny("no_recent_evidence")
        return BridgeDecision(
            allowed=True,
            reason=reason,
            confidence=confidence,
            source_chat_key=chat_key,
            target_chat_key=chat_key,
            source_topic_epoch=source_epoch,
            target_topic_epoch=target_epoch,
            evidence_event_ids=evidence,
            source_anchor_preview=anchor.subject_preview[:self.ANCHOR_MAX_SUBJECT_CHARS],
            source_open_loop=anchor.open_loop[:self.ANCHOR_MAX_OPEN_LOOP_CHARS],
            turns=turns,
            created_at=now,
            expires_at=now + min(self.BRIDGE_TTL_SECONDS - age, self._group_topic_active_ttl_seconds - age),
            source_age_seconds=age,
        )

    def restore_snapshot(self, chat_id: str, snapshot: Any) -> None:
        """Restore only known continuity fields; malformed anchor fields degrade locally."""
        if not isinstance(snapshot, Mapping):
            return
        state = self._state(str(chat_id or ""))
        for field_name in ("current_topic", "current_goal", "goal_status", "last_social_intent", "last_action_taken"):
            if field_name in snapshot and isinstance(snapshot[field_name], str):
                setattr(state, field_name, snapshot[field_name])
        state.topic_anchor = TopicAttentionAnchor.from_value(snapshot.get("topic_anchor", {}))

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
        sender_id: str = "",
        source_event_id: str = "",
        assistant_outbound_ids: Iterable[Any] = (),
        anchor_event: Any = None,
        topic_epoch: int | None = None,
        now: float | None = None,
    ) -> ConversationTurnRecord:
        now = time.time() if now is None else now
        state = self._state(chat_id)
        self.recent(chat_id, now=now)
        raw_focus_preview = str(focus_preview or "")
        safe_focus_preview = self._sanitize_topic_preview(raw_focus_preview)
        rejected_internal_focus = bool(raw_focus_preview.strip() and not safe_focus_preview)
        item = ConversationTurnRecord(
            timestamp=now,
            chat_id=chat_id,
            focus_preview=safe_focus_preview[:160],
            goal_summary=str(goal_summary or "")[:220],
            social_intent=str(social_intent or ""),
            action_tier=str(action_tier or ""),
            action_taken=str(action_taken or ""),
            reply_preview=str(reply_preview or "")[:160],
            reply_need=str(reply_need or "reply"),
            sender_id=str(sender_id or ""),
            source_event_id=str(source_event_id or ""),
            assistant_outbound_ids=self._normalize_evidence_ids(
                assistant_outbound_ids, self.ANCHOR_MAX_EVENT_IDS
            ),
            topic_epoch=max(0, int(topic_epoch or 0)),
        )
        # 设计意图：lightweight_event 和 wait/ignore 是非实质性轮次，
        # 不应推进对话主题/目标状态机。仅记录轮次，不更新 state.current_topic、
        # state.current_goal、state.goal_status、continuity_weight 等。
        # 此行为是有意设计，非 bug（参见 TECHNICAL-DEBT-INVENTORY D22）。
        if lightweight_event or rejected_internal_focus or item.reply_need in {"wait", "ignore"}:
            state.turns = [*self.recent(chat_id, now=now), item][-self.MAX_TURNS_PER_CHAT :]
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
        if topic_epoch is not None and int(topic_epoch or 0) > 0:
            state.topic_epoch = int(topic_epoch)
        elif goal_status in {"new", "redirected"} or state.topic_epoch <= 0:
            state.topic_epoch = max(1, int(state.topic_epoch or 0) + 1)
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
        state.last_sender_id = item.sender_id or state.last_sender_id
        state.last_event_id = item.source_event_id or state.last_event_id
        if item.sender_id:
            state.topic_participants.add(item.sender_id)
        self.update_topic_anchor(
            chat_id,
            subject_preview=item.focus_preview,
            reply_text=item.reply_preview,
            open_loop=item.goal_summary if self._anchor_is_question(item.goal_summary) else "",
            source=("user_message",) if item.focus_preview else (),
            anchor_event=anchor_event,
            event_id=item.source_event_id,
            actor_id=item.sender_id,
            topic_epoch=state.topic_epoch,
            confidence=0.7 if item.focus_preview else 0.0,
            now=now,
        )
        platform_event_id = self._anchor_event_value(anchor_event, "platform_message_id", "")
        if item.source_event_id and platform_event_id and item.source_event_id != platform_event_id:
            self.register_event_id_mapping(
                chat_id,
                canonical_id=item.source_event_id,
                platform_id=platform_event_id,
            )
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
    "TopicAttentionAnchor",
]
