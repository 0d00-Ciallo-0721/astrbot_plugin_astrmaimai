from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any


class VectorResourceState(str, Enum):
    ACTIVE = "active"
    CANDIDATE_BUILDING = "candidate_building"
    CANDIDATE_READY = "candidate_ready"
    CUTOVER_PENDING = "cutover_pending"
    RETIRED_PENDING = "retired_pending"
    RETIRED_CLOSING = "retired_closing"
    RETIRED_RETRY_WAIT = "retired_retry_wait"
    RETIRED_EXHAUSTED = "retired_exhausted"
    OVERFLOW_PENDING = "overflow_pending"
    OVERFLOW_RETRY_WAIT = "overflow_retry_wait"
    REPAIR_PENDING = "repair_pending"
    REPAIR_RETRY_WAIT = "repair_retry_wait"
    REPAIR_BLOCKED = "repair_blocked"
    REPAIR_EXHAUSTED = "repair_exhausted"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    DEGRADED = "degraded"

    @property
    def terminal(self) -> bool:
        return self in {
            self.CLOSED,
            self.RETIRED_EXHAUSTED,
            self.REPAIR_BLOCKED,
            self.REPAIR_EXHAUSTED,
        }


_ALLOWED_TRANSITIONS: dict[VectorResourceState, frozenset[VectorResourceState]] = {
    VectorResourceState.CANDIDATE_BUILDING: frozenset({VectorResourceState.CANDIDATE_READY, VectorResourceState.RETIRED_PENDING}),
    VectorResourceState.CANDIDATE_READY: frozenset({VectorResourceState.CUTOVER_PENDING}),
    VectorResourceState.CUTOVER_PENDING: frozenset({VectorResourceState.ACTIVE, VectorResourceState.RETIRED_PENDING}),
    VectorResourceState.ACTIVE: frozenset({VectorResourceState.RETIRED_PENDING}),
    VectorResourceState.RETIRED_PENDING: frozenset({VectorResourceState.RETIRED_CLOSING}),
    VectorResourceState.RETIRED_CLOSING: frozenset({VectorResourceState.CLOSED, VectorResourceState.RETIRED_RETRY_WAIT}),
    VectorResourceState.RETIRED_RETRY_WAIT: frozenset({VectorResourceState.RETIRED_CLOSING, VectorResourceState.RETIRED_EXHAUSTED}),
    VectorResourceState.OVERFLOW_PENDING: frozenset({VectorResourceState.OVERFLOW_RETRY_WAIT, VectorResourceState.RETIRED_CLOSING}),
    VectorResourceState.OVERFLOW_RETRY_WAIT: frozenset({VectorResourceState.OVERFLOW_RETRY_WAIT, VectorResourceState.RETIRED_CLOSING, VectorResourceState.RETIRED_EXHAUSTED}),
    VectorResourceState.REPAIR_PENDING: frozenset({VectorResourceState.REPAIR_RETRY_WAIT, VectorResourceState.REPAIR_BLOCKED, VectorResourceState.REPAIR_EXHAUSTED, VectorResourceState.CLOSED}),
    VectorResourceState.REPAIR_RETRY_WAIT: frozenset({VectorResourceState.REPAIR_PENDING, VectorResourceState.REPAIR_EXHAUSTED, VectorResourceState.REPAIR_BLOCKED}),
}


def normalize_vector_resource_state(value: Any) -> VectorResourceState:
    try:
        return value if isinstance(value, VectorResourceState) else VectorResourceState(str(value or "unknown"))
    except (TypeError, ValueError):
        return VectorResourceState.UNKNOWN


def transition_vector_resource_state(current: Any, target: Any) -> tuple[bool, str]:
    source = normalize_vector_resource_state(current)
    destination = normalize_vector_resource_state(target)
    if source is VectorResourceState.UNKNOWN or destination is VectorResourceState.UNKNOWN:
        return False, "unknown_state"
    if source == destination:
        return True, "idempotent"
    if source.terminal:
        return False, "terminal_state"
    if destination in _ALLOWED_TRANSITIONS.get(source, frozenset()):
        return True, "allowed"
    return False, "invalid_transition"


@dataclass(frozen=True, slots=True)
class VectorResourceSnapshot:
    resource_id: str
    stack_id: str = ""
    generation: int = 0
    role: str = "unknown"
    state: VectorResourceState = VectorResourceState.UNKNOWN
    index_path: str = ""
    model_id: str = ""
    provider_source_fingerprint: str = ""
    dimension: int | None = None
    revision: int = 0
    owner_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    next_retry_at: float | None = None
    attempts: int = 0
    last_error: str = ""
    protected: bool = False
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = {"resource_id": self.resource_id, **{field: getattr(self, field) for field in self.__dataclass_fields__ if field != "resource_id"}}
        payload["state"] = self.state.value
        return payload

    def evolve(self, target: Any, **changes: Any) -> "VectorResourceSnapshot":
        allowed, reason = transition_vector_resource_state(self.state, target)
        if not allowed:
            raise ValueError(f"vector resource state transition rejected: {reason}")
        return replace(self, state=normalize_vector_resource_state(target), **changes)


RESOURCE_STATE_SCHEMA_VERSION = 1

