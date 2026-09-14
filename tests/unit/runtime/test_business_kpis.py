import sqlite3

from astrmai.infrastructure.runtime.business_kpis import aggregate_business_kpis


def _create_db(path, *, complete=True):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messagelog (outcome TEXT)")
        if complete:
            db.execute("CREATE TABLE memoryretrievaltrace (selected_memory_ids TEXT, confidence REAL)")
            db.execute("CREATE TABLE learning_mining_run (status TEXT)")
            db.execute("CREATE TABLE proactive_scenario_delivery (status TEXT)")
            db.execute("CREATE TABLE background_task_ledger (status TEXT)")
        db.commit()


def test_empty_persisted_outcome_is_unknown_not_success(tmp_path):
    path = tmp_path / "db.sqlite"
    _create_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO messagelog(outcome) VALUES (?)", [("",), (None,)])

    report = aggregate_business_kpis(path)

    assert report["source"] == "database"
    assert report["metrics"]["outcome_completeness"]["value"] == 0.0
    assert report["metrics"]["reply_success_rate"]["value"] is None
    assert report["metrics"]["reply_success_rate"]["reason"] == "outcome_missing"


def test_database_kpis_use_explicit_denominators(tmp_path):
    path = tmp_path / "db.sqlite"
    _create_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO messagelog(outcome) VALUES (?)", [("sent",), ("failed",), ("",)])
        db.executemany("INSERT INTO memoryretrievaltrace VALUES (?, ?)", [("[1]", 0.9), ("[]", 0.3)])
        db.executemany("INSERT INTO learning_mining_run VALUES (?)", [("saved",), ("timeout",)])
        db.executemany("INSERT INTO proactive_scenario_delivery VALUES (?)", [("sent",), ("skipped",)])
        db.executemany("INSERT INTO background_task_ledger VALUES (?)", [("replayed",), ("waiting",)])

    metrics = aggregate_business_kpis(path)["metrics"]

    assert metrics["reply_success_rate"]["numerator"] == 1
    assert metrics["reply_success_rate"]["denominator"] == 2
    assert metrics["reply_success_rate"]["unknown_count"] == 1
    assert metrics["memory_injection_rate"]["value"] == 0.5
    assert metrics["learning_saved_rate"]["value"] == 0.5
    assert metrics["learning_timeout_rate"]["value"] == 0.5
    assert metrics["terminal_settlement_coverage"]["value"] == 0.5
    assert metrics["proactive_delivery_rate"]["value"] is None
    assert metrics["proactive_delivery_rate"]["reason"] == "acknowledgement_missing"


def test_old_database_missing_tables_is_explicitly_unknown(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE messagelog (legacy TEXT)")

    metrics = aggregate_business_kpis(path)["metrics"]

    assert metrics["outcome_completeness"]["reason"] == "missing_table_or_column"
    assert metrics["memory_injection_rate"]["reason"] == "missing_table"
    assert metrics["learning_saved_rate"]["reason"] == "missing_table"


def test_trace_fallback_is_marked_derived_and_deduplicated():
    traces = [
        {
            "turn_id": "t1",
            "reply_sent": True,
            "llm_call_ledger": [{"call_id": "c1", "status": "success", "elapsed_ms": 10}],
            "stage_ledger": [{"status": "queue_timeout", "kind": "background_queue"}],
        },
        {
            "turn_id": "t1",
            "reply_sent": False,
            "llm_call_ledger": [{"call_id": "c1", "status": "error", "elapsed_ms": 99}],
        },
    ]

    report = aggregate_business_kpis(None, traces=traces)
    metrics = report["metrics"]

    assert report["source"] == "trace"
    assert metrics["reply_success_rate"]["source"] == "trace"
    assert metrics["reply_success_rate"]["value"] == 1.0
    assert metrics["queue_timeout_rate"]["value"] == 1.0
    assert metrics["provider_latency_p50"]["value"] == 10.0
    assert metrics["reply_success_rate"]["status"] == "derived"


def test_no_trace_does_not_claim_trace_source():
    report = aggregate_business_kpis(None)
    assert report["source"] == "unknown"
    assert report["metrics"]["reply_success_rate"]["source"] == "unknown"


def test_unknown_status_does_not_enter_learning_denominator(tmp_path):
    path = tmp_path / "db.sqlite"
    _create_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO learning_mining_run VALUES (?)", [("saved",), ("future_status",)])
        db.executemany("INSERT INTO proactive_scenario_delivery VALUES (?)", [("sent",), ("future_status",)])

    metrics = aggregate_business_kpis(path)["metrics"]
    assert metrics["learning_saved_rate"]["value"] == 1.0
    assert metrics["learning_saved_rate"]["denominator"] == 1
    assert metrics["learning_saved_rate"]["unknown_count"] == 1
    assert metrics["proactive_delivery_rate"]["value"] is None
    assert metrics["proactive_delivery_rate"]["reason"] == "acknowledgement_missing"
    assert metrics["proactive_delivery_rate"]["denominator"] == 1
    assert metrics["proactive_engagement_rate"]["value"] is None


def test_proactive_delivery_requires_platform_ack(tmp_path):
    path = tmp_path / "delivery.sqlite"
    _create_db(path)
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO proactive_scenario_delivery VALUES (?)",
            [("sent",), ("delivered",), ("delivery_failed",)],
        )

    metric = aggregate_business_kpis(path)["metrics"]["proactive_delivery_rate"]
    assert metric["numerator"] == 1
    assert metric["denominator"] == 3
    assert metric["status"] == "partial"
    assert "acknowledgement_missing" in metric["warnings"]


def test_partial_outcome_is_not_presented_as_complete(tmp_path):
    path = tmp_path / "db.sqlite"
    _create_db(path)
    with sqlite3.connect(path) as db:
        db.executemany("INSERT INTO messagelog(outcome) VALUES (?)", [("sent",), ("",)])
    metric = aggregate_business_kpis(path)["metrics"]["reply_success_rate"]
    assert metric["status"] == "partial"
    assert metric["unknown_count"] == 1


def test_jsonl_reader_skips_corrupt_and_truncated_lines(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(
        '{"turn_id":"a","reply_sent":true}\nnot-json\n{"turn_id":"b","reply_sent":',
        encoding="utf-8",
    )
    report = aggregate_business_kpis(path)
    assert report["source"] == "trace"
    assert report["metrics"]["reply_success_rate"]["denominator"] == 1


def test_trace_window_is_start_inclusive_end_exclusive():
    traces = [
        {"turn_id": "before", "started_at": 9, "reply_sent": True},
        {"turn_id": "at", "started_at": 10, "reply_sent": True},
        {"turn_id": "end", "started_at": 20, "reply_sent": False},
        {"turn_id": "missing", "reply_sent": True},
    ]
    metric = aggregate_business_kpis(None, traces=traces, start_ts=10, end_ts=20)["metrics"]["reply_success_rate"]
    assert metric["numerator"] == 1
    assert metric["denominator"] == 1
    assert metric["window_start"] == 10
    assert metric["window_end"] == 20


def test_exception_summary_is_bounded_and_redacted():
    # Public helper behavior is exercised indirectly through the module's safety contract.
    from astrmai.infrastructure.runtime.business_kpis import _safe_error_summary

    value = _safe_error_summary("token=secret-value " + "x" * 500)
    assert "secret-value" not in value
    assert len(value) <= 243
