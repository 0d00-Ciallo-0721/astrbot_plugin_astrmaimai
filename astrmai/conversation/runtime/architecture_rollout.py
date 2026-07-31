from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Mapping


ARCHITECTURE_OBSERVATION_EXTRA_KEY = "astrmai_architecture_observation"


def rollout_config(config: Any) -> Any:
    return getattr(config, "architecture_rollout", None)


def rollout_enabled(config: Any, field_name: str, default: bool = True) -> bool:
    section = rollout_config(config)
    return bool(getattr(section, field_name, default)) if section is not None else default


def rollout_state(config: Any) -> dict[str, bool]:
    attention = getattr(config, "attention", None)
    return {
        "shadow_enabled": rollout_enabled(config, "shadow_enabled", True),
        "canonical_read_enabled": rollout_enabled(config, "canonical_read_enabled", True),
        "turn_target_read_enabled": rollout_enabled(config, "turn_target_read_enabled", True),
        "committed_history_enabled": rollout_enabled(config, "committed_history_enabled", True),
        "context_renderer_enabled": rollout_enabled(config, "context_renderer_enabled", True),
        "participation_force_pass_enabled": bool(
            getattr(attention, "participation_force_pass_enabled", True)
        ),
        "memory_actor_filter_enabled": rollout_enabled(
            config, "memory_actor_filter_enabled", True
        ),
        "participation_drop_enabled": bool(
            getattr(attention, "participation_drop_enabled", False)
        ),
        "proactive_due_enabled": rollout_enabled(config, "proactive_due_enabled", True),
    }


def stable_structure_hash(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = repr(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def get_architecture_observation(event: Any) -> dict[str, Any]:
    if event is None or not hasattr(event, "get_extra"):
        return {}
    value = event.get_extra(ARCHITECTURE_OBSERVATION_EXTRA_KEY, {})
    return dict(value) if isinstance(value, Mapping) else {}


def record_architecture_observation(
    event: Any,
    section: str,
    payload: Mapping[str, Any],
) -> None:
    if event is None or not hasattr(event, "set_extra"):
        return
    observation = get_architecture_observation(event)
    observation[str(section or "unknown")] = dict(payload or {})
    event.set_extra(ARCHITECTURE_OBSERVATION_EXTRA_KEY, observation)


class ArchitectureTimer:
    def __init__(self) -> None:
        self._started = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000.0, 3)


__all__ = [
    "ARCHITECTURE_OBSERVATION_EXTRA_KEY",
    "ArchitectureTimer",
    "get_architecture_observation",
    "record_architecture_observation",
    "rollout_enabled",
    "rollout_state",
    "stable_structure_hash",
]
