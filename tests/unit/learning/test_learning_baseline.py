from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.audit_learning_data import (
    build_gold_sampling_frame,
    build_plugin_baseline,
    canonical_json_bytes,
    inspect_sqlite_source,
    iter_trace_records,
    open_sqlite_read_only,
    snapshot_file_state,
    build_learning_funnel,
    build_memory_baseline,
    scan_artifact_for_sensitive_values,
    _trace_summary,
)
from scripts.build_learning_baseline import _path_overlaps, _portable_value


def _plugin_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE messagelog (
                id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT,
                message TEXT, timestamp REAL, processed INTEGER DEFAULT 0,
                event_schema_version INTEGER DEFAULT 0, event_id TEXT
            );
            CREATE TABLE learning_pipeline_checkpoint (
                pipeline TEXT, chat_id TEXT, cursor_log_id INTEGER,
                last_batch_id TEXT, last_status TEXT, failure_count INTEGER,
                retry_at REAL
            );
            CREATE TABLE learning_mining_run (
                run_id TEXT, pipeline TEXT, status TEXT, reason TEXT,
                candidate_count INTEGER, saved_count INTEGER,
                deduplicated_count INTEGER, details_json TEXT
            );
            INSERT INTO messagelog VALUES
                (1, 'g1', 'u1', 'before', 99, 1, 1, 'e1'),
                (2, 'g2', 'u2', 'other', 101, 0, 1, 'e2'),
                (3, 'g1', 'u1', 'start', 100, 1, 1, 'e3'),
                (4, 'g1', '', 'end', 110, 0, 0, NULL);
            INSERT INTO learning_pipeline_checkpoint VALUES
                ('expression', 'g1', 1, 'b1', 'waiting', 1, 0),
                ('jargon', 'g1', 3, 'b2', 'completed', 0, 0);
            INSERT INTO learning_mining_run VALUES
                ('r1', 'expression', 'completed', 'ok', 2, 1, 1, '{"stages": []}'),
                ('r2', 'jargon', 'future_state', 'unknown', 1, 0, 0, '{bad');
            """
        )


def _memory_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE canonical_memories (id TEXT PRIMARY KEY, kind TEXT, status TEXT, dedup_key TEXT)")
        db.execute("INSERT INTO canonical_memories VALUES ('m1', 'jargon', 'active', 'jargon:m1')")
        db.commit()


def test_read_only_uri_and_sidecars(tmp_path: Path):
    path = tmp_path / "空 格.db"
    _plugin_db(path)
    before = snapshot_file_state(path)
    with open_sqlite_read_only(path) as db:
        assert db.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.execute("CREATE TABLE forbidden(value TEXT)")
    assert snapshot_file_state(path) == before
    assert not (path.parent / f"{path.name}-wal").exists()


def test_plugin_baseline_uses_scope_count_and_window_unknowns(tmp_path: Path):
    path = tmp_path / "astrmai.db"
    _plugin_db(path)
    with open_sqlite_read_only(path) as db:
        report = build_plugin_baseline(
            db,
            window=(100.0, 110.0),
            query_catalog={},
        )
    assert report["messages"]["window"]["numerator"] == 2
    backlog = {(row["pipeline"], row["chat_id"]): row for row in report["checkpoint_backlog"]}
    assert backlog[("expression", "g1")]["pending_count"] == 2
    assert backlog[("expression", "g1")]["id_span"] == 3
    assert report["runs"]["unknown_status_count"] == 1
    assert report["runs"]["details_json_invalid"] == 1


def test_trace_conflict_and_sampling_are_deterministic(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"turn_id":"t1","reply_sent":true}\n'
        '{"turn_id":"t1","reply_sent":true}\n'
        '{"turn_id":"t1","reply_sent":false}\n'
        '{"turn_id":"t2","reply_sent":null}\n'
        '{bad}\n',
        encoding="utf-8",
    )
    records = list(iter_trace_records(trace))
    assert len(records) == 5
    assert sum(item["issue_code"] == "duplicate_conflict" for item in records) == 1
    assert sum(item["issue_code"] == "invalid_json" for item in records) == 1

    rows = [
        {"source_row_id": 2, "scope_id": "p:g:2", "candidate_id": "c2", "candidate_family": "jargon"},
        {"source_row_id": 1, "scope_id": "p:g:1", "candidate_id": "c1", "candidate_family": "expression"},
    ]
    first = build_gold_sampling_frame(rows, seed=20260915, query_version="v1")
    second = build_gold_sampling_frame(list(reversed(rows)), seed=20260915, query_version="v1")
    assert first == second
    assert all("label" not in row and "text" not in row for row in first)

    legacy = tmp_path / "legacy.json"
    legacy.write_text('[{"turn_id":"legacy-1"}]', encoding="utf-8")
    legacy_records = list(iter_trace_records(legacy))
    assert legacy_records[0]["format"] == "json"
    assert legacy_records[0]["record"]["turn_id"] == "legacy-1"


def test_funnel_does_not_infer_missing_join_or_empty_reply():
    result = build_learning_funnel(
        {"candidates": [{"candidate_id": "c1", "saved": True}]},
        {"canonical": []},
        {"selected": [{"candidate_id": "missing", "final_answer": ""}]},
    )
    assert result["edges"]["saved_to_canonical"]["joined_count"] is None
    assert result["edges"]["saved_to_canonical"]["status"] == "partial"
    assert result["edges"]["selected_to_reply"]["unknown_count"] == 1


def test_baseline_cli_rejects_apply_and_missing_sources(tmp_path: Path):
    script = Path("scripts/build_learning_baseline.py")
    missing = tmp_path / "missing.db"
    result = subprocess.run(
        [sys.executable, str(script), "--plugin-db", str(missing), "--apply"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "apply" not in subprocess.run(
        [sys.executable, str(script), "--help"], capture_output=True, text=True
    ).stdout.lower()


def test_path_overlap_rejects_repository_source_parent_and_symlink(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    source = tmp_path / "source.db"
    source.write_bytes(b"x")
    assert _path_overlaps(repo, repo)
    assert _path_overlaps(source.parent, source)
    assert _path_overlaps(source, source)
    link = tmp_path / "source-link.db"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    assert _path_overlaps(link, source)


def test_memory_missing_columns_returns_structured_result(tmp_path: Path):
    path = tmp_path / "memory.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE canonical_memories (id TEXT PRIMARY KEY)")
        db.commit()
    with open_sqlite_read_only(path) as db:
        result = build_memory_baseline(db, window=(0, 1), query_catalog={})
    assert result["schema"]["canonical_memories"]["status"] == "blocked"
    assert "kind" in result["schema"]["canonical_memories"]["missing_columns"]
    assert result["canonical"] == []


def test_sensitive_scanner_detects_secret_id_and_url_query(tmp_path: Path):
    path = tmp_path / "artifact.json"
    path.write_text('{"token":"secret", "group_id":"123456", "url":"https://example.invalid/?key=x"}', encoding="utf-8")
    kinds = {item["kind"] for item in scan_artifact_for_sensitive_values(path)}
    assert {"credential", "platform_id", "url_query"}.issubset(kinds)


def test_portable_value_hashes_all_identity_fields():
    source = {"id": "memory-raw-123", "chat_id": "8240782", "source_row_ids": [12, 13], "dedup_key": "jargon:raw", "event_id": "evt-1"}
    portable = _portable_value(source)
    assert portable["id"] != source["id"]
    assert portable["chat_id"].startswith("sha256:")
    assert portable["source_row_ids"] != source["source_row_ids"]
    assert portable["dedup_key"] != source["dedup_key"]
    assert portable["event_id"] != source["event_id"]


def test_trace_summary_filters_window_deduplicates_and_excludes_conflicts(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"turn_id":"t0","timestamp":99,"candidate_id":"outside","final_answer":"ok"}\n'
        '{"turn_id":"t1","timestamp":100,"candidate_id":"c1","final_answer":"ok"}\n'
        '{"turn_id":"t1","timestamp":100,"candidate_id":"c1","final_answer":"ok"}\n'
        '{"turn_id":"t2","timestamp":101,"candidate_id":"c2","final_answer":""}\n'
        '{"turn_id":"t2","timestamp":101,"candidate_id":"different","final_answer":"bad"}\n'
        '{"turn_id":"t3","timestamp":110,"candidate_id":"end","final_answer":"ok"}\n',
        encoding="utf-8",
    )
    summary = _trace_summary(trace, (100.0, 110.0))
    assert summary["record_count"] == 1
    assert summary["selected_count"] == 1
    assert summary["unknown_outcome_count"] == 0
    assert summary["conflict_count"] == 1
    assert summary["status"] == "partial"


def test_trace_without_timestamp_or_identity_is_partial_and_unknown(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"candidate_id":"c1"}\n', encoding="utf-8")
    summary = _trace_summary(trace, (100.0, 110.0))
    assert summary["status"] == "partial"
    assert summary["unknown_time_count"] == 1
    assert summary["unknown_identity_count"] == 1


def test_trace_missing_identity_is_counted_once_when_time_is_known(tmp_path: Path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"timestamp":100,"candidate_id":"c1"}\n', encoding="utf-8")
    summary = _trace_summary(trace, (100.0, 110.0))
    assert summary["unknown_identity_count"] == 1
    assert summary["metrics"]["outcome"]["unknown_count"] == 1


def test_saved_canonical_equal_strings_do_not_imply_join():
    result = build_learning_funnel(
        {"candidate_summary": {"saved_count": 1}, "candidates": [{"candidate_id": "same", "saved": True}]},
        {"canonical": [{"id": "same"}]},
        {},
    )
    edge = result["edges"]["saved_to_canonical"]
    assert edge["joined_count"] is None
    assert edge["unknown_count"] == 1
    assert edge["status"] == "partial"


def test_portable_source_hash_ignores_trace_mtime():
    first = {"sha256": "abc", "size_bytes": 12, "mtime_ns": 100, "format": "jsonl"}
    second = {"sha256": "abc", "size_bytes": 12, "mtime_ns": 200, "format": "jsonl"}
    assert canonical_json_bytes(_portable_value(first)) == canonical_json_bytes(_portable_value(second))


def test_cli_rejects_artifact_root_containing_source_before_creating_run(tmp_path: Path):
    plugin_db = tmp_path / "astrmai.db"
    memory_db = tmp_path / "memory_v2.db"
    _plugin_db(plugin_db)
    _memory_db(memory_db)
    (tmp_path / "turn.jsonl").write_text('{"turn_id":"t1"}\n', encoding="utf-8")
    (tmp_path / "raw.jsonl").write_text('{"turn_id":"r1"}\n', encoding="utf-8")
    command = [sys.executable, "scripts/build_learning_baseline.py", "--plugin-db", str(plugin_db), "--memory-db", str(memory_db),
               "--turn-trace", str(tmp_path / "turn.jsonl"), "--raw-trace", str(tmp_path / "raw.jsonl"),
               "--artifact-root", str(tmp_path), "--window-start", "1970-01-01T00:00:00Z", "--window-end", "1970-01-01T00:01:00Z",
               "--seed", "1", "--query-version", "learning-baseline-v1", "--code-revision", "a" * 40]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 4
    assert not list(tmp_path.glob("baseline-*"))


def test_corrupt_sqlite_never_writes_passed_manifest(tmp_path: Path):
    plugin_db = tmp_path / "astrmai.db"
    memory_db = tmp_path / "memory_v2.db"
    plugin_db.write_bytes(b"not sqlite")
    _memory_db(memory_db)
    (tmp_path / "turn.jsonl").write_text('{"turn_id":"t1"}\n', encoding="utf-8")
    (tmp_path / "raw.jsonl").write_text('{"turn_id":"r1"}\n', encoding="utf-8")
    result = subprocess.run([sys.executable, "scripts/build_learning_baseline.py", "--plugin-db", str(plugin_db), "--memory-db", str(memory_db),
                             "--turn-trace", str(tmp_path / "turn.jsonl"), "--raw-trace", str(tmp_path / "raw.jsonl"),
                             "--artifact-root", str(tmp_path / "artifacts"), "--window-start", "1970-01-01T00:00:00Z", "--window-end", "1970-01-01T00:01:00Z",
                             "--seed", "1", "--query-version", "learning-baseline-v1", "--code-revision", "a" * 40], capture_output=True, text=True)
    assert result.returncode == 4
    assert not list((tmp_path / "artifacts").glob("**/baseline_manifest.json"))


def test_baseline_cli_writes_complete_external_artifact(tmp_path: Path):
    plugin_db = tmp_path / "astrmai.db"
    memory_db = tmp_path / "memory_v2.db"
    _plugin_db(plugin_db)
    _memory_db(memory_db)
    turn_trace = tmp_path / "turn.jsonl"
    raw_trace = tmp_path / "raw.jsonl"
    turn_trace.write_text('{"turn_id":"t1","reply_sent":true}\n', encoding="utf-8")
    raw_trace.write_text('{"turn_id":"r1"}\n', encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    command = [
        sys.executable, "scripts/build_learning_baseline.py",
        "--plugin-db", str(plugin_db), "--memory-db", str(memory_db),
        "--turn-trace", str(turn_trace), "--raw-trace", str(raw_trace),
        "--artifact-root", str(artifact_root), "--window-start", "1970-01-01T00:01:40Z",
        "--window-end", "1970-01-01T00:01:50Z", "--seed", "20260915",
        "--query-version", "learning-baseline-v1", "--code-revision", "a" * 40,
    ]
    first = subprocess.run(command, capture_output=True, text=True)
    stat = turn_trace.stat()
    os.utime(turn_trace, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
    second = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 2, first.stderr
    assert second.returncode == 2, second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result["portable_manifest_hash"] == second_result["portable_manifest_hash"]
    first_dir = Path(first_result["artifact_path"])
    assert (first_dir / "baseline_manifest.json").is_file()
    assert json.loads((first_dir / "baseline_manifest.json").read_text(encoding="utf-8"))["status"] == "partial"
    assert json.loads((first_dir / "run_checks.json").read_text(encoding="utf-8"))["provider_calls"] == 0
