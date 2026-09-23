"""Local-only contracts for controlled AstrMai learning releases.

This package deliberately stops at shadow/preflight boundaries.  It does not
own provider calls, production database writes, cursor advancement, or prompt
injection.
"""

from .cohort import CohortLevel, CohortSelection, ScopeFacts, select_cohort
from .contracts import ProviderIdentity, ReleaseManifest
from .flags import LearningReleaseFlags, PersistentKillSwitch, ReleaseCheckpoint, register_runtime_kill_switch, runtime_kill_switch_active, runtime_release_flags
from .metrics import BaselineMetrics, MetricWindow, StopEvaluation, StopEvaluator
from .preflight import MigrationResult, PreflightChecks, PreflightResult, SQLiteSnapshot, migrate_temporary_sqlite, run_preflight
from .rollback import PointerState, RollbackCoordinator
from .runtime_gate import GateDecision, LearningReleaseGate, runtime_gate_check
from .state_machine import ManualApproval, ReleasePhase, ReleaseStateMachine

__all__ = [
    "BaselineMetrics",
    "CohortLevel",
    "CohortSelection",
    "LearningReleaseFlags",
    "LearningReleaseGate",
    "ManualApproval",
    "MigrationResult",
    "MetricWindow",
    "PersistentKillSwitch",
    "PointerState",
    "PreflightChecks",
    "PreflightResult",
    "ProviderIdentity",
    "ReleaseCheckpoint",
    "ReleaseManifest",
    "ReleasePhase",
    "ReleaseStateMachine",
    "RollbackCoordinator",
    "ScopeFacts",
    "SQLiteSnapshot",
    "StopEvaluation",
    "StopEvaluator",
    "GateDecision",
    "runtime_gate_check",
    "run_preflight",
    "migrate_temporary_sqlite",
    "runtime_kill_switch_active",
    "runtime_release_flags",
    "register_runtime_kill_switch",
    "select_cohort",
]
