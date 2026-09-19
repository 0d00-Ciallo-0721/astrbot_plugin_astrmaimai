from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.persistence.persistence_schema import _MIGRATIONS, _run_migrations
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.learning.persistence.candidate_ledger import CandidateLedger


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "discovery.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 146")
        for version, ddl in _MIGRATIONS:
            if version in (145, 146):
                db.execute(ddl)
        _run_migrations(db)
        db.commit()
    return CandidateLedger(path)


class _InputPolicy:
    def normalize(self, items):
        return list(items)


class _FilteringInputPolicy:
    def normalize(self, items):
        return [item for item in items if item.id != 12]


class _Extractor:
    async def extract(self, group_id, _logs, **_kwargs):
        return [
            {
                "candidate_type": "catchphrase",
                "normalized_expression": "脱敏表达",
                "expression": "脱敏表达",
                "distinct_turn_count": 1,
                "source_message_ids": ["event-11"],
                "group_id": group_id,
            }
        ]


class _UnboundExtractor:
    async def extract(self, group_id, _logs, **_kwargs):
        return [
            {
                "candidate_type": "catchphrase",
                "normalized_expression": "unbound",
                "expression": "unbound",
                "distinct_turn_count": 1,
                "source_message_ids": ["missing-source-id"],
                "group_id": group_id,
            }
        ]


class _ExpressionMiner:
    input_policy = _InputPolicy()
    candidate_extractor = _Extractor()
    expression_min_distinct_turns = 1

    async def _existing_patterns(self, _group_id):
        return set()


def _manager(ledger):
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.candidate_ledger = ledger
    manager.expression_miner = _ExpressionMiner()
    manager.jargon_miner = SimpleNamespace()
    manager._next_pipeline_cursor = lambda logs, _pipeline: (logs[-1].id, 0)
    return manager


def _logs():
    return [
        SimpleNamespace(
            id=11,
            group_id="qq:group:42",
            sender_id="user-1",
            event_id="event-11",
            platform_message_id="platform-11",
            topic_epoch=1,
            learning_evidence_eligible=True,
        ),
        SimpleNamespace(
            id=12,
            group_id="qq:group:42",
            sender_id="user-2",
            event_id="event-12",
            platform_message_id="platform-12",
            topic_epoch=1,
            learning_evidence_eligible=True,
        ),
    ]


@pytest.mark.asyncio
async def test_discovery_writes_complete_source_batch_candidate_and_evidence(ledger):
    report = await _manager(ledger)._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )

    due = await ledger.list_due_enrichment(now=200.0, limit=10)
    assert report["status"] == "completed"
    assert report["cursor_after"] == 12
    assert report["source_count"] == 2
    assert report["candidate_count"] == 1
    assert len(due) == 1
    assert len(await ledger.list_evidence(due[0].candidate_id)) == 1


@pytest.mark.asyncio
async def test_discovery_replay_is_idempotent_without_duplicate_candidate_or_evidence(ledger):
    manager = _manager(ledger)
    first = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )
    replay = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )
    due = await ledger.list_due_enrichment(now=200.0, limit=10)

    assert first["batch_id"] == replay["batch_id"]
    assert replay["candidate_inserted"] == 0
    assert replay["candidate_deduplicated"] == 1
    assert replay["evidence_inserted"] == 0
    assert len(due) == 1
    assert len(await ledger.list_evidence(due[0].candidate_id)) == 1


@pytest.mark.asyncio
async def test_discovery_records_policy_rejection_as_skipped_disposition(ledger):
    manager = _manager(ledger)
    manager.expression_miner.input_policy = _FilteringInputPolicy()

    report = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )

    with sqlite3.connect(ledger.db_path) as db:
        payload = db.execute(
            "SELECT source_dispositions_json FROM learning_source_batch WHERE batch_id = ?",
            (report["batch_id"],),
        ).fetchone()[0]
        diagnostic_stage = db.execute(
            "SELECT stage FROM learning_stage_diagnostic"
        ).fetchone()[0]
    dispositions = {item["source_row_id"]: item for item in json.loads(payload)}
    assert diagnostic_stage == "discover"
    assert dispositions[11]["disposition"] == "candidate_ids"
    assert dispositions[12]["disposition"] == "skipped"
    assert dispositions[12]["reason_code"] == "input_policy_rejected"


@pytest.mark.asyncio
async def test_discovery_fails_closed_when_candidate_source_identity_is_unbound(ledger):
    manager = _manager(ledger)
    manager.expression_miner.candidate_extractor = _UnboundExtractor()

    with pytest.raises(RuntimeError, match="source_evidence_incomplete"):
        await manager._write_candidate_discovery(
            pipeline="expression",
            group_id="qq:group:42",
            logs=_logs(),
            cursor_before=10,
        )

    with sqlite3.connect(ledger.db_path) as db:
        assert db.execute(
            "SELECT status FROM learning_source_batch"
        ).fetchone()[0] == "started"


@pytest.mark.asyncio
async def test_cursor_v2_branch_commits_discovery_and_never_runs_legacy_miner():
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(
            learning_pipeline_timeout_sec=60.0,
            learning_candidate_ledger_enabled=True,
            learning_discovery_cursor_v2_enabled=True,
        )
    )
    manager._pipeline_failure_counts = {}
    manager.expression_miner = SimpleNamespace(
        mine=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy miner must not run")
        )
    )
    manager._get_pipeline_checkpoint = lambda *_args, **_kwargs: _async_value(
        {
            "cursor_log_id": 10,
            "revision": 2,
            "cursor_semantics": "legacy_batch_atomic_v1",
            "pipeline_version": "expression-cursor-state-v1",
        }
    )
    manager._write_candidate_discovery = lambda **_kwargs: _async_value(
        {
            "status": "completed",
            "reason": "discovery_durable",
            "batch_id": "batch:v2",
            "cursor_after": 12,
            "retained_count": 0,
            "candidate_count": 1,
        }
    )
    commits = []

    async def _commit(**kwargs):
        commits.append(kwargs)
        return {"committed": True}

    manager._settle_pipeline_checkpoint = _commit
    manager._record_pipeline_state = lambda **kwargs: _async_value(kwargs)
    manager._mining_batch_id = lambda *_args, **_kwargs: "legacy-batch"

    outcome = await manager._run_learning_pipeline_unlimited(
        "expression",
        "qq:group:42",
        _logs(),
        run_id="run-v2",
    )

    assert outcome["cursor_after"] == 12
    assert commits[0]["cursor_semantics"] == "source_batch_contiguous_v2"
    assert commits[0]["pipeline_version"] == "expression-discovery-v2"
    assert commits[0]["reason"] == "discovery_durable_enrichment_pending"


@pytest.mark.parametrize(
    ("ledger_enabled", "cursor_v2_enabled", "worker_enabled", "provider_enabled", "ready"),
    [
        (True, True, True, True, True),
        (False, True, True, True, False),
        (True, False, True, True, False),
        (True, True, False, True, False),
        (True, True, True, False, False),
    ],
)
def test_enrichment_worker_flag_truth_table_is_fail_closed(
    ledger_enabled,
    cursor_v2_enabled,
    worker_enabled,
    provider_enabled,
    ready,
):
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(
            learning_candidate_ledger_enabled=ledger_enabled,
            learning_discovery_cursor_v2_enabled=cursor_v2_enabled,
            learning_enrichment_worker_enabled=worker_enabled,
            learning_enrichment_enabled=provider_enabled,
        )
    )

    assert manager._enrichment_worker_gate()[0] is ready


@pytest.mark.asyncio
async def test_runtime_flag_disable_stops_worker_before_next_claim(monkeypatch):
    manager = EvolutionManager.__new__(EvolutionManager)
    config = SimpleNamespace(
        learning_candidate_ledger_enabled=True,
        learning_discovery_cursor_v2_enabled=True,
        learning_enrichment_worker_enabled=True,
        learning_enrichment_enabled=True,
    )
    manager.config = SimpleNamespace(evolution=config)
    calls = 0

    class _Worker:
        async def run_due_once(self, *, limit):
            nonlocal calls
            calls += 1
            config.learning_enrichment_enabled = False

    manager.enrichment_worker = _Worker()
    monkeypatch.setattr("astrmai.learning.evolution_manager.asyncio.sleep", _async_value)

    await manager._enrichment_worker_loop()
    assert calls == 1


async def _async_value(value):
    return value
