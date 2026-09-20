from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.persistence.persistence_schema import _MIGRATIONS, _run_migrations
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.learning.mining.expression_candidate_extractor import (
    ExpressionCandidateExtractor,
)
from astrmai.learning.mining.learning_evidence import durable_message_evidence_id
from astrmai.learning.mining.learning_input_policy import LearningInputPolicy
from astrmai.learning.persistence.candidate_ledger import CandidateLedger


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "discovery.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 146")
        for version, ddl in _MIGRATIONS:
            if version in (145, 146):
                db.execute(ddl)
        _run_migrations(db)
        db.commit()
    return CandidateLedger(path)


class _InputPolicy:
    def normalize(self, items):
        return list(items)


class _FilteringInputPolicy:
    def normalize(self, items):
        return [item for item in items if item.id != 12]


class _Extractor:
    async def extract(self, group_id, _logs, **_kwargs):
        return [
            {
                "candidate_type": "catchphrase",
                "normalized_expression": "脱敏表达",
                "expression": "脱敏表达",
                "distinct_turn_count": 1,
                "source_message_ids": ["event-11"],
                "group_id": group_id,
            }
        ]


class _UnboundExtractor:
    async def extract(self, group_id, _logs, **_kwargs):
        return [
            {
                "candidate_type": "catchphrase",
                "normalized_expression": "unbound",
                "expression": "unbound",
                "distinct_turn_count": 1,
                "source_message_ids": ["missing-source-id"],
                "group_id": group_id,
            }
        ]


class _ExpressionMiner:
    input_policy = _InputPolicy()
    candidate_extractor = _Extractor()
    expression_min_distinct_turns = 1

    async def _existing_patterns(self, _group_id):
        return set()


def _manager(ledger, *, quality_logs=None):
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.candidate_ledger = ledger
    manager.expression_miner = _ExpressionMiner()
    manager.jargon_miner = SimpleNamespace()
    manager._next_pipeline_cursor = lambda logs, _pipeline: (logs[-1].id, 0)

    class _QualityDatabase:
        async def get_quality_window_message_logs_async(
            self, group_id, *, window_start, window_end
        ):
            corpus = _logs() if quality_logs is None else list(quality_logs)
            return [
                item
                for item in corpus
                if item.group_id == group_id
                and (
                    float(getattr(item, "timestamp", 0) or 0) <= 0
                    or window_start <= float(item.timestamp) < window_end
                )
            ]

    manager.db = _QualityDatabase()
    return manager


def _logs():
    return [
        SimpleNamespace(
            id=11,
            group_id="qq:group:42",
            sender_id="user-1",
            sender_name="Alice",
            content="structured expression",
            timestamp=1_700_000_001.0,
            event_id="event-11",
            event_schema_version=1,
            platform_message_id="platform-11",
            chat_kind="group",
            role="user",
            message_kind="text",
            is_bot=False,
            reply_target_event_id="event-10",
            reply_target_actor_id="user-2",
            reply_target_actor_name="Bob",
            quote_event_id="",
            at_actor_ids="[]",
            causal_parent_event_id="event-10",
            source_event_ids='["event-11"]',
            provenance="original",
            image_refs="[]",
            recalled=False,
            topic_epoch=1,
            learning_evidence_eligible=True,
        ),
        SimpleNamespace(
            id=12,
            group_id="qq:group:42",
            sender_id="user-2",
            sender_name="Bob",
            content="context",
            timestamp=1_700_000_002.0,
            event_id="event-12",
            event_schema_version=1,
            platform_message_id="platform-12",
            chat_kind="group",
            role="user",
            message_kind="text",
            is_bot=False,
            reply_target_event_id="",
            reply_target_actor_id="",
            reply_target_actor_name="",
            quote_event_id="",
            at_actor_ids="[]",
            causal_parent_event_id="",
            source_event_ids='["event-12"]',
            provenance="original",
            image_refs="[]",
            recalled=False,
            topic_epoch=1,
            learning_evidence_eligible=True,
        ),
    ]


@pytest.mark.asyncio
async def test_discovery_writes_complete_source_batch_candidate_and_evidence(ledger):
    report = await _manager(ledger)._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )

    due = await ledger.list_due_enrichment(now=200.0, limit=10)
    assert report["status"] == "completed"
    assert report["cursor_after"] == 12
    assert report["source_count"] == 2
    assert report["candidate_count"] == 1
    assert len(due) == 1
    evidence = await ledger.list_evidence(due[0].candidate_id)
    assert len(evidence) == 1
    assert due[0].evidence_quality == "direct"
    assert "source_attributions" not in due[0].source_payload
    assert evidence[0].evidence_quality == "direct"


@pytest.mark.asyncio
async def test_attribution_flag_writes_structured_ledger_evidence(ledger):
    manager = _manager(ledger)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(learning_attribution_enabled=True)
    )

    await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )

    candidate = (await ledger.list_due_enrichment(now=200.0, limit=10))[0]
    evidence = (await ledger.list_evidence(candidate.candidate_id))[0]
    assert candidate.scope_id == "qq:group:42"
    assert candidate.speaker_id == "user-1"
    assert candidate.speaker_scope_id == "qq:group:42:user-1"
    assert candidate.evidence_quality == "high"
    assert evidence.source_row_id == 11
    assert evidence.source_message_id == "event-11"
    assert evidence.pairwise_scope_id == (
        "pair:qq:group:42:user-1:qq:group:42:user-2"
    )
    assert evidence.source_type == "user_said"
    assert evidence.eligible is True
    quality = await ledger.load_quality_snapshot(
        candidate.candidate_id,
        candidate.revision,
        "quality-v1",
    )
    assert quality is not None
    assert quality.candidate_revision == candidate.revision
    assert quality.speaker_message_count == 1
    assert quality.group_message_count == 2


@pytest.mark.asyncio
async def test_attribution_mixed_speakers_stays_group_shadow(ledger):
    manager = _manager(ledger)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(learning_attribution_enabled=True)
    )

    class _MixedExtractor:
        async def extract(self, group_id, _logs, **_kwargs):
            return [{
                "candidate_type": "catchphrase",
                "normalized_expression": "mixed",
                "expression": "mixed",
                "distinct_turn_count": 2,
                "source_message_ids": ["event-11", "event-12"],
                "group_id": group_id,
            }]

    manager.expression_miner.candidate_extractor = _MixedExtractor()
    await manager._write_candidate_discovery(
        pipeline="expression", group_id="qq:group:42", logs=_logs(), cursor_before=10
    )

    candidate = (await ledger.list_due_enrichment(now=200.0, limit=10))[0]
    assert candidate.speaker_id == ""
    assert candidate.speaker_scope_id == ""
    assert candidate.scope_id == "qq:group:42"
    assert candidate.source_payload["personal_attribution_eligible"] is False
    assert candidate.source_payload["support_count"] == 2


@pytest.mark.parametrize("provenance", ["quoted", "forwarded"])
@pytest.mark.asyncio
async def test_attribution_makes_non_source_messages_context_only_before_extraction(
    ledger, provenance
):
    manager = _manager(ledger)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(learning_attribution_enabled=True)
    )
    manager.expression_miner = SimpleNamespace(
        input_policy=LearningInputPolicy(),
        candidate_extractor=ExpressionCandidateExtractor(min_count=2),
        expression_min_distinct_turns=2,
        _existing_patterns=lambda _group_id: _async_value(set()),
    )
    logs = _logs()
    logs[0].content = "唉嘿嘿呀"
    logs[1].content = "唉嘿嘿呀"
    logs[1].provenance = provenance

    report = await manager._write_candidate_discovery(
        pipeline="expression", group_id="qq:group:42", logs=logs, cursor_before=10
    )

    assert report["candidate_count"] == 0
    assert await ledger.list_due_enrichment(now=200.0, limit=10) == ()


@pytest.mark.asyncio
async def test_attribution_excludes_quoted_samples_from_enrichment_payload(ledger):
    manager = _manager(ledger)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(learning_attribution_enabled=True)
    )
    manager.expression_miner = SimpleNamespace(
        input_policy=LearningInputPolicy(),
        candidate_extractor=ExpressionCandidateExtractor(min_count=2),
        expression_min_distinct_turns=2,
        _existing_patterns=lambda _group_id: _async_value(set()),
    )
    logs = _logs()
    logs[0].content = "唉 嘿嘿呀"
    logs[1].content = "唉嘿嘿呀"
    quoted = SimpleNamespace(**vars(logs[1]))
    quoted.id = 13
    quoted.event_id = "event-13"
    quoted.platform_message_id = "platform-13"
    quoted.sender_id = "user-3"
    quoted.content = "唉嘿嘿呀 quoted-contamination"
    quoted.provenance = "quoted"

    await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=[*logs, quoted],
        cursor_before=10,
    )

    candidates = await ledger.list_due_enrichment(now=200.0, limit=20)
    assert candidates
    for candidate in candidates:
        assert candidate.source_payload["count"] == 2
        assert candidate.source_payload["support_count"] == 2
        assert all(
            "quoted-contamination" not in sample
            for sample in candidate.source_payload["source_examples"]
        )


@pytest.mark.asyncio
async def test_discovery_replay_is_idempotent_without_duplicate_candidate_or_evidence(ledger):
    manager = _manager(ledger)
    first = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )
    replay = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )
    due = await ledger.list_due_enrichment(now=200.0, limit=10)

    assert first["batch_id"] == replay["batch_id"]
    assert replay["candidate_inserted"] == 0
    assert replay["candidate_deduplicated"] == 1
    assert replay["evidence_inserted"] == 0
    assert len(due) == 1
    assert len(await ledger.list_evidence(due[0].candidate_id)) == 1


@pytest.mark.asyncio
async def test_quality_snapshot_rebuilds_complete_window_across_discovery_batches(ledger):
    first_log = _logs()[0]
    second_log = SimpleNamespace(**vars(first_log))
    second_log.id = 13
    second_log.event_id = "event-13"
    second_log.platform_message_id = "platform-13"
    second_log.timestamp = first_log.timestamp + 86_400.0
    expired_log = SimpleNamespace(**vars(first_log))
    expired_log.id = 3
    expired_log.event_id = "event-3"
    expired_log.platform_message_id = "platform-3"
    expired_log.timestamp = second_log.timestamp - 15 * 86_400.0
    corpus = [expired_log, first_log, second_log]
    manager = _manager(ledger, quality_logs=corpus)

    class _BatchExtractor:
        async def extract(self, group_id, logs, **_kwargs):
            current = list(logs)[0]
            return [{
                "candidate_type": "catchphrase",
                "normalized_expression": "跨批表达",
                "expression": "跨批表达",
                "distinct_turn_count": 99,
                "distinct_day_count": 99,
                "distinct_contributor_count": 99,
                "source_message_ids": [current.event_id],
                "group_id": group_id,
            }]

    manager.expression_miner.candidate_extractor = _BatchExtractor()
    await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=[first_log],
        cursor_before=10,
    )
    await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=[second_log],
        cursor_before=11,
    )

    candidate = (await ledger.list_due_enrichment(now=2_000_000_000.0, limit=10))[0]
    snapshot = await ledger.load_quality_snapshot(
        candidate.candidate_id, candidate.revision, "quality-v1"
    )
    evidence = await ledger.list_evidence(candidate.candidate_id)

    assert len(evidence) == 2
    assert snapshot is not None
    assert snapshot.group_message_count == 2
    assert snapshot.support_count == 2
    assert snapshot.distinct_turn_count == 2
    assert snapshot.distinct_day_count == 2


@pytest.mark.asyncio
async def test_quality_snapshot_fails_closed_without_complete_window_source(ledger):
    manager = _manager(ledger)
    manager.db = SimpleNamespace()

    report = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )
    candidate = (await ledger.list_due_enrichment(now=2_000_000_000.0, limit=10))[0]

    assert report["status"] == "completed"
    assert report["quality_shadow"]["status"] == "partial"
    assert report["quality_shadow"]["reason"] == "window_source_unavailable"
    assert await ledger.load_quality_snapshot(
        candidate.candidate_id, candidate.revision, "quality-v1"
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quality_logs", "reason"),
    [
        (
            [
                SimpleNamespace(
                    group_id="qq:group:42",
                    sender_id="user-1",
                    sender_name="Alice",
                    content="missing durable identity",
                    timestamp=1_700_000_001.0,
                    learning_evidence_eligible=True,
                )
            ],
            "source_identity_unavailable",
        ),
        (
            [
                SimpleNamespace(
                    id=None,
                    group_id="qq:group:42",
                    sender_id="user-1",
                    sender_name="Alice",
                    content="fallback identity is not durable",
                    timestamp=1_700_000_001.0,
                    event_id="fallback_deadbeef",
                    platform_message_id="",
                    learning_evidence_eligible=True,
                )
            ],
            "source_identity_unavailable",
        ),
        (
            [
                SimpleNamespace(
                    group_id="qq:group:42",
                    sender_id="user-1",
                    sender_name="Alice",
                    content="first fact",
                    timestamp=1_700_000_001.0,
                    event_id="event-conflict",
                    learning_evidence_eligible=True,
                ),
                SimpleNamespace(
                    group_id="qq:group:42",
                    sender_id="user-1",
                    sender_name="Alice",
                    content="conflicting fact",
                    timestamp=1_700_000_001.0,
                    event_id="event-conflict",
                    learning_evidence_eligible=True,
                ),
            ],
            "source_identity_conflict",
        ),
    ],
)
async def test_quality_snapshot_blocks_invalid_durable_source_identity(
    ledger, quality_logs, reason
):
    manager = _manager(ledger, quality_logs=quality_logs)

    report = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )
    candidate = (await ledger.list_due_enrichment(now=2_000_000_000.0, limit=10))[0]

    assert report["status"] == "completed"
    assert report["quality_shadow"]["status"] == "blocked"
    assert report["quality_shadow"]["reason"] == reason
    assert report["quality_shadow"]["snapshot_count"] == 0
    assert await ledger.load_quality_snapshot(
        candidate.candidate_id, candidate.revision, "quality-v1"
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform_message_id", "expected_identity"),
    [
        ("platform-authority", "platform_message_id:platform-authority"),
        ("", "row:11"),
    ],
)
async def test_quality_candidate_evidence_and_window_fact_share_fallback_authority(
    ledger, platform_message_id, expected_identity
):
    source = SimpleNamespace(**vars(_logs()[0]))
    source.event_id = "fallback_deadbeef"
    source.platform_message_id = platform_message_id

    class _FallbackExtractor:
        async def extract(self, group_id, _logs, **_kwargs):
            return [
                {
                    "candidate_type": "catchphrase",
                    "normalized_expression": "fallback authority",
                    "expression": "fallback authority",
                    "distinct_turn_count": 1,
                    "source_message_ids": ["fallback_deadbeef"],
                    "group_id": group_id,
                }
            ]

    manager = _manager(ledger, quality_logs=[source])
    manager.expression_miner.candidate_extractor = _FallbackExtractor()
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(learning_attribution_enabled=True)
    )

    report = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=[source],
        cursor_before=10,
    )
    candidate = (await ledger.list_due_enrichment(now=2_000_000_000.0, limit=10))[0]
    snapshot = await ledger.load_quality_snapshot(
        candidate.candidate_id, candidate.revision, "quality-v1"
    )
    evidence = (await ledger.list_evidence(candidate.candidate_id))[0]

    assert report["quality_shadow"]["status"] == "completed"
    assert snapshot is not None
    assert snapshot.eligible_message_count == 1
    assert snapshot.group_message_count == 1
    assert snapshot.support_count == 1
    assert durable_message_evidence_id(
        {
            "event_id": evidence.event_id,
            "platform_message_id": evidence.platform_message_id,
            "id": evidence.source_row_id,
        }
    ) == expected_identity


@pytest.mark.asyncio
async def test_discovery_records_policy_rejection_as_skipped_disposition(ledger):
    manager = _manager(ledger)
    manager.expression_miner.input_policy = _FilteringInputPolicy()

    report = await manager._write_candidate_discovery(
        pipeline="expression",
        group_id="qq:group:42",
        logs=_logs(),
        cursor_before=10,
    )

    with sqlite3.connect(ledger.db_path) as db:
        payload = db.execute(
            "SELECT source_dispositions_json FROM learning_source_batch WHERE batch_id = ?",
            (report["batch_id"],),
        ).fetchone()[0]
        diagnostic_stage = db.execute(
            "SELECT stage FROM learning_stage_diagnostic"
        ).fetchone()[0]
    dispositions = {item["source_row_id"]: item for item in json.loads(payload)}
    assert diagnostic_stage == "discover"
    assert dispositions[11]["disposition"] == "candidate_ids"
    assert dispositions[12]["disposition"] == "skipped"
    assert dispositions[12]["reason_code"] == "input_policy_rejected"


@pytest.mark.asyncio
async def test_discovery_fails_closed_when_candidate_source_identity_is_unbound(ledger):
    manager = _manager(ledger)
    manager.expression_miner.candidate_extractor = _UnboundExtractor()

    with pytest.raises(RuntimeError, match="source_evidence_incomplete"):
        await manager._write_candidate_discovery(
            pipeline="expression",
            group_id="qq:group:42",
            logs=_logs(),
            cursor_before=10,
        )

    with sqlite3.connect(ledger.db_path) as db:
        assert db.execute(
            "SELECT status FROM learning_source_batch"
        ).fetchone()[0] == "started"


@pytest.mark.asyncio
async def test_cursor_v2_branch_commits_discovery_and_never_runs_legacy_miner():
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(
            learning_pipeline_timeout_sec=60.0,
            learning_candidate_ledger_enabled=True,
            learning_discovery_cursor_v2_enabled=True,
        )
    )
    manager._pipeline_failure_counts = {}
    manager.expression_miner = SimpleNamespace(
        mine=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy miner must not run")
        )
    )
    manager._get_pipeline_checkpoint = lambda *_args, **_kwargs: _async_value(
        {
            "cursor_log_id": 10,
            "revision": 2,
            "cursor_semantics": "legacy_batch_atomic_v1",
            "pipeline_version": "expression-cursor-state-v1",
        }
    )
    manager._write_candidate_discovery = lambda **_kwargs: _async_value(
        {
            "status": "completed",
            "reason": "discovery_durable",
            "batch_id": "batch:v2",
            "cursor_after": 12,
            "retained_count": 0,
            "candidate_count": 1,
        }
    )
    commits = []

    async def _commit(**kwargs):
        commits.append(kwargs)
        return {"committed": True}

    manager._settle_pipeline_checkpoint = _commit
    manager._record_pipeline_state = lambda **kwargs: _async_value(kwargs)
    manager._mining_batch_id = lambda *_args, **_kwargs: "legacy-batch"

    outcome = await manager._run_learning_pipeline_unlimited(
        "expression",
        "qq:group:42",
        _logs(),
        run_id="run-v2",
    )

    assert outcome["cursor_after"] == 12
    assert commits[0]["cursor_semantics"] == "source_batch_contiguous_v2"
    assert commits[0]["pipeline_version"] == "expression-discovery-v2"
    assert commits[0]["reason"] == "discovery_durable_enrichment_pending"


@pytest.mark.parametrize(
    ("ledger_enabled", "cursor_v2_enabled", "worker_enabled", "provider_enabled", "ready"),
    [
        (True, True, True, True, True),
        (False, True, True, True, False),
        (True, False, True, True, False),
        (True, True, False, True, False),
        (True, True, True, False, False),
    ],
)
def test_enrichment_worker_flag_truth_table_is_fail_closed(
    ledger_enabled,
    cursor_v2_enabled,
    worker_enabled,
    provider_enabled,
    ready,
):
    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(
            learning_candidate_ledger_enabled=ledger_enabled,
            learning_discovery_cursor_v2_enabled=cursor_v2_enabled,
            learning_enrichment_worker_enabled=worker_enabled,
            learning_enrichment_enabled=provider_enabled,
        )
    )

    assert manager._enrichment_worker_gate()[0] is ready


@pytest.mark.asyncio
async def test_runtime_flag_disable_stops_worker_before_next_claim(monkeypatch):
    manager = EvolutionManager.__new__(EvolutionManager)
    config = SimpleNamespace(
        learning_candidate_ledger_enabled=True,
        learning_discovery_cursor_v2_enabled=True,
        learning_enrichment_worker_enabled=True,
        learning_enrichment_enabled=True,
    )
    manager.config = SimpleNamespace(evolution=config)
    calls = 0

    class _Worker:
        async def run_due_once(self, *, limit):
            nonlocal calls
            calls += 1
            config.learning_enrichment_enabled = False

    manager.enrichment_worker = _Worker()
    monkeypatch.setattr("astrmai.learning.evolution_manager.asyncio.sleep", _async_value)

    await manager._enrichment_worker_loop()
    assert calls == 1


async def _async_value(value):
    return value
