from __future__ import annotations


def score_focus_candidate(gate, candidate, normalized_events):
    if candidate.is_self:
        return -10_000, "self_message"

    score = 20
    reason = "latest_user_message"
    attention_config = getattr(gate.config, "attention", None)
    same_speaker_window = int(getattr(getattr(gate.config, "attention", None), "thread_same_speaker_followup_sec", 8) or 8)
    reply_priority_enabled = bool(getattr(attention_config, "thread_reply_priority_enabled", True))
    is_historical = bool(candidate.event.get_extra("astrmai_attention_historical", False))

    if candidate.is_reply_to_bot and reply_priority_enabled and not is_historical:
        score += 1000
        reason = "reply_to_bot"
    elif candidate.is_at_bot and reply_priority_enabled and not is_historical:
        score += 800
        reason = "at_bot"
    elif candidate.is_direct_wakeup and reply_priority_enabled and not is_historical:
        score += 700
        reason = "direct_wakeup"
    elif candidate.has_direct_vision and not is_historical:
        score += 500
        reason = "direct_vision_request"
    elif candidate.is_near_context_query and not is_historical:
        score += 350
        reason = "near_context_followup"

    for previous in reversed(normalized_events[:candidate.index]):
        if previous.is_self:
            continue
        if previous.sender_id != candidate.sender_id:
            break
        if candidate.timestamp and previous.timestamp and (candidate.timestamp - previous.timestamp) > same_speaker_window:
            break
        score += 120
        if reason == "latest_user_message":
            reason = "same_sender_followup"
        break

    latest_ts = normalized_events[-1].timestamp if normalized_events else candidate.timestamp
    delta = max(0.0, (latest_ts - (candidate.timestamp or latest_ts))) if candidate.timestamp else 0.0
    recency_bonus = max(0, 90 - int(delta * 5))
    score += recency_bonus
    return score, reason


def select_focus_event(gate, events, self_id: str, normalized_events=None):
    if not events:
        return None, [], "empty"

    attention_config = getattr(gate.config, "attention", None)
    if not bool(getattr(attention_config, "focus_thread_enabled", True)):
        candidates = [event for event in events if str(event.get_sender_id()) != str(self_id)]
        if not candidates:
            focus_event = events[-1]
            return focus_event, [event for event in events if event is not focus_event], "fallback_last_event"
        for event in reversed(candidates):
            if not event.get_extra("astrmai_attention_historical", False) and gate._is_reply_to_bot_event(event, self_id):
                return event, [item for item in events if item is not event], "reply_to_bot"
        for event in reversed(candidates):
            if not event.get_extra("astrmai_attention_historical", False) and gate._is_direct_wakeup_event(event, self_id):
                return event, [item for item in events if item is not event], "direct_wakeup"
        focus_event = candidates[-1]
        return focus_event, [item for item in events if item is not focus_event], "fallback_last_event"

    if normalized_events is None:
        normalized_events = gate._build_normalized_events(events, self_id)
    candidates = [candidate for candidate in normalized_events if not candidate.is_self]
    if not candidates:
        focus_event = events[-1]
        return focus_event, [event for event in events if event is not focus_event], "fallback_last_event"

    scored = [
        (score_focus_candidate(gate, candidate, normalized_events), candidate)
        for candidate in candidates
    ]
    best = max(scored, key=lambda item: (item[0][0], item[1].index))
    focus_reason = best[0][1]
    focus_event = best[1].event
    return focus_event, [item for item in events if item is not focus_event], focus_reason


__all__ = [
    "score_focus_candidate",
    "select_focus_event",
]
