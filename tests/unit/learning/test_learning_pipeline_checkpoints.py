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


def test_learning_run_ledger_rejects_duplicate_run_id(tmp_path):
    service = _database_service(tmp_path)
    payload = {
        "run_id": "fixed-run-id",
        "pipeline": "jargon",
        "chat_id": "ff:GroupMessage:6",
        "status": "completed",
    }
    service.record_learning_mining_run(payload)

    try:
        service.record_learning_mining_run(payload)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("append-only ledger must reject duplicate run_id")


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
