from __future__ import annotations

from typing import Any, Iterable

from ..contracts.conversation_event import ConversationEvent
from ..contracts.turn_target import ActorSet, TargetKind, TurnTarget


def _ordered_unique(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _canonical(candidate: Any) -> ConversationEvent | None:
    value = getattr(candidate, "canonical_event", None)
    if isinstance(value, ConversationEvent):
        return value
    event = getattr(candidate, "event", None)
    value = getattr(event, "get_extra", lambda _key, default=None: default)(
        "astrmai_conversation_event",
        None,
    )
    return value if isinstance(value, ConversationEvent) else None


def _actor_name(actor_id: str, normalized_events: Iterable[Any], fallback: str = "") -> str:
    normalized_id = str(actor_id or "").strip()
    for candidate in reversed(list(normalized_events or [])):
        if str(getattr(candidate, "sender_id", "") or "").strip() == normalized_id:
            return str(getattr(candidate, "sender_name", "") or "").strip()
    return str(fallback or "").strip()


def resolve_turn_target(
    focus_candidate: Any,
    root_candidate: Any,
    normalized_events: Iterable[Any],
    *,
    bot_id: str = "",
) -> tuple[TurnTarget, ActorSet]:
    events = list(normalized_events or [])
    canonical = _canonical(focus_candidate)
    current_actor_id = str(getattr(focus_candidate, "sender_id", "") or "").strip()
    current_actor_name = str(getattr(focus_candidate, "sender_name", "") or "").strip()
    normalized_bot_id = str(bot_id or "").strip()
    at_actor_ids = _ordered_unique(
        actor_id
        for actor_id in (canonical.at_actor_ids if canonical else ())
        if str(actor_id or "").strip() != normalized_bot_id
    )
    reply_actor_id = str(
        (canonical.reply_target_actor_id if canonical else "")
        or getattr(focus_candidate, "reply_target_sender_id", "")
        or ""
    ).strip()
    reply_actor_name = str(
        (canonical.reply_target_actor_name if canonical else "")
        or getattr(focus_candidate, "reply_target_sender_name", "")
        or ""
    ).strip()
    reply_event_id = str(canonical.reply_target_event_id if canonical else "").strip()
    is_reply_to_bot = bool(
        getattr(focus_candidate, "is_reply_to_bot", False)
        or (canonical.is_reply_to_bot if canonical else False)
        or (reply_actor_id and reply_actor_id == normalized_bot_id)
    )
    is_direct_at = bool(
        getattr(focus_candidate, "is_at_bot", False)
        or (canonical.is_at_bot if canonical else False)
    )
    is_direct_wakeup = bool(
        getattr(focus_candidate, "is_direct_wakeup", False)
        or (canonical.is_direct_wakeup if canonical else False)
    )
    interaction_kind = str(canonical.interaction_kind if canonical else "").strip()
    topic_epoch = max(0, int(canonical.topic_epoch if canonical else 0))
    source_event_ids = _ordered_unique(
        (
            *((canonical.source_event_ids if canonical else ()) or ()),
            canonical.event_id if canonical else "",
        )
    )
    created_at = float(
        (canonical.timestamp if canonical else 0.0)
        or getattr(focus_candidate, "timestamp", 0.0)
        or 0.0
    )

    target_kind = TargetKind.NONE
    target_actor_id = ""
    target_actor_name = ""
    target_event_id = canonical.event_id if canonical else ""
    evidence = ""
    confidence = 0.0

    if reply_event_id and not is_reply_to_bot:
        target_kind = TargetKind.MESSAGE
        target_actor_id = reply_actor_id
        target_actor_name = _actor_name(reply_actor_id, events, reply_actor_name)
        target_event_id = reply_event_id
        evidence = "reply"
        confidence = 1.0 if reply_actor_id else 0.9
    elif is_reply_to_bot:
        target_kind = TargetKind.ACTOR
        target_actor_id = current_actor_id
        target_actor_name = current_actor_name
        target_event_id = reply_event_id or target_event_id
        evidence = "reply_to_bot"
        confidence = 1.0
    elif is_direct_at:
        target_kind = TargetKind.ACTOR
        target_actor_id = current_actor_id
        target_actor_name = current_actor_name
        evidence = "direct_at"
        confidence = 1.0
    elif interaction_kind:
        target_kind = TargetKind.ACTOR
        target_actor_id = current_actor_id
        target_actor_name = current_actor_name
        evidence = "interaction"
        confidence = 0.95
    elif is_direct_wakeup:
        target_kind = TargetKind.ACTOR
        target_actor_id = current_actor_id
        target_actor_name = current_actor_name
        evidence = "direct_wakeup"
        confidence = 0.95
    elif current_actor_id:
        target_kind = TargetKind.ACTOR
        target_actor_id = current_actor_id
        target_actor_name = current_actor_name
        evidence = "focus"
        confidence = 0.55

    explicit_target_actor_ids = _ordered_unique(
        (
            reply_actor_id if reply_actor_id and reply_actor_id != normalized_bot_id else "",
            *at_actor_ids,
        )
    )
    quoted_actor_ids = _ordered_unique(
        (reply_actor_id,)
        if reply_actor_id and reply_actor_id != normalized_bot_id
        else ()
    )
    recent_topic_actor_ids: tuple[str, ...] = ()
    if topic_epoch > 0:
        recent_topic_actor_ids = _ordered_unique(
            getattr(candidate, "sender_id", "")
            for candidate in events
            if candidate is not focus_candidate
            and str(getattr(candidate, "sender_id", "") or "").strip()
            not in {current_actor_id, normalized_bot_id}
            and int((_canonical(candidate).topic_epoch if _canonical(candidate) else 0) or 0)
            == topic_epoch
        )[-8:]

    actor_set = ActorSet(
        current_actor_id=current_actor_id,
        explicit_target_actor_ids=explicit_target_actor_ids,
        at_actor_ids=at_actor_ids,
        quoted_actor_ids=quoted_actor_ids,
        recent_topic_actor_ids=recent_topic_actor_ids,
        bot_id=normalized_bot_id,
    )
    return (
        TurnTarget(
            target_kind=target_kind,
            target_actor_id=target_actor_id,
            target_actor_name=target_actor_name,
            target_event_id=target_event_id,
            topic_epoch=topic_epoch,
            source_event_ids=source_event_ids,
            evidence=evidence,
            confidence=confidence,
            created_at=created_at,
        ),
        actor_set,
    )


def resolve_legacy_turn_target(
    focus_candidate: Any,
    root_candidate: Any,
    normalized_events: Iterable[Any],
    *,
    bot_id: str = "",
) -> tuple[TurnTarget, ActorSet]:
    events = list(normalized_events or [])
    current_actor_id = str(getattr(focus_candidate, "sender_id", "") or "").strip()
    current_actor_name = str(getattr(focus_candidate, "sender_name", "") or "").strip()
    reply_actor_id = str(
        getattr(focus_candidate, "reply_target_sender_id", "") or ""
    ).strip()
    reply_actor_name = str(
        getattr(focus_candidate, "reply_target_sender_name", "") or ""
    ).strip()
    normalized_bot_id = str(bot_id or "").strip()
    is_reply_to_bot = bool(
        getattr(focus_candidate, "is_reply_to_bot", False)
        or (reply_actor_id and reply_actor_id == normalized_bot_id)
    )
    is_direct = bool(
        getattr(focus_candidate, "is_at_bot", False)
        or getattr(focus_candidate, "is_direct_wakeup", False)
        or is_reply_to_bot
    )
    if reply_actor_id and not is_reply_to_bot:
        target_kind = TargetKind.ACTOR
        target_actor_id = reply_actor_id
        target_actor_name = _actor_name(reply_actor_id, events, reply_actor_name)
        evidence = "legacy_reply_actor"
        confidence = 0.8
    elif current_actor_id:
        target_kind = TargetKind.ACTOR
        target_actor_id = current_actor_id
        target_actor_name = current_actor_name
        evidence = "legacy_direct_focus" if is_direct else "legacy_focus"
        confidence = 0.75 if is_direct else 0.5
    else:
        target_kind = TargetKind.NONE
        target_actor_id = ""
        target_actor_name = ""
        evidence = "legacy_unknown"
        confidence = 0.0
    explicit_ids = _ordered_unique(
        (reply_actor_id,)
        if reply_actor_id and reply_actor_id != normalized_bot_id
        else ()
    )
    actor_set = ActorSet(
        current_actor_id=current_actor_id,
        explicit_target_actor_ids=explicit_ids,
        quoted_actor_ids=explicit_ids,
        bot_id=normalized_bot_id,
    )
    return (
        TurnTarget(
            target_kind=target_kind,
            target_actor_id=target_actor_id,
            target_actor_name=target_actor_name,
            evidence=evidence,
            confidence=confidence,
            created_at=float(getattr(focus_candidate, "timestamp", 0.0) or 0.0),
        ),
        actor_set,
    )


__all__ = ["resolve_legacy_turn_target", "resolve_turn_target"]
