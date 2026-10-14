"""Deterministic restart/idle replay; providers and production data are not used."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from config import AstrMaiConfig
from astrmai.infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskQueueTimeout,
)
from astrmai.infrastructure.runtime.background_task_ledger import BackgroundTaskLedger
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.memory.contracts.memory_query import MemoryWriteRequest
from astrmai.memory.services.memory_index_projector import MemoryIndexProjector
from astrmai.memory.services.memory_maintenance_service import MemoryMaintenanceService
from astrmai.memory.services.v2_store import MemoryV2Store
from astrmai.proactive.proactive_task import ProactiveTask
from tests.regression.learning.test_background_schedule_contracts import (
    _create_background_ledger_schema,
)


def scheduler(budget=None, ledger=None):
    task = ProactiveTask.__new__(ProactiveTask)
    task.config = AstrMaiConfig()
    task._is_running = True
    task._task_ledger = ledger
    task._background_tasks = set()
    task._maintenance_tasks = {}
    task._background_task_stats = {}
    task._lease_settlement_tasks = set()
    task.background_task_budget = budget or BackgroundTaskBudget(2)
    task.owner_registry = None
    task._diary_pending_date = ""
    task._last_diary_date = ""
    return task


@pytest.mark.parametrize("elapsed,expected", [(5, 0), (59, 0), (60, 1)])
async def test_maintenance_period_uses_one_clock(monkeypatch, elapsed, expected):
    import astrmai.proactive.proactive_task as module

    task = scheduler()
    task._last_global_maintenance_run = 100.0
    task._scheduler_poll_interval_seconds = 5.0
    task._run_chat_heartbeat_pass = AsyncMock()
    task._run_maintenance_cycle = AsyncMock()
    task.scheduled_scenario_service = SimpleNamespace(tick=AsyncMock())
    task._last_profile_run = 1_700_000_000.0
    task.diary_service = SimpleNamespace(should_run=lambda *_args: False)
    count = 0

    async def tick(_delay):
        nonlocal count
        count += 1
        if count > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(module, "monotonic", lambda: 100.0 + elapsed)
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: 1_700_000_000.0))
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=tick, CancelledError=asyncio.CancelledError))
    await task._loop()
    assert task._run_maintenance_cycle.await_count == expected


async def test_diary_delay_does_not_occupy_budget_and_cancel_releases_lease(tmp_path, monkeypatch):
    import astrmai.proactive.proactive_task as module

    db_path = tmp_path / "ledger.db"
    _create_background_ledger_schema(db_path)
    ledger = BackgroundTaskLedger(db_path)
    ledger.finish_with_recovery = AsyncMock(wraps=ledger.finish_with_recovery)
    budget = BackgroundTaskBudget(1)
    task = scheduler(budget, ledger)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delay(_seconds):
        entered.set()
        await release.wait()

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        **{name: getattr(asyncio, name) for name in ("create_task", "current_task", "CancelledError", "Task", "shield")},
        sleep=delay,
    ))
    work = AsyncMock(return_value={"processed": 1})
    await task._enqueue_managed_maintenance(
        task_family="diary", scope_id="global", awaitable_factory=work,
        admission_delay_sec=300.0,
    )
    pending = task._maintenance_tasks[("diary", "global")]
    task._diary_pending_date = "2026-10-13"
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert budget.status()["active"] == 0
        assert await budget.run(lambda: asyncio.sleep(0, result="other work")) == "other work"
        work.assert_not_awaited()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    await asyncio.sleep(0)
    rows = await ledger.list_recent(task_family="diary", limit=1)
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["lease_until"] == 0
    assert not task._diary_pending_date
    assert not task._maintenance_tasks
    assert budget.status()["active"] == 0
    assert ledger.finish_with_recovery.await_count == 1
    assert not task._lease_settlement_tasks


@pytest.mark.parametrize("reason", ["projection_rebuild_in_progress", "retriever_not_ready", "shutdown_rejected"])
async def test_dependency_wait_preserves_durable_attempt_budget(tmp_path, reason):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    for _ in range(10):
        assert await store.schedule_projection_retry("memory", reason, max_attempts=2)
    restarted = MemoryV2Store(store.db_path)
    assert await restarted.projection_retry_status("memory") == "pending"
    async with aiosqlite.connect(store.db_path) as db:
        async with db.execute("SELECT attempts FROM memory_projection_outbox") as cursor:
            assert (await cursor.fetchone())[0] == 0


async def test_actual_projection_failure_still_exhausts_and_is_not_resurrected(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    assert await store.schedule_projection_retry("memory", "vector_add_failed", max_attempts=2)
    assert await store.schedule_projection_retry("memory", "vector_add_failed", max_attempts=2)
    assert not await store.schedule_projection_retry("memory", "vector_add_failed", max_attempts=2)
    assert not await store.schedule_projection_retry("memory", "retriever_not_ready", max_attempts=2)
    assert await store.projection_retry_status("memory") == "repair_exhausted"


async def test_retry_reports_dependency_deferral_not_failure(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    store._now = lambda: 1000.0
    await store.schedule_projection_retry("memory", "projection_rebuild_in_progress", base_delay_sec=1)
    store._now = lambda: 1002.0
    projector = MemoryIndexProjector(SimpleNamespace(v2_store=store, _projection_rebuild_active=True))
    result = await projector.retry_due()
    assert result["failed"] == 0
    assert result["deferred"] == 1
    assert projector._retry_failure_count == 0
    assert projector._last_retry_items[0]["status"] == "deferred"


async def test_ready_replay_includes_rebuild_wait_and_coalesces_with_periodic_retry(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    store._now = lambda: 1000.0
    await store.schedule_projection_retry("memory", "projection_rebuild_in_progress", base_delay_sec=1)
    store._now = lambda: 1002.0
    projector = MemoryIndexProjector(SimpleNamespace(v2_store=store))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def project(memory_id):
        calls.append(memory_id)
        entered.set()
        await release.wait()
        await store.complete_projection_retry(memory_id)
        return True

    projector.project = project
    replay = asyncio.create_task(projector.replay_pending_after_ready())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        periodic = asyncio.create_task(projector.retry_due())
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(replay, periodic)
    finally:
        release.set()
        if not replay.done():
            replay.cancel()
        await asyncio.gather(replay, return_exceptions=True)
    assert calls == ["memory"]
    assert await store.projection_retry_snapshot() == {}


async def test_retry_worker_waits_before_budget_when_rebuild_active(monkeypatch):
    import astrmai.memory.services.memory_index_projector as module

    budget = SimpleNamespace(run=AsyncMock())
    projector = MemoryIndexProjector(SimpleNamespace(
        _projection_rebuild_active=True, background_task_budget=budget,
    ))

    async def finish_wait(awaitable, timeout):
        if hasattr(awaitable, "close"):
            awaitable.close()
        projector._retry_stop.set()
        raise asyncio.TimeoutError

    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        wait_for=finish_wait, TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError,
    ))
    await projector._retry_loop()
    budget.run.assert_not_awaited()


async def test_daily_maintenance_survives_restart_without_repeated_decay(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    written = await store.upsert(MemoryWriteRequest(
        source="test", kind="memory", session_id="chat", content="keep this",
        importance=1.0, dedup_key="daily",
    ))
    maintenance = MemoryMaintenanceService(store)
    first = scheduler()
    first.memory_engine = SimpleNamespace(maintenance_service=maintenance)
    await first._run_memory_store_maintenance()
    before = await store.get_canonical(written.memory_id)
    restarted = scheduler()
    restarted.memory_engine = SimpleNamespace(maintenance_service=MemoryMaintenanceService(MemoryV2Store(store.db_path)))
    await restarted._run_memory_store_maintenance()
    after = await store.get_canonical(written.memory_id)
    assert before.decay_score == after.decay_score


async def test_failed_maintenance_does_not_write_success_marker():
    task = scheduler()
    work = AsyncMock(side_effect=[RuntimeError("database unavailable"), {"errors": []}])
    task.memory_engine = SimpleNamespace(maintenance_service=SimpleNamespace(run_once=work))
    with pytest.raises(RuntimeError):
        await task._run_memory_store_maintenance()
    assert not getattr(task, "_last_store_maintenance_at", 0)
    await task._run_memory_store_maintenance()
    assert work.await_count == 2


async def test_partial_maintenance_retry_does_not_apply_decay_twice(tmp_path, monkeypatch):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    written = await store.upsert(MemoryWriteRequest(
        source="test", kind="memory", session_id="chat", content="keep this",
        importance=1.0, dedup_key="partial",
    ))
    service = MemoryMaintenanceService(store)
    original = store.purge_kind_candidates
    monkeypatch.setattr(store, "purge_kind_candidates", AsyncMock(side_effect=RuntimeError("cleanup failed")))
    task = scheduler()
    task.memory_engine = SimpleNamespace(maintenance_service=service)
    with pytest.raises(RuntimeError, match="maintenance"):
        await task._run_memory_store_maintenance()
    before = await store.get_canonical(written.memory_id)
    monkeypatch.setattr(store, "purge_kind_candidates", original)
    await task._run_memory_store_maintenance()
    after = await store.get_canonical(written.memory_id)
    assert before.decay_score == after.decay_score


async def test_blocked_learning_does_not_queue_discovery(monkeypatch):
    import time

    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = AstrMaiConfig()
    manager.config.evolution.learning_release_kill_switch = True
    manager.background_task_budget = SimpleNamespace(run=AsyncMock())
    manager.db = SimpleNamespace()
    manager._last_learning_run_purge_at = time.time()
    manager._last_message_log_purge = {"purged_at": time.time()}
    manager.run_backlog_mining_once = AsyncMock()
    await manager._run_backlog_mining_budgeted()
    manager.background_task_budget.run.assert_not_awaited()
    manager.run_backlog_mining_once.assert_not_awaited()
    assert manager._last_backlog_report["reason"] == "learning_release_kill_switch"


async def test_queue_timeout_identifies_owners_and_waiting_task():
    budget = BackgroundTaskBudget(1, wait_timeout_sec=0.1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def owner():
        entered.set()
        await release.wait()

    running = asyncio.create_task(budget.run(owner, task_name="vector_bootstrap", scope_id="global"))
    try:
        await entered.wait()
        with pytest.raises(BackgroundTaskQueueTimeout) as caught:
            await budget.run(lambda: asyncio.sleep(0), task_name="learning.backlog", scope_id="global")
        assert "learning.backlog" in str(caught.value)
        assert "vector_bootstrap" in str(caught.value)
    finally:
        release.set()
        await running


async def test_scheduled_decay_concurrent_instances_apply_once(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    written = await store.upsert(MemoryWriteRequest(
        source="test", kind="memory", session_id="chat", content="preserve", importance=1.0,
    ))
    second = MemoryV2Store(store.db_path)
    await second.initialize()
    outcomes = await asyncio.gather(
        store.apply_decay_if_due(decay_rate=0.08),
        second.apply_decay_if_due(decay_rate=0.08),
    )
    assert sum(applied for _, applied in outcomes) == 1
    assert (await store.get_canonical(written.memory_id)).decay_score == pytest.approx(0.92)


async def test_decay_marker_failure_rolls_back_data(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    written = await store.upsert(MemoryWriteRequest(
        source="test", kind="memory", session_id="chat", content="preserve", importance=1.0,
    ))
    async with aiosqlite.connect(store.db_path) as db:
        await db.execute("""CREATE TRIGGER reject_marker BEFORE INSERT ON memory_v2_meta
            WHEN NEW.key='scheduled_decay_completed_at'
            BEGIN SELECT RAISE(ABORT, 'checkpoint unavailable'); END""")
        await db.commit()
    with pytest.raises(aiosqlite.IntegrityError, match="checkpoint unavailable"):
        await store.apply_decay_if_due(decay_rate=0.08)
    assert (await store.get_canonical(written.memory_id)).decay_score == 1.0
    assert await store.get_meta("scheduled_decay_completed_at") == ""


async def test_canonical_marker_failure_retry_does_not_repeat_decay(tmp_path, monkeypatch):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    written = await store.upsert(MemoryWriteRequest(
        source="test", kind="memory", session_id="chat", content="preserve", importance=1.0,
    ))
    task = scheduler()
    task.memory_engine = SimpleNamespace(maintenance_service=MemoryMaintenanceService(store))
    original = store.set_meta
    monkeypatch.setattr(store, "set_meta", AsyncMock(side_effect=RuntimeError("checkpoint unavailable")))
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await task._run_memory_store_maintenance()
    assert not getattr(task, "_last_store_maintenance_at", 0)
    monkeypatch.setattr(store, "set_meta", original)
    result = await task._run_memory_store_maintenance()
    assert result["decayed"] == 0
    assert (await store.get_canonical(written.memory_id)).decay_score == pytest.approx(0.92)


async def test_not_due_maintenance_does_not_claim_or_queue(tmp_path):
    import time

    store = MemoryV2Store(str(tmp_path / "memory.db"))
    await store.set_meta("scheduled_maintenance_completed_at", time.time())
    task = scheduler()
    task.memory_engine = SimpleNamespace(maintenance_service=MemoryMaintenanceService(store))
    task._enqueue_managed_maintenance = AsyncMock()
    task.decay_service = SimpleNamespace(run_once=AsyncMock())
    task.group_signin_service = SimpleNamespace(run_once=AsyncMock())
    task.heartflow_topic_digest_service = SimpleNamespace(run_once=AsyncMock())
    task.heartflow_manager = object()
    await task._run_maintenance_cycle()
    assert "memory_maintenance" not in [call.kwargs["task_family"] for call in task._enqueue_managed_maintenance.await_args_list]


async def test_daily_and_maintenance_decay_have_separate_restart_fences(tmp_path):
    from astrmai.proactive.decay_service import DecayService
    from astrmai.memory.services.memory_engine import MemoryEngine

    store = MemoryV2Store(str(tmp_path / "memory.db"))
    written = await store.upsert(MemoryWriteRequest(
        source="test", kind="memory", session_id="chat", content="preserve", importance=1.0,
    ))
    config = AstrMaiConfig()
    engine = MemoryEngine.__new__(MemoryEngine)
    engine.config = config
    engine.maintenance_service = MemoryMaintenanceService(store)
    states = SimpleNamespace(get_active_states=lambda: [], get_active_profiles=lambda: [])
    await DecayService(states, engine, config).run_once()
    before = (await store.get_canonical(written.memory_id)).decay_score
    await DecayService(states, engine, config).run_once()
    assert (await store.get_canonical(written.memory_id)).decay_score == before
    await engine.maintenance_service.run_once(policy={"maintenance_interval_sec": 86400})
    assert (await store.get_canonical(written.memory_id)).decay_score == pytest.approx(before * 0.92)


async def test_blocked_learning_preserves_due_retention(tmp_path):
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = AstrMaiConfig()
    manager.config.evolution.learning_release_kill_switch = True
    manager.background_task_budget = BackgroundTaskBudget(1)
    manager.db = SimpleNamespace(
        purge_learning_mining_runs_async=AsyncMock(return_value={"deleted": 2}),
        purge_consumed_message_logs_async=AsyncMock(return_value={"deleted": 3}),
    )
    manager._task_ledger = None
    manager._last_learning_run_purge_at = 0.0
    manager._last_learning_run_purge = {}
    manager._last_message_log_purge = {}
    manager.run_backlog_mining_once = AsyncMock()
    await manager._run_backlog_mining_budgeted()
    manager.run_backlog_mining_once.assert_not_awaited()
    manager.db.purge_learning_mining_runs_async.assert_awaited_once()
    manager.db.purge_consumed_message_logs_async.assert_awaited_once()
    await manager._run_backlog_mining_budgeted()
    assert manager.db.purge_consumed_message_logs_async.await_count == 1
    assert manager.background_task_budget.status()["active"] == 0


async def test_ready_replay_filters_before_batch_limit(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    await store.schedule_projection_retry("unrelated", "vector_add_failed")
    await store.schedule_projection_retry("ready", "projection_rebuild_in_progress")
    projector = MemoryIndexProjector(SimpleNamespace(v2_store=store))
    projector.project = AsyncMock(return_value=True)
    result = await projector.replay_pending_after_ready(limit=1)
    assert result["projected"] == 1
    projector.project.assert_awaited_once_with("ready")


async def test_idle_services_do_not_enter_background_queue(tmp_path):
    import time
    from astrmai.proactive.decay_service import DecayService
    from astrmai.proactive.group_signin_service import GroupSigninService
    from astrmai.proactive.heartflow.topic_digest_service import HeartflowTopicDigestService

    store = MemoryV2Store(str(tmp_path / "memory.db"))
    await store.set_meta("scheduled_maintenance_completed_at", time.time())
    await store.set_meta("daily_decay_completed_at", time.time())
    task = scheduler()
    states = SimpleNamespace(get_active_states=lambda: [], get_active_profiles=lambda: [])
    task.memory_engine = SimpleNamespace(
        v2_store=store, maintenance_service=MemoryMaintenanceService(store),
        apply_daily_decay=AsyncMock(), record_cognitive_feedback=AsyncMock(),
    )
    task.decay_service = DecayService(states, task.memory_engine, task.config)
    task.group_signin_service = GroupSigninService(
        state_engine=states, persistence=object(), dispatcher=object(), config=task.config,
    )
    task.heartflow_topic_digest_service = HeartflowTopicDigestService(task.memory_engine)
    task.heartflow_manager = SimpleNamespace(list_sessions=lambda **_: [])
    for _ in range(3):
        await task._run_maintenance_cycle()
        await asyncio.gather(*list(task._background_tasks))
    assert task.background_task_budget.status().get("completed_by_kind", {}) == {}
    task.memory_engine.apply_daily_decay.assert_not_awaited()
    task.memory_engine.record_cognitive_feedback.assert_not_awaited()


async def test_cancelled_physical_work_keeps_slot_until_drain(tmp_path):
    path = tmp_path / "ledger.db"
    _create_background_ledger_schema(path)
    ledger = BackgroundTaskLedger(path)
    task = scheduler(BackgroundTaskBudget(1), ledger)
    entered, release = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        await release.wait()
        return {"status": "succeeded"}

    await task._enqueue_managed_maintenance(task_family="memory", scope_id="global", awaitable_factory=work)
    pending = task._maintenance_tasks[("memory", "global")]
    await entered.wait()
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert task.background_task_budget.status()["active"] == 1
    assert (await task.background_task_budget.drain(0))["remaining"] == 1
    release.set()
    assert (await task.background_task_budget.wait_until_idle(1))["remaining"] == 0
    assert (await ledger.list_recent(limit=1))[0]["status"] == "cancelled"
    assert not task._lease_settlement_tasks


@pytest.mark.parametrize("outcome,expected", [("no_work", "rejected"), ("deferred", "retry_wait"), ("partial", "retry_wait"), ("succeeded", "succeeded")])
async def test_business_outcome_is_not_misreported_as_lease_success(tmp_path, outcome, expected):
    path = tmp_path / "ledger.db"
    _create_background_ledger_schema(path)
    ledger = BackgroundTaskLedger(path)
    task = scheduler(ledger=ledger)
    await task._enqueue_managed_maintenance(
        task_family="maintenance", scope_id="global",
        awaitable_factory=AsyncMock(return_value={"status": outcome, "reason": "test_reason"}),
    )
    await asyncio.gather(*list(task._background_tasks))
    await asyncio.sleep(0)
    row = (await ledger.list_recent(limit=1))[0]
    assert row["status"] == expected
    assert task._background_task_stats["maintenance"]["last_outcome"] == outcome
    if expected != "succeeded":
        assert row["last_error"] == "test_reason"


async def test_queue_warning_sampling_preserves_all_failures(monkeypatch, caplog):
    import astrmai.proactive.proactive_task as module

    task = scheduler()
    monkeypatch.setattr(module, "monotonic", lambda: 1000.0)
    async def timeout():
        raise BackgroundTaskQueueTimeout("test_queue_timeout")
    for _ in range(3):
        pending = asyncio.create_task(timeout())
        await asyncio.gather(pending, return_exceptions=True)
        pending._astrmai_task_name = "maintenance"
        task._handle_task_result(pending)
    stats = task._background_task_stats["maintenance"]
    assert stats["failed"] == 3
    assert stats["queue_warning_suppressed"] == 2
    assert sum("test_queue_timeout" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("cancelled", [False, True])
async def test_unsuccessful_task_keeps_phase_diagnostics(cancelled):
    task = scheduler()

    async def finish():
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("controlled failure")

    pending = asyncio.create_task(finish())
    await asyncio.gather(pending, return_exceptions=True)
    pending._astrmai_task_name = "maintenance"
    pending._astrmai_scope_id = "global"
    pending._astrmai_run_id = "run-test"
    pending._astrmai_terminal_stage = "execution"
    pending._astrmai_stage = "settlement"
    pending._astrmai_delay_ms = 1.0
    pending._astrmai_queue_wait_ms = 2.0
    pending._astrmai_execution_ms = 3.0
    pending._astrmai_settlement_ms = 4.0
    task._handle_task_result(pending)
    stats = task._background_task_stats["maintenance"]
    assert stats["last_terminal_stage"] == "execution"
    assert stats["last_run_id"] == "run-test"
    assert stats["last_scope_id"] == "global"
    assert stats["last_outcome"] == ("cancelled" if cancelled else "error")
    assert [stats[f"last_{phase}_ms"] for phase in ("delay", "queue_wait", "execution", "settlement")] == [1, 2, 3, 4]
    assert stats["completed"] == 0


async def test_queue_timeout_keeps_actual_admission_duration():
    budget = BackgroundTaskBudget(1, wait_timeout_sec=0.05)
    task = scheduler(budget)
    entered, release = asyncio.Event(), asyncio.Event()

    async def owner():
        entered.set()
        await release.wait()

    running = asyncio.create_task(budget.run(owner, task_name="vector_bootstrap"))
    await entered.wait()
    try:
        waiting = task._fire_background_task(AsyncMock(), task_name="maintenance")
        await asyncio.gather(waiting, return_exceptions=True)
        await asyncio.sleep(0)
        stats = task._background_task_stats["maintenance"]
        assert stats["last_terminal_stage"] == "admission"
        assert stats["last_outcome"] == "queue_timeout"
        assert stats["last_queue_wait_ms"] >= 40
        assert stats["last_execution_ms"] == 0
    finally:
        release.set()
        await running


async def test_ready_replay_respects_existing_budget_and_cancellation(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    await store.schedule_projection_retry("ready", "projection_rebuild_in_progress")
    budget = BackgroundTaskBudget(1)
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold():
        entered.set()
        await release.wait()
    owner = asyncio.create_task(budget.run(hold, task_name="vector_bootstrap"))
    await entered.wait()
    projector = MemoryIndexProjector(SimpleNamespace(v2_store=store, background_task_budget=budget))
    projector.project = AsyncMock(return_value=True)
    replay = asyncio.create_task(projector.replay_pending_after_ready())
    await asyncio.sleep(0)
    projector.project.assert_not_awaited()
    replay.cancel()
    await asyncio.gather(replay, return_exceptions=True)
    assert not projector._retry_batch_lock.locked()
    assert await store.projection_retry_status("ready") == "pending"
    release.set()
    await owner
    assert budget.status()["active"] == 0


async def test_periodic_retry_cancellation_preserves_pending_and_releases_lock(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    store._now = lambda: 1000.0
    await store.schedule_projection_retry("memory", "retriever_not_ready", base_delay_sec=1)
    store._now = lambda: 1002.0
    projector = MemoryIndexProjector(SimpleNamespace(v2_store=store))
    entered = asyncio.Event()
    async def wait(_memory_id):
        entered.set()
        await asyncio.Event().wait()
    projector.project = wait
    pending = asyncio.create_task(projector.retry_due())
    await entered.wait()
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert not projector._retry_batch_lock.locked()
    assert await store.projection_retry_status("memory") == "pending"
    projector.project = AsyncMock(return_value=True)
    assert (await projector.retry_due())["projected"] == 1


async def test_index_failure_retries_without_repeating_canonical_work(tmp_path, monkeypatch):
    store = MemoryV2Store(str(tmp_path / "memory.db"))
    await store.initialize()
    cleanup = AsyncMock(wraps=store.purge_kind_candidates)
    monkeypatch.setattr(store, "purge_kind_candidates", cleanup)
    projector = SimpleNamespace(
        audit_consistency=AsyncMock(side_effect=[RuntimeError("index unavailable"), {"pending_projection_count": 0}]),
    )
    service = MemoryMaintenanceService(store, projector)
    task = scheduler()
    task.memory_engine = SimpleNamespace(maintenance_service=service)
    assert (await task._run_memory_store_maintenance())["status"] == "partial"
    assert not getattr(task, "_last_store_maintenance_at", 0)
    assert await task._memory_store_maintenance_due()
    assert (await task._run_memory_store_maintenance())["status"] == "succeeded"
    assert cleanup.await_count == 2
    assert projector.audit_consistency.await_count == 2
    assert not await task._memory_store_maintenance_due()


async def test_empty_projection_worker_does_not_acquire_slot(tmp_path, monkeypatch):
    import astrmai.memory.services.memory_index_projector as module

    store = MemoryV2Store(str(tmp_path / "memory.db"))
    await store.initialize()
    budget = SimpleNamespace(run=AsyncMock())
    projector = MemoryIndexProjector(SimpleNamespace(v2_store=store, retriever=object(), background_task_budget=budget))
    async def finish_wait(awaitable, timeout):
        awaitable.close()
        projector._retry_stop.set()
        raise asyncio.TimeoutError
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        wait_for=finish_wait, TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError,
    ))
    await projector._retry_loop()
    budget.run.assert_not_awaited()


async def test_cancelled_settlement_recovers_original_success_intent(tmp_path):
    path = tmp_path / "ledger.db"
    _create_background_ledger_schema(path)
    ledger = BackgroundTaskLedger(path)
    original = ledger.finish_with_recovery
    entered = asyncio.Event()
    attempts = 0
    async def settle(lease, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await asyncio.Event().wait()
        return await original(lease, **kwargs)
    ledger.finish_with_recovery = settle
    task = scheduler(ledger=ledger)
    work = AsyncMock(return_value={"status": "succeeded"})
    await task._enqueue_managed_maintenance(task_family="diary", scope_id="global", awaitable_factory=work)
    pending = task._maintenance_tasks[("diary", "global")]
    await entered.wait()
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    await asyncio.sleep(0)
    await asyncio.gather(*list(task._lease_settlement_tasks))
    row = (await ledger.list_recent(limit=1))[0]
    assert row["status"] == "succeeded"
    assert row["lease_until"] == 0
    work.assert_awaited_once()


async def test_maintenance_log_is_bounded_but_report_remains_complete(caplog):
    reasons = {f"memory_{index}": f"failure_{index}:" + "detail" * 100 for index in range(654)}
    task = scheduler()
    task.memory_engine = SimpleNamespace(maintenance_service=SimpleNamespace(
        run_once=AsyncMock(return_value={
            "canonical_completed": True, "errors": [],
            "index_repair": {"remaining_pending": 654, "remaining_pending_reasons": reasons},
        }),
    ))
    report = await task._run_memory_store_maintenance()
    message = next(record.message for record in caplog.records if "memory store maintenance completed" in record.message)
    assert len(message) < 4096
    assert "pending_reason_kind_count': 654" in message
    assert len(report["index_repair"]["remaining_pending_reasons"]) == 654
    assert report["status"] == "deferred"


async def test_slow_vector_boot_diary_delay_and_foreground_reserve(tmp_path, monkeypatch):
    import astrmai.proactive.proactive_task as module
    from astrmai.infrastructure.gateway.gateway_call import GatewayCallMixin

    class GatewaySlots(GatewayCallMixin):
        def __init__(self):
            self._global_semaphore = asyncio.Semaphore(3)
            self._background_semaphore = asyncio.Semaphore(2)
            self.settings = SimpleNamespace(semaphore_wait_timeout_sec=1.0)

    gateway = GatewaySlots()
    budget = BackgroundTaskBudget(1)
    task = scheduler(budget)
    entered, release, delayed, release_delay = (asyncio.Event() for _ in range(4))
    async def slow_vector():
        async with gateway._concurrency_slot(False):
            entered.set()
            await release.wait()
    owner = asyncio.create_task(budget.run(slow_vector, task_name="vector_bootstrap"))
    await entered.wait()
    async def jitter(_seconds):
        delayed.set()
        await release_delay.wait()
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(
        **{name: getattr(asyncio, name) for name in ("create_task", "current_task", "CancelledError", "Task", "shield")},
        sleep=jitter,
    ))
    diary = AsyncMock(return_value={"status": "succeeded"})
    await task._enqueue_managed_maintenance(
        task_family="diary", scope_id="global", awaitable_factory=diary, admission_delay_sec=300,
    )
    await delayed.wait()
    try:
        async with gateway._concurrency_slot(True):
            assert budget.status()["active"] == 1
            assert budget.status()["queued"] == 0
            diary.assert_not_awaited()
        release.set()
        await owner
        release_delay.set()
        await asyncio.gather(*list(task._background_tasks))
        diary.assert_awaited_once()
        assert budget.status()["active"] == 0
        assert gateway._global_semaphore._value == 3
        assert gateway._background_semaphore._value == 2
    finally:
        release.set()
        release_delay.set()
        await asyncio.gather(owner, *list(task._background_tasks), return_exceptions=True)


async def test_service_reports_keep_failure_and_no_work_distinct():
    from astrmai.proactive.decay_service import DecayService
    from astrmai.proactive.heartflow.topic_digest_service import HeartflowTopicDigestService

    states = SimpleNamespace(get_active_states=lambda: [], get_active_profiles=lambda: [])
    engine = SimpleNamespace(apply_daily_decay=AsyncMock(side_effect=RuntimeError("memory unavailable")))
    service = DecayService(states, engine, AstrMaiConfig())
    assert (await service.run_once())["status"] == "partial"
    assert service._last_memory_decay == 0
    digest = HeartflowTopicDigestService(SimpleNamespace(record_cognitive_feedback=AsyncMock()))
    assert (await digest.run_once(SimpleNamespace(list_sessions=lambda **_: [])))["status"] == "no_work"
    def fail_scan(**_kwargs):
        raise RuntimeError("state unavailable")
    assert (await digest.run_once(SimpleNamespace(list_sessions=fail_scan)))["status"] == "failed"
