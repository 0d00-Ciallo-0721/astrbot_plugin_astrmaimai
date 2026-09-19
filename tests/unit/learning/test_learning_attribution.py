from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from sqlmodel import Session, create_engine

from astrmai.infrastructure.persistence.database_service import DatabaseService
from astrmai.infrastructure.persistence.orm_models import MessageLog
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.learning.mining.learning_attribution import LearningAttributionAdapter
from astrmai.learning.mining.learning_evidence import (
    build_evidence_bundle,
    merge_evidence_metadata,
)
from astrmai.learning.mining.learning_input_policy import LearningInputPolicy
from astrmai.learning.persistence.candidate_ledger import CandidateEvidence
from astrmai.memory.services.expression_pattern_service import ExpressionPatternService
from astrmai.memory.services.memory_injection_service import MemoryInjectionService
from scripts.audit_learning_attribution import (
    _candidate_persistence_id,
    _validate_output_path,
    audit_database,
    main as audit_main,
)


def _message(**overrides):
    values = {
        "id": 17,
        "group_id": "qq:GroupMessage:42",
        "sender_id": "user-a",
        "sender_name": "Alice",
        "content": "structured source text",
        "timestamp": 100.0,
        "event_id": "event-17",
        "event_schema_version": 1,
        "platform_message_id": "platform-17",
        "chat_kind": "group",
        "role": "user",
        "message_kind": "text",
        "is_bot": False,
        "reply_target_event_id": "event-16",
        "reply_target_actor_id": "user-b",
        "reply_target_actor_name": "Bob",
        "quote_event_id": "",
        "at_actor_ids": '["user-b"]',
        "topic_epoch": 3,
        "causal_parent_event_id": "event-16",
        "source_event_ids": '["event-17"]',
        "provenance": "original",
        "image_refs": "[]",
        "interaction_kind": "",
        "recalled": False,
        "outcome": "",
        "learning_evidence_eligible": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_a05_01_structured_group_event_preserves_authoritative_attribution():
    result = LearningAttributionAdapter().attribute(_message())

    assert result.source_row_id == 17
    assert result.source_message_id == "event-17"
    assert result.identity_source == "event_id"
    assert result.platform_id == "qq"
    assert result.chat_id == "42"
    assert result.scope_id == "qq:group:42"
    assert result.speaker_scope_id == "qq:group:42:user-a"
    assert result.pairwise_id == "pair:qq:group:42:user-a:qq:group:42:user-b"
    assert result.topic_epoch == 3
    assert result.source_type == "user_said"
    assert result.evidence_quality == "high"
    assert result.attribution_confidence is None
    assert result.evidence_eligible is True
    assert result.eligible_for_speaker_stats is True


def test_attribution_relation_payload_is_immutable_but_serializes_as_json_arrays():
    result = LearningAttributionAdapter().attribute(_message())
    exported = result.to_dict()
    exported["relation_payload"]["at_actor_ids"].append("changed")

    assert result.relation_payload["at_actor_ids"] == ("user-b",)
    assert result.to_dict()["relation_payload"]["at_actor_ids"] == ["user-b"]


def test_a05_02_legacy_row_is_low_quality_group_shadow_only():
    result = LearningAttributionAdapter().attribute(
        _message(
            event_id="fallback_deadbeef",
            platform_message_id="",
            event_schema_version=0,
            reply_target_event_id="",
            reply_target_actor_id="",
            at_actor_ids="[]",
            source_event_ids="[]",
            topic_epoch=0,
            provenance="legacy",
        )
    )

    assert result.identity_source == "fallback_hash"
    assert result.evidence_quality == "low"
    assert result.topic_epoch is None
    assert result.eligible_for_group_shadow is True
    assert result.eligible_for_speaker_stats is False


def test_a05_03_missing_sender_never_uses_display_name_as_identity():
    result = LearningAttributionAdapter().attribute(
        _message(sender_id="", sender_name="Alice")
    )

    assert result.speaker_id is None
    assert result.speaker_scope_id is None
    assert result.evidence_quality == "unknown"
    assert result.evidence_eligible is False
    assert "speaker_missing" in result.unknown_reasons


def test_a05_04_same_display_name_on_different_platforms_never_merges():
    qq = LearningAttributionAdapter().attribute(_message(sender_name="Same"))
    discord = LearningAttributionAdapter().attribute(
        _message(group_id="discord:GroupMessage:42", sender_name="Same")
    )

    assert qq.speaker_scope_id != discord.speaker_scope_id
    assert qq.scope_id == "qq:group:42"
    assert discord.scope_id == "discord:group:42"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"provenance": "quoted", "quote_event_id": "quoted-1"}, "quoted"),
        ({"provenance": "forwarded"}, "forwarded"),
        ({"is_bot": True, "role": "assistant", "provenance": "bot_echo"}, "bot_echo"),
        ({"provenance": "external_plugin"}, "plugin_output"),
        ({"recalled": True}, "retracted"),
    ],
)
def test_a05_05_a05_06_a05_07_non_source_types_do_not_count_actor_support(
    overrides, expected
):
    result = LearningAttributionAdapter().attribute(_message(**overrides))

    assert result.source_type == expected
    assert result.evidence_eligible is False
    assert result.eligible_for_speaker_stats is False


def test_a05_07_empty_and_image_only_are_not_source_support():
    empty = LearningAttributionAdapter().attribute(_message(content=""))
    image = LearningAttributionAdapter().attribute(
        _message(content="", message_kind="image", image_refs='["asset"]')
    )

    assert empty.evidence_eligible is False
    assert image.evidence_eligible is False
    assert "text_missing" in empty.unknown_reasons


def test_a05_08_pairwise_identity_is_directed_and_requires_explicit_actor():
    a_to_b = LearningAttributionAdapter().attribute(
        _message(sender_id="a", reply_target_actor_id="b")
    )
    b_to_a = LearningAttributionAdapter().attribute(
        _message(sender_id="b", reply_target_actor_id="a")
    )
    inferred = LearningAttributionAdapter().attribute(
        _message(reply_target_actor_id="", reply_target_actor_name="Bob")
    )

    assert a_to_b.pairwise_id == "pair:qq:group:42:a:qq:group:42:b"
    assert b_to_a.pairwise_id == "pair:qq:group:42:b:qq:group:42:a"
    assert a_to_b.pairwise_id != b_to_a.pairwise_id
    assert inferred.pairwise_id is None


def test_reply_quote_reference_does_not_reclassify_current_actor_text():
    result = LearningAttributionAdapter().attribute(
        _message(
            provenance="original",
            reply_target_actor_id="user-b",
            reply_target_event_id="event-16",
            quote_event_id="event-16",
        )
    )

    assert result.source_type == "user_said"
    assert result.evidence_eligible is True


def test_a05_11_reorder_does_not_change_real_source_identity_or_support():
    first = _message(id=1, event_id="event-1", platform_message_id="platform-1")
    second = _message(id=2, event_id="event-2", platform_message_id="platform-2")

    before = build_evidence_bundle(
        group_id=first.group_id,
        messages=[first, second],
        matched_indexes=[0, 1],
        attribution_enabled=True,
    )
    after = build_evidence_bundle(
        group_id=first.group_id,
        messages=[second, first],
        matched_indexes=[0, 1],
        attribution_enabled=True,
    )

    assert set(before["source_row_ids"]) == {1, 2}
    assert set(before["source_row_ids"]) == set(after["source_row_ids"])
    assert before["support_count"] == after["support_count"] == 2


def test_context_only_and_quoted_messages_do_not_increase_support():
    source = _message(id=1, event_id="event-1")
    context = _message(
        id=2,
        event_id="event-2",
        learning_evidence_eligible=False,
    )
    quoted = _message(
        id=3,
        event_id="event-3",
        quote_event_id="quoted-0",
        provenance="quoted",
        reply_target_actor_id="",
    )

    bundle = build_evidence_bundle(
        group_id=source.group_id,
        messages=[source, context, quoted],
        matched_indexes=[0, 1, 2],
        attribution_enabled=True,
    )

    assert bundle["support_count"] == 1
    assert bundle["source_row_ids"] == [1]
    assert len(bundle["context_windows"]) == 3


def test_context_only_source_examples_and_spans_are_removed_before_enrichment():
    source = _message(id=1, event_id="event-1", content="clean source")
    quoted = _message(
        id=2,
        event_id="event-2",
        content="quoted contamination",
        provenance="quoted",
        learning_evidence_eligible=False,
    )

    bundle = build_evidence_bundle(
        group_id=source.group_id,
        messages=[source, quoted],
        matched_indexes=[0, 1],
        source_examples=["clean source", "quoted contamination"],
        source_spans=[
            {"message_id": "event-1", "start": 0, "end": 5, "text": "clean"},
            {"message_id": "event-2", "start": 0, "end": 6, "text": "quoted"},
        ],
        attribution_enabled=True,
    )

    assert bundle["support_count"] == 1
    assert bundle["source_examples"] == ["clean source"]
    assert [item["message_id"] for item in bundle["source_spans"]] == ["event-1"]
    assert "quoted contamination" in str(bundle["context_windows"])


def test_a05_12_invalid_relation_json_is_unknown_not_empty_success():
    result = LearningAttributionAdapter().attribute(
        _message(at_actor_ids="{not-json", source_event_ids="[broken")
    )

    assert result.evidence_quality == "unknown"
    assert result.evidence_eligible is False
    assert "invalid_at_actor_ids" in result.unknown_reasons
    assert "invalid_source_event_ids" in result.unknown_reasons


@pytest.mark.parametrize("provenance", ["synthetic", "replay"])
def test_a05_13_synthetic_and_replay_never_become_source_support(provenance):
    result = LearningAttributionAdapter().attribute(
        _message(id=None, provenance=provenance, source_event_ids="[]")
    )

    assert result.source_row_id is None
    assert result.source_type == provenance
    assert result.is_generated is True
    assert result.evidence_eligible is False


def test_non_durable_input_never_fakes_source_row_id():
    result = LearningAttributionAdapter().attribute(
        _message(id="synthetic:abc", event_id="", platform_message_id="")
    )

    assert result.source_row_id is None
    assert result.source_message_id == ""


def test_generated_source_evidence_preserves_generated_marker():
    evidence = CandidateEvidence.source(
        candidate_id="candidate-1",
        batch_id="batch-1",
        source_row_id=17,
        source_message_id="event-17",
        identity_source="event_id",
        source_type="synthetic",
        evidence_quality="unknown",
        eligible=False,
        eligibility_reason="source_type_synthetic",
        payload={},
        created_at=100.0,
        is_generated=True,
    )

    assert evidence.is_generated is True
    assert evidence.eligible is False


def _database_service(tmp_path):
    path = tmp_path / "attribution.db"
    engine = create_engine(f"sqlite:///{path}")
    MessageLog.__table__.create(engine)
    service = DatabaseService.__new__(DatabaseService)
    service.persistence = SimpleNamespace(
        db_path=path,
        get_session=lambda: Session(engine),
    )
    service._db_lock_instance = None
    return service, path


def test_a05_09_identical_duplicate_is_atomic_idempotent(tmp_path):
    service, path = _database_service(tmp_path)
    event = {
        "event_id": "event-duplicate",
        "schema_version": 1,
        "timestamp": 10.0,
        "chat_kind": "group",
        "role": "user",
        "message_kind": "text",
        "provenance": "original",
    }

    first = service.add_message_log_with_diagnostic(
        "qq:GroupMessage:42", "user-a", "Alice", "same", conversation_event=event
    )
    replay = service.add_message_log_with_diagnostic(
        "qq:GroupMessage:42", "user-a", "Renamed", "same", conversation_event=event
    )

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM messagelog").fetchone()[0]
    assert first.inserted is True
    assert replay.idempotent is True
    assert replay.conflict is False
    assert count == 1


def test_a05_10_conflicting_duplicate_fails_closed_without_sensitive_diagnostic(tmp_path):
    service, path = _database_service(tmp_path)
    event = {
        "event_id": "event-conflict",
        "schema_version": 1,
        "timestamp": 10.0,
        "chat_kind": "group",
        "role": "user",
        "message_kind": "text",
        "provenance": "original",
    }
    service.add_message_log_with_diagnostic(
        "qq:GroupMessage:42", "user-a", "Alice", "first secret", conversation_event=event
    )

    conflict = service.add_message_log_with_diagnostic(
        "qq:GroupMessage:42", "user-a", "Alice", "second secret", conversation_event=event
    )

    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT content FROM messagelog WHERE event_id = ?", ("event-conflict",)
        ).fetchone()[0]
    diagnostic_text = str(conflict.to_dict())
    assert conflict.inserted is False
    assert conflict.idempotent is False
    assert conflict.conflict is True
    assert conflict.failure_kind == "duplicate_event_conflict"
    assert stored == "first secret"
    assert "first secret" not in diagnostic_text
    assert "second secret" not in diagnostic_text


def test_recorder_preserves_invalid_relation_json_as_unknown(tmp_path):
    service, path = _database_service(tmp_path)
    service.add_message_log_with_diagnostic(
        "qq:GroupMessage:42",
        "user-a",
        "Alice",
        "source text",
        conversation_event={
            "event_id": "invalid-relations",
            "schema_version": 1,
            "timestamp": 10.0,
            "chat_kind": "group",
            "role": "user",
            "message_kind": "text",
            "provenance": "original",
            "at_actor_ids": "{broken",
        },
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM messagelog").fetchone()
    view = LearningAttributionAdapter().attribute(dict(row))
    assert view.evidence_quality == "unknown"
    assert "invalid_at_actor_ids" in view.unknown_reasons


def test_twenty_round_reorder_identical_and_conflicting_replay_is_stable(tmp_path):
    for round_id in range(20):
        location = tmp_path / f"round-{round_id}"
        location.mkdir()
        service, path = _database_service(location)
        event = {
            "event_id": "event-replayed",
            "schema_version": 1,
            "timestamp": 10.0,
            "chat_kind": "group",
            "role": "user",
            "message_kind": "text",
            "provenance": "original",
        }
        first = service.add_message_log_with_diagnostic(
            "qq:GroupMessage:42", "user-a", "Alice", "source text", conversation_event=event
        )
        replay = service.add_message_log_with_diagnostic(
            "qq:GroupMessage:42", "user-a", "Alice", "source text", conversation_event=event
        )
        conflict = service.add_message_log_with_diagnostic(
            "qq:GroupMessage:42", "user-a", "Alice", "other text", conversation_event=event
        )
        with sqlite3.connect(path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM messagelog").fetchone()[0]
        messages = [
            _message(id=1, event_id="event-one"),
            _message(id=2, event_id="event-two"),
        ]
        if round_id % 2:
            messages.reverse()
        bundle = build_evidence_bundle(
            group_id="qq:GroupMessage:42",
            messages=messages,
            matched_indexes=[0, 1],
            attribution_enabled=True,
        )
        assert first.inserted and replay.idempotent and conflict.conflict
        assert count == 1
        assert bundle["source_row_ids"] == [1, 2]
        assert bundle["support_count"] == 2


def test_a05_14_attribution_metadata_survives_evidence_merge():
    first = build_evidence_bundle(
        group_id="qq:GroupMessage:42",
        messages=[_message(id=1, event_id="event-1")],
        matched_indexes=[0],
        attribution_enabled=True,
    )
    second = build_evidence_bundle(
        group_id="qq:GroupMessage:42",
        messages=[_message(id=2, event_id="event-2")],
        matched_indexes=[0],
        attribution_enabled=True,
    )

    merged = merge_evidence_metadata(first, second)

    assert merged["source_row_ids"] == [1, 2]
    assert merged["support_count"] == 2
    assert {item["source_row_id"] for item in merged["source_attributions"]} == {1, 2}
    assert merged["evidence_qualities"] == ["high"]
    assert merged["source_types"] == ["user_said"]


def test_ineligible_attribution_does_not_fall_back_to_legacy_support_count():
    incoming = build_evidence_bundle(
        group_id="qq:GroupMessage:42",
        messages=[_message(id=2, event_id="event-2", provenance="synthetic")],
        matched_indexes=[0],
        attribution_enabled=True,
    )

    merged = merge_evidence_metadata(
        {"source_message_ids": ["legacy-1"], "support_count": 1}, incoming
    )

    assert merged["support_count"] == 0


@pytest.mark.asyncio
async def test_a05_14_candidate_metadata_reaches_canonical_and_existing_trace():
    bundle = build_evidence_bundle(
        group_id="qq:GroupMessage:42",
        messages=[_message(id=17)],
        matched_indexes=[0],
        attribution_enabled=True,
    )
    bundle.update(
        {
            "attribution_scope_ids": ["qq:group:42"],
            "attribution_speaker_ids": ["user-a"],
            "attribution_speaker_scope_ids": ["qq:group:42:user-a"],
            "personal_attribution_eligible": True,
            "candidate_revision": 2,
            "candidate_persistence_id": "candidate-enrichment:test",
            "mining_batch_id": "candidate-enrichment:test",
        }
    )

    class _Store:
        async def get_by_dedup_key(self, _key, include_inactive=True):
            return None

        async def resolve_dedup_key(self, key):
            return key

    class _Writer:
        def __init__(self):
            self.request = None

        async def write(self, request):
            self.request = request
            return "memory-1"

    writer = _Writer()
    service = ExpressionPatternService(_Store(), writer)
    memory_id = await service.write_pattern(
        "qq:group:42",
        {
            **bundle,
            "expression": "structured expression",
            "situation": "test",
            "candidate_id": "candidate-1",
        },
    )
    selected = SimpleNamespace(id=memory_id, metadata=dict(writer.request.metadata))
    summary = MemoryInjectionService._build_trace_summary(
        SimpleNamespace(policy="group", metadata={}, retrieve_keys=[]),
        {},
        SimpleNamespace(selected_ids=[memory_id], selected_count=1, candidate_count=1),
        "",
        [selected],
    )

    assert writer.request.metadata["source_row_ids"] == [17]
    assert writer.request.metadata["attribution_speaker_scope_ids"] == [
        "qq:group:42:user-a"
    ]
    assert summary["selected_attribution"][0]["propagation_status"] == "available"
    assert summary["selected_attribution"][0]["source_row_ids"] == [17]
    assert summary["selected_attribution"][0]["is_generated"] == [False]
    assert summary["selected_attribution"][0]["evidence_eligible"] == [True]


def test_read_only_coverage_audit_separates_structured_and_legacy_without_content(
    tmp_path,
):
    service, path = _database_service(tmp_path)
    service.add_message_log(
        "qq:GroupMessage:42",
        "user-a",
        "Alice",
        "do-not-emit-secret-content",
        conversation_event={
            "event_id": "structured-1",
            "schema_version": 1,
            "timestamp": 10.0,
            "chat_kind": "group",
            "role": "user",
            "message_kind": "text",
            "provenance": "original",
        },
    )
    service.add_message_log(
        "qq:GroupMessage:42",
        "user-b",
        "Bob",
        "legacy-content",
        conversation_event={"timestamp": 11.0, "provenance": "legacy"},
    )

    report = audit_database(path, start=10.0, end=12.0)
    empty = audit_database(path, start=12.0, end=13.0)
    encoded = str(report)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert report["database_mode"] == "read_only"
    assert report["structured"]["row_count"] == 1
    assert report["legacy"]["row_count"] == 1
    assert report["propagation"]["memory_database_status"] == "unavailable"
    assert report["propagation"]["candidate_to_canonical"]["ratio"] is None
    assert empty["structured"]["source_row"]["denominator"] == 0
    assert empty["structured"]["source_row"]["ratio"] is None
    assert "do-not-emit-secret-content" not in encoded
    assert "user-a" not in encoded


@pytest.mark.parametrize(
    "invocation",
    [
        ("scripts/audit_learning_attribution.py",),
        ("-m", "scripts.audit_learning_attribution"),
    ],
)
def test_audit_cli_help_supports_script_and_module_invocation(invocation):
    repository = Path(__file__).resolve().parents[3]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, *invocation, "--help"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Read-only learning attribution coverage audit" in result.stdout


def test_audit_report_has_anonymous_self_describing_provenance(tmp_path):
    service, database = _database_service(tmp_path)
    service.add_message_log(
        "qq:GroupMessage:42",
        "user-a",
        "Alice",
        "do-not-emit-provenance-content",
        conversation_event={
            "event_id": "structured-1",
            "schema_version": 1,
            "timestamp": 10.0,
            "chat_kind": "group",
            "role": "user",
            "message_kind": "text",
            "provenance": "original",
        },
    )

    report = audit_database(database, start=10.0, end=12.0)
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True)

    assert report["report_version"] == "learning-attribution-audit-v1"
    assert report["source"]["plugin_database"]["mode"] == "read_only"
    assert report["source"]["plugin_database"]["descriptor"].startswith(
        "sha256:"
    )
    assert report["source"]["memory_database"] == {
        "descriptor": "unavailable",
        "mode": "not_supplied",
    }
    assert report["filters"] == {
        "candidate_evidence": "eligible = 1 AND is_generated = 0",
        "legacy_messages": "event_schema_version <= 0",
        "speaker_eligibility": (
            "LearningInputPolicy acceptance AND LearningAttributionAdapter eligibility"
        ),
        "structured_messages": "event_schema_version > 0",
        "time_window": "timestamp >= start_inclusive AND timestamp < end_exclusive",
        "trace_success": "propagation_status = available with durable identity match",
    }
    assert str(database.resolve()) not in encoded
    assert database.name not in encoded
    assert "do-not-emit-provenance-content" not in encoded
    assert "user-a" not in encoded


def test_audit_uses_input_policy_result_for_speaker_eligibility(tmp_path):
    service, path = _database_service(tmp_path)
    service.add_message_log(
        "qq:GroupMessage:42",
        "admin-user",
        "Admin",
        "/admin secret",
        conversation_event={
            "event_id": "command-1",
            "schema_version": 1,
            "timestamp": 10.0,
            "chat_kind": "group",
            "role": "user",
            "message_kind": "text",
            "provenance": "original",
        },
    )

    report = audit_database(path, start=10.0, end=11.0)

    assert report["structured"]["input_policy_accepted"]["numerator"] == 0
    assert report["structured"]["speaker_stats_eligibility"]["numerator"] == 0
    assert report["structured"]["group_shadow_eligibility"]["numerator"] == 0


def test_audit_output_rejects_database_same_file_and_hardlink(tmp_path):
    database = tmp_path / "plugin.db"
    database.write_bytes(b"sqlite-fixture")
    memory = tmp_path / "memory.db"
    memory.write_bytes(b"memory-fixture")
    hardlink = tmp_path / "plugin-hardlink.db"
    os.link(database, hardlink)

    with pytest.raises(ValueError, match="overlaps input database"):
        _validate_output_path(database, database, None)
    with pytest.raises(ValueError, match="overlaps input database"):
        _validate_output_path(hardlink, database, None)
    with pytest.raises(ValueError, match="overlaps input database"):
        _validate_output_path(memory, database, memory)
    assert database.read_bytes() == b"sqlite-fixture"


def test_audit_cli_cannot_overwrite_input_database(tmp_path, monkeypatch):
    service, database = _database_service(tmp_path)
    before = database.read_bytes()
    monkeypatch.setattr(
        "sys.argv",
        [
            "audit_learning_attribution.py",
            str(database),
            "--start",
            "0",
            "--end",
            "1",
            "--output",
            str(database),
        ],
    )

    with pytest.raises(ValueError, match="overlaps input database"):
        audit_main()
    assert database.read_bytes() == before


def test_audit_output_rejects_input_symlink_when_supported(tmp_path):
    database = tmp_path / "plugin.db"
    database.write_bytes(b"sqlite-fixture")
    symlink = tmp_path / "plugin-link.db"
    try:
        symlink.symlink_to(database)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ValueError, match="overlaps input database"):
        _validate_output_path(symlink, database, None)


def test_audit_propagation_rejects_stale_canonical_and_unavailable_trace(tmp_path):
    service, plugin_path = _database_service(tmp_path)
    service.add_message_log(
        "qq:GroupMessage:42",
        "user-a",
        "Alice",
        "source",
        conversation_event={
            "event_id": "event-1",
            "schema_version": 1,
            "timestamp": 10.0,
            "chat_kind": "group",
            "role": "user",
            "message_kind": "text",
            "provenance": "original",
        },
    )
    evidence_ids = ["evidence-1"]
    persistence_id = _candidate_persistence_id("candidate-1", evidence_ids)
    with sqlite3.connect(plugin_path) as connection:
        connection.executescript(
            """
            CREATE TABLE learning_candidate(candidate_id TEXT PRIMARY KEY, revision INTEGER);
            CREATE TABLE learning_candidate_evidence(
                candidate_id TEXT, evidence_id TEXT, created_at REAL,
                is_generated INTEGER, eligible INTEGER
            );
            CREATE TABLE learning_candidate_attempt(
                candidate_id TEXT, revision INTEGER, status TEXT,
                diagnostics_json TEXT, canonical_ids_json TEXT
            );
            CREATE TABLE memoryretrievaltrace(created_at REAL, trace_summary TEXT);
            """
        )
        connection.execute(
            "INSERT INTO learning_candidate VALUES (?, ?)", ("candidate-1", 3)
        )
        connection.execute(
            "INSERT INTO learning_candidate_evidence VALUES (?, ?, ?, 0, 1)",
            ("candidate-1", evidence_ids[0], 10.0),
        )
        connection.execute(
            "INSERT INTO learning_candidate_attempt VALUES (?, ?, 'completed', ?, ?)",
            (
                "candidate-1",
                2,
                json.dumps({"canonical_persistence_id": persistence_id}),
                json.dumps(["memory-1"]),
            ),
        )
        connection.execute(
            "INSERT INTO memoryretrievaltrace VALUES (?, ?)",
            (
                10.0,
                json.dumps({
                    "selected_attribution": [{
                        "memory_id": "memory-1",
                        "propagation_status": "unavailable",
                    }]
                }),
            ),
        )
        connection.commit()
    memory_path = tmp_path / "memory.db"
    with sqlite3.connect(memory_path) as connection:
        connection.execute(
            "CREATE TABLE canonical_memories(id TEXT, metadata TEXT, create_time REAL, update_time REAL)"
        )
        connection.execute(
            "INSERT INTO canonical_memories VALUES (?, ?, ?, ?)",
            (
                "memory-1",
                json.dumps({
                    "candidate_id": "candidate-1",
                    "candidate_revision": 1,
                    "candidate_persistence_id": persistence_id,
                    "source_attributions": [{"source_row_id": 1}],
                }),
                10.0,
                10.0,
            ),
        )
        connection.commit()

    report = audit_database(
        plugin_path, start=10.0, end=11.0, memory_database=memory_path
    )

    assert report["propagation"]["candidate_to_canonical"]["numerator"] == 0
    assert report["propagation"]["candidate_to_canonical"]["ratio"] == 0.0
    assert report["propagation"]["retrieval_trace_attribution"]["numerator"] == 0

    with sqlite3.connect(memory_path) as connection:
        connection.execute(
            "UPDATE canonical_memories SET metadata = ? WHERE id = ?",
            (
                json.dumps({
                    "candidate_id": "candidate-1",
                    "candidate_revision": 2,
                    "candidate_persistence_id": persistence_id,
                    "evidence_digest": "digest-2",
                    "source_attributions": [{"source_row_id": 1}],
                }),
                "memory-1",
            ),
        )
        connection.commit()
    with sqlite3.connect(plugin_path) as connection:
        connection.execute(
            "UPDATE memoryretrievaltrace SET trace_summary = ?",
            (
                json.dumps({
                    "selected_attribution": [{
                        "memory_id": "memory-1",
                        "propagation_status": "available",
                        "candidate_id": "candidate-1",
                        "candidate_revision": 2,
                        "candidate_persistence_id": persistence_id,
                        "evidence_digest": "digest-2",
                    }]
                }),
            ),
        )
        connection.commit()

    repaired = audit_database(
        plugin_path, start=10.0, end=11.0, memory_database=memory_path
    )
    assert repaired["propagation"]["candidate_to_canonical"]["numerator"] == 1
    assert repaired["propagation"]["retrieval_trace_attribution"]["numerator"] == 1


def test_twenty_round_context_only_reorder_keeps_support_and_digest_stable():
    digests = set()
    for round_id in range(20):
        source = _message(id=1, event_id="event-1", content="clean source")
        quoted = _message(
            id=2,
            event_id="event-2",
            content="quoted contamination",
            provenance="quoted",
            learning_evidence_eligible=False,
        )
        messages = [source, quoted]
        if round_id % 2:
            messages.reverse()
        normalized = LearningInputPolicy().normalize(messages)
        messages = EvolutionManager._apply_attribution_evidence_gate(
            normalized, LearningAttributionAdapter()
        )
        bundle = build_evidence_bundle(
            group_id=source.group_id,
            messages=messages,
            matched_indexes=[0, 1],
            source_examples=[item.content for item in messages],
            attribution_enabled=True,
        )
        assert bundle["support_count"] == 1
        assert bundle["source_row_ids"] == [1]
        assert bundle["source_examples"] == ["clean source"]
        digests.add(bundle["evidence_digest"])
    assert len(digests) == 1
