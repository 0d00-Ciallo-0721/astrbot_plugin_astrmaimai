"""Process-wide outbound side-effect gate for AstrMai lifecycle transitions."""

from __future__ import annotations

import threading
from typing import Any


class OutboundSendGate:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._accepting = False
        self._generation = 0
        self._provider_enforced = False

    def open(self) -> int:
        with self._lock:
            self._generation += 1
            self._accepting = True
            self._provider_enforced = False
            return self._generation

    def close(self, *, enforce_provider: bool = False) -> int:
        with self._lock:
            self._generation += 1
            self._accepting = False
            # Tests and standalone integrations may use close() merely to
            # reset the process gate.  Lifecycle shutdown opts into the
            # stricter provider fence explicitly so unbound background calls
            # are rejected after unload.
            self._provider_enforced = bool(enforce_provider)
            return self._generation

    def snapshot(self) -> tuple[bool, int]:
        with self._lock:
            return self._accepting, self._generation

    def provider_fence_enforced(self) -> bool:
        with self._lock:
            return self._provider_enforced

    def allowed(self, event: Any = None, generation: int | None = None) -> bool:
        accepting, current_generation = self.snapshot()
        # Before the lifecycle manager has ever opened the gate (generation
        # zero), standalone tool/unit invocations may not carry runtime
        # metadata. Preserve that legacy behavior; once a lifecycle transition
        # occurred, a closed gate is always fail-closed.
        if not accepting and current_generation == 0 and generation is None:
            getter = getattr(event, "get_extra", None)
            bound = getter("astrmai_outbound_generation", None) if callable(getter) else None
            if event is None or bound in (None, ""):
                return True
        if not accepting:
            if self.provider_fence_enforced():
                # Lifecycle shutdown is a hard fence. Do not let synthetic or
                # unbound continuations use the legacy compatibility path.
                return False
            if event is not None and generation is None:
                getter = getattr(event, "get_extra", None)
                bound = getter("astrmai_outbound_generation", None) if callable(getter) else None
                # Synthetic/unit events that never traversed ingress have no
                # generation token. Keep them compatible; real ingress events
                # are always bound and therefore fail closed above.
                if bound in (None, ""):
                    return True
            return False
        if generation is not None and int(generation) != current_generation:
            return False
        if event is not None:
            getter = getattr(event, "get_extra", None)
            if callable(getter):
                bound = getter("astrmai_outbound_generation", None)
                if bound not in (None, ""):
                    try:
                        if int(bound) != current_generation:
                            return False
                    except (TypeError, ValueError):
                        return False
        return True


OUTBOUND_SEND_GATE = OutboundSendGate()


def bind_event_generation(event: Any) -> int:
    _, generation = OUTBOUND_SEND_GATE.snapshot()
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        try:
            setter("astrmai_outbound_generation", generation)
        except Exception:
            pass
    return generation


def outbound_send_allowed(event: Any = None, generation: int | None = None) -> bool:
    return OUTBOUND_SEND_GATE.allowed(event, generation)


def provider_request_allowed(event: Any = None, generation: int | None = None) -> bool:
    """Return whether an external provider request may start.

    Real ingress events carry ``astrmai_outbound_generation``. Once the
    lifecycle gate has transitioned, those events are fail-closed so a late
    continuation cannot start another network request. Legacy integrations
    that have no event object remain compatible; callers that own a lifecycle
    generation should pass it explicitly.
    """
    allowed = OUTBOUND_SEND_GATE.allowed(event, generation)
    if allowed:
        return True
    accepting, _ = OUTBOUND_SEND_GATE.snapshot()
    if event is None and generation is None and not accepting:
        # Once lifecycle shutdown opts into the strict provider fence, even
        # legacy/no-event background calls must stop. Before that explicit
        # transition, preserve standalone integrations that have no event.
        return not OUTBOUND_SEND_GATE.provider_fence_enforced()
    return False


__all__ = [
    "OUTBOUND_SEND_GATE",
    "bind_event_generation",
    "outbound_send_allowed",
    "provider_request_allowed",
]
