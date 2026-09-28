from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..contracts.attention_topic import AttentionTopicIdentity

_EXPLICIT_CONTINUATIONS = {
    "?",
    "？",
    "嗯",
    "嗯嗯",
    "好",
    "好的",
    "行",
    "可以",
    "对",
    "不对",
    "继续",
    "然后呢",
    "那个呢",
    "还有呢",
    "是吗",
    "为什么",
    "啥意思",
    "什么意思",
    "我呢",
    "那我呢",
    "再说一遍",
}


@dataclass(frozen=True, slots=True)
class ParticipationState:
    phase: str = "detached"
    actor_id: str = ""
    topic_epoch: int = 0
    attention_topic_key: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ParticipationResult:
    action: str
    reason: str
    score: int
    signals: tuple[str, ...]
    phase: str
    phase_age_ms: int
    invalidated_reason: str = ""
    strong_wakeup_event_ids: tuple[str, ...] = ()
    social_signals: tuple[str, ...] = ()
    social_admission: str = "ambiguous"
    social_evidence: tuple[tuple[str, str], ...] = ()
    social_window_seconds: float = 0.0
    social_ttl_seconds: float = 0.0
    feedback_effect: str = "none"
    feedback_evidence: tuple[tuple[str, str], ...] = ()


def _event_id(event: Any) -> str:
    canonical = getattr(event, "get_extra", lambda *_args: None)(
        "astrmai_conversation_event",
        None,
    )
    return str(
        getattr(canonical, "event_id", "")
        or getattr(event, "get_extra", lambda *_args: "")(
            "astrmai_conversation_event_id",
            "",
        )
        or getattr(getattr(event, "message_obj", None), "message_id", "")
        or ""
    ).strip()


def _actor_id(event: Any) -> str:
    try:
        return str(event.get_sender_id() or "").strip()
    except Exception:
        return ""


def _event_text(event: Any) -> str:
    if event is None:
        return ""
    rich_text = getattr(event, "get_extra", lambda *_args: "")(
        "astrmai_rich_text",
        "",
    )
    return str(rich_text or getattr(event, "message_str", "") or "").strip()


def _event_timestamp(event: Any) -> float:
    value = getattr(event, "get_extra", lambda *_args: 0.0)(
        "astrmai_timestamp",
        getattr(event, "timestamp", 0.0),
    )
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_bot_event(event: Any, bot_ids: set[str]) -> bool:
    if event is None:
        return False
    if bool(getattr(event, "get_extra", lambda *_args: False)("is_self", False)):
        return True
    try:
        sender_id = str(event.get_sender_id() or "").strip()
    except Exception:
        sender_id = ""
    return bool(sender_id and sender_id in bot_ids)


def _social_signals(
    focus_event: Any,
    batch_events: Iterable[Any],
    *,
    ttl_seconds: float,
    now: float,
    open_question_wait_seconds: float = 3.0,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    events = list(batch_events or ())
    extras = getattr(focus_event, "get_extra", lambda *_args: None)
    bot_ids = {
        str(value or "").strip()
        for value in (
            extras("astrmai_bot_id", ""),
            extras("self_id", ""),
            getattr(focus_event, "get_self_id", lambda: "")(),
        )
        if str(value or "").strip()
    }
    actor_ids = {_actor_id(event) for event in events if _actor_id(event) and not _is_bot_event(event, bot_ids)}
    human_events = [event for event in events if _actor_id(event) and not _is_bot_event(event, bot_ids) and _event_text(event)]
    signals: list[str] = []
    evidence: list[tuple[str, str]] = []
    focus_text = _event_text(focus_event)
    canonical = extras("astrmai_conversation_event", None)
    direct = bool(
        extras("astrmai_group_direct_wakeup", False)
        or extras("astrmai_is_direct_wakeup", False)
        or extras("is_at_bot", False)
        or extras("is_reply_to_bot", False)
        or extras("astrmai_strong_wakeup", False)
        or getattr(canonical, "is_direct_wakeup", False)
        or getattr(canonical, "is_at_bot", False)
        or getattr(canonical, "is_reply_to_bot", False)
    )
    if direct:
        signals.append("direct_wakeup")
    focus_index = len(events) - 1
    for index, event in enumerate(events):
        if event is focus_event:
            focus_index = index
            break
    focus_ts = _event_timestamp(focus_event) or now
    recent_bot = False
    for event in reversed(events[:focus_index]):
        if not _event_text(event):
            continue
        event_ts = _event_timestamp(event)
        if event_ts and focus_ts and focus_ts - event_ts > max(1.0, float(ttl_seconds or 180.0)):
            break
        recent_bot = _is_bot_event(event, bot_ids)
        break
    if recent_bot:
        signals.append("bot_recently_spoke")
        evidence.append(("bot_recently_spoke", f"previous_meaningful_event_within_{ttl_seconds:.1f}s"))

    question_index = -1
    for index, event in enumerate(events[: focus_index + 1]):
        text = _event_text(event)
        if "?" in text or "？" in text or any(token in text for token in ("为什么", "怎么", "什么", "吗", "能不能", "可不可以")):
            question_index = index
    question_event = events[question_index] if question_index >= 0 else None
    question_age = 0.0
    question_answered = False
    if question_event is not None:
        question_ts = _event_timestamp(question_event)
        question_age = max(0.0, now - question_ts) if question_ts else 0.0
        question_actor = _actor_id(question_event)
        question_answered = any(
            _actor_id(event) and not _is_bot_event(event, bot_ids) and _actor_id(event) != question_actor
            for event in events[question_index + 1 :]
            if _event_text(event)
        )
    if (
        question_event is not None
        and not question_answered
        and not recent_bot
        and question_age >= max(0.0, float(open_question_wait_seconds or 0.0))
    ):
        signals.append("open_question_waiting")
        evidence.append(("open_question_waiting", f"age={question_age:.1f}s;wait={open_question_wait_seconds:.1f}s;unanswered=true"))
    else:
        if question_event is not None and question_answered:
            evidence.append(("open_question_answered", "later_human_event_in_window"))

    if len(actor_ids) == 2 and len(human_events) >= 3:
        alternations = sum(1 for left, right in zip(human_events, human_events[1:]) if _actor_id(left) != _actor_id(right))
        if alternations >= 2 and not recent_bot:
            signals.append("human_dyad_active")
            evidence.append(("human_dyad_active", f"actors=2;alternations={alternations};window_events={len(human_events)}"))
    if direct:
        evidence.append(("direct_wakeup", "canonical_or_explicit_direct_signal"))
    return tuple(dict.fromkeys(signals)), tuple(evidence)


class ParticipationPolicy:
    """Pure structural participation scoring for group attention prefiltering."""

    def evaluate(
        self,
        *,
        focus_event: Any,
        batch_events: Iterable[Any],
        strong_wakeup_event_ids: Iterable[str] = (),
        recent_committed_turn: Any = None,
        previous_state: ParticipationState | None = None,
        topic_identity: AttentionTopicIdentity | None = None,
        feedback_summary: dict[str, Any] | None = None,
        ttl_seconds: float = 180.0,
        now: float | None = None,
    ) -> tuple[ParticipationResult, ParticipationState]:
        batch_events = list(batch_events or ())
        timestamp = float(now if now is not None else (_event_timestamp(focus_event) or time.time()))
        actor_id = _actor_id(focus_event)
        identity = AttentionTopicIdentity.from_value(
            topic_identity or AttentionTopicIdentity.from_event(focus_event)
        )
        topic_epoch = identity.history_topic_epoch
        attention_topic_key = identity.attention_topic_key
        text = _event_text(focus_event).lower()
        strong_ids = tuple(dict.fromkeys(str(value) for value in strong_wakeup_event_ids if str(value)))
        score = 0
        signals: list[str] = []
        feedback_summary = dict(feedback_summary or {})
        feedback_evidence: list[tuple[str, str]] = []
        feedback_effect = "none"
        invalidated_reason = ""
        phase_age_ms = 0

        previous = previous_state or ParticipationState()
        social_signals, social_evidence = _social_signals(
            focus_event,
            batch_events,
            ttl_seconds=ttl_seconds,
            now=timestamp,
        )
        if previous.updated_at > 0.0:
            phase_age_ms = max(0, int((timestamp - previous.updated_at) * 1000))
            if timestamp - previous.updated_at > max(1.0, float(ttl_seconds or 180.0)):
                invalidated_reason = "ttl_expired"
                previous = ParticipationState()
            elif previous.attention_topic_key and (
                not attention_topic_key
                or previous.attention_topic_key != attention_topic_key
            ):
                invalidated_reason = (
                    "topic_identity_changed"
                    if attention_topic_key
                    else "topic_identity_unknown"
                )
                previous = ParticipationState()

        if strong_ids:
            score += 100
            signals.append("owned_batch_strong_wakeup")

        explicit_negative = bool(feedback_summary.get("explicit_negative_suppression", False))
        unanswered_count = int(feedback_summary.get("consecutive_unanswered_count", 0) or 0)
        followup_strength = float(feedback_summary.get("recent_followup_strength", 0.0) or 0.0)
        reaction_strength = float(feedback_summary.get("recent_reaction_strength", 0.0) or 0.0)
        if explicit_negative:
            score -= 60
            signals.append("feedback_explicit_negative")
            feedback_effect = "local_suppression"
            feedback_evidence.append(("explicit_negative_suppression", "bounded_actor_or_chat_ttl"))
        if unanswered_count >= 2:
            score -= min(30, unanswered_count * 10)
            signals.append("feedback_unanswered")
            feedback_effect = "wait" if feedback_effect == "none" else feedback_effect
            feedback_evidence.append(("consecutive_unanswered_count", str(min(8, unanswered_count))))
        if followup_strength > 0.0 or reaction_strength > 0.0:
            score += 15
            signals.append("feedback_recent_engagement")
            feedback_effect = "restore" if feedback_effect == "none" else feedback_effect
            feedback_evidence.append(("recent_engagement", f"followup={followup_strength:.2f};reaction={reaction_strength:.2f}"))

        extras = getattr(focus_event, "get_extra", lambda *_args: None)
        provenance = str(extras("astrmai_event_provenance", "original") or "original")
        if provenance == "external_plugin" and not strong_ids:
            score -= 100
            signals.append("external_plugin_unaddressed")
        if bool(extras("astrmai_repeater_echo", False)) or bool(
            extras("astrmai_is_external_bot_reply", False)
        ):
            score -= 100
            signals.append("bot_or_repeater_echo")

        has_media = bool(
            extras("extracted_image_refs", extras("extracted_image_urls", []))
            or extras("direct_image_refs", extras("direct_vision_urls", []))
        )
        interaction_kind = str(extras("astrmai_interaction_kind", "") or "").strip()
        if not text and not has_media and not interaction_kind:
            score -= 100
            signals.append("empty_event")

        committed = recent_committed_turn
        if committed is not None:
            committed_actor = str(getattr(committed, "target_sender_id", "") or "").strip()
            committed_topic = max(0, int(getattr(committed, "topic_epoch", 0) or 0))
            committed_attention_key = str(
                getattr(committed, "attention_topic_key", "") or ""
            ).strip()
            committed_ts = float(getattr(committed, "timestamp", 0.0) or 0.0)
            committed_age = timestamp - committed_ts if committed_ts > 0.0 else 0.0
            same_actor = bool(actor_id and committed_actor == actor_id)
            same_topic = bool(
                attention_topic_key
                and committed_attention_key
                and attention_topic_key == committed_attention_key
            ) or bool(
                not committed_attention_key
                and identity.confidence >= 0.7
                and topic_epoch > 0
                and committed_topic > 0
                and topic_epoch == committed_topic
            )
            if same_actor and same_topic and committed_age <= max(1.0, float(ttl_seconds or 180.0)):
                score += 40
                signals.append("committed_target_continuation")
                if text in _EXPLICIT_CONTINUATIONS:
                    score += 35
                    signals.append("explicit_short_continuation")
                canonical = extras("astrmai_conversation_event", None)
                referenced_id = str(
                    getattr(canonical, "reply_target_event_id", "")
                    or getattr(canonical, "quote_event_id", "")
                    or ""
                ).strip()
                committed_ids = {
                    str(getattr(committed, "turn_id", "") or "").strip(),
                    *(
                        str(value or "").strip()
                        for value in getattr(committed, "source_event_ids", ())
                    ),
                }
                if referenced_id and referenced_id in committed_ids:
                    score += 50
                    signals.append("references_committed_turn")

        if previous.phase == "engaged":
            same_actor = bool(actor_id and previous.actor_id == actor_id)
            same_topic = bool(
                attention_topic_key
                and previous.attention_topic_key
                and previous.attention_topic_key == attention_topic_key
            )
            if same_actor and same_topic:
                score += 25
                signals.append("engaged_hysteresis")
                if text in _EXPLICIT_CONTINUATIONS:
                    score += 20
                    signals.append("hysteresis_short_continuation")
            elif not same_actor:
                signals.append("different_actor_observing")

        if score >= 70:
            action = "FORCE_PASS"
            reason = signals[-1] if signals else "high_confidence_participation"
            phase = "engaged"
        elif score <= -80:
            action = "DROP"
            reason = signals[-1] if signals else "high_confidence_nonparticipation"
            phase = "detached"
        else:
            action = "NEED_JUDGE"
            reason = "ambiguous_group_message"
            phase = "cooling" if previous.phase == "engaged" else "observing"
        if "direct_wakeup" in social_signals or "open_question_waiting" in social_signals:
            social_admission = "allow"
        elif explicit_negative and not ("direct_wakeup" in social_signals or "open_question_waiting" in social_signals):
            social_admission = "wait"
        elif unanswered_count >= 2 and not ("direct_wakeup" in social_signals or "open_question_waiting" in social_signals):
            social_admission = "wait"
        elif "human_dyad_active" in social_signals and "bot_recently_spoke" not in social_signals:
            social_admission = "wait"
        elif action == "DROP":
            social_admission = "block"
        elif action == "FORCE_PASS":
            social_admission = "allow"
        else:
            social_admission = "judge"

        if previous.phase == "engaged" and "different_actor_observing" in signals:
            # Another participant may join the public topic, but must neither
            # inherit nor erase the committed target's short continuation lane.
            next_state = previous
        else:
            next_state = ParticipationState(
                phase=phase,
                actor_id=actor_id if phase == "engaged" else previous.actor_id,
                topic_epoch=topic_epoch,
                attention_topic_key=attention_topic_key,
                updated_at=timestamp,
            )
        return (
            ParticipationResult(
                action=action,
                reason=reason,
                score=score,
                signals=tuple(signals),
                phase=phase,
                phase_age_ms=phase_age_ms,
                invalidated_reason=invalidated_reason,
                strong_wakeup_event_ids=strong_ids,
                social_signals=social_signals,
                social_admission=social_admission,
                social_evidence=social_evidence,
                social_window_seconds=max(0.0, timestamp - min((_event_timestamp(item) for item in list(batch_events or ()) if _event_timestamp(item)), default=timestamp)),
                social_ttl_seconds=max(0.0, float(ttl_seconds or 0.0)),
                feedback_effect=feedback_effect,
                feedback_evidence=tuple(feedback_evidence),
            ),
            next_state,
        )


__all__ = [
    "ParticipationPolicy",
    "ParticipationResult",
    "ParticipationState",
]
