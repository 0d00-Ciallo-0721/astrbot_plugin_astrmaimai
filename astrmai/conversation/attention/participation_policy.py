from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable


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


def _topic_epoch(event: Any) -> int:
    canonical = getattr(event, "get_extra", lambda *_args: None)(
        "astrmai_conversation_event",
        None,
    )
    if canonical is not None:
        return max(0, int(getattr(canonical, "topic_epoch", 0) or 0))
    policy = getattr(event, "get_extra", lambda *_args: None)(
        "astrmai_dialog_history_policy",
        None,
    )
    value = (
        policy.get("topic_epoch", 0)
        if isinstance(policy, dict)
        else getattr(policy, "topic_epoch", 0)
    )
    return max(0, int(value or 0))


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
        ttl_seconds: float = 180.0,
        now: float | None = None,
    ) -> tuple[ParticipationResult, ParticipationState]:
        timestamp = float(now if now is not None else (_event_timestamp(focus_event) or time.time()))
        actor_id = _actor_id(focus_event)
        topic_epoch = _topic_epoch(focus_event)
        text = _event_text(focus_event).lower()
        strong_ids = tuple(dict.fromkeys(str(value) for value in strong_wakeup_event_ids if str(value)))
        score = 0
        signals: list[str] = []
        invalidated_reason = ""
        phase_age_ms = 0

        previous = previous_state or ParticipationState()
        if previous.updated_at > 0.0:
            phase_age_ms = max(0, int((timestamp - previous.updated_at) * 1000))
            if timestamp - previous.updated_at > max(1.0, float(ttl_seconds or 180.0)):
                invalidated_reason = "ttl_expired"
                previous = ParticipationState()
            elif (
                previous.topic_epoch > 0
                and topic_epoch > 0
                and previous.topic_epoch != topic_epoch
            ):
                invalidated_reason = "topic_epoch_changed"
                previous = ParticipationState()

        if strong_ids:
            score += 100
            signals.append("owned_batch_strong_wakeup")

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
            committed_ts = float(getattr(committed, "timestamp", 0.0) or 0.0)
            committed_age = timestamp - committed_ts if committed_ts > 0.0 else 0.0
            same_actor = bool(actor_id and committed_actor == actor_id)
            same_topic = bool(
                topic_epoch <= 0
                or committed_topic <= 0
                or topic_epoch == committed_topic
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
                topic_epoch <= 0
                or previous.topic_epoch <= 0
                or previous.topic_epoch == topic_epoch
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

        if previous.phase == "engaged" and "different_actor_observing" in signals:
            # Another participant may join the public topic, but must neither
            # inherit nor erase the committed target's short continuation lane.
            next_state = previous
        else:
            next_state = ParticipationState(
                phase=phase,
                actor_id=actor_id if phase == "engaged" else previous.actor_id,
                topic_epoch=topic_epoch if topic_epoch > 0 else previous.topic_epoch,
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
            ),
            next_state,
        )


__all__ = [
    "ParticipationPolicy",
    "ParticipationResult",
    "ParticipationState",
]
