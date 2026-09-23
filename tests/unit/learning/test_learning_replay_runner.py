from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from astrmai.learning.evaluation.contracts import ReplayManifest, canonical_hash
from astrmai.learning.evaluation.replay_runner import ReplayInputError, ReplaySecurityError, recorded_request_fingerprint, run_replay, sample_manifest_hash, sha256_file, snapshot_integrity, validate_output_file
from scripts.replay_learning_snapshot import main as replay_cli_main


def _manifest(source: Path, output: Path, sample: Path, *, mode: str = "deterministic", recordings: Path | None = None) -> ReplayManifest:
    integrity = snapshot_integrity(source)
    payload = json.loads(sample.read_text(encoding="utf-8"))
    return ReplayManifest(
        run_id="eval-fixed", mode=mode, source_snapshot_hash=integrity["sha256"],
        source_path_is_read_only=True, output_root=str(output),
        source_schema_version=integrity["user_version"], execution_schema_version=integrity["user_version"],
        migration_path=(), pipeline_version="p1", extractor_version="e1", fingerprint_version="f1",
        prompt_version="none",
        provider_id="p" if mode == "recorded" else "none",
        model_id="m" if mode == "recorded" else "none",
        request_fixture_hash=sha256_file(recordings) if mode == "recorded" and recordings else None,
        seed=20260915, network_policy="loopback-only", sample_manifest_hash=payload["manifest_hash"],
        created_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        source_snapshot_path=str(source), sample_manifest_path=str(sample), recordings_path=str(recordings) if recordings else None,
    )


@pytest.fixture
def fixture_paths(tmp_path: Path):
    source_dir = tmp_path / "source"
    output_dir = tmp_path / "output"
    source_dir.mkdir()
    source = source_dir / "snapshot.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO sample(value) VALUES ('redacted')")
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    return source, output_dir, sample


def test_deterministic_replay_is_read_only_and_repeatable(fixture_paths):
    source, output, sample = fixture_paths
    # The output must be outside the source directory by contract.
    first = run_replay(_manifest(source, output, sample))
    source_hash = sha256_file(source)
    second = run_replay(_manifest(source, output, sample))
    assert first.provider_calls == second.provider_calls == 0
    assert first.result_hash == second.result_hash
    assert first.status == second.status == "unavailable"
    assert sha256_file(source) == source_hash
    assert (output / "source_integrity_before.json").is_file()


def test_result_hash_ignores_source_mtime(fixture_paths):
    source, output, sample = fixture_paths
    first = run_replay(_manifest(source, output, sample))
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 86_400_000_000_000))
    second = run_replay(_manifest(source, output, sample))
    assert first.result_hash == second.result_hash


def test_recorded_request_fingerprint_matches_stage_contract():
    assert recorded_request_fingerprint("provider", "model", "normalized", "prompt-v1") == __import__("hashlib").sha256(
        b"providermodelnormalizedprompt-v1"
    ).hexdigest()


def test_sample_manifest_hash_rejects_forged_declaration(tmp_path):
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": "0" * 64, "units": []}, sort_keys=True), encoding="utf-8")
    with pytest.raises(ReplayInputError, match="hash"):
        sample_manifest_hash(sample)


def test_direct_recorded_replay_requires_fixture_hash(fixture_paths):
    source, output, sample = fixture_paths
    recordings = sample.parent / "provider_recordings.jsonl"
    recordings.write_text("", encoding="utf-8")
    manifest = replace(_manifest(source, output, sample, mode="recorded", recordings=recordings), request_fixture_hash=None)
    with pytest.raises(ReplayInputError, match="fixture hash"):
        run_replay(manifest)


def test_recorded_duplicate_response_conflict_is_blocked(fixture_paths):
    source, output, sample = fixture_paths
    recordings = sample.parent / "provider_recordings.jsonl"
    row = {"request_fingerprint": "a" * 64, "provider_id": "p", "model_id": "m", "prompt_version": "none", "schema_version": "1", "response_hash": "b" * 64}
    recordings.write_text(json.dumps(row) + "\n" + json.dumps({**row, "response_hash": "c" * 64}) + "\n", encoding="utf-8")
    result = run_replay(_manifest(source, output, sample, mode="recorded", recordings=recordings))
    assert result.status == "blocked"
    assert any("recording_conflict" in item for item in result.blocked_reasons)


def test_staging_is_blocked_without_provider_call(fixture_paths):
    source, output, sample = fixture_paths
    result = run_replay(_manifest(source, output, sample, mode="staging"))
    assert result.status == "blocked"
    assert result.provider_calls == 0


def test_memory_v2_snapshot_is_copied_and_reports_independent_integrity(tmp_path):
    source_dir = tmp_path / "source"
    memory_dir = tmp_path / "memory"
    source_dir.mkdir()
    memory_dir.mkdir()
    source = source_dir / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
    memory = memory_dir / "memory_v2.db"
    with sqlite3.connect(memory) as db:
        db.execute("CREATE TABLE memory_v2_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO memory_v2_meta VALUES ('schema_version', '4')")
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    memory_integrity = snapshot_integrity(memory)
    manifest = replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    )
    result = run_replay(manifest)
    assert result.memory_v2_integrity_before["sha256"] == memory_integrity["sha256"]
    assert result.memory_v2_integrity_after["sha256"] == memory_integrity["sha256"]
    assert (tmp_path / "output" / "memory_v2_integrity_before.json").is_file()


def test_memory_v2_schema_mismatch_is_blocked(tmp_path):
    source_dir = tmp_path / "source"
    memory_dir = tmp_path / "memory"
    source_dir.mkdir()
    memory_dir.mkdir()
    source = source_dir / "source.db"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")
    memory = memory_dir / "memory_v2.db"
    with sqlite3.connect(memory) as db:
        db.execute("CREATE TABLE memory_v2_meta(key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO memory_v2_meta VALUES ('schema_version', '3')")
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    memory_integrity = snapshot_integrity(memory)
    manifest = replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    )
    result = run_replay(manifest)
    assert result.status == "blocked"
    assert "memory_v2_schema_mismatch" in result.blocked_reasons


def _complete_dual_snapshot(tmp_path: Path, *, mutate=None):
    """Build a redacted main/Memory V2 pair with a complete durable workflow."""
    from tests.unit.learning.test_review_admission_stage07 import (
        _candidate, _database, _settle_pair,
    )
    from astrmai.learning.persistence.review_repository import LearningReviewRepository
    from astrmai.learning.review.admission import AdmissionRepository

    source_dir = tmp_path / "main"
    memory_dir = tmp_path / "memory"
    source_dir.mkdir()
    memory_dir.mkdir()
    source = source_dir / "main.db"
    _database(source)
    _candidate(source)
    with sqlite3.connect(source) as db:
        db.execute(
            "UPDATE learning_candidate_evidence SET evidence_id='row:107' "
            "WHERE candidate_id='candidate-1'"
        )
        db.commit()

    async def prepare_review_and_admission() -> None:
        reviews = LearningReviewRepository(source)
        await _settle_pair(reviews, reviewer="reviewer-a", model_identity="model-a", prefix="fixture-a", start=10.0)
        await _settle_pair(reviews, reviewer="reviewer-b", model_identity="model-b", prefix="fixture-b", start=40.0)
        final = await reviews.finalize_quorum(
            "candidate-1", 7, created_at=80.0,
            expected_reviewer_ids=("reviewer-a", "reviewer-b"),
        )
        assert final is not None
        with sqlite3.connect(source) as db:
            db.execute(
                """INSERT INTO learning_admission(
                    candidate_id, candidate_revision, review_decision_ids_json,
                    admission_revision, pre_index_eligible, post_publish_eligible,
                    index_blocked, blocked_reason, blocked_stage, blocked_kind,
                    index_generation, mapping_digest, index_hash, provenance_digest,
                    publish_proof_digest, revision, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "candidate-1", 7, json.dumps([final.decision_id, *final.source_decision_ids]),
                        1, 1, 1, 0, "", "", "", "1", "c" * 64, "d" * 64,
                    "a" * 64, "b" * 64, 1, 90.0, 90.0,
                ),
            )
            db.commit()
        assert await AdmissionRepository(source).record_human_admission(
            admission_id="human-admission-1", candidate_id="candidate-1",
            candidate_revision=7, admission_revision=1,
            reviewer_identity="human:admitter-1", decision="approved",
            reason="fixture", provenance_digest="a" * 64,
            publish_proof_digest="b" * 64, now=91.0,
        )
        return final

    final = asyncio.run(prepare_review_and_admission())
    source_evidence_ids = ["row:107"]
    canonical_persistence_id = "candidate-enrichment:" + canonical_hash({
        "candidate_id": "candidate-1",
        "source_evidence_ids": source_evidence_ids,
        "version": 1,
    })
    settlement_payload = {
        "status": "enriched",
        "enrichment_payload": {"expression": "fixture"},
        "retryable": False,
        "retry_at": 0.0,
        "failure_stage": "",
        "failure_kind": "",
        "diagnostics": {
            "canonical_persistence_id": canonical_persistence_id,
            "provider_attempt": {
                "work_attempt_id": "enrichment-attempt-1",
                "candidate_work_attempt": 1,
                "provider_attempt": 1,
                "provider_request_started": True,
                "provider_id": "fixture-provider",
                "provider_family": "fixture-family",
                "model_id": "fixture-model",
                "request_id": "provider-request-1",
                "identity_source": "fixture",
                "diagnostics": {
                    "gateway_call_id": "gateway-call-1",
                    "provider_request_id": "provider-request-1",
                    "identity_source": "fixture",
                },
            },
        },
        "provider_id": "fixture-provider",
        "model_id": "fixture-model",
        "canonical_ids": ["canonical-1"],
        "generated_evidence_ids": [],
        "queue_wait_ms": 0.0,
        "logical_queue_wait_ms": 0.0,
        "runtime_budget_wait_ms": 0.0,
        "gateway_background_wait_ms": 0.0,
        "gateway_global_wait_ms": 0.0,
        "provider_latency_ms": 0.0,
        "elapsed_ms": 1.0,
        "input_count": 1,
        "output_count": 1,
        "settlement_started_at": 91.0,
        "finished_at": 92.0,
    }
    with sqlite3.connect(source) as db:
        db.execute(
            """INSERT INTO learning_candidate_attempt(
                attempt_id, candidate_id, run_id, stage, attempt,
                candidate_work_attempt, provider_attempt, revision,
                task_name, scope_id, created_at, finished_at, status,
                provider_request_started, provider_id, provider_family, model_id,
                identity_source, provider_request_id, gateway_call_id,
                owner, lease_token, lease_until, started_at,
                diagnostics_json, canonical_ids_json, result_digest, settlement_payload_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "enrichment-attempt-1", "candidate-1", "fixture-run", "enrichment", 1,
                1, 1, 6, "fixture", "group-1", 91.0, 92.0, "completed",
                1, "fixture-provider", "fixture-family", "fixture-model",
                "fixture", "provider-request-1", "gateway-call-1",
                "fixture-owner", "fixture-lease-token", 120.0, 90.0,
                json.dumps(settlement_payload["diagnostics"], sort_keys=True),
                json.dumps(["canonical-1"], sort_keys=True),
                canonical_hash(settlement_payload), json.dumps(settlement_payload, sort_keys=True),
            ),
        )
        db.commit()
    memory = memory_dir / "memory_v2.db"
    with sqlite3.connect(memory) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            """
            CREATE TABLE canonical_memories(id TEXT PRIMARY KEY);
            CREATE TABLE memory_v2_meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE learning_asset_version(
                asset_id TEXT NOT NULL, asset_revision INTEGER NOT NULL,
                canonical_memory_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                candidate_revision INTEGER NOT NULL, admission_revision INTEGER NOT NULL,
                lifecycle_status TEXT NOT NULL, provenance_hash TEXT NOT NULL,
                PRIMARY KEY(asset_id, asset_revision),
                FOREIGN KEY(canonical_memory_id) REFERENCES canonical_memories(id)
            );
            CREATE TABLE learning_index_membership(
                asset_id TEXT NOT NULL, asset_revision INTEGER NOT NULL,
                generation INTEGER NOT NULL, resource_id TEXT NOT NULL,
                vector_id TEXT NOT NULL, mapping_hash TEXT NOT NULL, index_hash TEXT NOT NULL,
                membership_status TEXT NOT NULL,
                PRIMARY KEY(asset_id, asset_revision, generation),
                FOREIGN KEY(asset_id, asset_revision)
                    REFERENCES learning_asset_version(asset_id, asset_revision)
            );
            """
        )
        db.execute("INSERT INTO canonical_memories VALUES ('canonical-1')")
        db.executemany(
            "INSERT INTO memory_v2_meta(key,value) VALUES (?,?)",
            [("schema_version", "4"), ("learning_current_generation", "1"), ("learning_pending_generation", "")],
        )
        db.execute(
            """INSERT INTO learning_asset_version VALUES
                ('asset-1',7,'canonical-1','candidate-1',7,1,'active',?)""",
            ("a" * 64,),
        )
        db.execute(
            """INSERT INTO learning_index_membership VALUES
                ('asset-1',7,1,'resource-1','membership-1',?,?, 'current')""",
            ("c" * 64, "d" * 64),
        )
        db.commit()

    provenance = [{
        "candidate_id": "candidate-1", "asset_id": "asset-1", "asset_revision": 7, "generation": 1,
        "candidate_revision": 7, "admission_revision": 1,
        "canonical_memory_id": "canonical-1", "provenance_hash": "a" * 64,
        "source_evidence_ids": ["row:107"],
    }]
    retrieval_digest = canonical_hash(provenance)
    with sqlite3.connect(source) as db:
        db.execute(
            """INSERT INTO learning_retrieval_event(
                event_id,idempotency_key,turn_id,correlation_id,source_layer,stage,event_status,
                scope_id,sender_id,query_fingerprint,candidate_revision,review_revision,
                admission_revision,asset_revision_ids_json,selected_ids_json,accepted_ids_json,
                visible_ids_json,generation,policy_version,prompt_revision,reason_code,
                trimmed_reason,budget_chars,budget_tokens,outcome,reply_id,diagnostics_json,
                created_at,asset_provenance_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "event-1", "idem-1", "turn-1", "corr-1", "memory_injection", "reply_outcome", "observed",
                "group-1", "sender-1", "query-1", 7, 7, 1, json.dumps(["asset-1:7"]),
                json.dumps(["asset-1"]), json.dumps(["asset-1"]), json.dumps(["asset-1"]), 1,
                "policy-1", "prompt-1", "selected", "", 100, 10, "reply_sent", "reply-1", "{}", 100.0,
                json.dumps(provenance),
            ),
        )
        db.commit()
    if mutate is not None:
        mutate(source, memory)
    sample_payload = {
        "units": [],
        "workflow_evidence": [{
            "candidate_id": "candidate-1", "candidate_revision": 7,
            "source_evidence_ids": ["row:107"], "review_id": final.decision_id,
            "review_revision": 7, "review_candidate_revision": 7,
            "review_status": "completed", "reviewer_identity": "rule:review-quorum-v2",
            "human_admission_id": "human-admission-1", "admission_revision": 1,
            "admission_candidate_revision": 7, "human_admission_decision": "approved",
            "admission_reviewer_identity": "human:admitter-1", "publish_proof_digest": "b" * 64,
            "asset_id": "asset-1", "canonical_memory_id": "canonical-1",
            "index_membership_id": "membership-1", "generation": 1,
            "retrieval_event_id": "event-1", "retrieval_generation": 1,
            "retrieval_provenance_digest": retrieval_digest,
        }],
    }
    sample_payload["manifest_hash"] = canonical_hash(sample_payload)
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps(sample_payload, sort_keys=True), encoding="utf-8")
    return source, memory, sample


def test_complete_dual_sqlite_workflow_replay_reaches_stage10(tmp_path):
    source, memory, sample = _complete_dual_snapshot(tmp_path)
    memory_integrity = snapshot_integrity(memory)
    manifest = replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    )
    result = run_replay(manifest)
    assert result.status == "completed"
    assert result.funnel["replay_status"] == "completed"
    assert result.funnel["full_learning_pipeline_completed"] is True
    assert result.funnel["review_complete"] is True
    assert result.funnel["admission_complete"] is True
    assert result.funnel["publish_complete"] is True
    assert result.funnel["retrieval_complete"] is True
    assert result.funnel["stage10_readiness"]["stage10_authorized"] is True
    assert result.funnel["stage10_readiness"]["semantic_quality_status"] == "unavailable_without_human_gold"
    assert all(item.status == "unavailable" for item in result.metrics if item.metric_name == "candidate_precision")


@pytest.mark.parametrize("broken", ["candidate", "generation", "retrieval_status", "enrichment_attempt"])
def test_complete_dual_sqlite_workflow_replay_fails_closed_on_durable_break(tmp_path, broken):
    def mutate(source: Path, memory: Path) -> None:
        with sqlite3.connect(source) as db:
            if broken == "candidate":
                db.execute("UPDATE learning_candidate SET revision=8 WHERE candidate_id='candidate-1'")
            elif broken == "retrieval_status":
                db.execute("UPDATE learning_retrieval_event SET event_status='failed' WHERE event_id='event-1'")
            elif broken == "enrichment_attempt":
                db.execute("DELETE FROM learning_candidate_attempt WHERE attempt_id='enrichment-attempt-1'")
            db.commit()
        if broken == "generation":
            with sqlite3.connect(memory) as db:
                db.execute("UPDATE memory_v2_meta SET value='2' WHERE key='learning_current_generation'")
                db.commit()

    source, memory, sample = _complete_dual_snapshot(tmp_path, mutate=mutate)
    memory_integrity = snapshot_integrity(memory)
    manifest = replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    )
    result = run_replay(manifest)
    assert result.status != "completed"
    assert result.funnel.get("full_learning_pipeline_completed") is False
    assert result.funnel.get("stage10_readiness", {}).get("stage10_authorized") is False


@pytest.mark.parametrize("mutation", [
    lambda db: db.execute("UPDATE learning_candidate_attempt SET revision=7 WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET status='running' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET canonical_ids_json='[]' WHERE attempt_id='enrichment-attempt-1'"),
])
def test_enrichment_attempt_identity_and_completion_are_verified(tmp_path, mutation):
    source, memory, sample = _complete_dual_snapshot(tmp_path)
    with sqlite3.connect(source) as db:
        mutation(db)
        db.commit()
    memory_integrity = snapshot_integrity(memory)
    manifest = replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    )
    result = run_replay(manifest)
    assert result.funnel["enrichment_complete"] is False
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["stage10_readiness"]["stage10_authorized"] is False


@pytest.mark.parametrize("mutation", [
    lambda db: db.execute("UPDATE learning_candidate_attempt SET provider_request_started=0 WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET provider_attempt=0 WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET provider_id='' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET provider_request_id='other-request' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET gateway_call_id='other-gateway' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET provider_family='other-family' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET identity_source='' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET owner='' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET lease_token='' WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET lease_until=91 WHERE attempt_id='enrichment-attempt-1'"),
    lambda db: db.execute("UPDATE learning_candidate_attempt SET started_at=NULL WHERE attempt_id='enrichment-attempt-1'"),
])
def test_enrichment_provider_start_fence_is_required(tmp_path, mutation):
    source, memory, sample = _complete_dual_snapshot(tmp_path)
    with sqlite3.connect(source) as db:
        mutation(db)
        db.commit()
    memory_integrity = snapshot_integrity(memory)
    result = run_replay(replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    ))
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["stage10_readiness"]["stage10_authorized"] is False


def test_enrichment_provider_nested_identity_is_bound(tmp_path):
    source, memory, sample = _complete_dual_snapshot(tmp_path)
    with sqlite3.connect(source) as db:
        diagnostics = json.loads(db.execute(
            "SELECT diagnostics_json FROM learning_candidate_attempt WHERE attempt_id='enrichment-attempt-1'"
        ).fetchone()[0])
        diagnostics["provider_attempt"]["diagnostics"]["provider_request_id"] = "other-request"
        db.execute(
            "UPDATE learning_candidate_attempt SET diagnostics_json=? WHERE attempt_id='enrichment-attempt-1'",
            (json.dumps(diagnostics, sort_keys=True),),
        )
        db.commit()
    memory_integrity = snapshot_integrity(memory)
    result = run_replay(replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    ))
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["stage10_readiness"]["stage10_authorized"] is False


def test_enrichment_canonical_identity_is_bound_to_memory_asset(tmp_path):
    source, memory, sample = _complete_dual_snapshot(tmp_path)
    with sqlite3.connect(source) as db:
        payload = json.loads(db.execute(
            "SELECT settlement_payload_json FROM learning_candidate_attempt WHERE attempt_id='enrichment-attempt-1'"
        ).fetchone()[0])
        payload["canonical_ids"] = ["canonical-other"]
        db.execute(
            """UPDATE learning_candidate_attempt
               SET canonical_ids_json=?, settlement_payload_json=?, result_digest=?
               WHERE attempt_id='enrichment-attempt-1'""",
            (json.dumps(["canonical-other"]), json.dumps(payload, sort_keys=True), canonical_hash(payload)),
        )
        db.commit()
    memory_integrity = snapshot_integrity(memory)
    result = run_replay(replace(
        _manifest(source, tmp_path / "output", sample),
        memory_v2_snapshot_path=str(memory),
        memory_v2_snapshot_hash=memory_integrity["sha256"],
        memory_v2_schema_version=4,
    ))
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["stage10_readiness"]["stage10_authorized"] is False


def test_output_inside_source_directory_is_rejected(fixture_paths):
    source, _, sample = fixture_paths
    with pytest.raises(ReplaySecurityError):
        run_replay(_manifest(source, source.parent / "nested-output", sample))


def test_json_output_rejects_source_directory_symlink_and_hardlink(fixture_paths):
    source, output, _ = fixture_paths
    with pytest.raises(ReplaySecurityError):
        validate_output_file(source, source.parent / "summary.json")
    hardlink = output.parent / "source-hardlink.db"
    os.link(source, hardlink)
    with pytest.raises(ReplaySecurityError):
        validate_output_file(source, hardlink)
    symlink = output.parent / "summary-link.json"
    try:
        symlink.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ReplaySecurityError):
        validate_output_file(source, symlink)


def test_unknown_mode_validates_json_output_before_writing(fixture_paths):
    source, output, sample = fixture_paths
    hardlink = output.parent / "source-hardlink.db"
    os.link(source, hardlink)
    before = source.read_bytes()
    code = replay_cli_main([
        "--source-snapshot", str(source), "--output-root", str(output),
        "--mode", "unknown", "--sample-manifest", str(sample),
        "--pipeline-version", "p1", "--extractor-version", "e1",
        "--fingerprint-version", "f1", "--json-out", str(hardlink),
    ])
    assert code == 4
    assert source.read_bytes() == before


def test_complete_copy_invokes_evolution_manager_pipeline_without_provider(tmp_path):
    from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service

    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:replay", 3)
    source = Path(service.persistence.db_path)
    service.persistence.engine.dispose()
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    result = run_replay(_manifest(source, tmp_path.parent / f"{tmp_path.name}-output", sample))
    assert result.provider_calls == 0
    assert result.funnel["pipeline_executed"] is True
    assert result.status in {"partial", "unavailable"}


def test_discovery_completion_is_not_full_learning_completion(tmp_path):
    from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service

    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:discovery-contract", 3)
    source = Path(service.persistence.db_path)
    service.persistence.engine.dispose()
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    result = run_replay(_manifest(source, tmp_path.parent / f"{tmp_path.name}-full-contract", sample))
    assert result.funnel["pipeline_completed"] is True
    assert result.funnel["discovery_pipeline_completed"] is True
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["enrichment_complete"] is False
    assert result.funnel["admission_complete"] is False


def test_missing_human_admission_blocks_stage10(tmp_path):
    from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service

    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:admission-gate", 3)
    source = Path(service.persistence.db_path)
    service.persistence.engine.dispose()
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    result = run_replay(_manifest(source, tmp_path.parent / f"{tmp_path.name}-admission-gate", sample))
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["admission_complete"] is False
    assert result.funnel["publish_complete"] is False
    assert result.funnel["retrieval_complete"] is False


def test_no_human_admission_no_publish_or_retrieval_success(tmp_path):
    from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service

    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:no-admission", 3)
    source = Path(service.persistence.db_path)
    service.persistence.engine.dispose()
    sample = tmp_path / "sample.json"
    sample.write_text(json.dumps({"manifest_hash": canonical_hash({"units": []}), "units": []}, sort_keys=True), encoding="utf-8")
    result = run_replay(_manifest(source, tmp_path.parent / f"{tmp_path.name}-no-admission", sample))
    assert not result.funnel["publish_complete"]
    assert not result.funnel["retrieval_complete"]


def test_sample_workflow_evidence_cannot_forge_full_pipeline(tmp_path):
    from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service

    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:full-workflow", 3)
    source = Path(service.persistence.db_path)
    service.persistence.engine.dispose()
    workflow = {
        "candidate_id": "candidate-1", "candidate_revision": 1,
        "source_evidence_ids": ["row:1"], "review_id": "review-1",
        "review_revision": 1, "review_candidate_revision": 1,
        "review_status": "completed", "reviewer_identity": "human:reviewer-1",
        "human_admission_id": "admission-1", "admission_revision": 1,
        "admission_candidate_revision": 1, "human_admission_decision": "approved",
        "admission_reviewer_identity": "human:admitter-1", "publish_proof_digest": "a" * 64,
        "asset_id": "asset-1", "canonical_memory_id": "canonical-1",
        "index_membership_id": "membership-1", "generation": 1,
        "retrieval_event_id": "event-1", "retrieval_generation": 1,
        "retrieval_provenance_digest": "b" * 64,
    }
    content = {"units": [], "workflow_evidence": [workflow]}
    content["manifest_hash"] = canonical_hash(content)
    sample = tmp_path / "sample-with-workflow.json"
    sample.write_text(json.dumps(content, sort_keys=True), encoding="utf-8")
    result = run_replay(_manifest(source, tmp_path.parent / f"{tmp_path.name}-workflow", sample))
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["retrieval_complete"] is False
    assert "memory_v2_snapshot_required" in result.blocked_reasons


def test_mixed_valid_and_invalid_workflow_evidence_is_blocked(tmp_path):
    from tests.unit.learning.test_learning_pipeline_checkpoints import _add_logs, _database_service

    service = _database_service(tmp_path)
    _add_logs(service, "ff:GroupMessage:mixed-workflow", 3)
    source = Path(service.persistence.db_path)
    service.persistence.engine.dispose()
    content = {
        "units": [],
        "workflow_evidence": [{"candidate_id": "missing", "candidate_revision": 1}, "invalid"],
    }
    content["manifest_hash"] = canonical_hash(content)
    sample = tmp_path / "sample-with-invalid-workflow.json"
    sample.write_text(json.dumps(content, sort_keys=True), encoding="utf-8")
    result = run_replay(_manifest(source, tmp_path.parent / f"{tmp_path.name}-mixed", sample))
    assert result.funnel["full_learning_pipeline_completed"] is False
    assert result.funnel["workflow_evidence_status"] == "incomplete"
    assert "memory_v2_snapshot_required" in result.blocked_reasons


def test_ai_exploratory_never_counts_as_human_gold():
    from astrmai.learning.evaluation.gold_sampler import build_gold_manifest
    row = {
        "source_row_ids": [1], "scope_id": "group", "snapshot_hash": canonical_hash({"fixture": "ai"}),
        "pipeline_version": "p1", "extractor_version": "e1",
    }
    manifest = build_gold_manifest([row], annotation_status="ai_exploratory")
    assert manifest.annotation_status != "human_gold"
    assert manifest.human_gold_status != "complete"
    assert manifest.business_gate_eligible is False
