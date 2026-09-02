import asyncio
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

from astrmai.app.lifecycle import PluginLifecycleManager
from astrmai.app.runtime_context import RuntimeStatus
from astrmai.infrastructure.runtime.lifecycle_state import (
    RuntimeLifecycleState,
    can_transition,
)
from astrmai.infrastructure.runtime.runtime_status_schema import build_runtime_status_schema


def test_runtime_lifecycle_state_accepts_startup_and_shutdown_chain():
    status = RuntimeStatus(runtime_generation=7)
    assert status.transition_lifecycle_state("initializing", reason="boot")
    assert status.transition_lifecycle_state("ready", reason="boot_complete")
    assert status.transition_lifecycle_state("shutdown_requested", reason="stop")
    assert status.transition_lifecycle_state("draining", reason="drain")
    assert status.transition_lifecycle_state("shutdown_complete", reason="drained")
    assert status.lifecycle_state == RuntimeLifecycleState.SHUTDOWN_COMPLETE.value
    assert status.lifecycle_state_revision == 5


def test_invalid_transition_and_generation_mismatch_are_fail_closed():
    status = RuntimeStatus(runtime_generation=3)
    assert not status.transition_lifecycle_state("ready", reason="skipped_init")
    assert status.lifecycle_state == RuntimeLifecycleState.CREATED.value
    assert status.lifecycle_transition_errors[-1]["error"] == "invalid_transition"
    assert not status.transition_lifecycle_state(
        "initializing", reason="stale", expected_generation=2
    )
    assert status.lifecycle_state == RuntimeLifecycleState.CREATED.value
    assert status.lifecycle_transition_errors[-1]["error"] == "generation_mismatch"
    assert can_transition(RuntimeLifecycleState.CREATED, RuntimeLifecycleState.INITIALIZING)
    assert not can_transition(RuntimeLifecycleState.READY, RuntimeLifecycleState.SHUTDOWN_COMPLETE)


def test_same_state_transition_is_idempotent_and_history_is_bounded():
    status = RuntimeStatus()
    assert status.transition_lifecycle_state("created", reason="initial")
    assert status.lifecycle_state_revision == 0
    for index in range(80):
        status.transition_lifecycle_state("initializing", reason=str(index))
        status.transition_lifecycle_state("ready", reason=str(index))
    assert len(status.lifecycle_transition_history) <= 64
    assert len(status.lifecycle_transition_errors) <= 64
    assert status.lifecycle_state == RuntimeLifecycleState.READY.value


def test_runtime_status_snapshot_is_json_safe_and_schema_uses_explicit_state():
    status = RuntimeStatus(runtime_generation=11)
    status.transition_lifecycle_state("initializing", reason="boot")
    payload = status.as_dict()
    json.dumps(payload)
    schema = build_runtime_status_schema(runtime_status=payload)
    assert schema["lifecycle_status"] == "initializing"
    assert schema["runtime_generation"] == 11


def test_begin_shutdown_enters_shutdown_requested_without_needing_async_cleanup():
    status = RuntimeStatus(runtime_generation=4)
    status.transition_lifecycle_state("initializing")
    status.transition_lifecycle_state("ready")
    phases = []
    runtime = SimpleNamespace(
        status=status,
        background_tasks=set(),
        lifecycle=SimpleNamespace(manager=None),
        transition_lifecycle_state=status.transition_lifecycle_state,
        mark_degraded=lambda *_: None,
        set_boot_phase=phases.append,
        attention_gate=None,
        proactive_task=None,
        external_result_dispatcher=None,
        event_bus=None,
        memory_engine=None,
    )
    manager = PluginLifecycleManager(runtime)
    manager.begin_shutdown()
    assert status.lifecycle_state == RuntimeLifecycleState.SHUTDOWN_REQUESTED.value
    assert status.shutdown_generation == 1
    assert phases[-1] == "shutdown.start"


def test_complete_startup_marks_ready_only_after_all_startup_stages(monkeypatch):
    status = RuntimeStatus(runtime_generation=2)
    status.transition_lifecycle_state("initializing")
    runtime = SimpleNamespace(
        status=status,
        background_tasks=set(),
        lifecycle=SimpleNamespace(manager=None),
        transition_lifecycle_state=status.transition_lifecycle_state,
        set_boot_phase=lambda phase: None,
        memory_engine=SimpleNamespace(schedule_vector_bootstrap_after_startup=lambda **_: None),
        attention_gate=None,
    )
    manager = PluginLifecycleManager(runtime)
    manager._await_runtime_reload_fence = AsyncMock(return_value=True)
    manager._restore_dialogue_snapshot = AsyncMock()
    manager.initialize_memory = AsyncMock()
    manager._initialize_persona_core_until_ready = AsyncMock(return_value=True)
    manager.load_command_metadata = AsyncMock()
    manager._wire_private_chat_plugin = AsyncMock()
    manager.start_expression_governance_services = AsyncMock()
    manager.start_proactive_services = AsyncMock()
    manager.start_visual_services = AsyncMock()
    manager.start_workmode_guard = AsyncMock()
    manager.start_background_services = lambda: None
    monkeypatch.setattr("astrmai.app.lifecycle.init_meme_storage", lambda: None)

    asyncio.run(manager._complete_startup())
    assert status.lifecycle_state == RuntimeLifecycleState.READY.value
    assert status.accepting_events is True
    assert status.lifecycle_started is True


def test_unhandled_startup_exception_enters_failed_state():
    status = RuntimeStatus(runtime_generation=1)
    phases = []
    runtime = SimpleNamespace(
        status=status,
        background_tasks=set(),
        lifecycle=SimpleNamespace(manager=None),
        transition_lifecycle_state=status.transition_lifecycle_state,
        mark_degraded=lambda *_: None,
        set_boot_phase=phases.append,
        event_bus=None,
        attention_gate=None,
        external_result_dispatcher=None,
    )
    manager = PluginLifecycleManager(runtime)

    async def fail_startup():
        raise RuntimeError("startup boom")

    manager._complete_startup = fail_startup

    async def run():
        await manager.on_program_start(source="test")
        await manager._startup_task

    asyncio.run(run())
    assert status.lifecycle_state == RuntimeLifecycleState.FAILED.value
    assert status.startup_blocked_reason == "startup_failed"


def test_shutdown_requested_requires_draining_before_completion():
    status = RuntimeStatus()
    assert status.transition_lifecycle_state("initializing")
    assert status.transition_lifecycle_state("ready")
    assert status.transition_lifecycle_state("shutdown_requested")
    assert not status.transition_lifecycle_state("shutdown_complete")
    assert status.transition_lifecycle_state("draining")
    assert status.transition_lifecycle_state("shutdown_complete")


def test_shutdown_complete_guard_rejects_nonzero_pending_report():
    status = RuntimeStatus()
    status.transition_lifecycle_state("initializing")
    status.transition_lifecycle_state("ready")
    status.transition_lifecycle_state("shutdown_requested")
    status.transition_lifecycle_state("draining")
    runtime = SimpleNamespace(
        status=status,
        background_tasks=set(),
        lifecycle=SimpleNamespace(manager=None),
        transition_lifecycle_state=status.transition_lifecycle_state,
        set_boot_phase=lambda _: None,
    )
    manager = PluginLifecycleManager(runtime)
    assert not manager._transition_shutdown_complete(
        pending_report={"remaining": 1, "remaining_by_kind": {"worker": 1}},
        reason="guard",
    )
    assert status.lifecycle_state == RuntimeLifecycleState.DRAINING.value
    assert manager._transition_shutdown_complete(
        pending_report={"remaining": 0, "remaining_by_kind": {}},
        reason="complete",
    )


def test_lifecycle_retry_and_recovery_chain_is_explicit():
    status = RuntimeStatus()
    assert status.transition_lifecycle_state("initializing")
    assert status.transition_lifecycle_state("retry_wait")
    assert status.transition_lifecycle_state("initializing")
    assert status.transition_lifecycle_state("failed")
    assert status.transition_lifecycle_state("retry_wait")
    assert status.transition_lifecycle_state("initializing")
    assert status.transition_lifecycle_state("ready")


def test_start_and_shutdown_boundary_cannot_publish_ready_after_shutdown():
    status = RuntimeStatus(runtime_generation=1)
    phases = []
    runtime = SimpleNamespace(
        status=status,
        background_tasks=set(),
        lifecycle=SimpleNamespace(manager=None),
        transition_lifecycle_state=status.transition_lifecycle_state,
        mark_degraded=lambda *_: None,
        set_boot_phase=phases.append,
        event_bus=None,
        attention_gate=None,
        external_result_dispatcher=None,
    )
    manager = PluginLifecycleManager(runtime)

    async def run():
        await manager.on_program_start(source="race")
        manager.begin_shutdown()
        await manager._startup_task

    asyncio.run(run())
    assert status.lifecycle_state != RuntimeLifecycleState.READY.value
    assert status.lifecycle_state == RuntimeLifecycleState.SHUTDOWN_REQUESTED.value
