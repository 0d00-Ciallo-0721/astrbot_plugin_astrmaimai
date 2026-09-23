from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Mapping

from .flags import (
    LearningReleaseFlags,
    PersistentKillSwitch,
    ReleaseCheckpoint,
    register_runtime_kill_switch,
    runtime_release_flags,
    runtime_kill_switch_active,
)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: str


class LearningReleaseGate:
    """A narrow checkpoint guard; it never owns a provider or a second attempt id."""

    def __init__(self, flags: LearningReleaseFlags, kill_switch: PersistentKillSwitch):
        self.flags = flags
        self.kill_switch = kill_switch
        register_runtime_kill_switch(kill_switch)

    @classmethod
    def from_config(cls, config_or_evolution: object | None) -> "LearningReleaseGate":
        flags = runtime_release_flags(config_or_evolution)
        evolution = getattr(config_or_evolution, "evolution", config_or_evolution)
        release_id = str(getattr(evolution, "learning_release_id", "runtime") or "runtime")
        raw_path = getattr(evolution, "learning_release_kill_switch_path", "")
        path = Path(raw_path) if raw_path else Path(tempfile.gettempdir()) / "astrmai-learning-kill-switch.json"
        return cls(flags, PersistentKillSwitch(path, release_id=release_id))

    def check(self, checkpoint: ReleaseCheckpoint, *, environ: Mapping[str, str] | None = None) -> GateDecision:
        kill = self.kill_switch.checkpoint(checkpoint, environ=environ)
        if not kill.allowed:
            return GateDecision(False, kill.reason)
        enabled = {
            ReleaseCheckpoint.DISCOVERY: self.flags.learning_discovery_enabled,
            ReleaseCheckpoint.CLAIM: self.flags.learning_enrichment_enabled,
            ReleaseCheckpoint.ADMISSION: self.flags.learning_injection_enabled,
            ReleaseCheckpoint.RETRIEVAL: self.flags.learning_retrieval_shadow_enabled,
            ReleaseCheckpoint.SEND: self.flags.learning_injection_enabled,
        }[checkpoint]
        if not enabled:
            return GateDecision(False, f"flag_disabled:{checkpoint.value}")
        return GateDecision(True, "allowed")


def runtime_gate_check(
    config_or_evolution: object | None,
    checkpoint: ReleaseCheckpoint,
    *,
    environ: Mapping[str, str] | None = None,
) -> GateDecision:
    """Evaluate the exact release flags used by formal runtime boundaries."""
    if runtime_kill_switch_active(config_or_evolution, environ=environ):
        return GateDecision(False, "learning_release_kill_switch")
    evolution = getattr(config_or_evolution, "evolution", config_or_evolution)
    release_fields = {
        "learning_discovery_enabled",
        "learning_enrichment_enabled",
        "learning_quality_shadow_enabled",
        "learning_retrieval_shadow_enabled",
        "learning_injection_enabled",
        "learning_release_kill_switch",
    }
    if not any(hasattr(evolution, name) for name in release_fields):
        # Legacy test doubles and pre-Stage-10 callers have no release config;
        # retain their existing behavior while real config remains fail-closed.
        return GateDecision(True, "legacy_compatibility")
    try:
        flags = runtime_release_flags(config_or_evolution)
    except ValueError as exc:
        return GateDecision(False, f"release_flag_invalid:{exc}")
    enabled = {
        ReleaseCheckpoint.DISCOVERY: flags.learning_discovery_enabled,
        ReleaseCheckpoint.CLAIM: flags.learning_enrichment_enabled,
        ReleaseCheckpoint.ADMISSION: flags.learning_injection_enabled,
        ReleaseCheckpoint.RETRIEVAL: flags.learning_retrieval_shadow_enabled,
        ReleaseCheckpoint.SEND: flags.learning_injection_enabled,
    }[checkpoint]
    if not enabled:
        return GateDecision(False, f"flag_disabled:{checkpoint.value}")
    return GateDecision(True, "allowed")
