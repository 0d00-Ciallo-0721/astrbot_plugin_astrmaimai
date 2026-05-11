from __future__ import annotations

from typing import Any

from .cognitive_loop import CognitiveDecision


class BehaviorTuningPolicy:
    """Conservative post-cognitive guardrails for human-like timing and posture."""

    DIRECT_FOCUS_REASONS = {
        "reply_to_bot",
        "at_bot",
        "direct_wakeup",
        "direct_vision_request",
        "near_context_followup",
    }
    UNCERTAIN_FLAGS = {
        "uncertain",
        "low_relevance",
        "not_for_bot",
        "ambiguous",
        "ambiguous_context",
    }
    PUSHBACK_DIRECT_FLAGS = {
        "direct_attack_to_bot",
        "bot_direct_attack",
        "target_bot",
        "directed_at_bot",
    }
    PUSHBACK_BLOCKED_FLAGS = {
        "vulnerable_user",
        "help_seeking",
        "self_harm",
        "medical",
        "legal",
        "financial",
        "medical/legal/financial_sensitive",
        "sensitive_topic",
        "unclear_target",
        "third_party_conflict",
        "joke",
        "low_intensity",
        "ambiguous",
        "ambiguous_context",
    }
    SHORT_QUESTION_CUES = {"?", "？", "吗", "嘛", "呢", "么"}

    @staticmethod
    def _risk_flags(decision: CognitiveDecision) -> list[str]:
        flags: list[str] = []
        for item in list(getattr(decision, "risk_flags", []) or []):
            flag = str(item or "").strip().lower()
            if flag and flag not in flags:
                flags.append(flag)
        decision.risk_flags = flags
        return flags

    @staticmethod
    def _append_flag(decision: CognitiveDecision, flag: str) -> None:
        flags = BehaviorTuningPolicy._risk_flags(decision)
        if flag not in flags:
            flags.append(flag)
        decision.risk_flags = flags

    @staticmethod
    def _is_group_event(event: Any) -> bool:
        if not hasattr(event, "get_group_id"):
            return False
        try:
            return bool(event.get_group_id())
        except Exception:
            return False

    @classmethod
    def _is_directly_addressed(cls, event: Any, prompt_envelope: Any | None) -> bool:
        if not cls._is_group_event(event):
            return True
        turn_context = None
        if hasattr(event, "get_extra"):
            turn_context = event.get_extra("astrmai_turn_context", None)
        perception = getattr(turn_context, "perception", None)
        if perception and bool(
            getattr(perception, "is_strong_wakeup", False)
            or getattr(perception, "is_at_bot", False)
            or getattr(perception, "is_reply_to_bot", False)
            or getattr(perception, "is_direct_wakeup", False)
        ):
            return True
        focus_reason = str(getattr(prompt_envelope, "focus_reason", "") or "").strip()
        focus_thread_reason = str(getattr(prompt_envelope, "focus_thread_reason", "") or "").strip()
        return focus_reason in cls.DIRECT_FOCUS_REASONS or focus_thread_reason in cls.DIRECT_FOCUS_REASONS

    @classmethod
    def _looks_like_short_ambient(cls, event: Any) -> bool:
        text = str(getattr(event, "message_str", "") or "").strip()
        compact = "".join(text.split())
        if not compact or len(compact) > 12:
            return False
        return not any(cue in compact for cue in cls.SHORT_QUESTION_CUES)

    @classmethod
    def _allow_pushback(cls, decision: CognitiveDecision, cooldown_tags: set[str]) -> bool:
        flags = set(cls._risk_flags(decision))
        if float(getattr(decision, "attack_confidence", 0.0) or 0.0) < 0.85:
            return False
        if not (flags & cls.PUSHBACK_DIRECT_FLAGS):
            return False
        if flags & cls.PUSHBACK_BLOCKED_FLAGS:
            return False
        return "sharp_reply" not in cooldown_tags

    @classmethod
    def _downgrade_pushback(cls, decision: CognitiveDecision, reason: str) -> CognitiveDecision:
        decision.social_intent = "boundary"
        decision.stance = "guarded"
        decision.action_tier = "none"
        decision.allowed_action_families = []
        cls._append_flag(decision, "pushback_downgraded")
        cls._append_flag(decision, reason)
        return decision

    @classmethod
    def apply(
        cls,
        *,
        event: Any,
        decision: CognitiveDecision,
        prompt_envelope: Any | None = None,
        cooldown_tags: list[str] | set[str] | None = None,
    ) -> CognitiveDecision:
        cooldown_set = {str(tag or "").strip() for tag in (cooldown_tags or []) if str(tag or "").strip()}
        flags = set(cls._risk_flags(decision))

        if decision.social_intent == "pushback":
            if not cls._allow_pushback(decision, cooldown_set):
                reason = "sharp_reply_cooldown" if "sharp_reply" in cooldown_set else "pushback_guardrail"
                cls._downgrade_pushback(decision, reason)

        flags = set(cls._risk_flags(decision))
        if flags & cls.UNCERTAIN_FLAGS:
            decision.action = "wait"
            decision.reply_need = "wait"
            decision.social_intent = "observe"
            decision.action_tier = "none"
            decision.allowed_action_families = []
            decision.stance = "cool"
            cls._append_flag(decision, "uncertain_observe")
            return decision

        directly_addressed = cls._is_directly_addressed(event, prompt_envelope)
        if not directly_addressed and decision.action != "tool_call":
            if cls._looks_like_short_ambient(event) and decision.social_intent in {"answer", "join", "tease"}:
                decision.action = "wait"
                decision.reply_need = "wait"
                decision.social_intent = "observe"
                decision.action_tier = "none"
                decision.allowed_action_families = []
                decision.stance = "cool"
                cls._append_flag(decision, "group_ambient_short_wait")
                return decision
            if decision.social_intent in {"join", "tease"}:
                decision.action_tier = "none"
                decision.allowed_action_families = []
                cls._append_flag(decision, "group_non_direct_softened")

        if "long_reply" in cooldown_set and decision.reply_need == "reply":
            if decision.style_policy:
                decision.style_policy = f"{decision.style_policy}; keep_this_turn_short"
            else:
                decision.style_policy = "keep_this_turn_short"
            cls._append_flag(decision, "long_reply_cooldown")

        return decision


__all__ = ["BehaviorTuningPolicy"]
