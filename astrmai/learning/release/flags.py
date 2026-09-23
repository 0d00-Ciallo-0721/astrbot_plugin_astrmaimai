from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
from typing import Mapping


FLAG_NAMES = (
    "learning_discovery_enabled",
    "learning_enrichment_enabled",
    "learning_quality_shadow_enabled",
    "learning_retrieval_shadow_enabled",
    "learning_injection_enabled",
    "learning_release_kill_switch",
)

def runtime_kill_switch_active(
    config_or_evolution: object | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Check the process override before the persisted configuration flag."""
    environ = os.environ if environ is None else environ
    if environ.get("ASTRMAI_LEARNING_KILL_SWITCH") == "1":
        return True
    evolution = getattr(config_or_evolution, "evolution", config_or_evolution)
    persistent_path = str(
        getattr(evolution, "learning_release_kill_switch_path", "") or ""
    ).strip()
    if persistent_path:
        release_id = str(
            getattr(evolution, "learning_release_id", "runtime") or "runtime"
        )
        try:
            state = PersistentKillSwitch(
                Path(persistent_path), release_id=release_id
            )._read()
        except ValueError:
            # A corrupted or unreadable persisted switch must fail closed at
            # every runtime boundary, including boolean compatibility callers.
            return True
        if state.get("active") or state.get("release_id") not in (release_id, None):
            return True
    return bool(getattr(evolution, "learning_release_kill_switch", False))


def register_runtime_kill_switch(switch: "PersistentKillSwitch") -> None:
    # Kept as an explicit registration hook for release orchestration. Formal
    # business paths read the configured path directly, avoiding process-global
    # state leaking between independent runtimes/tests.
    return None


def runtime_release_flags(config_or_evolution: object | None) -> "LearningReleaseFlags":
    evolution = getattr(config_or_evolution, "evolution", config_or_evolution)
    values = {
        "learning_discovery_enabled": getattr(evolution, "learning_discovery_enabled", True),
        "learning_enrichment_enabled": getattr(evolution, "learning_enrichment_enabled", False),
        "learning_quality_shadow_enabled": getattr(evolution, "learning_quality_shadow_enabled", True),
        "learning_retrieval_shadow_enabled": getattr(evolution, "learning_retrieval_shadow_enabled", False),
        "learning_injection_enabled": getattr(
            evolution,
            "learning_injection_enabled",
            getattr(evolution, "learning_prompt_injection_enabled", False),
        ),
        "learning_release_kill_switch": getattr(evolution, "learning_release_kill_switch", False),
    }
    legacy_injection = getattr(evolution, "learning_prompt_injection_enabled", values["learning_injection_enabled"])
    if type(legacy_injection) is not bool or legacy_injection != values["learning_injection_enabled"]:
        raise ValueError("learning injection flags disagree")
    return LearningReleaseFlags.from_mapping(values)


@dataclass(frozen=True, slots=True)
class LearningReleaseFlags:
    learning_discovery_enabled: bool = True
    learning_enrichment_enabled: bool = False
    learning_quality_shadow_enabled: bool = True
    learning_retrieval_shadow_enabled: bool = False
    learning_injection_enabled: bool = False
    learning_release_kill_switch: bool = False

    @classmethod
    def defaults(cls) -> "LearningReleaseFlags":
        return cls()

    @classmethod
    def from_config(cls, config_or_evolution: object | None) -> "LearningReleaseFlags":
        return runtime_release_flags(config_or_evolution)

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "LearningReleaseFlags":
        missing = [name for name in FLAG_NAMES if name not in values]
        if missing:
            raise ValueError(f"missing release flags: {', '.join(missing)}")
        unknown = sorted(set(values) - set(FLAG_NAMES))
        if unknown:
            raise ValueError(f"unknown release flags: {', '.join(unknown)}")
        if any(type(values[name]) is not bool for name in FLAG_NAMES):
            raise ValueError("release flags must be boolean")
        if values["learning_injection_enabled"] and not values["learning_retrieval_shadow_enabled"]:
            raise ValueError("injection and retrieval shadow flags conflict")
        return cls(**{name: values[name] for name in FLAG_NAMES})

    def as_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in FLAG_NAMES}


class ReleaseCheckpoint(str, Enum):
    DISCOVERY = "discovery"
    CLAIM = "claim"
    ADMISSION = "admission"
    RETRIEVAL = "retrieval"
    SEND = "send"


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    allowed: bool
    reason: str
    checkpoint: ReleaseCheckpoint


class PersistentKillSwitch:
    def __init__(self, path: Path, *, release_id: str):
        self.path = Path(path)
        self.release_id = release_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"active": False, "release_id": self.release_id}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("kill switch state is unreadable") from exc
        if not isinstance(value, dict) or type(value.get("active")) is not bool:
            raise ValueError("kill switch state is invalid")
        return value

    def trip(self, *, reason: str, operator_id: str) -> None:
        if not reason or not operator_id:
            raise ValueError("kill switch trip requires reason and operator")
        self.path.write_text(
            json.dumps({"active": True, "release_id": self.release_id, "reason": reason, "operator_id": operator_id}, sort_keys=True),
            encoding="utf-8",
        )
        register_runtime_kill_switch(self)

    def clear(self, *, operator_id: str, new_release_id: str) -> None:
        if not operator_id or not new_release_id or new_release_id == self.release_id:
            raise ValueError("clearing kill switch requires a new release")
        self.path.write_text(
            json.dumps({"active": False, "release_id": new_release_id, "operator_id": operator_id}, sort_keys=True),
            encoding="utf-8",
        )
        register_runtime_kill_switch(self)

    def checkpoint(self, checkpoint: ReleaseCheckpoint, *, environ: Mapping[str, str] | None = None) -> CheckpointResult:
        environ = os.environ if environ is None else environ
        if environ.get("ASTRMAI_LEARNING_KILL_SWITCH") == "1":
            return CheckpointResult(False, "environment_kill_switch", checkpoint)
        state = self._read()
        if state.get("active") or state.get("release_id") not in (self.release_id, None):
            return CheckpointResult(False, "persistent_kill_switch", checkpoint)
        return CheckpointResult(True, "allowed", checkpoint)
