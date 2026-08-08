from __future__ import annotations

import sqlite3

from scripts.audit_learning_data import build_report, clean_memory_db


def _create_plugin_db(path):
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE messagelog (
                id INTEGER PRIMARY KEY,
                group_id TEXT NOT NULL
            );
            CREATE TABLE learning_pipeline_checkpoint (
                pipeline TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                cursor_log_id INTEGER NOT NULL DEFAULT 0,
                last_status TEXT NOT NULL DEFAULT '',
                failure_count INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE learning_mining_run (
                pipeline TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO messagelog(id, group_id) VALUES
                (1, 'ff:GroupMessage:1'),
                (2, 'ff:GroupMessage:1');
            INSERT INTO learning_pipeline_checkpoint VALUES
                ('expression', 'ff:GroupMessage:1', 0, 'waiting_for_evidence', 0, 0),
                ('jargon', 'ff:GroupMessage:1', 1, 'completed', 0, 0);
            INSERT INTO learning_mining_run VALUES
                ('expression', 'waiting'),
                ('jargon', 'completed');
            """
        )


def _create_memory_db(path):
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE canonical_memories (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                dedup_key TEXT NOT NULL DEFAULT '',
                update_time REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE canonical_fts (memory_id TEXT);
            CREATE TABLE memory_dedup_aliases (
                alias_key TEXT PRIMARY KEY,
                canonical_memory_id TEXT NOT NULL
            );
            INSERT INTO canonical_memories VALUES
                ('expr-r', 'expression_pattern', 'rejected', 'expression:one', 1),
                ('expr-a', 'expression_pattern', 'active', 'expression:two', 2),
                ('jar-r', 'jargon', 'rejected', 'jargon:group-1:old', 3),
                ('jar-a', 'jargon', 'active', 'jargon:global:new', 4);
            INSERT INTO canonical_fts VALUES ('expr-r'), ('expr-a'), ('jar-r'), ('jar-a');
            INSERT INTO memory_dedup_aliases VALUES ('old-expression', 'expr-r');
            """
        )


def test_learning_audit_is_read_only_and_reports_both_databases(tmp_path):
    plugin_db = tmp_path / "astrmai.db"
    memory_db = tmp_path / "memory_v2.db"
    _create_plugin_db(plugin_db)
    _create_memory_db(memory_db)

    report = build_report(plugin_db=plugin_db, memory_db=memory_db)

    assert report["mode"] == "read_only"
    assert report["plugin_db"]["runs"]["expression"]["waiting"] == 1
    assert report["plugin_db"]["evidence_backlog"][0]["pending_messages"] == 2
    assert report["memory_db"]["expression_status"] == {"active": 1, "rejected": 1}
    with sqlite3.connect(memory_db) as db:
        assert db.execute("SELECT COUNT(*) FROM canonical_memories").fetchone()[0] == 4


def test_learning_cleanup_requires_selected_actions_and_creates_backup(tmp_path):
    memory_db = tmp_path / "memory_v2.db"
    backup_dir = tmp_path / "backups"
    _create_memory_db(memory_db)

    result = clean_memory_db(
        memory_db,
        backup_dir=backup_dir,
        delete_rejected_expression=True,
        delete_rejected_jargon=True,
    )

    assert result["deleted"] == {
        "expression_pattern:rejected": 1,
        "jargon:rejected": 1,
    }
    assert next(backup_dir.glob("memory_v2.learning-cleanup-*.db")).is_file()
    with sqlite3.connect(memory_db) as db:
        remaining = db.execute(
            "SELECT id FROM canonical_memories ORDER BY id"
        ).fetchall()
        assert remaining == [("expr-a",), ("jar-a",)]
        assert db.execute(
            "SELECT COUNT(*) FROM canonical_fts WHERE memory_id IN ('expr-r', 'jar-r')"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM memory_dedup_aliases WHERE canonical_memory_id = 'expr-r'"
        ).fetchone()[0] == 0


def test_learning_cleanup_chunks_large_id_sets(tmp_path):
    memory_db = tmp_path / "memory_v2.db"
    backup_dir = tmp_path / "backups"
    _create_memory_db(memory_db)
    rows = [
        (f"bulk-{index}", "expression_pattern", "rejected", f"expression:bulk:{index}", index)
        for index in range(1205)
    ]
    with sqlite3.connect(memory_db) as db:
        db.executemany(
            "INSERT INTO canonical_memories VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        db.executemany(
            "INSERT INTO canonical_fts VALUES (?)",
            [(item[0],) for item in rows],
        )
        db.executemany(
            "INSERT INTO memory_dedup_aliases VALUES (?, ?)",
            [(f"alias-{index}", item[0]) for index, item in enumerate(rows)],
        )
        db.commit()

    result = clean_memory_db(
        memory_db,
        backup_dir=backup_dir,
        delete_rejected_expression=True,
    )

    assert result["deleted"]["expression_pattern:rejected"] == 1206
    with sqlite3.connect(memory_db) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM canonical_memories WHERE kind = 'expression_pattern' AND status = 'rejected'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM canonical_fts WHERE memory_id LIKE 'bulk-%'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM memory_dedup_aliases WHERE canonical_memory_id LIKE 'bulk-%'"
        ).fetchone()[0] == 0
