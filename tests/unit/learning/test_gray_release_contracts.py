from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from config import AstrMaiConfig
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.planning.prompt_refiner import PromptRefiner

from astrmai.learning.release.cohort import CohortLevel, ScopeFacts, select_cohort
from astrmai.learning.release.contracts import ProviderIdentity, ReleaseManifest
from astrmai.learning.release.flags import (
    LearningReleaseFlags,
    PersistentKillSwitch,
    ReleaseCheckpoint,
    runtime_kill_switch_active,
)
from astrmai.learning.release.runtime_gate import LearningReleaseGate, runtime_gate_check
from astrmai.learning.release.metrics import BaselineMetrics, MetricWindow, StopEvaluator
from astrmai.learning.release.preflight import (
    PreflightChecks,
    SQLiteSnapshot,
    migrate_temporary_sqlite,
    run_preflight,
)
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.learning.release.rollback import PointerState, RollbackCoordinator
from astrmai.learning.release.state_machine import (
    ManualApproval,
    ReleasePhase,
    ReleaseStateMachine,
)
from astrmai.learning.release.stop import StopCoordinator


def _manifest(tmp_path: Path, **changes) -> ReleaseManifest:
    repo_root = Path(__file__).resolve().parents[3]
    values = {
        "code_revision": "code-a",
        "schema_version": 175,
        "configuration": {"lane_limit": 1},
        "prompt_fingerprint_version": "prompt-v1",
        "provider": ProviderIdentity(
            provider_id="recorded-provider",
            provider_family="recorded",
            model_id="recorded-model",
            identity_source="fixture",
        ),
        "cohort_id": "cohort-1",
        "cohort_candidates": ("scope-a", "scope-b"),
        "cohort_exclusions": (("scope-private", ("sensitive_scope",)),),
        "flags": LearningReleaseFlags.defaults().as_dict(),
        "baseline_window": "previous-7d-same-utc-hour",
        "artifact_root": tmp_path.parent / f"artifacts-{tmp_path.name}",
        "created_at": "2026-09-22T00:00:00Z",
        "repository_root": repo_root,
    }
    values.update(changes)
    return ReleaseManifest.create(**values)


def test_release_manifest_hash_is_canonical_idempotent_and_change_sensitive(tmp_path):
    first = _manifest(tmp_path, configuration={"b": 2, "a": 1})
    second = _manifest(tmp_path, configuration={"a": 1, "b": 2})

    assert first == second
    assert first.release_id.startswith("release-")
    assert len(first.manifest_sha256) == 64
    with pytest.raises(TypeError):
        first.configuration["a"] = 9
    with pytest.raises(TypeError):
        first.flags["learning_injection_enabled"] = True
    for field, value in (
        ("code_revision", "code-b"),
        ("schema_version", 176),
        ("configuration", {"a": 2}),
        ("prompt_fingerprint_version", "prompt-v2"),
        ("provider", replace(first.provider, model_id="recorded-model-b")),
        ("cohort_candidates", ("scope-a",)),
        ("flags", {**first.flags, "learning_quality_shadow_enabled": False}),
    ):
        changed = _manifest(tmp_path, **{field: value})
        assert changed.release_id != first.release_id
        assert changed.manifest_sha256 != first.manifest_sha256


def test_release_manifest_rejects_repository_overlap_and_sensitive_content(tmp_path):
    repo_root = Path(__file__).resolve().parents[3]
    with pytest.raises(ValueError, match="outside repository"):
        _manifest(tmp_path, artifact_root=repo_root / "artifacts")
    with pytest.raises(ValueError, match="sensitive"):
        _manifest(tmp_path, configuration={"api_key": "secret"})


def test_release_manifest_rejects_source_sidecar_and_hardlink_targets(tmp_path):
    source = tmp_path / "source.db"
    source.write_bytes(b"sqlite")
    sidecar = tmp_path / "source.db-wal"
    sidecar.write_bytes(b"wal")
    hardlink = tmp_path / "artifact-hardlink"
    hardlink.hardlink_to(source)

    with pytest.raises(ValueError, match="protected"):
        _manifest(
            tmp_path,
            artifact_root=hardlink,
            protected_paths=(source, sidecar),
        )


def test_flags_fail_closed_and_environment_kill_switch_has_priority(tmp_path):
    defaults = LearningReleaseFlags.defaults()
    assert defaults.as_dict() == {
        "learning_discovery_enabled": True,
        "learning_enrichment_enabled": False,
        "learning_quality_shadow_enabled": True,
        "learning_retrieval_shadow_enabled": False,
        "learning_injection_enabled": False,
        "learning_release_kill_switch": False,
    }
    with pytest.raises(ValueError, match="missing"):
        LearningReleaseFlags.from_mapping({"learning_discovery_enabled": True})
    with pytest.raises(ValueError, match="boolean"):
        LearningReleaseFlags.from_mapping(
            {**defaults.as_dict(), "learning_enrichment_enabled": 1}
        )
    with pytest.raises(ValueError, match="conflict"):
        LearningReleaseFlags.from_mapping(
            {
                **defaults.as_dict(),
                "learning_injection_enabled": True,
                "learning_retrieval_shadow_enabled": False,
            }
        )

    state_path = tmp_path / "kill-switch.json"
    switch = PersistentKillSwitch(state_path, release_id="release-a")
    assert switch.checkpoint(ReleaseCheckpoint.DISCOVERY, environ={}).allowed
    switch.trip(reason="test", operator_id="operator")
    assert not PersistentKillSwitch(
        state_path, release_id="release-a"
    ).checkpoint(ReleaseCheckpoint.SEND, environ={}).allowed
    assert not switch.checkpoint(
        ReleaseCheckpoint.RETRIEVAL,
        environ={"ASTRMAI_LEARNING_KILL_SWITCH": "1"},
    ).allowed
    with pytest.raises(ValueError, match="new release"):
        switch.clear(operator_id="operator", new_release_id="release-a")


def test_stage10_config_defaults_keep_injection_and_provider_disabled():
    evolution = AstrMaiConfig().evolution
    assert evolution.learning_discovery_enabled is True
    assert evolution.learning_enrichment_enabled is False
    assert evolution.learning_quality_shadow_enabled is True
    assert evolution.learning_injection_enabled is False
    assert evolution.learning_release_kill_switch is False
    assert evolution.learning_prompt_injection_enabled is False
    assert LearningReleaseFlags.from_config(evolution).as_dict() == {
        "learning_discovery_enabled": True,
        "learning_enrichment_enabled": False,
        "learning_quality_shadow_enabled": True,
        "learning_retrieval_shadow_enabled": False,
        "learning_injection_enabled": False,
        "learning_release_kill_switch": False,
    }
    conflict = evolution.model_copy(
        update={
            "learning_injection_enabled": True,
            "learning_prompt_injection_enabled": False,
        }
    )
    with pytest.raises(ValueError, match="injection flags disagree"):
        LearningReleaseFlags.from_config(conflict)
    with pytest.raises(ValueError, match="missing release configuration"):
        ReleaseManifest.from_config(SimpleNamespace(), artifact_root=Path("unused"))


def test_runtime_gate_checks_every_learning_boundary_and_does_not_own_dialog(tmp_path):
    switch = PersistentKillSwitch(tmp_path / "kill.json", release_id="release-a")
    flags = LearningReleaseFlags.defaults()
    gate = LearningReleaseGate(flags, switch)

    assert gate.check(ReleaseCheckpoint.DISCOVERY, environ={}).allowed is True
    for checkpoint in (
        ReleaseCheckpoint.CLAIM,
        ReleaseCheckpoint.ADMISSION,
        ReleaseCheckpoint.SEND,
    ):
        assert gate.check(checkpoint, environ={}).allowed is False
    assert gate.check(ReleaseCheckpoint.RETRIEVAL, environ={}).allowed is False
    for checkpoint in ReleaseCheckpoint:
        decision = gate.check(
            checkpoint,
            environ={"ASTRMAI_LEARNING_KILL_SWITCH": "1"},
        )
        assert decision.allowed is False
        assert decision.reason == "environment_kill_switch"
    assert runtime_kill_switch_active(
        object(), environ={"ASTRMAI_LEARNING_KILL_SWITCH": "1"}
    ) is True


def test_stop_coordinator_persistent_switch_is_consumed_by_formal_runtime_gate(tmp_path):
    config = AstrMaiConfig()
    config.evolution.learning_release_kill_switch_path = str(
        tmp_path / "formal-kill.json"
    )
    switch = PersistentKillSwitch(
        Path(config.evolution.learning_release_kill_switch_path),
        release_id="runtime",
    )
    coordinator = StopCoordinator(
        kill_switch=switch,
        disable_flags=lambda: None,
        stop_claims=lambda: None,
        persist_alert_and_pointers=lambda: None,
        produce_rollback_artifact=lambda: None,
    )
    coordinator.stop(reason="formal-path-test", operator_id="operator")

    for checkpoint in ReleaseCheckpoint:
        decision = runtime_gate_check(config, checkpoint, environ={})
        assert decision.allowed is False
        assert decision.reason == "learning_release_kill_switch"


def test_corrupt_persistent_switch_fails_closed_without_raising(tmp_path):
    config = AstrMaiConfig()
    state_path = tmp_path / "corrupt-kill.json"
    state_path.write_text("{not-json", encoding="utf-8")
    config.evolution.learning_release_kill_switch_path = str(state_path)

    decision = runtime_gate_check(
        config,
        ReleaseCheckpoint.DISCOVERY,
        environ={},
    )

    assert decision.allowed is False
    assert decision.reason == "learning_release_kill_switch"
    assert runtime_kill_switch_active(config, environ={}) is True


def test_persistent_switch_for_another_release_fails_closed(tmp_path):
    config = AstrMaiConfig()
    state_path = tmp_path / "foreign-release-kill.json"
    state_path.write_text(
        '{"active": false, "release_id": "release-new"}',
        encoding="utf-8",
    )
    config.evolution.learning_release_kill_switch_path = str(state_path)
    config.evolution.learning_release_id = "release-old"

    decision = runtime_gate_check(
        config,
        ReleaseCheckpoint.DISCOVERY,
        environ={},
    )

    assert decision.allowed is False
    assert decision.reason == "learning_release_kill_switch"


def test_prompt_visibility_checkpoint_strips_learning_context_when_killed(monkeypatch):
    monkeypatch.setenv("ASTRMAI_LEARNING_KILL_SWITCH", "1")
    refiner = PromptRefiner(memory_engine=None, config=SimpleNamespace(evolution=SimpleNamespace()))
    envelope = PromptEnvelope()
    envelope.learning_context_sections = {"jargon": "must-not-be-visible"}

    rendered, diagnostics = refiner._render_learning_context_sections(
        envelope,
        is_fast_mode=False,
        near_context_priority=False,
    )

    assert rendered == ""
    assert diagnostics["skipped_reason"] == "learning_release_kill_switch"


def _scope(scope_id: str, **changes) -> ScopeFacts:
    values = {
        "scope_id": scope_id,
        "eligible_turns": 100,
        "has_platform_acknowledgement": True,
        "active_incident": False,
        "sensitive_scope": False,
        "admin_only_scope": False,
        "cursor_regression": False,
        "unresolved_vector_identity": False,
        "attribution_coverage": 0.95,
        "dialog_outcome_unknown_rate": 0.1,
        "baseline_sufficient": True,
    }
    values.update(changes)
    return ScopeFacts(**values)


def test_cohort_selection_is_stable_reports_exclusions_and_never_auto_expands():
    scopes = [
        _scope("scope-c"),
        _scope("scope-a"),
        _scope("scope-b"),
        _scope("scope-private", sensitive_scope=True),
        _scope("scope-low", eligible_turns=99),
    ]
    first = select_cohort("release-a", scopes, level=CohortLevel.ONE)
    second = select_cohort("release-a", list(reversed(scopes)), level=CohortLevel.ONE)

    assert first.selected == second.selected
    assert len(first.selected) == 1
    assert first.blocked_reasons == ()
    assert dict(first.excluded)["scope-private"] == ("sensitive_scope",)
    assert dict(first.excluded)["scope-low"] == ("insufficient_eligible_turns",)
    with pytest.raises(ValueError, match="approval"):
        select_cohort("release-a", scopes, level=CohortLevel.THREE)


def test_cohort_blocks_candidate_drift_zero_eligible_and_missing_baseline():
    empty = select_cohort("release-a", [_scope("x", active_incident=True)])
    assert "no_eligible_scopes" in empty.blocked_reasons
    baseline = select_cohort("release-a", [_scope("x", baseline_sufficient=False)])
    assert "baseline_insufficient" in baseline.blocked_reasons
    stable = select_cohort("release-a", [_scope("x")])
    drift = select_cohort(
        "release-a",
        [_scope("y")],
        expected_candidate_digest=stable.candidate_digest,
    )
    assert "candidate_list_changed" in drift.blocked_reasons


def _sqlite(path: Path, version: int = 175) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        db.execute(
            "CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))"
        )
        db.execute(f"PRAGMA user_version={version}")
        db.commit()


def test_p0_preflight_verifies_dual_sqlite_and_zero_provider_network_calls(tmp_path):
    main = tmp_path / "main.db"
    memory = tmp_path / "memory.db"
    _sqlite(main)
    with sqlite3.connect(memory) as db:
        db.execute("CREATE TABLE memory_v2_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute(
            "INSERT INTO memory_v2_meta(key, value) VALUES ('schema_version', '4')"
        )
        db.commit()
    snapshots = (
        SQLiteSnapshot.capture(
            main, role="main", expected_schema_version=175
        ),
        SQLiteSnapshot.capture(
            memory,
            role="memory_v2",
            expected_meta_schema_version=4,
        ),
    )
    checks = PreflightChecks.all_passed()

    result = run_preflight(
        snapshots=snapshots,
        checks=checks,
        provider_mode="recorded",
        provider_calls=0,
        network_calls=0,
    )

    assert result.passed is True
    assert result.provider_calls == 0
    assert result.network_calls == 0
    assert all(item.integrity_ok and item.foreign_keys_ok for item in result.databases)
    assert result.databases[0].pragma_user_version == 175
    assert result.databases[1].meta_schema_version == 4
    assert run_preflight(
        snapshots=snapshots,
        checks=replace(checks, lease_recovery=False),
        provider_mode="recorded",
        provider_calls=0,
        network_calls=0,
    ).passed is False


def test_p0_migrates_only_temporary_copy_and_preserves_source(tmp_path):
    source = tmp_path / "source.db"
    working = tmp_path / "staging" / "working.db"
    with sqlite3.connect(source) as db:
        db.execute(
            "CREATE TABLE learning_human_admission("
            "admission_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, "
            "candidate_revision INTEGER NOT NULL, admission_revision INTEGER NOT NULL)"
        )
        db.execute("PRAGMA user_version=174")
        db.commit()
    before = source.read_bytes()

    result = migrate_temporary_sqlite(
        source=source,
        working_copy=working,
        expected_schema_version=175,
        migrate=_run_migrations,
    )

    assert result.source_unchanged is True
    assert source.read_bytes() == before
    assert result.working_copy.schema_version == 175
    assert result.working_copy.schema_version_ok is True
    assert result.working_copy.integrity_ok is True
    assert result.working_copy.foreign_keys_ok is True


def _approval(manifest: ReleaseManifest, target: ReleasePhase) -> ManualApproval:
    return ManualApproval(
        release_id=manifest.release_id,
        manifest_sha256=manifest.manifest_sha256,
        target_phase=target,
        operator_id="operator-a",
        reviewer_id="reviewer-b",
        approved_at="2026-09-22T01:00:00Z",
    )


def test_state_machine_requires_manual_sequential_transition_and_p4_p5_disabled(tmp_path):
    manifest = _manifest(tmp_path)
    machine = ReleaseStateMachine.create(manifest)
    failed = machine.transition(
        ReleasePhase.P2_RECORDED_ENRICHMENT,
        approval=_approval(manifest, ReleasePhase.P2_RECORDED_ENRICHMENT),
        previous_phase_complete=True,
        cohort_digest="cohort-digest",
        kill_switch_active=False,
    )
    assert failed.applied is False
    assert machine.phase is ReleasePhase.P0_PREFLIGHT

    for target in (
        ReleasePhase.P1_DISCOVERY_SHADOW,
        ReleasePhase.P2_RECORDED_ENRICHMENT,
        ReleasePhase.P3_RETRIEVAL_SHADOW,
    ):
        result = machine.transition(
            target,
            approval=_approval(manifest, target),
            previous_phase_complete=True,
            cohort_digest="cohort-digest",
            kill_switch_active=False,
        )
        assert result.applied is True
    blocked = machine.transition(
        ReleasePhase.P4_ONE_SCOPE_CANARY,
        approval=_approval(manifest, ReleasePhase.P4_ONE_SCOPE_CANARY),
        previous_phase_complete=True,
        cohort_digest="cohort-digest",
        kill_switch_active=False,
    )
    assert blocked.applied is False
    assert "disabled" in blocked.reason


def _window(**changes) -> MetricWindow:
    values = {
        "window_id": "w1",
        "eligible_turns": 100,
        "confirmed_dialog_reply_loss_rate": 0.0,
        "dialog_p95_latency_sec": 1.0,
        "learning_caused_starvation": 0,
        "queue_wait_p95_sec": 1.0,
        "provider_timeout_rate": 0.0,
        "prompt_visible": 50,
        "prompt_selected": 50,
        "attribution_correct": 50,
        "attribution_total": 50,
        "unsafe_asset_count": 0,
        "vector_mismatch_count": 0,
        "cursor_regression_count": 0,
        "old_revision_overwrite_count": 0,
        "duplicate_visible_send_count": 0,
        "repeated_injection_count": 0,
        "known_outcomes": 80,
        "unknown_outcomes": 20,
    }
    values.update(changes)
    return MetricWindow(**values)


def test_stop_evaluator_handles_denominator_unknown_three_windows_and_safety_once():
    evaluator = StopEvaluator(
        BaselineMetrics(
            reply_loss_rate=0.0,
            dialog_p95_latency_sec=1.0,
            queue_wait_p95_sec=1.0,
            provider_timeout_rate=0.0,
        )
    )
    insufficient = evaluator.evaluate(_window(prompt_selected=10, prompt_visible=10))
    assert insufficient.status == "insufficient_observation"
    unknown = evaluator.evaluate(_window(known_outcomes=60, unknown_outcomes=40))
    assert unknown.status == "partial"
    assert unknown.semantic_kpi_status == "unavailable"

    degraded = _window(dialog_p95_latency_sec=3.0)
    assert evaluator.evaluate(replace(degraded, window_id="w2")).stop is False
    assert evaluator.evaluate(replace(degraded, window_id="w3")).stop is False
    assert evaluator.evaluate(replace(degraded, window_id="w4")).stop is True

    immediate = StopEvaluator(evaluator.baseline).evaluate(
        _window(vector_mismatch_count=1)
    )
    assert immediate.stop is True
    assert immediate.status == "stopped"


def test_stop_evaluator_requires_three_consecutive_degraded_windows():
    evaluator = StopEvaluator(
        BaselineMetrics(0.0, 1.0, 1.0, 0.0)
    )
    degraded = _window(dialog_p95_latency_sec=3.0)
    assert evaluator.evaluate(replace(degraded, window_id="w1")).consecutive_windows == 1
    assert evaluator.evaluate(_window(window_id="w2")).consecutive_windows == 0
    assert evaluator.evaluate(replace(degraded, window_id="w3")).stop is False
    assert evaluator.evaluate(replace(degraded, window_id="w4")).stop is False
    assert evaluator.evaluate(replace(degraded, window_id="w5")).stop is True


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"dialog_p95_latency_sec": math.nan}, "invalid_metric:dialog_p95_latency_sec"),
        ({"queue_wait_p95_sec": math.inf}, "invalid_metric:queue_wait_p95_sec"),
        ({"prompt_selected": -1}, "invalid_metric:prompt_selected"),
        ({"prompt_visible": 51}, "invalid_metric:prompt_visible_exceeds_selected"),
        ({"attribution_correct": 51}, "invalid_metric:attribution_correct_exceeds_total"),
        ({"known_outcomes": 90, "unknown_outcomes": 20}, "invalid_metric:outcomes_exceed_eligible_turns"),
    ],
)
def test_stop_evaluator_blocks_invalid_numeric_windows(changes, reason):
    result = StopEvaluator(BaselineMetrics(0.0, 1.0, 1.0, 0.0)).evaluate(
        _window(**changes)
    )
    assert result.status == "blocked"
    assert result.stop is True
    assert result.reasons == (reason,)


def test_stop_evaluator_blocks_invalid_baseline():
    result = StopEvaluator(
        BaselineMetrics(math.nan, 1.0, 1.0, 0.0)
    ).evaluate(_window())
    assert result.status == "blocked"
    assert result.stop is True
    assert result.reasons == ("invalid_baseline:reply_loss_rate",)


def test_stop_coordinator_order_is_fixed_and_idempotent(tmp_path):
    events = []
    switch = PersistentKillSwitch(tmp_path / "kill.json", release_id="release-a")
    coordinator = StopCoordinator(
        kill_switch=switch,
        disable_flags=lambda: events.append("disable_flags"),
        stop_claims=lambda: events.append("stop_claims"),
        persist_alert_and_pointers=lambda: events.append("persist_alert"),
        produce_rollback_artifact=lambda: events.append("rollback_artifact"),
    )

    first = coordinator.stop(reason="safety", operator_id="operator")
    second = coordinator.stop(reason="safety", operator_id="operator")

    assert first.actions == (
        "kill_switch",
        "disable_injection_enrichment",
        "stop_new_claims",
        "persist_alert_pointer_state",
        "produce_rollback_artifact",
    )
    assert events == ["disable_flags", "stop_claims", "persist_alert", "rollback_artifact"]
    assert second == first


def test_concurrent_stop_and_twenty_rehearsals_have_no_duplicate_actions(tmp_path):
    events = []
    switch = PersistentKillSwitch(tmp_path / "kill-race.json", release_id="release-a")
    coordinator = StopCoordinator(
        kill_switch=switch,
        disable_flags=lambda: events.append("disable_flags"),
        stop_claims=lambda: events.append("stop_claims"),
        persist_alert_and_pointers=lambda: events.append("persist_alert"),
        produce_rollback_artifact=lambda: events.append("rollback_artifact"),
    )
    results = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                coordinator.stop(reason="race", operator_id="operator")
            )
        )
        for _ in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 20
    assert len(events) == 4
    assert all(result.actions == results[0].actions for result in results)
    assert sum(event == "rollback_artifact" for event in events) == 1


def _pointer(generation: int) -> PointerState:
    return PointerState(
        canonical_pointer=f"canonical-{generation}",
        vector_pointer=f"vector-{generation}",
        retrieval_pointer=f"retrieval-{generation}",
        generation=generation,
        mapping_sha256=f"mapping-{generation}",
        index_sha256=f"index-{generation}",
        provenance_digest=f"provenance-{generation}",
    )


def test_rollback_restores_only_manifest_bound_pointer_and_fails_closed(tmp_path):
    current = [_pointer(2)]
    expected = _pointer(1)
    artifact = tmp_path / "rollback.json"
    coordinator = RollbackCoordinator(
        inspect=lambda: current[0],
        restore=lambda target: current.__setitem__(0, target),
        verify=lambda target: current[0] == target,
        artifact_path=artifact,
    )

    result = coordinator.rehearse(
        release_id="release-a",
        manifest_sha256="a" * 64,
        expected_current=_pointer(2),
        manifest_before=expected,
        requested_target=expected,
    )
    assert result.succeeded is True
    assert current[0] == expected
    assert json.loads(artifact.read_text(encoding="utf-8"))["succeeded"] is True

    current[0] = _pointer(2)
    failed = coordinator.rehearse(
        release_id="release-a",
        manifest_sha256="a" * 64,
        expected_current=_pointer(2),
        manifest_before=expected,
        requested_target=_pointer(0),
    )
    assert failed.succeeded is False
    assert current[0] == _pointer(2)
