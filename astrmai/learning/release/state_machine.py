from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, Enum

from .contracts import ReleaseManifest


class ReleasePhase(IntEnum):
    P0_PREFLIGHT = 0
    P1_DISCOVERY_SHADOW = 1
    P2_RECORDED_ENRICHMENT = 2
    P3_RETRIEVAL_SHADOW = 3
    P4_ONE_SCOPE_CANARY = 4
    P5_MANUAL_EXPANSION = 5


@dataclass(frozen=True, slots=True)
class ManualApproval:
    release_id: str
    manifest_sha256: str
    target_phase: ReleasePhase
    operator_id: str
    reviewer_id: str
    approved_at: str


@dataclass(frozen=True, slots=True)
class TransitionResult:
    applied: bool
    reason: str
    phase: ReleasePhase


@dataclass(frozen=True, slots=True)
class TransitionAudit:
    from_phase: ReleasePhase
    to_phase: ReleasePhase
    release_id: str
    manifest_sha256: str
    operator_id: str
    reviewer_id: str
    approved_at: str
    cohort_digest: str


@dataclass
class ReleaseStateMachine:
    manifest: ReleaseManifest
    phase: ReleasePhase
    cohort_digest: str
    allow_injection: bool = False
    allow_expansion: bool = False
    audit: list[TransitionAudit] = field(default_factory=list)

    @classmethod
    def create(cls, manifest: ReleaseManifest) -> "ReleaseStateMachine":
        return cls(manifest=manifest, phase=ReleasePhase.P0_PREFLIGHT, cohort_digest="")

    def transition(
        self,
        target: ReleasePhase,
        *,
        approval: ManualApproval,
        previous_phase_complete: bool,
        cohort_digest: str,
        kill_switch_active: bool,
    ) -> TransitionResult:
        if target in {ReleasePhase.P4_ONE_SCOPE_CANARY, ReleasePhase.P5_MANUAL_EXPANSION} and not self.allow_injection:
            return TransitionResult(False, "phase disabled by default", self.phase)
        if target is ReleasePhase.P5_MANUAL_EXPANSION and not self.allow_expansion:
            return TransitionResult(False, "automatic expansion disabled", self.phase)
        if target != self.phase + 1:
            return TransitionResult(False, "phase transition must be sequential", self.phase)
        if not previous_phase_complete:
            return TransitionResult(False, "previous phase incomplete", self.phase)
        if kill_switch_active:
            return TransitionResult(False, "kill switch active", self.phase)
        if cohort_digest == "" or (self.cohort_digest and cohort_digest != self.cohort_digest):
            return TransitionResult(False, "cohort changed or missing", self.phase)
        if approval.release_id != self.manifest.release_id or approval.manifest_sha256 != self.manifest.manifest_sha256 or approval.target_phase != target:
            return TransitionResult(False, "manual approval does not match manifest", self.phase)
        if not approval.operator_id or not approval.reviewer_id or not approval.approved_at:
            return TransitionResult(False, "manual approval identity missing", self.phase)
        if approval.operator_id == approval.reviewer_id:
            return TransitionResult(False, "operator and reviewer must be independent", self.phase)
        old = self.phase
        self.phase = target
        self.cohort_digest = cohort_digest
        self.audit.append(TransitionAudit(old, target, self.manifest.release_id, self.manifest.manifest_sha256, approval.operator_id, approval.reviewer_id, approval.approved_at, cohort_digest))
        return TransitionResult(True, "applied", self.phase)
