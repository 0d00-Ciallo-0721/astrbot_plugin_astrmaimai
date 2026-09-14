import asyncio
import json
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.runtime.background_task_ledger import BackgroundTaskLedger
from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline


class _DelayedPersistence:
    def __init__(self):
        self.ready = asyncio.Event()
        self.wait_started = asyncio.Event()

    async def wait_until_ready(self):
        self.wait_started.set()
        await self.ready.wait()


class _CheckpointStore:
    def __init__(self, persistence):
        self.persistence = persistence
        self.loaded = asyncio.Event()

    async def load_all(self):
        assert self.persistence.ready.is_set(), "checkpoint restore raced schema readiness"
        self.loaded.set()
        return {}


class _FailingPersistence:
    async def wait_until_ready(self):
        raise RuntimeError("persistence schema initialization failed")


@pytest.mark.asyncio
async def test_memory_pipeline_direct_start_waits_for_schema_readiness():
    persistence = _DelayedPersistence()
    store = _CheckpointStore(persistence)
    pipeline = MemoryTurnPipeline(
        context=SimpleNamespace(),
        gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2))),
        engine=SimpleNamespace(db_service=SimpleNamespace(persistence=persistence)),
        session_summarizer=SimpleNamespace(),
        instant_gate=SimpleNamespace(),
        checkpoint_store=store,
    )

    start_task = asyncio.create_task(pipeline.start())
    await persistence.wait_started.wait()
    await asyncio.sleep(0)
    assert not store.loaded.is_set()
    persistence.ready.set()
    await asyncio.wait_for(start_task, timeout=1)
    assert store.loaded.is_set()
    await pipeline.stop()


@pytest.mark.asyncio
async def test_schema_failure_blocks_direct_consumers_before_recovery_or_tasks():
    persistence = _FailingPersistence()
    checkpoint_store = SimpleNamespace(load_all=lambda: pytest.fail("checkpoint restore ran"))
    pipeline = MemoryTurnPipeline(
        context=SimpleNamespace(),
        gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(summary_threshold=2))),
        engine=SimpleNamespace(db_service=SimpleNamespace(persistence=persistence)),
        session_summarizer=SimpleNamespace(),
        instant_gate=SimpleNamespace(),
        checkpoint_store=checkpoint_store,
    )

    with pytest.raises(RuntimeError, match="schema initialization failed"):
        await pipeline.start()
    assert not pipeline._running

    from astrmai.memory.services.memory_engine import MemoryEngine

    engine = MemoryEngine.__new__(MemoryEngine)
    engine.db_service = SimpleNamespace(persistence=persistence)
    with pytest.raises(RuntimeError, match="schema initialization failed"):
        await engine.start_background_tasks()

    from astrmai.learning.review.expression_governance_runner import ExpressionGovernanceRunner
    from astrmai.proactive.proactive_task import ProactiveTask

    class _Ledger:
        async def recover_expired_leases(self):
            pytest.fail("lease recovery ran before schema readiness")

    governance = ExpressionGovernanceRunner.__new__(ExpressionGovernanceRunner)
    governance._is_running = False
    governance.state_engine = SimpleNamespace(persistence=persistence)
    governance._task_ledger = _Ledger()
    governance._task = None
    governance.owner_registry = None

    proactive = ProactiveTask.__new__(ProactiveTask)
    proactive._is_running = False
    proactive.persistence = persistence
    proactive._task_ledger = _Ledger()

    for consumer in (governance, proactive):
        with pytest.raises(RuntimeError, match="schema initialization failed"):
            await consumer.start()
        assert not consumer._is_running


@pytest.mark.asyncio
async def test_pending_settlement_retains_diagnostics_across_retry(tmp_path):
    db_path = tmp_path / "ledger.db"
    import sqlite3

    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE background_task_ledger ("
            "task_id TEXT PRIMARY KEY, task_family TEXT, scope_id TEXT, scheduled_at REAL, "
            "started_at REAL, finished_at REAL DEFAULT 0, lease_until REAL DEFAULT 0, "
            "lease_token TEXT, input_fingerprint TEXT, checkpoint_before TEXT DEFAULT '{}', "
            "checkpoint_after TEXT DEFAULT '{}', llm_call_count INTEGER DEFAULT 0, status TEXT, "
            "retry_count INTEGER DEFAULT 0, last_error TEXT DEFAULT '', payload_json TEXT DEFAULT '{}', "
            "created_at REAL DEFAULT 0, updated_at REAL DEFAULT 0)"
        )
        db.commit()

    ledger = BackgroundTaskLedger(db_path)
    lease = await ledger.claim(task_family="memory", scope_id="chat", input_fingerprint="diag")
    assert lease is not None
    original_finish = ledger.finish

    async def fail_finish(*_args, **_kwargs):
        raise OSError("temporary sqlite failure")

    ledger.finish = fail_finish
    diagnostics = {"failure_stage": "provider", "failure_kind": "timeout", "attempt": 2}
    assert not await ledger.finish_with_recovery(
        lease,
        run_id="run-1",
        status="retry_wait",
        error="provider timeout",
        diagnostics=diagnostics,
    )
    ledger.finish = original_finish
    with sqlite3.connect(db_path) as db:
        raw = db.execute("SELECT settlement_json FROM background_task_pending_settlements").fetchone()[0]
    assert json.loads(raw)["diagnostics"] == diagnostics

    replay = await ledger.replay_pending_settlements()
    assert replay["replayed"] == 1
    rows = await ledger.list_recent(task_family="memory", scope_id="chat", limit=1)
    assert rows[0]["status"] == "retry_wait"
    assert rows[0]["payload"]["diagnostics"] == diagnostics


@pytest.mark.asyncio
async def test_settle_task_lease_forwards_diagnostics_to_recovery_adapter():
    from astrmai.infrastructure.runtime.background_task_ledger import settle_task_lease

    calls = {}

    class _LedgerAdapter:
        async def finish_with_recovery(self, _lease, **kwargs):
            calls.update(kwargs)
            return True

    lease = SimpleNamespace(task_id="task-1", lease_token="token")
    diagnostics = {"failure_stage": "provider", "failure_kind": "timeout"}
    assert await settle_task_lease(
        _LedgerAdapter(), lease, run_id="run-1", status="failed", diagnostics=diagnostics
    )
    assert calls["diagnostics"] == diagnostics
