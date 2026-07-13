from __future__ import annotations

import time
from typing import Any


class AttentionWindowBuffer:
    """Per-chat message buffering, debounce timing, and attention-window retention."""

    def __init__(self, gate: Any):
        self.gate = gate

    def compute_debounce_delay(self, session: Any, is_private: bool, is_strong_wakeup: bool) -> float:
        if is_strong_wakeup:
            return 0.10
        if is_private:
            return 0.20
        gap = time.time() - float(getattr(session, "last_active_user_time", 0.0) or 0.0)
        if gap < 1.0:
            return 0.25
        if gap < 5.0:
            return 0.45
        return 0.70

    def prune(self, session: Any, now: float | None = None) -> list[Any]:
        now = time.time() if now is None else now
        retained_events: list[Any] = []
        retained_ts: list[float] = []
        for event, timestamp in zip(list(session.attention_window), list(session.attention_window_ts)):
            try:
                event_ts = float(timestamp or 0.0)
            except (TypeError, ValueError):
                event_ts = 0.0
            if event_ts <= 0.0 or now - event_ts > self.gate.ATTENTION_WINDOW_TTL_SECONDS:
                continue
            retained_events.append(event)
            retained_ts.append(event_ts)
        if len(retained_events) > self.gate.ATTENTION_WINDOW_MAX_EVENTS:
            retained_events = retained_events[-self.gate.ATTENTION_WINDOW_MAX_EVENTS :]
            retained_ts = retained_ts[-self.gate.ATTENTION_WINDOW_MAX_EVENTS :]
        session.attention_window = retained_events
        session.attention_window_ts = retained_ts
        return retained_events[:]

    def append(self, session: Any, events: list[Any], timestamp: float | None = None) -> None:
        timestamp = time.time() if timestamp is None else timestamp
        self.prune(session, now=timestamp)
        seen_ids = {self.gate._build_message_id(event) for event in session.attention_window}
        for event in events:
            event_id = self.gate._build_message_id(event)
            if event_id in seen_ids:
                continue
            session.attention_window.append(event)
            session.attention_window_ts.append(timestamp)
            seen_ids.add(event_id)
        if len(session.attention_window) > self.gate.ATTENTION_WINDOW_MAX_EVENTS:
            session.attention_window = session.attention_window[-self.gate.ATTENTION_WINDOW_MAX_EVENTS :]
            session.attention_window_ts = session.attention_window_ts[-self.gate.ATTENTION_WINDOW_MAX_EVENTS :]

    def merge(self, session: Any, batch_events: list[Any]) -> list[Any]:
        window_events = self.prune(session)
        batch_ids = {self.gate._build_message_id(event) for event in batch_events}
        for event in window_events:
            if hasattr(event, "set_extra"):
                event.set_extra(
                    "astrmai_attention_historical",
                    self.gate._build_message_id(event) not in batch_ids,
                )
        for event in batch_events:
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_attention_historical", False)
        merged_events: list[Any] = []
        seen_ids: set[str] = set()
        for event in list(window_events) + list(batch_events):
            event_id = self.gate._build_message_id(event)
            if event_id in seen_ids:
                continue
            merged_events.append(event)
            seen_ids.add(event_id)
        return merged_events


__all__ = ["AttentionWindowBuffer"]
