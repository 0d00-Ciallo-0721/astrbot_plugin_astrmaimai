from __future__ import annotations

import time
from typing import Any

from ..contracts.vision_candidate import load_vision_candidates


class AttentionWindowBuffer:
    """Per-chat message buffering, debounce timing, and attention-window retention."""

    def __init__(self, gate: Any):
        self.gate = gate

    def compute_debounce_delay(self, session: Any, is_private: bool, is_strong_wakeup: bool) -> float:
        if is_private:
            private_config = getattr(getattr(self.gate, "config", None), "private_chat", None)
            return max(0.0, float(getattr(private_config, "input_settle_sec", 1.5) or 0.0))
        if is_strong_wakeup:
            return 0.10
        gap = time.time() - float(getattr(session, "last_active_user_time", 0.0) or 0.0)
        if gap < 1.0:
            return 0.25
        if gap < 5.0:
            return 0.45
        return 0.70

    def pair_window_seconds(self) -> float:
        vision_config = getattr(getattr(self.gate, "config", None), "vision", None)
        configured = getattr(vision_config, "at_image_pair_window_sec", 3.0)
        try:
            return max(0.5, min(float(configured or 3.0), 10.0))
        except (TypeError, ValueError):
            return 3.0

    @staticmethod
    def _event_candidates(event: Any) -> list[dict[str, Any]]:
        getter = getattr(event, "get_extra", None)
        if not callable(getter):
            return []
        return [candidate.as_dict() for candidate in load_vision_candidates(getter("astrmai_vision_candidates", []) or [])]

    @staticmethod
    def _set_pairing(event: Any, candidates: list[dict[str, Any]], mode: str) -> None:
        loaded = load_vision_candidates(candidates)
        selected = [candidate.with_selection(selected=True, pairing_mode=mode) for candidate in loaded]
        refs = [candidate.primary_ref for candidate in selected if candidate.primary_ref]
        event.set_extra("astrmai_vision_candidates", [candidate.as_dict() for candidate in selected])
        event.set_extra("extracted_image_refs", refs)
        event.set_extra("extracted_image_urls", refs)
        event.set_extra("vision_prefilter_selected", bool(selected))
        event.set_extra("vision_direct_skip_reason", "deferred_to_reply")
        event.set_extra("astrmai_vision_pairing_mode", mode)
        event.set_extra("astrmai_cross_message_vision_bound", mode in {"at_then_image", "image_then_at"})

    @staticmethod
    def _prune_pairing(session: Any, now: float) -> None:
        for attr_name in ("pending_vision_images", "pending_vision_mentions"):
            pending = getattr(session, attr_name, None)
            if not isinstance(pending, dict):
                continue
            for sender_id, item in list(pending.items()):
                if float(item.get("expires_at", 0.0) or 0.0) <= now:
                    pending.pop(sender_id, None)

    def register_group_vision_pairing(
        self,
        session: Any,
        event: Any,
        *,
        sender_id: str,
        is_at_bot: bool,
    ) -> str:
        """Bind split pure-@ and image messages for one sender in one group session."""

        now = time.monotonic()
        self._prune_pairing(session, now)
        candidates = self._event_candidates(event)
        pure_at = bool(event.get_extra("astrmai_pure_at_bot", False))
        image_only = bool(candidates) and not str(getattr(event, "message_str", "") or "").strip()
        expires_at = now + self.pair_window_seconds()

        if candidates and is_at_bot:
            mode = "same_message_reply" if any(
                str(item.get("source_kind") or "") == "reply" for item in candidates
            ) else "same_message"
            self._set_pairing(event, candidates, mode)
            session.pending_vision_images.pop(sender_id, None)
            session.pending_vision_mentions.pop(sender_id, None)
            session.vision_pair_signal.set()
            return mode

        if pure_at:
            pending_image = session.pending_vision_images.pop(sender_id, None)
            if pending_image is not None:
                self._set_pairing(event, list(pending_image.get("candidates", []) or []), "image_then_at")
                event.set_extra("astrmai_release_vision_pair_waiter", True)
                return "image_then_at"
            session.pending_vision_mentions[sender_id] = {
                "event": event,
                "expires_at": expires_at,
            }
            event.set_extra("astrmai_wait_for_image_pair", True)
            session.vision_pair_signal.clear()
            return "pending_at"

        if candidates and not is_at_bot and image_only:
            pending_at = session.pending_vision_mentions.pop(sender_id, None)
            if pending_at is not None:
                mention_event = pending_at.get("event")
                if mention_event is not None:
                    self._set_pairing(mention_event, candidates, "at_then_image")
                self._set_pairing(event, candidates, "at_then_image")
                event.set_extra("astrmai_group_direct_wakeup", True)
                event.set_extra("astrmai_at_bot_wakeup", True)
                event.set_extra("astrmai_release_vision_pair_waiter", True)
                return "at_then_image"
            session.pending_vision_images[sender_id] = {
                "candidates": candidates,
                "expires_at": expires_at,
            }
            return "pending_image"

        return "none"

    def pending_pair_wait_seconds(
        self,
        session: Any,
        *,
        sender_ids: set[str] | None = None,
    ) -> float:
        now = time.monotonic()
        self._prune_pairing(session, now)
        deadlines = [
            float(item.get("expires_at", 0.0) or 0.0)
            for pending in (
                getattr(session, "pending_vision_images", {}),
                getattr(session, "pending_vision_mentions", {}),
            )
            for sender_id, item in pending.items()
            if sender_ids is None or sender_id in sender_ids
        ]
        return max(0.0, max(deadlines, default=0.0) - now)

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
