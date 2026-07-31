from __future__ import annotations

import json
import sqlite3

from astrmai.infrastructure.persistence.architecture_migration_audit import (
    LATEST_ARCHITECTURE_SCHEMA_VERSION,
    audit_architecture_database,
    inspect_architecture_migration,
)
from astrmai.infrastructure.persistence.persistence_schema import (
    _MIGRATIONS,
    _run_migrations,
)


def _create_current_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE messagelog (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT,
            event_id TEXT,
            event_schema_version INTEGER,
            platform_message_id TEXT,
            chat_kind TEXT,
            role TEXT,
            message_kind TEXT,
            is_bot INTEGER,
            reply_target_event_id TEXT,
            reply_target_actor_id TEXT,
            reply_target_actor_name TEXT,
            quote_event_id TEXT,
            at_actor_ids TEXT,
            topic_epoch INTEGER,
            causal_parent_event_id TEXT,
            source_event_ids TEXT,
            provenance TEXT,
            image_refs TEXT,
            interaction_kind TEXT,
            recalled INTEGER,
            outcome TEXT
        );
        CREATE INDEX ix_messagelog_event_id ON messagelog(event_id);
        CREATE TABLE chat_states (
            chat_id TEXT PRIMARY KEY,
            chat_kind TEXT,
            last_real_user_activity_at REAL,
            last_committed_bot_reply_at REAL,
            next_proactive_due_at REAL,
            proactive_generation INTEGER,
            unanswered_proactive_count INTEGER,
            last_proactive_commit_id TEXT,
            last_proactive_cancel_reason TEXT,
            proactive_claim_token TEXT,
            proactive_claimed_at REAL
        );
        CREATE INDEX ix_chat_states_next_proactive_due_at
            ON chat_states(next_proactive_due_at);
        CREATE TABLE reply_commit_outbox (
            commit_id TEXT PRIMARY KEY,
            committed_turn_json TEXT NOT NULL,
            repair_context_json TEXT NOT NULL DEFAULT '{}',
            consumer_status_json TEXT NOT NULL DEFAULT '{}',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX ix_reply_commit_outbox_next_retry_at
            ON reply_commit_outbox(next_retry_at);
        PRAGMA user_version = 73;
        """
    )


def _insert_message(
    db: sqlite3.Connection,
    *,
    event_id: str,
    sender_id: str,
    content: str,
    reply_target_event_id: str = "",
    reply_target_actor_id: str = "",
    at_actor_ids: str = "[]",
) -> None:
    db.execute(
        """
        INSERT INTO messagelog (
            group_id, sender_id, sender_name, content, event_id,
            event_schema_version, platform_message_id, chat_kind, role,
            message_kind, is_bot, reply_target_event_id,
            reply_target_actor_id, reply_target_actor_name, quote_event_id,
            at_actor_ids, topic_epoch, causal_parent_event_id,
            source_event_ids, provenance, image_refs, interaction_kind,
            recalled, outcome
        ) VALUES (
            'ff:GroupMessage:fixture', ?, '匿名用户', ?, ?,
            1, '', 'group', 'user', 'text', 0, ?, ?, '', '',
            ?, 0, '', '[]', 'fixture', '[]', '', 0, ''
        )
        """,
        (
            sender_id,
            content,
            event_id,
            reply_target_event_id,
            reply_target_actor_id,
            at_actor_ids,
        ),
    )


def test_migration_audit_is_repeatable_and_never_repairs_unknown_actor(tmp_path):
    path = tmp_path / "astrmai.db"
    with sqlite3.connect(path) as db:
        _create_current_schema(db)
        _insert_message(
            db,
            event_id="evt:unknown",
            sender_id="unknown",
            content="保留未知人物",
        )
        db.commit()

    first = audit_architecture_database(path)
    second = audit_architecture_database(path)

    assert first == second
    assert first.ready is True
    assert first.unknown_actor_rows == 1
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT sender_id, content FROM messagelog WHERE event_id = 'evt:unknown'"
        ).fetchone()
    assert row == ("unknown", "保留未知人物")


def test_migration_audit_reports_conflicts_unresolved_targets_and_invalid_json():
    db = sqlite3.connect(":memory:")
    _create_current_schema(db)
    _insert_message(db, event_id="evt:dup", sender_id="actor-a", content="第一条")
    _insert_message(db, event_id="evt:dup", sender_id="actor-b", content="冲突条目")
    _insert_message(
        db,
        event_id="evt:target",
        sender_id="actor-c",
        content="目标缺失",
        reply_target_event_id="evt:parent",
        at_actor_ids="not-json",
    )
    db.commit()
    before = db.total_changes

    report = inspect_architecture_migration(db)

    assert report.ready is False
    assert report.duplicate_event_id_groups == 1
    assert report.conflicting_event_id_groups == 1
    assert report.unresolved_target_evidence_rows == 1
    assert report.invalid_json_fields == {"messagelog.at_actor_ids": 1}
    assert db.total_changes == before
    assert json.loads(json.dumps(report.as_dict(), ensure_ascii=False))["ready"] is False
    db.close()


def test_migration_audit_reports_legacy_schema_without_mutating_it():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE messagelog (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            sender_id TEXT,
            content TEXT
        );
        CREATE TABLE chat_states (chat_id TEXT PRIMARY KEY);
        INSERT INTO messagelog(group_id, sender_id, content)
            VALUES ('legacy', 'actor-a', '旧数据');
        """
    )
    before = db.total_changes

    report = inspect_architecture_migration(db)

    assert report.ready is False
    assert "event_id" in report.missing_columns["messagelog"]
    assert "proactive_generation" in report.missing_columns["chat_states"]
    assert set(report.missing_indexes) == {
        "ix_messagelog_event_id",
        "ix_chat_states_next_proactive_due_at",
        "ix_reply_commit_outbox_next_retry_at",
    }
    assert report.table_row_counts == {"messagelog": 1, "chat_states": 0}
    assert db.total_changes == before
    db.close()


def test_context_architecture_migrations_upgrade_v36_and_are_idempotent():
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE messagelog (
            id INTEGER PRIMARY KEY,
            group_id TEXT,
            sender_id TEXT,
            sender_name TEXT,
            content TEXT,
            timestamp REAL,
            processed INTEGER
        );
        CREATE TABLE chat_states (
            chat_id TEXT PRIMARY KEY,
            last_reply_time REAL DEFAULT 0,
            next_wakeup_timestamp REAL DEFAULT 0
        );
        INSERT INTO messagelog(
            group_id, sender_id, sender_name, content, timestamp, processed
        ) VALUES ('legacy-chat', 'unknown', '匿名用户', '旧记录', 1000, 0);
        INSERT INTO chat_states(
            chat_id, last_reply_time, next_wakeup_timestamp
        ) VALUES ('legacy-chat', 900, 1200);
        PRAGMA user_version = 36;
        """
    )

    _run_migrations(db)
    db.commit()
    first = inspect_architecture_migration(db)
    first_snapshot = db.iterdump()
    first_dump = "\n".join(first_snapshot)

    _run_migrations(db)
    db.commit()
    second = inspect_architecture_migration(db)
    second_dump = "\n".join(db.iterdump())

    assert max(version for version, _ddl in _MIGRATIONS) == LATEST_ARCHITECTURE_SCHEMA_VERSION
    assert first.ready is True
    assert second == first
    assert second_dump == first_dump
    assert second.unknown_actor_rows == 1
    state = db.execute(
        """
        SELECT last_real_user_activity_at, last_committed_bot_reply_at,
               next_proactive_due_at
        FROM chat_states WHERE chat_id = 'legacy-chat'
        """
    ).fetchone()
    assert state == (900.0, 900.0, 1200.0)
    db.close()
