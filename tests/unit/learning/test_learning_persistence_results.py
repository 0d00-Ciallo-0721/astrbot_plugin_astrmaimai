from astrmai.learning.mining.persistence_results import JargonSaveReport, PersistenceFailure


def test_jargon_save_report_preserves_conservation_and_empty_id_safety():
    failure = PersistenceFailure(
        candidate_id="candidate-1",
        content_hash="hash-1",
        failure_stage="persist",
        failure_kind="persist_empty_id",
        retryable=False,
        detail="writer_returned_empty_id",
    )
    report = JargonSaveReport(
        attempted=3,
        saved=1,
        deduplicated=1,
        failed=1,
        memory_ids=("memory-1", "memory-existing"),
        failures=(failure,),
    )

    assert report.conservation_valid
    assert not report.complete
    assert report.to_report()["failures"][0]["failure_kind"] == "persist_empty_id"


def test_jargon_save_report_does_not_repair_mismatched_counts():
    report = JargonSaveReport(
        attempted=2,
        saved=1,
        deduplicated=0,
        failed=0,
        memory_ids=("memory-1",),
    )

    assert not report.conservation_valid
    assert not report.complete
    assert report.to_report()["failure_kind"] == "persist_error"
