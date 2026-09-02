"""Validated runtime lifecycle state transitions.

The existing granular ``boot_phase`` remains available for compatibility. This
module supplies the coarser state machine used by status and lifecycle guards.
"""

from __future__ import annotations

from enum import Enum


class RuntimeLifecycleState(str, Enum):
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    RETRY_WAIT = "retry_wait"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    DRAINING = "draining"
    SHUTDOWN_COMPLETE = "shutdown_complete"
    FAILED = "failed"


LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    RuntimeLifecycleState.CREATED.value: frozenset(
        {RuntimeLifecycleState.INITIALIZING.value, RuntimeLifecycleState.SHUTDOWN_REQUESTED.value, RuntimeLifecycleState.FAILED.value}
    ),
    RuntimeLifecycleState.INITIALIZING.value: frozenset(
        {
            RuntimeLifecycleState.READY.value,
            RuntimeLifecycleState.DEGRADED.value,
            RuntimeLifecycleState.RETRY_WAIT.value,
            RuntimeLifecycleState.SHUTDOWN_REQUESTED.value,
            RuntimeLifecycleState.FAILED.value,
        }
    ),
    RuntimeLifecycleState.READY.value: frozenset(
        {
            RuntimeLifecycleState.INITIALIZING.value,
            RuntimeLifecycleState.DEGRADED.value,
            RuntimeLifecycleState.RETRY_WAIT.value,
            RuntimeLifecycleState.SHUTDOWN_REQUESTED.value,
        }
    ),
    RuntimeLifecycleState.DEGRADED.value: frozenset(
        {
            RuntimeLifecycleState.INITIALIZING.value,
            RuntimeLifecycleState.RETRY_WAIT.value,
            RuntimeLifecycleState.READY.value,
            RuntimeLifecycleState.SHUTDOWN_REQUESTED.value,
            RuntimeLifecycleState.FAILED.value,
        }
    ),
    RuntimeLifecycleState.RETRY_WAIT.value: frozenset(
        {
            RuntimeLifecycleState.INITIALIZING.value,
            RuntimeLifecycleState.SHUTDOWN_REQUESTED.value,
            RuntimeLifecycleState.FAILED.value,
            RuntimeLifecycleState.DEGRADED.value,
        }
    ),
    RuntimeLifecycleState.SHUTDOWN_REQUESTED.value: frozenset(
        {RuntimeLifecycleState.DRAINING.value, RuntimeLifecycleState.FAILED.value}
    ),
    RuntimeLifecycleState.DRAINING.value: frozenset(
        {
            RuntimeLifecycleState.SHUTDOWN_COMPLETE.value,
            RuntimeLifecycleState.DEGRADED.value,
            RuntimeLifecycleState.SHUTDOWN_REQUESTED.value,
        }
    ),
    RuntimeLifecycleState.SHUTDOWN_COMPLETE.value: frozenset(
        {RuntimeLifecycleState.INITIALIZING.value, RuntimeLifecycleState.CREATED.value}
    ),
    RuntimeLifecycleState.FAILED.value: frozenset(
        {
            RuntimeLifecycleState.RETRY_WAIT.value,
            RuntimeLifecycleState.INITIALIZING.value,
            RuntimeLifecycleState.SHUTDOWN_REQUESTED.value,
        }
    ),
}


def normalize_lifecycle_state(value: str | RuntimeLifecycleState) -> str | None:
    try:
        normalized = value.value if isinstance(value, RuntimeLifecycleState) else str(value).strip().lower()
    except Exception:
        return None
    return normalized if normalized in LIFECYCLE_TRANSITIONS else None


def can_transition(current: str | RuntimeLifecycleState, target: str | RuntimeLifecycleState) -> bool:
    current_value = normalize_lifecycle_state(current)
    target_value = normalize_lifecycle_state(target)
    if current_value is None or target_value is None:
        return False
    return current_value == target_value or target_value in LIFECYCLE_TRANSITIONS[current_value]


__all__ = [
    "LIFECYCLE_TRANSITIONS",
    "RuntimeLifecycleState",
    "can_transition",
    "normalize_lifecycle_state",
]
