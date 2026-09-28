from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReplyForm(str, Enum):
    """Closed set of user-visible response forms."""

    SILENCE = "silence"
    SHORT_ACK = "short_ack"
    QUOTE_REPLY = "quote_reply"
    ANSWER = "answer"
    COMFORT = "comfort"
    REACTION = "reaction"
    TOPIC_START = "topic_start"


@dataclass(frozen=True, slots=True)
class ReplyFormSpec:
    form: ReplyForm
    intents: tuple[str, ...]
    target_required: bool
    message_required: bool
    allow_group: bool
    allow_private: bool
    allow_quote: bool
    allow_reaction: bool
    min_confidence: float
    sender_route: str
    fallback_form: ReplyForm | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "form": self.form.value,
            "intents": list(self.intents),
            "target_required": self.target_required,
            "message_required": self.message_required,
            "allow_group": self.allow_group,
            "allow_private": self.allow_private,
            "allow_quote": self.allow_quote,
            "allow_reaction": self.allow_reaction,
            "min_confidence": self.min_confidence,
            "sender_route": self.sender_route,
            "fallback_form": self.fallback_form.value if self.fallback_form else "",
        }


FORM_SPECS: dict[ReplyForm, ReplyFormSpec] = {
    ReplyForm.SILENCE: ReplyFormSpec(ReplyForm.SILENCE, ("answer", "followup", "comfort", "share", "break_silence", "ritual"), False, False, True, True, False, False, 0.0, "silence"),
    ReplyForm.SHORT_ACK: ReplyFormSpec(ReplyForm.SHORT_ACK, ("answer", "followup", "comfort", "share", "break_silence", "ritual"), False, True, True, True, False, False, 0.0, "text_short"),
    ReplyForm.QUOTE_REPLY: ReplyFormSpec(ReplyForm.QUOTE_REPLY, ("answer", "followup"), True, True, True, True, True, False, 0.5, "quote_reply", ReplyForm.SHORT_ACK),
    ReplyForm.ANSWER: ReplyFormSpec(ReplyForm.ANSWER, ("answer", "followup", "comfort", "share", "break_silence", "ritual"), False, True, True, True, False, False, 0.0, "text"),
    ReplyForm.COMFORT: ReplyFormSpec(ReplyForm.COMFORT, ("comfort",), False, True, True, True, False, False, 0.0, "text_comfort", ReplyForm.SHORT_ACK),
    ReplyForm.REACTION: ReplyFormSpec(ReplyForm.REACTION, ("answer", "followup", "comfort", "share", "break_silence", "ritual"), True, False, True, True, False, True, 0.5, "reaction", ReplyForm.SHORT_ACK),
    ReplyForm.TOPIC_START: ReplyFormSpec(ReplyForm.TOPIC_START, ("share", "break_silence", "ritual"), False, True, True, True, False, False, 0.0, "text_topic"),
}


_FORM_ALIASES = {
    "wait": ReplyForm.SILENCE,
    "ignore": ReplyForm.SILENCE,
    "no_reply": ReplyForm.SILENCE,
    "ack": ReplyForm.SHORT_ACK,
    "short": ReplyForm.SHORT_ACK,
    "reply": ReplyForm.ANSWER,
    "respond": ReplyForm.ANSWER,
    "support": ReplyForm.COMFORT,
    "topic": ReplyForm.TOPIC_START,
}


@dataclass(frozen=True, slots=True)
class ReplyFormDecision:
    form: ReplyForm
    requested_form: str
    fallback_form: ReplyForm | None = None
    reason: str = ""
    target_required: bool = False
    intent: str = ""
    spec: ReplyFormSpec | None = None
    sender_route: str = "text"

    @property
    def degraded(self) -> bool:
        return self.fallback_form is not None or bool(self.requested_form and self.requested_form != self.form.value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "form": self.form.value,
            "requested_form": self.requested_form,
            "fallback_form": self.fallback_form.value if self.fallback_form else "",
            "reason": self.reason,
            "target_required": self.target_required,
            "intent": self.intent,
            "sender_route": self.sender_route,
            "spec": self.spec.as_dict() if self.spec else {},
            "degraded": self.degraded,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ReplyFormDecision":
        if not isinstance(value, dict):
            return normalize_reply_form(value)
        raw_form = str(value.get("form", "") or "").strip().lower()
        try:
            form = ReplyForm(raw_form)
        except ValueError:
            return normalize_reply_form(value.get("requested_form", raw_form), intent=value.get("intent", ""))
        spec = FORM_SPECS[form]
        fallback_raw = str(value.get("fallback_form", "") or "").strip().lower()
        try:
            fallback = ReplyForm(fallback_raw) if fallback_raw else None
        except ValueError:
            fallback = None
        return cls(
            form=form,
            requested_form=str(value.get("requested_form", form.value) or form.value),
            fallback_form=fallback,
            reason=str(value.get("reason", "") or ""),
            target_required=bool(value.get("target_required", spec.target_required)),
            intent=str(value.get("intent", "") or ""),
            spec=spec,
            sender_route=str(value.get("sender_route", spec.sender_route) or spec.sender_route),
        )


def normalize_reply_form(
    value: Any,
    *,
    default: ReplyForm = ReplyForm.ANSWER,
    intent: Any = "",
    target: Any = None,
    is_private: bool = False,
    reaction_supported: bool = True,
) -> ReplyFormDecision:
    requested = str(value.value if isinstance(value, ReplyForm) else value or "").strip().lower()
    normalized_intent = str(intent or "").strip().lower()
    if not requested:
        requested = default.value
    canonical = _FORM_ALIASES.get(requested)
    if canonical is None:
        try:
            canonical = ReplyForm(requested)
        except ValueError:
            return ReplyFormDecision(
                form=ReplyForm.SILENCE,
                requested_form=requested,
                fallback_form=ReplyForm.SILENCE,
                reason="unsupported_form",
                intent=normalized_intent,
                spec=FORM_SPECS[ReplyForm.SILENCE],
                sender_route="silence",
            )
    spec = FORM_SPECS[canonical]
    if normalized_intent and normalized_intent not in spec.intents:
        fallback = spec.fallback_form or ReplyForm.SILENCE
        fallback_spec = FORM_SPECS[fallback]
        return ReplyFormDecision(
            fallback,
            requested,
            fallback_form=fallback,
            reason="intent_form_incompatible",
            target_required=fallback_spec.target_required,
            intent=normalized_intent,
            spec=fallback_spec,
            sender_route=fallback_spec.sender_route,
        )
    if is_private and not spec.allow_private:
        return ReplyFormDecision(ReplyForm.SILENCE, requested, fallback_form=ReplyForm.SILENCE, reason="private_form_unsupported", intent=normalized_intent, spec=FORM_SPECS[ReplyForm.SILENCE], sender_route="silence")
    if canonical is ReplyForm.REACTION and not reaction_supported:
        fallback = ReplyForm.SHORT_ACK
        return ReplyFormDecision(fallback, requested, fallback_form=fallback, reason="reaction_unsupported", intent=normalized_intent, spec=FORM_SPECS[fallback], sender_route=FORM_SPECS[fallback].sender_route)
    target_confidence_raw = target.get("confidence", 0.0) if isinstance(target, dict) else getattr(target, "confidence", 0.0)
    target_event_raw = target.get("target_event_id", "") if isinstance(target, dict) else getattr(target, "target_event_id", "")
    try:
        target_confidence = float(target_confidence_raw or 0.0)
    except (TypeError, ValueError):
        target_confidence = 0.0
    target_event_id = str(target_event_raw or "").strip()
    if spec.target_required and (not target_event_id or target_confidence < spec.min_confidence):
        fallback = spec.fallback_form or ReplyForm.SILENCE
        fallback_spec = FORM_SPECS[fallback]
        return ReplyFormDecision(fallback, requested, fallback_form=fallback, reason="target_missing_or_low_confidence", target_required=spec.target_required, intent=normalized_intent, spec=fallback_spec, sender_route=fallback_spec.sender_route)
    return ReplyFormDecision(canonical, requested, target_required=spec.target_required, intent=normalized_intent, spec=spec, sender_route=spec.sender_route)


__all__ = ["FORM_SPECS", "ReplyForm", "ReplyFormDecision", "ReplyFormSpec", "normalize_reply_form"]
