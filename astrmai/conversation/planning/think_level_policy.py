from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..contracts.prompt_envelope import PromptEnvelope
from ..contracts.turn_context import get_turn_context


@dataclass(slots=True)
class ThinkLevelDecision:
    level: int = 1
    reason: str = "default"
    signals: list[str] = field(default_factory=list)


class ThinkLevelPolicy:
    """Resolve the per-turn reasoning budget before CognitiveLoop and memory retrieval."""

    SHORT_ACKS = {
        "ok",
        "好",
        "嗯",
        "嗯嗯",
        "来了",
        "哈哈",
        "hhh",
        "hh",
        "好呢",
        "行",
        "可以",
        "收到",
    }
    TOOL_LEVEL3_KEYWORDS = {
        "查一下",
        "搜一下",
        "帮我查",
        "帮我看看",
        "你还记得",
        "你记得",
        "上次说什么",
        "撤回",
        "转移话题",
        "换个话题",
        "戳一下",
        "戳一戳",
        "poke",
        "艾特",
        "tool",
        "search",
        "look up",
        "check",
        "remember what",
        "do you remember",
        "still remember",
        "withdraw",
        "change topic",
        "switch topic",
    }
    MEMORY_LEVEL2_KEYWORDS = {
        "刚才",
        "之前",
        "上次",
        "记得",
        "remember",
        "before",
        "last time",
        "earlier",
    }
    COMPLEXITY_LEVEL2_KEYWORDS = {
        "为什么",
        "怎么做",
        "如何",
        "分析",
        "解释",
        "建议",
        "怎么办",
        "难受",
        "安慰",
        "焦虑",
        "why",
        "how",
        "analyze",
        "explain",
        "advice",
        "comfort",
    }

    def decide(
        self,
        *,
        event: Any,
        prompt_envelope: PromptEnvelope | None = None,
        retrieve_keys: list[str] | None = None,
        planning_context: dict[str, Any] | None = None,
        cooldown_tags: list[str] | None = None,
        judge_action: str = "REPLY",
        is_tool_call_mode: bool = False,
    ) -> ThinkLevelDecision:
        retrieve_keys = list(retrieve_keys or [])
        planning_context = planning_context or {}
        cooldown_tags = [str(tag or "").strip() for tag in (cooldown_tags or []) if str(tag or "").strip()]
        text = self._current_text(event, prompt_envelope)
        compact_text = "".join(text.split())
        lowered = text.lower()
        signals: list[str] = []
        continuity = self._continuity(event)
        is_proactive_event = bool(self._event_extra(event, "astrmai_is_proactive_event", False))

        if not is_proactive_event and (is_tool_call_mode or str(judge_action or "").upper() == "TOOL_CALL"):
            return ThinkLevelDecision(3, "sys3_tool_call", ["tool_call_mode"])

        if self._event_extra(event, "astrmai_lightweight_event", False) or planning_context.get("is_lightweight_event", False):
            return ThinkLevelDecision(0, "lightweight_event", ["lightweight_event"])

        if "CORE_ONLY" in retrieve_keys or self._event_extra(event, "is_fast_mode", False):
            return ThinkLevelDecision(0, "core_only", ["core_only"])

        if is_proactive_event:
            source = str(self._event_extra(event, "astrmai_proactive_source", "") or "").strip() or "unknown"
            signals = ["proactive_event", f"proactive_source_{source}"]
            try:
                urgency = float(self._event_extra(event, "astrmai_proactive_urgency", 0.0) or 0.0)
            except (TypeError, ValueError):
                urgency = 0.0
            if urgency >= 0.75 and self._is_strong_continuity(continuity):
                return ThinkLevelDecision(2, "proactive_high_urgency_with_continuity", [*signals, "high_urgency", "strong_continuity"])
            return ThinkLevelDecision(1, "proactive_opening", signals)

        memory_policy = str(self._event_extra(event, "astrmai_cognitive_memory_policy", "") or "").strip().lower()
        if memory_policy == "deep":
            return ThinkLevelDecision(3, "deep_memory_policy", ["memory_policy_deep"])

        if self._has_any(text, lowered, self.TOOL_LEVEL3_KEYWORDS) or "@" in text:
            signals.append("explicit_tool_or_deep_memory_intent")
            return ThinkLevelDecision(3, "explicit_tool_or_deep_memory_intent", signals)

        direct = self._is_direct(event, prompt_envelope)
        is_group = self._is_group(event)
        frequency_guard_signals = self._heartflow_frequency_guard_signals(continuity)
        if is_group and not direct and frequency_guard_signals:
            return ThinkLevelDecision(0, "heartflow_frequency_guard", frequency_guard_signals)
        heartflow_signals = self._heartflow_posture_signals(continuity)
        if direct and self._has_direct_question_signal(text, compact_text, continuity):
            return ThinkLevelDecision(2, "heartflow_direct_question", ["direct_turn", *heartflow_signals])
        if heartflow_signals and not (is_group and not direct and self._heartflow_action(continuity) in {"observe", "no_reply", "cool_down", "complete_topic"}):
            return ThinkLevelDecision(2, "heartflow_posture", heartflow_signals)
        if self._is_short_ack(compact_text):
            return ThinkLevelDecision(0, "short_ack", ["short_ack"])

        cognitive_cooldowns = [tag for tag in cooldown_tags if tag in {"sharp_reply", "long_reply"}]
        if cognitive_cooldowns and len(compact_text) <= 16 and not self._has_any(text, lowered, self.COMPLEXITY_LEVEL2_KEYWORDS):
            return ThinkLevelDecision(0, "cooldown_simple_turn", ["cooldown", *cognitive_cooldowns])

        if (
            continuity.get("goal_status") == "continuing"
            and int(continuity.get("turn_count") or 0) > 0
            and len(compact_text) <= 18
            and not self._looks_like_question(text)
        ):
            return ThinkLevelDecision(0, "simple_topic_continuation", ["continuity_strong", "simple_reply"])

        if len(compact_text) >= 60:
            signals.append("long_message")
        if self._has_any(text, lowered, self.COMPLEXITY_LEVEL2_KEYWORDS):
            signals.append("complexity_keyword")
        if self._has_any(text, lowered, self.MEMORY_LEVEL2_KEYWORDS):
            signals.append("memory_reference")
        quiet_heartflow_action = self._heartflow_action(continuity) in {"observe", "no_reply", "cool_down", "complete_topic"}
        if (
            float(continuity.get("heartflow_talk_willingness") or 0.0) < 0.25
            and continuity.get("heartflow_context")
            and not (is_group and not direct and quiet_heartflow_action)
        ):
            signals.append("heartflow_low_talk_willingness")
        if str(continuity.get("continuity_weight") or "") == "weak" and continuity.get("current_topic"):
            signals.append("weak_continuity")
        if signals:
            return ThinkLevelDecision(2, "deeper_reasoning", list(dict.fromkeys(signals)))

        if is_group and not direct:
            signals.append("group_non_direct")
            return ThinkLevelDecision(0, "group_non_direct", signals)

        if direct:
            return ThinkLevelDecision(1, "direct_normal_turn", ["direct_turn"])
        return ThinkLevelDecision(1, "default_normal_turn", ["default"])

    @staticmethod
    def _event_extra(event: Any, key: str, default: Any = None) -> Any:
        if event is not None and hasattr(event, "get_extra"):
            return event.get_extra(key, default)
        return default

    @staticmethod
    def _has_any(text: str, lowered: str, keywords: set[str]) -> bool:
        return any(keyword in text or keyword.lower() in lowered for keyword in keywords)

    @staticmethod
    def _is_short_ack(compact_text: str) -> bool:
        lowered = compact_text.lower()
        return lowered in ThinkLevelPolicy.SHORT_ACKS

    @staticmethod
    def _current_text(event: Any, prompt_envelope: PromptEnvelope | None) -> str:
        if isinstance(prompt_envelope, PromptEnvelope):
            text = (
                prompt_envelope.raw_user_text
                or prompt_envelope.focus_message_text
                or prompt_envelope.direct_context_text
                or ""
            )
        else:
            text = ""
        if not text:
            text = str(getattr(event, "message_str", "") or "")
        return str(text or "").strip()

    @staticmethod
    def _is_group(event: Any) -> bool:
        getter = getattr(event, "get_group_id", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                return False
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return "group" in origin.lower()

    def _is_direct(self, event: Any, prompt_envelope: PromptEnvelope | None) -> bool:
        turn_context = get_turn_context(event)
        if turn_context is not None:
            perception = turn_context.perception
            if perception.is_private or perception.is_direct_wakeup or perception.is_at_bot or perception.is_reply_to_bot:
                return True
            if perception.is_name_only_wakeup or perception.is_strong_wakeup:
                return True
        if self._event_extra(event, "is_force_wakeup", False) or self._event_extra(event, "is_keyword_wakeup", False):
            return True
        focus_reason = ""
        if isinstance(prompt_envelope, PromptEnvelope):
            focus_reason = prompt_envelope.focus_reason or prompt_envelope.focus_thread_reason or ""
        focus_reason = focus_reason or str(self._event_extra(event, "astrmai_focus_reason", "") or "")
        normalized = focus_reason.strip().lower().replace("-", "_").replace(" ", "_")
        non_direct_reasons = {
            "latest_user_message",
            "latest_message",
            "latest",
            "ambient",
            "background",
            "side_chat",
            "group_non_direct",
        }
        if normalized in non_direct_reasons:
            return False
        direct_reasons = {
            "at_bot",
            "mention",
            "reply_to_bot",
            "private",
            "direct",
            "direct_wakeup",
            "name_wakeup",
            "keyword_wakeup",
            "force_wakeup",
            "strong_wakeup",
        }
        if normalized in direct_reasons:
            return True
        tokens = {token for token in re.split(r"[^a-z0-9]+", normalized) if token}
        return bool(tokens & {"at", "reply", "direct", "wakeup", "private", "mention", "name"})

    @staticmethod
    def _continuity(event: Any) -> dict[str, Any]:
        turn_context = get_turn_context(event)
        if turn_context is None:
            return {}
        continuity = turn_context.continuity
        return {
            "goal_status": continuity.goal_status,
            "turn_count": continuity.turn_count,
            "heartflow_context": continuity.heartflow_context,
            "heartflow_interest": continuity.heartflow_interest,
            "heartflow_talk_willingness": continuity.heartflow_talk_willingness,
            "heartflow_pulse": continuity.heartflow_pulse,
            "heartflow_action": continuity.heartflow_action,
            "heartflow_urgency": continuity.heartflow_urgency,
            "heartflow_talk_frequency_adjust": continuity.heartflow_talk_frequency_adjust,
            "heartflow_insert_pressure": continuity.heartflow_insert_pressure,
            "heartflow_reply_pressure": continuity.heartflow_reply_pressure,
            "heartflow_candidate_score": continuity.heartflow_candidate_score,
            "continuity_weight": continuity.continuity_weight,
            "current_topic": continuity.current_topic,
        }

    @staticmethod
    def _is_strong_continuity(continuity: dict[str, Any]) -> bool:
        return bool(
            str(continuity.get("goal_status") or "") == "continuing"
            or str(continuity.get("continuity_weight") or "") == "strong"
            or int(continuity.get("turn_count") or 0) >= 2
        )

    @classmethod
    def _heartflow_action(cls, continuity: dict[str, Any]) -> str:
        action = str(continuity.get("heartflow_action") or "").strip()
        if action:
            return action
        context = str(continuity.get("heartflow_context") or "")
        match = re.search(r"action_type=([a-z_]+)", context)
        return str(match.group(1) if match else "")

    @classmethod
    def _has_direct_question_signal(cls, text: str, compact_text: str, continuity: dict[str, Any]) -> bool:
        if not cls._looks_like_question(text) and len(compact_text) < 18:
            return False
        interest = cls._heartflow_metric(continuity, "heartflow_interest") or cls._heartflow_metric(continuity, "interest")
        pulse = str(continuity.get("heartflow_pulse") or "").strip()
        action = cls._heartflow_action(continuity)
        return bool(interest >= 0.70 or pulse in {"prepare_reply", "join", "proactive_hint"} or action == "prepare_reply")

    @staticmethod
    def _looks_like_question(text: str) -> bool:
        return "?" in text or "？" in text

    @staticmethod
    def _heartflow_metric(continuity: dict[str, Any], key: str) -> float:
        if key in continuity and continuity.get(key) is not None and str(continuity.get(key)) != "":
            try:
                return float(continuity.get(key) or 0.0)
            except (TypeError, ValueError):
                pass
        context = str(continuity.get("heartflow_context") or "")
        match = re.search(rf"{re.escape(key)}=([0-9.]+)", context)
        if not match:
            return 0.0
        try:
            return float(match.group(1) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _heartflow_frequency_guard_signals(cls, continuity: dict[str, Any]) -> list[str]:
        if not continuity.get("heartflow_context"):
            return []
        signals: list[str] = []
        insert_pressure = cls._heartflow_metric(continuity, "insert_pressure")
        reply_pressure = cls._heartflow_metric(continuity, "reply_pressure")
        candidate_score = cls._heartflow_metric(continuity, "visible_candidate_score")
        talk_frequency_adjust = cls._heartflow_metric(continuity, "talk_frequency_adjust")
        if insert_pressure >= 0.65:
            signals.append("heartflow_high_insert_pressure")
        if 0.0 < reply_pressure < 0.22:
            signals.append("heartflow_low_reply_pressure")
        if 0.0 < candidate_score < 0.55:
            signals.append("heartflow_low_candidate_score")
        if 0.0 < talk_frequency_adjust < 0.70:
            signals.append("heartflow_low_talk_frequency")
        return list(dict.fromkeys(signals))

    @staticmethod
    def _heartflow_posture_signals(continuity: dict[str, Any]) -> list[str]:
        signals: list[str] = []
        context = str(continuity.get("heartflow_context") or "")
        if context:
            insert_match = re.search(r"insert_pressure=([0-9.]+)", context)
            score_match = re.search(r"visible_candidate_score=([0-9.]+)", context)
            try:
                insert_pressure = float(insert_match.group(1)) if insert_match else 0.0
            except (TypeError, ValueError):
                insert_pressure = 0.0
            try:
                candidate_score = float(score_match.group(1)) if score_match else 0.0
            except (TypeError, ValueError):
                candidate_score = 0.0
            if insert_pressure >= 0.65 or (0.0 < candidate_score < 0.55):
                return []
        pulse = str(continuity.get("heartflow_pulse") or "").strip()
        if pulse in {"prepare_reply", "join", "proactive_hint"}:
            signals.append(f"heartflow_pulse_{pulse}")
        try:
            interest = float(continuity.get("heartflow_interest") or 0.0)
        except (TypeError, ValueError):
            interest = 0.0
        if interest >= 0.70:
            signals.append("heartflow_high_interest")
        try:
            talk_willingness = float(continuity.get("heartflow_talk_willingness") or 0.0)
        except (TypeError, ValueError):
            talk_willingness = 0.0
        if 0.0 < talk_willingness < 0.25 and continuity.get("heartflow_context"):
            signals.append("heartflow_low_talk_willingness")
        return list(dict.fromkeys(signals))


__all__ = ["ThinkLevelDecision", "ThinkLevelPolicy"]
