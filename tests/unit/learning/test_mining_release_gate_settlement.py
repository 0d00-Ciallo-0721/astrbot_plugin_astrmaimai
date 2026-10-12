"""Replay both mining schedulers with the real release gate and SQLite lease CAS."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config import AstrMaiConfig
from astrmai.infrastructure.runtime.background_task_ledger import BackgroundTaskLedger
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.learning.evolution_manager import _LearningInputContractError


GROUP = "offline:GroupMessage:123"
PREVIOUS_MINING_AT = 123.0


class _Database:
    def __init__(self):
        self.logs = [
            SimpleNamespace(
                id=index, content=f"synthetic message {index}", timestamp=time.time(),
                sender_id=f"user-{index}", sender_name=f"user {index}",
                processed=False, chat_kind="group", role="user", event_id=f"e{index}",
            )
            for index in range(1, 6)
        ]
        self.marked = []
        self.meta = {}
        self.memory_engine = SimpleNamespace(
            v2_store=SimpleNamespace(get_meta=self.get_meta, set_meta=self.set_meta),
        )

    async def get_meta(self, key, default=""):
        return self.meta.get(key, default)

    async def set_meta(self, key, value):
        self.meta[key] = value

    async def get_unprocessed_logs_async(self, _group_id, limit=100):
        return [log for log in self.logs if log.id not in self.marked][:limit]

    async def list_unprocessed_log_groups_async(self, *, min_count=1, limit=20):
        count = len(await self.get_unprocessed_logs_async(GROUP))
        return [{"group_id": GROUP, "count": count}] if count >= min_count else []

    async def mark_logs_processed_async(self, ids):
        self.marked.extend(ids)


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRMAI_LEARNING_KILL_SWITCH", raising=False)
    config = AstrMaiConfig(evolution={
        "learning_release_kill_switch": False,
        "learning_discovery_enabled": True,
        "backlog_min_unprocessed_logs": 3,
        "backlog_batch_size": 5,
        "backlog_group_limit": 1,
        "min_mining_context": 3,
        "expression_min_valid_messages": 3,
        "expression_overlap_messages": 2,
        "mining_window_min_messages": 3,
    })
    db = _Database()
    instance = EvolutionManager(db, SimpleNamespace(config=config), config=config)
    path = tmp_path / "lease.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE background_task_ledger (
                task_id TEXT PRIMARY KEY, task_family TEXT NOT NULL,
                scope_id TEXT NOT NULL DEFAULT '', scheduled_at REAL NOT NULL DEFAULT 0,
                started_at REAL NOT NULL DEFAULT 0, finished_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, lease_token TEXT NOT NULL DEFAULT '',
                input_fingerprint TEXT NOT NULL DEFAULT '', checkpoint_before TEXT NOT NULL DEFAULT '{}',
                checkpoint_after TEXT NOT NULL DEFAULT '{}', llm_call_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0,
                UNIQUE(task_family, scope_id, input_fingerprint)
            );
        """)
    instance._task_ledger = BackgroundTaskLedger(path)
    instance._last_mining_at[GROUP] = PREVIOUS_MINING_AT
    instance._mining_timestamp_loaded.add(GROUP)
    db.meta[instance._mining_timestamp_key(GROUP)] = str(PREVIOUS_MINING_AT)
    instance.expression_miner.mine = AsyncMock(return_value=[])
    return instance


async def _run(manager, entry):
    if entry == "backlog":
        return await manager.run_backlog_mining_once()
    await manager._try_trigger_mining(GROUP)
    return None


async def _row(manager):
    rows = await manager._task_ledger.list_recent(task_family="learning_mining", scope_id=GROUP)
    assert len(rows) == 1
    with sqlite3.connect(manager._task_ledger.db_path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(connection.execute("SELECT * FROM background_task_ledger").fetchone())


def _assert_unattempted(manager):
    assert manager._last_mining_at[GROUP] == PREVIOUS_MINING_AT
    assert float(manager.db.meta[manager._mining_timestamp_key(GROUP)]) == PREVIOUS_MINING_AT
    assert not manager._mining_cooldown_active(GROUP)
    assert manager.db.marked == []
    assert manager._backlog_failure_counts == {}
    assert manager._backlog_failure_until == {}
    manager.expression_miner.mine.assert_not_awaited()


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_release_blocked_before_snapshot_or_lease_then_same_batch_can_run(manager, entry, monkeypatch):
    manager.config.evolution.learning_release_kill_switch = True
    snapshot = AsyncMock(wraps=manager._load_learning_snapshot)
    claim = AsyncMock(wraps=manager._task_ledger.claim)
    monkeypatch.setattr(manager, "_load_learning_snapshot", snapshot)
    monkeypatch.setattr(manager._task_ledger, "claim", claim)

    report = await _run(manager, entry)

    if report is not None:
        assert report["errors"] == []
        assert report["processed_groups"] == []
        assert report["skipped_groups"] == [{"group_id": GROUP, "reason": "learning_release_kill_switch"}]
    snapshot.assert_not_awaited()
    claim.assert_not_awaited()
    assert await manager._task_ledger.list_recent() == []
    _assert_unattempted(manager)

    manager.config.evolution.learning_release_kill_switch = False
    await _run(manager, entry)
    manager.expression_miner.mine.assert_awaited_once()
    assert manager.db.marked == [1, 2, 3]
    assert (await _row(manager))["status"] == "succeeded"


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_release_flips_during_claim_rejects_only_unattempted_lease(manager, entry, monkeypatch):
    original_claim = manager._task_ledger.claim

    async def claim_then_block(**kwargs):
        lease = await original_claim(**kwargs)
        manager.config.evolution.learning_release_kill_switch = True
        return lease

    monkeypatch.setattr(manager._task_ledger, "claim", claim_then_block)
    report = await _run(manager, entry)
    if report is not None:
        assert report["errors"] == []
        assert report["processed_groups"] == []
        assert report["skipped_groups"][0]["reason"] == "learning_release_kill_switch"
    row = await _row(manager)
    assert row["status"] == "rejected"
    assert row["last_error"] == "learning_release_kill_switch"
    assert row["retry_count"] == 0
    assert row["lease_token"] == ""
    assert row["lease_until"] == 0
    _assert_unattempted(manager)

    monkeypatch.setattr(manager._task_ledger, "claim", original_claim)
    manager.config.evolution.learning_release_kill_switch = False
    await _run(manager, entry)
    manager.expression_miner.mine.assert_awaited_once()
    assert (await _row(manager))["status"] == "succeeded"


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_empty_result_is_unattempted_not_six_hour_success(manager, entry, monkeypatch):
    monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value={}))
    report = await _run(manager, entry)
    if report is not None:
        assert report["errors"] == []
        assert report["processed_groups"] == []
        assert report["skipped_groups"][0]["reason"] == "mining_not_attempted"
    row = await _row(manager)
    assert row["status"] == "rejected"
    assert row["last_error"] == "mining_not_attempted"
    assert row["retry_count"] == 0
    assert row["lease_until"] == 0
    assert row["lease_token"] == ""
    _assert_unattempted(manager)


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_discovery_disabled_is_a_release_skip(manager, entry):
    manager.config.evolution.learning_discovery_enabled = False
    report = await _run(manager, entry)
    if report is not None:
        assert report["errors"] == []
        assert report["skipped_groups"][0]["reason"] == "flag_disabled:discovery"
    assert await manager._task_ledger.list_recent() == []
    _assert_unattempted(manager)


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
@pytest.mark.parametrize("result", [
    None, [], "blocked", {"expression": "completed"},
    {"expression": {"status": None}},
    {"status": "blocked", "reason": "release", "outcomes": {"expression": {"status": "completed"}}},
    {"status": "blocked", "reason": "release", "outcomes": {}, "expression": {"status": "completed"}},
    {"run_id": "aggregate", "pipelines": {"expression": {"status": "completed"}}},
])
async def test_invalid_results_are_contract_errors_not_success(manager, entry, result, monkeypatch):
    monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value=result))
    if entry == "realtime":
        with pytest.raises(_LearningInputContractError):
            await _run(manager, entry)
    else:
        report = await _run(manager, entry)
        assert report["processed_groups"] == []
        assert report["skipped_groups"] == []
        assert report["errors"][0]["error_type"] == "_LearningInputContractError"
        assert report["errors"][0]["stage"].startswith("mining_")
        assert report["errors"][0]["value_type"] != "unknown"
        assert manager._backlog_failure_counts[GROUP] == 1
    row = await _row(manager)
    assert row["status"] == "retry_wait"
    assert row["retry_count"] == 1
    assert row["lease_token"] == ""
    assert manager._last_mining_at[GROUP] == PREVIOUS_MINING_AT
    assert manager.db.marked == []


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
@pytest.mark.parametrize("result,expected_status", [
    ({"expression": {"status": "completed", "reason": "saved"}}, "succeeded"),
    ({"expression": {"status": "failed"}, "jargon": {"status": "quarantined"}}, "retry_wait"),
    ({"expression": {"status": "completed"}, "jargon": {"status": "failed"}}, "succeeded"),
    ({"expression": {"status": "blocked", "reason": "dependency_unavailable"}}, "succeeded"),
])
async def test_pipeline_results_preserve_existing_batch_policy(manager, entry, result, expected_status, monkeypatch):
    monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value=result))
    report = await _run(manager, entry)
    row = await _row(manager)
    assert row["status"] == expected_status
    assert manager._mining_cooldown_active(GROUP) == (expected_status == "succeeded")
    assert row["last_error"] == ("all_pipelines_failed" if expected_status == "retry_wait" else "")
    if report is not None:
        assert report["errors"] == []
        assert report["skipped_groups"] == []
        assert report["processed_groups"][0]["pipelines"] == {
            pipeline: {"status": item["status"], "reason": item.get("reason")}
            for pipeline, item in result.items()
        }


async def test_real_process_returns_pipeline_mapping_not_aggregate(manager):
    snapshot = await manager._load_learning_snapshot(GROUP, 5)
    result = await manager._process_mining_snapshot(GROUP, snapshot, run_id="offline-run")
    assert result["expression"]["status"] == "completed"
    assert set(result) == {"expression"}
    assert manager._last_mining_outcomes[GROUP]["pipelines"] == result
    assert manager.db.marked == [1, 2, 3]


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_rejected_settlement_failure_keeps_durable_intent_without_business_retry(manager, entry, monkeypatch):
    processor = manager.process_logs_and_mine
    monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value={}))
    finish = manager._task_ledger.finish
    monkeypatch.setattr(manager._task_ledger, "finish", AsyncMock(side_effect=RuntimeError("temporary storage failure")))
    report = await _run(manager, entry)
    if report is not None:
        assert report["errors"] == []
        assert report["processed_groups"] == []
    _assert_unattempted(manager)
    assert (await _row(manager))["status"] == "running"
    with sqlite3.connect(manager._task_ledger.db_path) as connection:
        pending = connection.execute(
            "SELECT status,settlement_json FROM background_task_pending_settlements"
        ).fetchall()
    assert len(pending) == 1
    assert pending[0][0] == "rejected"
    assert "mining_not_attempted" in pending[0][1]

    monkeypatch.setattr(manager._task_ledger, "finish", finish)
    replay = await manager._task_ledger.replay_pending_settlements()
    assert replay["replayed"] == 1
    assert (await _row(manager))["retry_count"] == 0
    assert (await _row(manager))["status"] == "rejected"
    monkeypatch.setattr(manager, "process_logs_and_mine", processor)
    await _run(manager, entry)
    manager.expression_miner.mine.assert_awaited_once()
    assert manager.db.marked == [1, 2, 3]


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_stale_token_cannot_reject_another_owner(manager, entry, monkeypatch):
    claim = manager._task_ledger.claim
    leases = []

    async def capture_claim(**kwargs):
        lease = await claim(**kwargs)
        leases.append(lease)
        return lease

    async def replace_owner(*_args, **_kwargs):
        old = leases[0]
        assert await manager._task_ledger.finish(old, status="rejected")
        with sqlite3.connect(manager._task_ledger.db_path) as connection:
            fingerprint = connection.execute("SELECT input_fingerprint FROM background_task_ledger").fetchone()[0]
        new = await claim(task_family="learning_mining", scope_id=GROUP, input_fingerprint=fingerprint)
        leases.append(new)
        return {"status": "blocked", "reason": "learning_release_kill_switch", "outcomes": {}}

    monkeypatch.setattr(manager._task_ledger, "claim", capture_claim)
    monkeypatch.setattr(manager, "process_logs_and_mine", replace_owner)
    await _run(manager, entry)
    row = await _row(manager)
    assert row["status"] == "running"
    assert row["lease_token"] == leases[1].lease_token
    assert row["lease_token"] != leases[0].lease_token
    assert row["retry_count"] == 0
    assert row["lease_until"] == leases[1].lease_until
    _assert_unattempted(manager)
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is False
    assert diagnostic["task_id"] == leases[0].task_id
    with sqlite3.connect(manager._task_ledger.db_path) as connection:
        pending = connection.execute("SELECT status FROM background_task_pending_settlements").fetchone()[0]
    assert pending == "superseded"


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
@pytest.mark.parametrize("stage", ["provider", "timestamp", "rejected_settlement", "successful_settlement"])
async def test_cancellation_propagates_and_preserves_known_settlement(manager, entry, stage, monkeypatch):
    entered = asyncio.Event()

    async def pause(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    if stage == "provider":
        monkeypatch.setattr(manager.expression_miner, "mine", pause)
    elif stage == "timestamp":
        original = manager._persist_mining_timestamp

        async def pause_timestamp(group_id, timestamp):
            if timestamp != PREVIOUS_MINING_AT:
                await pause()
            await original(group_id, timestamp)

        monkeypatch.setattr(manager, "_persist_mining_timestamp", pause_timestamp)
    else:
        if stage == "rejected_settlement":
            monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value={}))
        original = manager._task_ledger.finish_with_recovery
        attempts = 0

        async def pause_once(lease, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                await pause()
            return await original(lease, **kwargs)

        monkeypatch.setattr(manager._task_ledger, "finish_with_recovery", pause_once)

    task = asyncio.create_task(_run(manager, entry))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    row = await _row(manager)
    assert row["lease_token"] == ""
    assert row["lease_until"] <= time.time()
    if stage == "successful_settlement":
        assert row["status"] == "succeeded"
        assert row["retry_count"] == 0
        assert manager._mining_cooldown_active(GROUP)
        assert manager.db.marked == [1, 2, 3]
        await _run(manager, entry)
        manager.expression_miner.mine.assert_awaited_once()
    elif stage == "rejected_settlement":
        assert row["status"] == "rejected"
        assert row["retry_count"] == 0
        _assert_unattempted(manager)
    else:
        assert row["status"] == "retry_wait"
        assert row["last_error"] == "cancelled"
        assert manager._last_mining_at[GROUP] == PREVIOUS_MINING_AT
        assert float(manager.db.meta[manager._mining_timestamp_key(GROUP)]) == PREVIOUS_MINING_AT
        assert not manager._mining_cooldown_active(GROUP)
        assert manager.db.marked == []
    if stage in {"successful_settlement", "rejected_settlement"}:
        diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
        assert diagnostic["committed"] is True
        assert diagnostic["status"] == row["status"]


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_successful_mining_with_settlement_outage_is_not_mined_again(manager, entry, monkeypatch):
    finish = manager._task_ledger.finish
    monkeypatch.setattr(manager._task_ledger, "finish", AsyncMock(side_effect=RuntimeError("storage failure")))
    await _run(manager, entry)
    assert manager._mining_cooldown_active(GROUP)
    assert manager.db.marked == [1, 2, 3]
    await _run(manager, entry)
    manager.expression_miner.mine.assert_awaited_once()
    monkeypatch.setattr(manager._task_ledger, "finish", finish)
    assert (await manager._task_ledger.replay_pending_settlements())["replayed"] == 1
    assert (await _row(manager))["status"] == "succeeded"


async def test_backlog_report_failure_does_not_rollback_completed_batch(manager, monkeypatch):
    import astrmai.learning.evolution_manager as module

    monkeypatch.setattr(module.logger, "info", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("trace unavailable")))
    report = await manager.run_backlog_mining_once()
    assert report["errors"][0]["error"] == "trace unavailable"
    assert (await _row(manager))["status"] == "succeeded"
    assert manager._mining_cooldown_active(GROUP)
    assert manager._backlog_failure_counts == {}
    assert manager._backlog_failure_until == {}
    assert (await manager._task_ledger.describe_pending_settlements())["pending_total"] == 0
    await manager.run_backlog_mining_once()
    manager.expression_miner.mine.assert_awaited_once()
    assert manager.db.marked == [1, 2, 3]


@pytest.mark.parametrize("status", ["succeeded", "rejected", "retry_wait"])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_settlement_returns_primary_commit_result(manager, status, write_fails, monkeypatch):
    lease = await manager._task_ledger.claim(
        task_family="learning_mining", scope_id=GROUP, input_fingerprint="commit-result",
    )
    manager._last_mining_at[GROUP] = time.time()
    if write_fails:
        monkeypatch.setattr(manager._task_ledger, "finish", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    committed = await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, lease, run_id="commit-run", status=status, reason="test-result",
    )
    assert committed is (not write_fails)
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is committed
    assert diagnostic["status"] == status
    assert diagnostic["task_id"] == lease.task_id
    assert diagnostic["run_id"] == "commit-run"
    row = await _row(manager)
    assert row["status"] == ("running" if write_fails else status)
    assert manager._mining_cooldown_active(GROUP) == (status == "succeeded")
    if write_fails:
        assert (await manager._task_ledger.describe_pending_settlements())["pending_total"] == 1


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_entry_exposes_unconfirmed_commit_without_repeating_successful_work(manager, entry, monkeypatch):
    finish = manager._task_ledger.finish
    monkeypatch.setattr(manager._task_ledger, "finish", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    report = await _run(manager, entry)
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is False
    assert diagnostic["attempt_returned"] is True
    assert diagnostic["status"] == "succeeded"
    if report is not None:
        assert report["errors"] == []
        assert report["processed_groups"][0]["settlement_committed"] is False
    assert (await _row(manager))["status"] == "running"
    assert manager.db.marked == [1, 2, 3]
    await _run(manager, entry)
    manager.expression_miner.mine.assert_awaited_once()
    monkeypatch.setattr(manager._task_ledger, "finish", finish)
    assert (await manager._task_ledger.replay_pending_settlements())["replayed"] == 1
    assert (await _row(manager))["status"] == "succeeded"


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
@pytest.mark.parametrize("result,status", [
    ({"expression": {"status": "completed"}}, "succeeded"),
    ({"expression": {"status": "failed"}}, "retry_wait"),
    ({}, "rejected"),
])
async def test_entry_reports_committed_result_separately_from_business_status(manager, entry, result, status, monkeypatch):
    monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value=result))
    report = await _run(manager, entry)
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is True
    assert diagnostic["attempt_returned"] is True
    assert diagnostic["status"] == status
    row = await _row(manager)
    assert row["status"] == status
    assert row["lease_token"] == ""
    assert row["retry_count"] == (1 if status == "retry_wait" else 0)
    if status == "retry_wait":
        assert row["lease_until"] >= time.time() + 1700
    else:
        assert row["lease_until"] == 0
    if report is not None:
        records = report["skipped_groups"] if status == "rejected" else report["processed_groups"]
        assert records[0]["settlement_committed"] is True
        assert report["errors"] == []


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
@pytest.mark.parametrize("status", ["succeeded", "rejected", "retry_wait"])
@pytest.mark.parametrize("pending_failure", ["capacity", "storage"])
async def test_unconfirmed_commit_does_not_claim_pending_is_durable(manager, entry, status, pending_failure, monkeypatch, caplog):
    if status != "succeeded":
        result = {} if status == "rejected" else {"expression": {"status": "failed"}}
        monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value=result))
    monkeypatch.setattr(manager._task_ledger, "finish", AsyncMock(side_effect=RuntimeError("write failed")))
    await manager._task_ledger._ensure_pending_settlement_schema()
    if pending_failure == "capacity":
        monkeypatch.setattr(manager._task_ledger, "PENDING_SETTLEMENT_MAX", 0)
    else:
        monkeypatch.setattr(manager._task_ledger, "enqueue_pending_settlement", AsyncMock(side_effect=RuntimeError("pending write failed")))
    report = await _run(manager, entry)
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is False
    assert diagnostic["status"] == status
    assert "pending_durable" not in diagnostic
    assert "durable enqueue unconfirmed" in caplog.text
    assert (await manager._task_ledger.describe_pending_settlements())["pending_total"] == 0
    row = await _row(manager)
    assert row["status"] == "running"
    assert row["retry_count"] == 0
    assert manager._backlog_failure_counts == {}
    assert manager._backlog_failure_until == {}
    if report is not None:
        records = report["skipped_groups"] if status == "rejected" else report["processed_groups"]
        assert records[0]["settlement_committed"] is False
        assert report["errors"] == []
    if status == "succeeded":
        assert manager._mining_cooldown_active(GROUP)
        assert manager.db.marked == [1, 2, 3]
        await _run(manager, entry)
        manager.expression_miner.mine.assert_awaited_once()
    else:
        assert manager._last_mining_at[GROUP] == PREVIOUS_MINING_AT
        assert float(manager.db.meta[manager._mining_timestamp_key(GROUP)]) == PREVIOUS_MINING_AT
        manager.expression_miner.mine.assert_not_awaited()


@pytest.mark.parametrize("missing", ["ledger", "lease"])
async def test_no_lease_settlement_is_not_applicable(manager, missing):
    lease = await manager._task_ledger.claim(
        task_family="learning_mining", scope_id=GROUP, input_fingerprint="no-settlement",
    )
    if missing == "ledger":
        manager._task_ledger = None
    else:
        lease = None
    result = await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, lease, run_id="not-applicable", status="rejected", reason="mining_not_attempted",
    )
    assert result is None
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is None
    assert manager._last_mining_at[GROUP] == PREVIOUS_MINING_AT


async def test_legacy_ledger_without_task_identity_still_reports_commit(manager):
    manager._task_ledger = SimpleNamespace(finish=AsyncMock(return_value=True))
    result = await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, SimpleNamespace(), run_id="legacy-run", status="succeeded", reason="",
    )
    assert result is True
    diagnostic = manager._last_mining_outcomes[GROUP]["settlement"]
    assert diagnostic["committed"] is True
    assert diagnostic["task_id"] == ""
    manager._task_ledger.finish.assert_awaited_once()


@pytest.mark.parametrize("entry", ["backlog", "realtime"])
async def test_settlement_warning_failure_does_not_rewrite_completed_work(manager, entry, monkeypatch):
    import astrmai.learning.evolution_manager as module

    finish = AsyncMock(side_effect=RuntimeError("storage failed"))
    monkeypatch.setattr(manager._task_ledger, "finish", finish)
    monkeypatch.setattr(module.logger, "warning", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("logging failed")))
    report = await _run(manager, entry)
    assert finish.await_count == 3
    assert (await _row(manager))["status"] == "running"
    diagnostic = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]["settlement"]
    assert diagnostic["committed"] is False
    assert diagnostic["logging_error_type"] == "RuntimeError"
    assert manager.db.marked == [1, 2, 3]
    assert manager._mining_cooldown_active(GROUP)
    if report is not None:
        assert report["errors"] == []
    await _run(manager, entry)
    manager.expression_miner.mine.assert_awaited_once()


async def test_report_failure_after_unconfirmed_commit_does_not_repeat_settlement(manager, monkeypatch):
    import astrmai.learning.evolution_manager as module

    finish = AsyncMock(side_effect=RuntimeError("storage failed"))
    monkeypatch.setattr(manager._task_ledger, "finish", finish)
    monkeypatch.setattr(module.logger, "info", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("report failed")))
    report = await _run(manager, "backlog")
    assert report["errors"][0]["error"] == "report failed"
    assert report["processed_groups"][0]["settlement_committed"] is False
    assert finish.await_count == 3
    assert manager._mining_cooldown_active(GROUP)
    assert manager._backlog_failure_counts == {}
    with sqlite3.connect(manager._task_ledger.db_path) as connection:
        status = connection.execute("SELECT status FROM background_task_pending_settlements").fetchone()[0]
    assert status == "succeeded"
    await _run(manager, "backlog")
    manager.expression_miner.mine.assert_awaited_once()


@pytest.mark.parametrize("committed", [True, False])
async def test_cancellation_after_settlement_returns_preserves_commit_result(manager, committed, monkeypatch):
    import astrmai.learning.evolution_manager as module

    finish = AsyncMock(wraps=manager._task_ledger.finish) if committed else AsyncMock(side_effect=RuntimeError("storage failed"))
    monkeypatch.setattr(manager._task_ledger, "finish", finish)
    monkeypatch.setattr(module.logger, "info", lambda *_args, **_kwargs: (_ for _ in ()).throw(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await _run(manager, "backlog")
    assert (await _row(manager))["status"] == ("succeeded" if committed else "running")
    assert finish.await_count == (1 if committed else 3)
    assert manager._last_mining_outcomes[GROUP]["settlement"]["committed"] is committed
    assert (await manager._task_ledger.describe_pending_settlements())["pending_total"] == (0 if committed else 1)
    assert manager.db.marked == [1, 2, 3]
    await _run(manager, "backlog")
    manager.expression_miner.mine.assert_awaited_once()


async def _record_pipeline_diagnostic(manager, *, group_id, run_id, pipeline):
    return await manager._record_pipeline_state(
        run_id=run_id,
        pipeline=pipeline,
        group_id=group_id,
        logs=manager.db.logs,
        batch_id=f"batch:{run_id}",
        status="completed",
        reason="",
        cursor_before=0,
        cursor_after=3,
        retained_count=2,
        report={},
        persist_run=False,
    )


async def test_pipeline_diagnostics_share_one_group_slot_and_preserve_settlement(manager):
    await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, None,
        run_id="shared-run", status="succeeded", reason="",
    )
    await _record_pipeline_diagnostic(
        manager, group_id=GROUP, run_id="shared-run", pipeline="expression",
    )
    await _record_pipeline_diagnostic(
        manager, group_id=GROUP, run_id="shared-run", pipeline="jargon",
    )

    assert list(manager._last_mining_outcomes) == [GROUP]
    assert set(manager._last_mining_outcomes[GROUP]["pipelines"]) == {"expression", "jargon"}
    assert manager._last_mining_outcomes[GROUP]["settlement"]["run_id"] == "shared-run"


async def test_pipeline_diagnostics_retain_twenty_groups_not_twenty_mixed_entries(manager):
    for index in range(20):
        group_id = f"offline:GroupMessage:{index}"
        run_id = f"run-{index}"
        await manager._settle_mining_result(
            group_id, 0.0, None, run_id=run_id, status="succeeded", reason="",
        )
        await _record_pipeline_diagnostic(
            manager, group_id=group_id, run_id=run_id, pipeline="expression",
        )
        await _record_pipeline_diagnostic(
            manager, group_id=group_id, run_id=run_id, pipeline="jargon",
        )

    assert len(manager._last_mining_outcomes) == 20
    assert "offline:GroupMessage:0" in manager._last_mining_outcomes
    assert all(
        set(outcome["pipelines"]) == {"expression", "jargon"}
        and outcome["settlement"]["run_id"] == outcome["run_id"]
        for outcome in manager._last_mining_outcomes.values()
    )

    await _record_pipeline_diagnostic(
        manager,
        group_id="offline:GroupMessage:20",
        run_id="run-20",
        pipeline="expression",
    )
    assert len(manager._last_mining_outcomes) == 20
    assert "offline:GroupMessage:0" not in manager._last_mining_outcomes
    assert "offline:GroupMessage:20" in manager._last_mining_outcomes


async def test_pipeline_diagnostic_new_run_does_not_inherit_previous_run_state(manager):
    await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, None,
        run_id="old-run", status="succeeded", reason="",
    )
    await _record_pipeline_diagnostic(
        manager, group_id=GROUP, run_id="old-run", pipeline="expression",
    )
    await _record_pipeline_diagnostic(
        manager, group_id=GROUP, run_id="new-run", pipeline="jargon",
    )

    outcome = manager.describe_learning_runtime()["mining"]["last_outcomes"][GROUP]
    assert outcome["run_id"] == "new-run"
    assert set(outcome["pipelines"]) == {"jargon"}
    assert "settlement" not in outcome


async def test_settlement_diagnostics_are_bounded_and_do_not_reuse_another_run(manager):
    for index in range(25):
        await manager._settle_mining_result(
            f"offline:GroupMessage:{index}", 0.0, None,
            run_id=f"run-{index}", status="succeeded", reason="",
        )
    assert len(manager._last_mining_outcomes) == 20
    assert "offline:GroupMessage:0" not in manager._last_mining_outcomes
    manager._last_mining_outcomes[GROUP] = {"run_id": "previous-run", "pipelines": {"expression": {"status": "completed"}}}
    await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, None, run_id="new-run", status="rejected", reason="mining_not_attempted",
    )
    assert "pipelines" not in manager._last_mining_outcomes[GROUP]
    assert manager._last_mining_outcomes[GROUP]["run_id"] == "new-run"


async def test_settlement_keeps_latest_diagnostic_when_existing_report_cache_is_large(manager):
    manager._last_mining_outcomes[GROUP] = {"run_id": "current-run", "pipelines": {"expression": {"status": "completed"}}}
    for index in range(30):
        manager._last_mining_outcomes[f"old-group-{index}"] = {"run_id": f"old-run-{index}"}
    await manager._settle_mining_result(
        GROUP, PREVIOUS_MINING_AT, None, run_id="current-run", status="succeeded", reason="",
    )
    assert len(manager._last_mining_outcomes) == 20
    assert manager._last_mining_outcomes[GROUP]["settlement"]["committed"] is None
    assert manager._last_mining_outcomes[GROUP]["pipelines"]["expression"]["status"] == "completed"


@pytest.mark.parametrize("source", ["startup", "worker", "shutdown"])
@pytest.mark.parametrize("status", ["succeeded", "rejected", "retry_wait"])
async def test_scheduler_recovers_real_mining_settlement_without_provider_retry(manager, source, status, monkeypatch):
    from astrmai.proactive.proactive_task import ProactiveTask
    import astrmai.proactive.proactive_task as module

    finish = manager._task_ledger.finish
    monkeypatch.setattr(manager._task_ledger, "finish", AsyncMock(side_effect=RuntimeError("storage failed")))
    if status != "succeeded":
        result = {} if status == "rejected" else {"expression": {"status": "failed"}}
        monkeypatch.setattr(manager, "process_logs_and_mine", AsyncMock(return_value=result))
    await _run(manager, "backlog")
    assert (await manager._task_ledger.describe_pending_settlements())["pending_total"] == 1
    provider_calls = manager.expression_miner.mine.await_count
    monkeypatch.setattr(manager._task_ledger, "finish", finish)

    task = ProactiveTask.__new__(ProactiveTask)
    task._task_ledger = manager._task_ledger
    task._is_running = False
    task._task = None
    task._settlement_recovery_task = None
    task._background_tasks = set()
    task._background_task_stats = {}
    task._maintenance_tasks = {}
    task._lease_settlement_tasks = set()
    task._kernel_signal_tasks = {}
    task.scheduled_scenario_service = None
    task.dream_agent = object()
    task.proactive_dispatcher = SimpleNamespace(shutdown=AsyncMock())
    task._load_profile_scheduler_state = lambda: None
    task._persist_profile_scheduler_state = AsyncMock()

    async def idle():
        await asyncio.Event().wait()

    task._loop = idle
    sources = []
    replay = task._replay_pending_lease_settlements

    async def capture_source(*, source):
        sources.append(source)
        result = await replay(source=source)
        if source == "worker":
            task._is_running = False
        return result

    task._replay_pending_lease_settlements = capture_source
    if source == "startup":
        try:
            await task.start()
            assert sources == ["startup"]
            assert (await _row(manager))["status"] == status
        finally:
            await task.stop()
    elif source == "worker":
        sleeps = []

        async def tick(delay):
            sleeps.append(delay)

        # Replace only this module's binding, not the shared asyncio.sleep.
        monkeypatch.setattr(module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": tick}))
        task._is_running = True
        await task._settlement_recovery_loop()
        assert sleeps == [30.0]
        assert sources == ["worker"]
    else:
        task._is_running = True
        await task.stop()
        assert sources == ["shutdown"]
    assert (await _row(manager))["status"] == status
    assert (await manager._task_ledger.describe_pending_settlements())["pending_total"] == 0
    assert manager.expression_miner.mine.await_count == provider_calls
    assert manager._mining_cooldown_active(GROUP) == (status == "succeeded")
