import sqlite3

import pytest

from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service


def test_checkpoint_read_returns_revision_and_semantics(tmp_path):
    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:contract", 2)

    checkpoint = service.ensure_learning_checkpoint("jargon", "ff:GroupMessage:contract")

    assert checkpoint["revision"] == 0
    assert checkpoint["cursor_semantics"] == "legacy_batch_atomic_v1"
    assert checkpoint["pipeline_version"] == "legacy-unversioned"


def test_stale_revision_and_cursor_cannot_overwrite_checkpoint(tmp_path):
    service = _database_service(tmp_path)
    chat_id = "ff:GroupMessage:cas"
    _add_logs(service, chat_id, 2)
    checkpoint = service.ensure_learning_checkpoint("jargon", chat_id)

    first = service.commit_learning_pipeline(
        pipeline="jargon", chat_id=chat_id,
        expected_revision=checkpoint["revision"],
        expected_cursor=checkpoint["cursor_log_id"], cursor_after=1,
        batch_id="batch-1", status="completed", pipeline_version="jargon-cursor-state-v1",
        run_payload={"run_id": "run-1", "status": "completed", "batch_id": "batch-1"},
    )
    stale = service.commit_learning_pipeline(
        pipeline="jargon", chat_id=chat_id,
        expected_revision=checkpoint["revision"],
        expected_cursor=checkpoint["cursor_log_id"], cursor_after=2,
        batch_id="batch-2", status="completed", pipeline_version="jargon-cursor-state-v1",
        run_payload={"run_id": "run-2", "status": "completed", "batch_id": "batch-2"},
    )

    assert first["committed"] is True
    assert first["revision_after"] == 1
    assert stale["committed"] is False
    assert stale["conflict"] is True
    current = service.ensure_learning_checkpoint("jargon", chat_id)
    assert current["revision"] == 1
    assert current["cursor_log_id"] == 1


def test_failed_or_partial_settlement_cannot_advance_cursor(tmp_path):
    service = _database_service(tmp_path)
    chat_id = "ff:GroupMessage:immutable"
    _add_logs(service, chat_id, 3)
    checkpoint = service.ensure_learning_checkpoint("expression", chat_id)

    result = service.commit_learning_pipeline(
        pipeline="expression", chat_id=chat_id,
        expected_revision=checkpoint["revision"],
        expected_cursor=checkpoint["cursor_log_id"], cursor_after=3,
        cursor_upper_bound=3, batch_id="failed-batch", status="failed",
        pipeline_version="expression-cursor-state-v1",
        run_payload={"run_id": "failed-run", "status": "failed", "batch_id": "failed-batch"},
    )

    assert result["committed"] is True
    current = service.ensure_learning_checkpoint("expression", chat_id)
    assert current["cursor_log_id"] == checkpoint["cursor_log_id"]
    assert current["last_status"] == "failed"


def test_cursor_regression_and_out_of_scope_are_blocked(tmp_path):
    service = _database_service(tmp_path)
    chat_id = "ff:GroupMessage:bounds"
    _add_logs(service, chat_id, 2)
    checkpoint = service.ensure_learning_checkpoint("jargon", chat_id)

    regression = service.commit_learning_pipeline(
        pipeline="jargon", chat_id=chat_id,
        expected_revision=checkpoint["revision"], expected_cursor=1,
        cursor_after=0, batch_id="regression", status="completed",
        pipeline_version="jargon-cursor-state-v1",
        run_payload={"run_id": "regression", "status": "completed", "batch_id": "regression"},
    )
    out_of_scope = service.commit_learning_pipeline(
        pipeline="jargon", chat_id=chat_id,
        expected_revision=checkpoint["revision"], expected_cursor=0,
        cursor_after=9, cursor_upper_bound=2, batch_id="out-of-scope", status="completed",
        pipeline_version="jargon-cursor-state-v1",
        run_payload={"run_id": "out-of-scope", "status": "completed", "batch_id": "out-of-scope"},
    )

    assert regression["blocked"] and regression["failure_kind"] == "cursor_regression"
    assert out_of_scope["blocked"] and out_of_scope["failure_kind"] == "cursor_out_of_scope"


def test_same_run_result_digest_change_is_conflict(tmp_path):
    service = _database_service(tmp_path)
    chat_id = "ff:GroupMessage:digest"
    _add_logs(service, chat_id, 3)
    checkpoint = service.ensure_learning_checkpoint("expression", chat_id)
    base = dict(
        pipeline="expression", chat_id=chat_id,
        expected_revision=checkpoint["revision"], expected_cursor=0,
        cursor_after=2, batch_id="digest-batch", status="completed",
        pipeline_version="expression-cursor-state-v1",
    )
    first = service.commit_learning_pipeline(
        **base, run_payload={"run_id": "digest-run", "status": "completed", "batch_id": "digest-batch", "candidate_count": 1}
    )
    replay = service.commit_learning_pipeline(
        **base, run_payload={"run_id": "digest-run", "status": "completed", "batch_id": "digest-batch", "candidate_count": 2}
    )

    assert first["committed"] is True
    assert replay["conflict"] is True
    assert replay.get("idempotent") is False


def test_missing_planned_columns_fail_closed(tmp_path):
    service = _database_service(tmp_path)
    with sqlite3.connect(service.persistence.db_path) as conn:
        conn.execute("ALTER TABLE learning_pipeline_checkpoint RENAME TO learning_pipeline_checkpoint_new")
        conn.execute("""CREATE TABLE learning_pipeline_checkpoint (
            pipeline TEXT NOT NULL, chat_id TEXT NOT NULL,
            cursor_log_id INTEGER NOT NULL DEFAULT 0,
            last_batch_id TEXT NOT NULL DEFAULT '', last_status TEXT NOT NULL DEFAULT '',
            failure_count INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0, PRIMARY KEY (pipeline, chat_id)
        )""")
        conn.commit()
    with pytest.raises(RuntimeError, match="missing_planned_columns"):
        service.ensure_learning_checkpoint("jargon", "ff:GroupMessage:missing")
