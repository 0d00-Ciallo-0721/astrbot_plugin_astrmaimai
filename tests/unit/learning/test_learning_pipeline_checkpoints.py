from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace

from sqlmodel import SQLModel, Session, create_engine

from config import AstrMaiConfig
from astrmai.infrastructure.persistence.database_service import DatabaseService
from astrmai.infrastructure.persistence.orm_models import MessageLog
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.learning.evolution_manager import EvolutionManager


def _database_service(tmp_path) -> DatabaseService:
    path = tmp_path / "astrmai.db"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 73")
        _run_migrations(db)
        db.commit()

    persistence = SimpleNamespace(
        db_path=path,
        engine=engine,
        orm_models=None,
        get_session=lambda: Session(engine),
        bind_database_service=lambda _service: None,
    )
    return DatabaseService(persistence)


def _add_logs(service: DatabaseService, chat_id: str, count: int, *, processed_until: int = 0):
    with service.get_session() as session:
        for index in range(1, count + 1):
            session.add(
                MessageLog(
                    group_id=chat_id,
                    sender_id=f"user-{index % 3}",
                    sender_name=f"群友{index % 3}",
                    content=f"消息{index}",
                    timestamp=float(index),
                    processed=index <= processed_until,
                    chat_kind="group",
                    role="user",
                )
            )
        session.commit()


def test_expression_replays_bounded_history_while_jargon_resumes_legacy_cursor(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:1", 12, processed_until=8)

    expression = service.get_learning_logs(
        "expression",
        "ff:GroupMessage:1",
        limit=20,
        replay_recent=5,
    )
    jargon = service.get_learning_logs(
        "jargon",
        "ff:GroupMessage:1",
        limit=20,
    )

    assert [item.content for item in expression] == [f"消息{index}" for index in range(8, 13)]
    assert [item.content for item in jargon] == [f"消息{index}" for index in range(9, 13)]


def test_pipeline_cursors_advance_independently_and_survive_service_restart(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:2", 6)

    logs = service.get_learning_logs("expression", "ff:GroupMessage:2", limit=10)
    service.advance_learning_checkpoint(
        "expression",
        "ff:GroupMessage:2",
        logs[3].id,
        batch_id="expression-batch",
        status="completed",
    )

    restarted = DatabaseService(service.persistence)
    expression = restarted.get_learning_logs("expression", "ff:GroupMessage:2", limit=10)
    jargon = restarted.get_learning_logs("jargon", "ff:GroupMessage:2", limit=10)

    assert [item.content for item in expression] == ["消息5", "消息6"]
    assert [item.content for item in jargon] == [f"消息{index}" for index in range(1, 7)]


def test_legacy_processed_flag_waits_for_both_pipeline_checkpoints(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:3", 5)
    logs = service.get_learning_logs("expression", "ff:GroupMessage:3", limit=10)
    service.get_learning_logs("jargon", "ff:GroupMessage:3", limit=10)

    service.advance_learning_checkpoint("expression", "ff:GroupMessage:3", logs[-1].id)
    assert service.mark_logs_processed_through_learning_checkpoints("ff:GroupMessage:3") == 0

    service.advance_learning_checkpoint("jargon", "ff:GroupMessage:3", logs[2].id)
    assert service.mark_logs_processed_through_learning_checkpoints("ff:GroupMessage:3") == 3
    remaining = service.get_unprocessed_logs("ff:GroupMessage:3", limit=10)
    assert [item.content for item in remaining] == ["消息4", "消息5"]


def test_message_log_retention_only_deletes_fully_consumed_non_visual_rows(tmp_path):
    service = _database_service(tmp_path)
    chat_id = "ff:GroupMessage:retention"
    _add_logs(service, chat_id, 5, processed_until=3)
    with sqlite3.connect(service.persistence.db_path) as db:
        rows = db.execute(
            "SELECT id FROM messagelog WHERE group_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        db.execute("UPDATE messagelog SET image_refs='[\"image-1\"]' WHERE id=?", (ids[1],))
        db.execute(
            "UPDATE messagelog SET event_id=?, platform_message_id=? WHERE id=?",
            ("event-visual", "platform-visual", ids[2]),
        )
        db.execute(
            "INSERT INTO visualmessagebinding(chat_id,message_id,sender_id,image_index,asset_id,legacy_picid,source_ref_hash,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (chat_id, "event-visual", "user", 0, "", "", "hash", 1.0, 1.0),
        )
        db.commit()

    service.ensure_learning_checkpoint("expression", chat_id)
    service.ensure_learning_checkpoint("jargon", chat_id)
    service.advance_learning_checkpoint("expression", chat_id, ids[2])
    service.advance_learning_checkpoint("jargon", chat_id, ids[2])

    report = service.purge_consumed_message_logs(
        retention_days=7,
        batch_size=10,
        now=8 * 86400,
    )

    assert report["deleted"] == 1
    with sqlite3.connect(service.persistence.db_path) as db:
        remaining = [
            row[0]
            for row in db.execute(
                "SELECT content FROM messagelog WHERE group_id=? ORDER BY id",
                (chat_id,),
            ).fetchall()
        ]
    assert remaining == ["消息2", "消息3", "消息4", "消息5"]


def test_learning_run_ledger_is_append_only_and_filterable(tmp_path):
    service = _database_service(tmp_path)
    for status in ("insufficient_context", "completed"):
        service.record_learning_mining_run(
            {
                "pipeline": "expression",
                "chat_id": "ff:GroupMessage:4",
                "batch_id": status,
                "raw_count": 20,
                "normalized_count": 12,
                "required_count": 30,
                "status": status,
                "reason": status,
                "details": {"status": status},
            }
        )

    rows = service.list_learning_mining_runs(
        pipeline="expression",
        chat_id="ff:GroupMessage:4",
    )

    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"insufficient_context", "completed"}


def test_quarantined_pipeline_returns_no_logs_until_retry_time(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:5", 4)
    logs = service.get_learning_logs("expression", "ff:GroupMessage:5", limit=10)
    service.advance_learning_checkpoint(
        "expression",
        "ff:GroupMessage:5",
        0,
        status="quarantined",
        failure_count=3,
        retry_at=time.time() + 600,
        last_error="provider unavailable",
    )

    assert service.get_learning_logs("expression", "ff:GroupMessage:5", limit=10) == []
    checkpoint = service.list_learning_checkpoints(
        pipeline="expression",
        chat_id="ff:GroupMessage:5",
    )[0]
    assert checkpoint["failure_count"] == 3
    assert checkpoint["cursor_log_id"] == 0
    assert len(logs) == 4


def test_learning_run_ledger_retry_is_idempotent_by_run_id(tmp_path):
    service = _database_service(tmp_path)
    payload = {
        "run_id": "fixed-run-id",
        "pipeline": "jargon",
        "chat_id": "ff:GroupMessage:6",
        "status": "completed",
    }
    service.record_learning_mining_run(payload)
    service.record_learning_mining_run({**payload, "status": "completed_retry"})

    rows = service.list_learning_mining_runs(chat_id="ff:GroupMessage:6")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "fixed-run-id"
    assert rows[0]["mining_run_id"] == "fixed-run-id"
    assert rows[0]["status"] == "completed_retry"


def test_learning_snapshot_reads_pipeline_views_from_one_transaction(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:snapshot", 6)
    service.ensure_learning_checkpoint("expression", "ff:GroupMessage:snapshot")
    service.ensure_learning_checkpoint("jargon", "ff:GroupMessage:snapshot")
    service.advance_learning_checkpoint("expression", "ff:GroupMessage:snapshot", 3)
    service.advance_learning_checkpoint("jargon", "ff:GroupMessage:snapshot", 1)

    snapshot = service.load_learning_snapshot(
        "ff:GroupMessage:snapshot",
        limit=10,
        replay_recent={"expression": 0, "jargon": 0},
    )

    assert [item.content for item in snapshot["pipeline_logs"]["expression"]] == [
        "消息4", "消息5", "消息6"
    ]
    assert [item.content for item in snapshot["pipeline_logs"]["jargon"]] == [
        "消息2", "消息3", "消息4", "消息5", "消息6"
    ]


def test_expression_and_jargon_share_one_mining_run_id(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:shared-run", 3)
    service.memory_engine = SimpleNamespace(write_service=SimpleNamespace())
    config = AstrMaiConfig(
        evolution={
            "min_mining_context": 1,
            "expression_min_valid_messages": 3,
            "jargon_min_valid_messages": 2,
            "expression_overlap_messages": 0,
            "jargon_overlap_messages": 0,
        }
    )
    manager = EvolutionManager(service, SimpleNamespace(config=config), config=config)

    async def mine_expression(_group_id, _logs):
        manager.expression_miner.last_report = {
            "candidate_count": 0,
            "enrichment": {"terminal": True},
        }
        return []

    async def mine_jargon(_group_id, _logs):
        manager.jargon_miner.last_report = {
            "candidate_count": 0,
            "enrichment": {"terminal": True},
        }
        return []

    class PersistenceResult:
        saved = 0
        deduplicated = 0
        complete = True

        @staticmethod
        def to_report():
            return {"saved": 0, "deduplicated": 0, "complete": True}

    manager.expression_miner.mine = mine_expression
    manager.jargon_miner.mine = mine_jargon
    manager._save_patterns = lambda *_args, **_kwargs: asyncio.sleep(
        0, result=PersistenceResult()
    )
    manager._save_jargons = lambda *_args, **_kwargs: asyncio.sleep(0, result=0)

    async def run():
        snapshot = await manager._load_learning_snapshot(
            "ff:GroupMessage:shared-run", 10
        )
        return await manager._process_mining_snapshot(
            "ff:GroupMessage:shared-run",
            snapshot,
            run_id="shared-mining-run",
        )

    outcomes = asyncio.run(run())
    rows = service.list_learning_mining_runs(chat_id="ff:GroupMessage:shared-run")

    assert set(outcomes) == {"expression", "jargon"}
    assert {row["mining_run_id"] for row in rows} == {"shared-mining-run"}
    assert {row["run_id"] for row in rows} == {
        "shared-mining-run:expression",
        "shared-mining-run:jargon",
    }


def test_mining_outcome_uses_stable_run_id_for_same_snapshot():
    metadata = {}

    class Store:
        async def set_meta(self, key, value):
            metadata[key] = value

    manager = EvolutionManager.__new__(EvolutionManager)
    manager._last_mining_outcomes = {}
    manager.db = SimpleNamespace(memory_engine=SimpleNamespace(v2_store=Store()))
    logs = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

    async def record():
        await manager._record_mining_outcome(
            "group-1", logs, status="failed", reason="provider_error"
        )
        first = manager._last_mining_outcomes["group-1"]["run_id"]
        await manager._record_mining_outcome(
            "group-1", logs, status="retry_wait", reason="retry"
        )
        second = manager._last_mining_outcomes["group-1"]["run_id"]
        return first, second

    first, second = asyncio.run(record())
    assert first == second == manager._mining_run_id("group-1", logs)


def test_pipeline_failure_count_survives_manager_restart_and_quarantines(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:7", 4)
    config = AstrMaiConfig(
        evolution={
            "learning_pipeline_max_failures": 3,
            "learning_pipeline_quarantine_sec": 600,
        }
    )

    async def fail_once():
        manager = EvolutionManager(service, SimpleNamespace(config=config), config=config)

        async def _mine(_group_id, _logs):
            manager.expression_miner.last_report = {
                "reason": "provider_failed",
                "normalized_messages": 4,
                "candidate_count": 1,
                "enrichment": {"terminal": False, "retryable": True},
            }
            return []

        manager.expression_miner.mine = _mine
        logs = service.get_learning_logs(
            "expression",
            "ff:GroupMessage:7",
            limit=10,
        )
        return await manager._run_learning_pipeline(
            "expression",
            "ff:GroupMessage:7",
            logs,
        )

    first = asyncio.run(fail_once())
    second = asyncio.run(fail_once())
    third = asyncio.run(fail_once())

    assert first["status"] == "failed"
    assert second["status"] == "failed"
    assert third["status"] == "quarantined"
    checkpoint = service.list_learning_checkpoints(
        pipeline="expression",
        chat_id="ff:GroupMessage:7",
    )[0]
    assert checkpoint["failure_count"] == 3
    assert checkpoint["retry_at"] > time.time()
    assert checkpoint["cursor_log_id"] == 0
    assert service.get_learning_logs(
        "expression",
        "ff:GroupMessage:7",
        limit=10,
    ) == []


def test_learning_pipeline_shared_timeout_keeps_cursor_for_retry(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:timeout", 4)
    config = AstrMaiConfig(
        evolution={
            "learning_pipeline_max_failures": 3,
            "learning_pipeline_quarantine_sec": 600,
        }
    )
    config.evolution.learning_pipeline_timeout_sec = 0.02
    manager = EvolutionManager(service, SimpleNamespace(config=config), config=config)

    async def _slow_mine(_group_id, _logs):
        await asyncio.sleep(0.2)
        return []

    manager.expression_miner.mine = _slow_mine
    logs = service.get_learning_logs(
        "expression",
        "ff:GroupMessage:timeout",
        limit=10,
    )

    result = asyncio.run(
        manager._run_learning_pipeline(
            "expression",
            "ff:GroupMessage:timeout",
            logs,
        )
    )

    assert result["status"] == "failed"
    assert result["reason"] == "learning_pipeline_timeout:0.02s"
    assert result["error_type"] == "TimeoutError"
    assert result["cursor_after"] == result["cursor_before"]
    assert len(service.get_learning_logs("expression", "ff:GroupMessage:timeout", limit=10)) == 4


def test_batch_checkpoint_initialization_preserves_pipeline_cursor_rules(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:8", 8, processed_until=5)
    _add_logs(service, "ff:GroupMessage:9", 4, processed_until=2)

    assert service.ensure_learning_checkpoints_for_groups("jargon") == 2
    assert service.ensure_learning_checkpoints_for_groups("jargon") == 0
    checkpoints = {
        item["chat_id"]: item
        for item in service.list_learning_checkpoints(pipeline="jargon")
    }

    assert checkpoints["ff:GroupMessage:8"]["cursor_log_id"] == 5
    assert checkpoints["ff:GroupMessage:9"]["cursor_log_id"] == 10

    assert service.ensure_learning_checkpoints_for_groups(
        "expression",
        replay_recent=3,
    ) == 2
    expression = {
        item["chat_id"]: item
        for item in service.list_learning_checkpoints(pipeline="expression")
    }
    assert expression["ff:GroupMessage:8"]["cursor_log_id"] == 5
    assert expression["ff:GroupMessage:9"]["cursor_log_id"] == 9


def test_checkpoint_retry_reset_preserves_cursor_and_supports_filtered_pagination(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:10", 5)
    logs = service.get_learning_logs("expression", "ff:GroupMessage:10", limit=10)
    service.advance_learning_checkpoint(
        "expression",
        "ff:GroupMessage:10",
        logs[2].id,
        status="quarantined",
        failure_count=4,
        retry_at=time.time() + 3600,
        last_error="provider unavailable",
    )

    filtered = service.list_learning_checkpoints(
        pipeline="expression",
        status="quarantined",
        limit=1,
        offset=0,
    )
    assert len(filtered) == 1
    assert service.count_learning_checkpoints(
        pipeline="expression",
        status="quarantined",
    ) == 1

    reset = service.reset_learning_checkpoint("expression", "ff:GroupMessage:10")

    assert reset["cursor_log_id"] == logs[2].id
    assert reset["last_status"] == "manual_retry"
    assert reset["failure_count"] == 0
    assert reset["retry_at"] == 0
    assert reset["last_error"] == ""


def test_learning_run_retention_applies_age_and_per_scope_limits(tmp_path):
    service = _database_service(tmp_path)
    now = time.time()
    for index in range(6):
        service.record_learning_mining_run(
            {
                "run_id": f"expression-{index}",
                "pipeline": "expression",
                "chat_id": "ff:GroupMessage:11",
                "status": "completed" if index % 2 else "failed",
                "created_at": now - (10 - index),
            }
        )
    service.record_learning_mining_run(
        {
            "run_id": "old-jargon",
            "pipeline": "jargon",
            "chat_id": "ff:GroupMessage:11",
            "status": "completed",
            "created_at": now - 40 * 86400,
        }
    )
    service.record_learning_mining_run(
        {
            "run_id": "other-chat",
            "pipeline": "expression",
            "chat_id": "ff:GroupMessage:12",
            "status": "completed",
            "created_at": now,
        }
    )

    report = service.purge_learning_mining_runs(
        retention_days=30,
        max_per_pipeline_chat=3,
    )
    remaining = service.list_learning_mining_runs(
        pipeline="expression",
        chat_id="ff:GroupMessage:11",
        limit=20,
    )

    assert report["deleted_by_age"] == 1
    assert report["deleted_by_limit"] == 3
    assert [item["run_id"] for item in remaining] == [
        "expression-5",
        "expression-4",
        "expression-3",
    ]
    assert service.count_learning_mining_runs(chat_id="ff:GroupMessage:12") == 1
    assert service.list_learning_mining_runs(status="failed", limit=2, offset=0)
