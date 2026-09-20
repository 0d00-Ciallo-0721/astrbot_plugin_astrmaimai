from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.persistence.architecture_migration_audit import (
    LATEST_ARCHITECTURE_SCHEMA_VERSION,
    inspect_architecture_migration,
)
from astrmai.infrastructure.persistence.persistence_schema import (
    _MIGRATIONS,
    _run_migrations,
)
from astrmai.learning.dedup import (
    expression_fingerprint,
    expression_fingerprint_v2,
    jargon_fingerprint,
)
from astrmai.learning.mining.candidate_quality import (
    QualitySourceIdentityError,
    benjamini_hochberg,
    build_expression_features,
    build_expression_quality_snapshots,
    build_jargon_quality_snapshots,
    compute_burst_ratio,
    compute_g2,
    compute_log2_effect,
    compute_pmi,
    decay_suggestion,
    decide_expression_shadow,
    decide_jargon_shadow,
    duplicate_hint,
    g2_p_value_df1,
    resolve_alias,
    scan_jargon_shadow,
    shannon_entropy,
)
from astrmai.learning.persistence.candidate_ledger import CandidateLedger
from astrmai.learning.mining.expression_miner import ExpressionMiner
from astrmai.learning.mining.jargon_miner import JargonMiner
from astrmai.learning.quality.contracts import (
    CandidateQualityFeatures,
    LearningQualityProfile,
    candidate_quality_from_mapping,
)


WINDOW_END = 1_700_000_000.0
DAY = 86_400.0


def _features(**overrides) -> CandidateQualityFeatures:
    values = {
        "quality_id": "quality-1",
        "candidate_id": "candidate-1",
        "candidate_revision": 2,
        "profile_version": "quality-v1",
        "profile_hash": LearningQualityProfile.quality_v1().parameters_hash,
        "window_start": WINDOW_END - 7 * DAY,
        "window_end": WINDOW_END,
        "eligible_message_count": 110,
        "unknown_message_count": 0,
        "support_count": 5,
        "speaker_support": 5,
        "speaker_message_count": 30,
        "group_support": 6,
        "group_message_count": 110,
        "other_support": 1,
        "other_total": 80,
        "distinct_turns": 5,
        "distinct_turn_count": 5,
        "distinct_day_count": 3,
        "context_diversity": 4,
        "g2": 10.0,
        "log2_effect": 3.0,
        "signed_log2_lift": 3.0,
        "p_value": 0.001,
        "fdr_q": 0.01,
        "pmi": None,
        "left_entropy_bits": None,
        "right_entropy_bits": None,
        "burst_ratio": None,
        "first_seen_at": WINDOW_END - 6 * DAY,
        "last_seen_at": WINDOW_END - DAY,
        "feature_complete": True,
        "missing_reasons": (),
        "confidence_tier": "high",
        "reasons": ("expression_high_confidence",),
        "created_at": WINDOW_END,
    }
    values.update(overrides)
    return CandidateQualityFeatures(**values)


def test_quality_profile_is_versioned_and_hash_stable():
    first = LearningQualityProfile.quality_v1()
    second = LearningQualityProfile.quality_v1()

    assert first.version == "quality-v1"
    assert first.parameters_hash == second.parameters_hash
    assert len(first.parameters_hash) == 64
    with pytest.raises(TypeError):
        first.parameters["expression_min_support"] = 99


def test_expression_numeric_golden_and_zero_cell_correction():
    assert compute_g2(3, 17, 12, 68) == pytest.approx(0.0, abs=1e-12)
    assert compute_log2_effect(3, 17, 12, 68) == pytest.approx(
        math.log2(((3 + 0.5) / (20 + 1.0)) / ((12 + 0.5) / (80 + 1.0)))
    )

    g2 = compute_g2(5, 25, 1, 79)
    effect = compute_log2_effect(5, 25, 1, 79)
    assert g2 is not None and g2 > 6.63
    assert effect is not None and effect > 1.0
    assert g2_p_value_df1(g2) == pytest.approx(
        math.erfc(math.sqrt(g2 / 2.0))
    )
    assert math.isfinite(compute_log2_effect(0, 20, 5, 75))
    assert compute_log2_effect(0, 20, 5, 75) < 0


def test_expression_invalid_tables_fail_closed():
    assert compute_g2(0, 0, 0, 0) is None
    assert compute_log2_effect(3, 17, 0, 0) is None
    assert compute_g2(-1, 1, 1, 1) is None
    assert g2_p_value_df1(float("nan")) is None


def test_bh_is_stable_monotonic_and_ignores_unknown():
    expected = {
        "a": pytest.approx(0.003),
        "b": pytest.approx(0.015),
        "c": pytest.approx(0.04),
        "unknown": None,
    }
    items = [("c", 0.04), ("unknown", None), ("a", 0.001), ("b", 0.01)]
    assert benjamini_hochberg(items) == expected
    assert benjamini_hochberg(reversed(items)) == expected
    tied = benjamini_hochberg([("z", 0.01), ("a", 0.01)])
    assert tied == {"a": pytest.approx(0.01), "z": pytest.approx(0.01)}


def test_expression_decision_requires_all_personal_thresholds():
    profile = LearningQualityProfile.quality_v1()
    high = decide_expression_shadow(_features(), profile)
    negative = decide_expression_shadow(
        _features(log2_effect=-3.0, signed_log2_lift=-3.0), profile
    )
    too_few_messages = decide_expression_shadow(
        _features(
            speaker_message_count=19,
            group_message_count=99,
            eligible_message_count=99,
            other_total=80,
        ),
        profile,
    )

    assert high.decision == "high_confidence_shadow"
    assert high.shadow_eligible_for_enrichment is True
    assert not hasattr(high, "eligible_for_enrichment")
    assert negative.decision == "group_shadow_only"
    assert "negative_lift" in negative.reasons
    assert too_few_messages.decision == "insufficient_data"
    assert "insufficient_speaker_messages" in too_few_messages.reasons


def test_dto_alias_adapter_preserves_nullable_and_compatibility_columns():
    payload = _features().__dict__ if hasattr(_features(), "__dict__") else {
        name: getattr(_features(), name)
        for name in CandidateQualityFeatures.__dataclass_fields__
    }
    payload.pop("profile_version")
    payload.pop("fdr_q")
    payload.pop("pmi")
    payload.pop("left_entropy_bits")
    payload.pop("right_entropy_bits")
    payload.update(
        feature_version="quality-v1",
        q_value=0.01,
        min_split_pmi=None,
        left_entropy=None,
        right_entropy=None,
    )

    restored = candidate_quality_from_mapping(payload)

    assert restored.profile_version == "quality-v1"
    assert restored.fdr_q == pytest.approx(0.01)
    assert restored.pmi is None
    assert restored.signed_log2_lift == restored.log2_effect
    assert restored.distinct_turn_count == restored.distinct_turns


def test_quality_dto_fails_closed_on_denominator_or_nullable_mismatch():
    with pytest.raises(ValueError, match="eligible_denominator_conservation_failed"):
        replace(_features(), eligible_message_count=111)
    with pytest.raises(ValueError, match="incomplete_contingency_counts"):
        replace(_features(), speaker_support=None)


@pytest.mark.parametrize("value", [1.5, True, "1"])
def test_quality_dto_rejects_non_integer_counts(value):
    with pytest.raises(ValueError, match="invalid_quality_count"):
        replace(_features(), support_count=value)


def test_pmi_entropy_and_burst_are_deterministic_and_nullable():
    frequencies = {
        "甲乙丙": 5,
        "甲": 30,
        "乙丙": 10,
        "甲乙": 8,
        "丙": 25,
    }
    expected_splits = [
        math.log2(((5 + 1) / 101) / (((30 + 1) / 101) * ((10 + 1) / 101))),
        math.log2(((5 + 1) / 101) / (((8 + 1) / 101) * ((25 + 1) / 101))),
    ]
    assert compute_pmi("甲乙丙", frequencies, 100) == pytest.approx(
        min(expected_splits)
    )
    assert compute_pmi("甲", frequencies, 100) is None
    assert compute_pmi("甲乙", frequencies, 0) is None
    assert shannon_entropy({"a": 2, "b": 2, "c": 4}) == pytest.approx(1.5)
    assert shannon_entropy({}) is None
    assert compute_burst_ratio(5, 10, 1, 100) == pytest.approx(
        ((5.5 / 11.0) / (1.5 / 101.0))
    )
    assert compute_burst_ratio(1, 10, 0, 0) is None


def test_jargon_shadow_scan_is_bounded_deduplicated_and_permutation_stable():
    messages = [
        {
            "source_id": "source-b",
            "content": "甲小火箭乙 甲小火箭乙 yyds yyds",
            "timestamp": WINDOW_END - 100,
            "eligible": True,
        },
        {
            "source_id": "source-a",
            "content": "丙小火箭丁 yyds",
            "timestamp": WINDOW_END - DAY - 100,
            "eligible": True,
        },
    ]
    profile = LearningQualityProfile.quality_v1()

    first = scan_jargon_shadow(messages, profile=profile, window_end=WINDOW_END)
    second = scan_jargon_shadow(reversed(messages), profile=profile, window_end=WINDOW_END)

    assert first == second
    assert first["report"]["scan_passes"] == 1
    assert first["report"]["retained_candidate_count"] <= 2000
    assert first["candidates"]["小火箭"]["support_count"] == 2
    assert first["candidates"]["yyds"]["support_count"] == 2
    assert first["candidates"]["yyds"]["abbreviation_shape"] is True
    assert first["candidates"]["yyds"]["expansion_status"] == "unknown"


def test_jargon_shadow_source_replay_is_idempotent_and_conflicts_fail_closed():
    profile = LearningQualityProfile.quality_v1()
    message = {
        "source_id": "source-a",
        "content": "甲小火箭乙",
        "timestamp": WINDOW_END - 100,
        "eligible": True,
    }

    one = scan_jargon_shadow([message], profile=profile, window_end=WINDOW_END)
    replay = scan_jargon_shadow(
        [message, dict(message)], profile=profile, window_end=WINDOW_END
    )
    conflict = scan_jargon_shadow(
        [message, {**message, "content": "冲突内容"}],
        profile=profile,
        window_end=WINDOW_END,
    )

    assert replay["report"]["eligible_documents"] == one["report"]["eligible_documents"]
    assert replay["candidates"]["小火箭"]["support_count"] == 1
    assert conflict["candidates"] == {}
    assert conflict["report"]["status"] == "blocked"
    assert conflict["report"]["reason"] == "source_identity_conflict"
    assert conflict["report"]["scan_passes"] == 0


def test_jargon_decision_requires_support_and_two_sided_entropy():
    profile = LearningQualityProfile.quality_v1()
    low = replace(
        _features(
            speaker_support=None,
            speaker_message_count=None,
            other_support=None,
            other_total=None,
            support_count=5,
            group_support=5,
            group_message_count=20,
            eligible_message_count=20,
            g2=None,
            log2_effect=None,
            signed_log2_lift=None,
            p_value=None,
            fdr_q=None,
            pmi=2.0,
            left_entropy_bits=2.0,
            right_entropy_bits=1.0,
            feature_complete=True,
            confidence_tier="low",
            reasons=(),
        )
    )
    high = replace(low, right_entropy_bits=2.0)

    assert decide_jargon_shadow(low, profile).decision == "low_confidence_shadow"
    assert decide_jargon_shadow(high, profile).decision == "high_confidence_shadow"


def test_expression_v1_golden_unchanged_and_v2_is_explicitly_scoped():
    golden = expression_fingerprint("group-1", "ending", " 好呀～ ", "场景一")
    assert golden == "expression:5fa87ec8688999a3be0e27dd"
    assert golden == expression_fingerprint("group-1", "ending", "好呀~", "场景二")
    group = expression_fingerprint_v2(
        scope_id="qq:group:1",
        speaker_scope_id=None,
        habit_type="ending",
        pattern=" 好呀～ ",
        situation_family="reply",
    )
    same = expression_fingerprint_v2(
        scope_id="qq:group:1",
        speaker_scope_id="",
        habit_type="ending",
        pattern="好呀~",
        situation_family=" reply ",
    )
    personal = expression_fingerprint_v2(
        scope_id="qq:group:1",
        speaker_scope_id="qq:group:1:user-a",
        habit_type="ending",
        pattern="好呀~",
        situation_family="reply",
    )
    other_situation = expression_fingerprint_v2(
        scope_id="qq:group:1",
        speaker_scope_id=None,
        habit_type="ending",
        pattern="好呀~",
        situation_family="greeting",
    )

    assert group == same
    assert len({group, personal, other_situation}) == 3
    assert jargon_fingerprint("词", "旧义") == jargon_fingerprint("词", "新义")


def test_alias_and_near_duplicate_are_hint_only_and_fail_closed():
    assert resolve_alias({"v1": "canonical"}, "v1") == ("canonical", "resolved")
    assert resolve_alias({"a": "b", "b": "a"}, "a") == (None, "alias_cycle")
    assert resolve_alias({"a": ("x", "y")}, "a") == (None, "alias_conflict")
    short = duplicate_hint("哈", "哈哈")
    variant = duplicate_hint("哈哈!", "哈哈")
    embedding = duplicate_hint(
        "甲乙丙丁",
        "甲乙丙戊",
        embedding_similarity=0.9,
        embedding_identity=None,
    )

    assert short["method"] == "unavailable"
    assert variant["action"] == "hint_only"
    assert embedding["reason"] == "embedding_identity_unavailable"
    assert embedding["provider_call_count"] == 0


def test_decay_suggestion_never_claims_lifecycle_ownership():
    expression = decay_suggestion(
        candidate_family="expression",
        last_seen_at=WINDOW_END - 180 * DAY,
        window_end=WINDOW_END,
    )
    jargon = decay_suggestion(
        candidate_family="jargon",
        last_seen_at=WINDOW_END - 180 * DAY,
        window_end=WINDOW_END,
    )

    assert expression["factor"] == pytest.approx(0.9**180)
    assert jargon["factor"] == pytest.approx(0.95**180)
    assert expression["suggested_status"] == "stale"
    assert expression["lifecycle_owner"] == "MemoryV2Store.apply_decay"
    assert expression["state_changed"] is False


def _quality_database(path):
    with sqlite3.connect(path) as db:
        for version, ddl in _MIGRATIONS:
            if version in (145, 146):
                db.execute(ddl)
        db.execute("PRAGMA user_version = 146")
        _run_migrations(db)
        db.execute(
            """
            INSERT INTO learning_source_batch(
                batch_id, pipeline_type, scope_id, cursor_before, cursor_after,
                source_ids_json, source_ids_hash, source_count, eligible_count,
                skipped_count, no_candidate_count, candidate_count, status,
                revision, created_at, updated_at
            ) VALUES ('batch-1','expression','qq:group:1',0,1,'[1]','hash',1,1,0,0,1,
                      'completed',1,1,1)
            """
        )
        db.execute(
            """
            INSERT INTO learning_candidate(
                candidate_id, first_discovered_batch_id, scope_id, candidate_type,
                fingerprint, extractor_version, status, revision, created_at, updated_at
            ) VALUES ('candidate-1','batch-1','qq:group:1','expression','fp','v1',
                      'enrichment_pending',2,1,1)
            """
        )
        db.commit()


@pytest.mark.asyncio
async def test_quality_snapshot_round_trip_is_immutable_and_nullable(tmp_path):
    path = tmp_path / "quality.db"
    _quality_database(path)
    ledger = CandidateLedger(path)
    features = _features(pmi=None, burst_ratio=None)

    inserted = await ledger.store_quality_snapshot(features)
    replayed = await ledger.store_quality_snapshot(features)
    conflict = await ledger.store_quality_snapshot(replace(features, context_diversity=5))
    restored = await ledger.load_quality_snapshot("candidate-1", 2, "quality-v1")

    assert inserted.inserted is True
    assert replayed.idempotent is True
    assert conflict.conflict is True
    assert restored == features
    assert restored.pmi is None
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 160
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(learning_candidate_quality)")
        }
    assert {
        "profile_version",
        "profile_hash",
        "log2_effect",
        "signed_log2_lift",
        "distinct_turns",
        "distinct_turn_count",
        "missing_reasons_json",
        "reasons_json",
    } <= columns


@pytest.mark.asyncio
async def test_quality_persistence_revalidates_fractional_counts(tmp_path):
    path = tmp_path / "quality-invalid-count.db"
    _quality_database(path)
    ledger = CandidateLedger(path)
    invalid = _features()
    object.__setattr__(invalid, "support_count", 1.5)

    with pytest.raises(ValueError, match="invalid_quality_count"):
        await ledger.store_quality_snapshot(invalid)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM learning_candidate_quality").fetchone() == (0,)


@pytest.mark.asyncio
async def test_quality_snapshot_revision_and_invalid_json_fail_closed(tmp_path):
    path = tmp_path / "quality-conflicts.db"
    _quality_database(path)
    ledger = CandidateLedger(path)
    features = _features()

    stale = await ledger.store_quality_snapshot(replace(features, candidate_revision=1))
    assert stale.conflict is True
    assert stale.failure_kind == "candidate_revision_conflict"

    assert (await ledger.store_quality_snapshot(features)).inserted is True
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE learning_candidate_quality SET reasons_json = '{broken' "
            "WHERE quality_id = ?",
            (features.quality_id,),
        )
        db.commit()
    with pytest.raises(ValueError, match="invalid_quality_snapshot_json"):
        await ledger.load_quality_snapshot("candidate-1", 2, "quality-v1")


def test_quality_migration_from_v129_is_repeatable_and_audited(tmp_path):
    path = tmp_path / "v129.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 129")
        _run_migrations(db)
        db.commit()
        _run_migrations(db)
        db.commit()
        report = inspect_architecture_migration(db)

    assert LATEST_ARCHITECTURE_SCHEMA_VERSION == 160
    assert "learning_candidate_quality" not in report.missing_tables
    assert "ix_learning_quality_candidate" not in report.missing_indexes


def test_twenty_permutations_keep_shadow_results_stable():
    messages = [
        {
            "source_id": f"source-{index}",
            "content": f"甲小火箭{index % 4}乙 yyds",
            "timestamp": WINDOW_END - (index % 15) * DAY,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": False,
            "speaker_scope_id": "",
        }
        for index in range(30)
    ]
    profile = LearningQualityProfile.quality_v1()
    expected = scan_jargon_shadow(messages, profile=profile, window_end=WINDOW_END)
    expression_candidate = {
        "candidate_id": "replay-candidate",
        "candidate_revision": 1,
        "source_message_ids": [item["source_id"] for item in messages],
    }
    expected_expression = build_expression_quality_snapshots(
        [expression_candidate], messages, profile=profile, window_end=WINDOW_END
    )

    for offset in range(20):
        reordered = messages[offset:] + messages[:offset] + list(reversed(messages))
        assert scan_jargon_shadow(
            reordered, profile=profile, window_end=WINDOW_END
        ) == expected
        assert build_expression_quality_snapshots(
            [expression_candidate],
            reordered,
            profile=profile,
            window_end=WINDOW_END,
        ) == expected_expression


def test_durable_snapshot_builders_bind_candidate_revision_and_profile():
    profile = LearningQualityProfile.quality_v1()
    messages = [
        {
            "source_id": "source-a",
            "content": "甲小火箭乙 yyds",
            "timestamp": WINDOW_END - 100,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": True,
            "speaker_scope_id": "scope:user-a",
        },
        {
            "source_id": "source-b",
            "content": "丙小火箭丁 yyds",
            "timestamp": WINDOW_END - DAY - 100,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": True,
            "speaker_scope_id": "scope:user-b",
        },
    ]
    expression = build_expression_quality_snapshots(
        [
            {
                "candidate_id": "expression-candidate",
                "candidate_revision": 4,
                "speaker_scope_id": "scope:user-a",
                "source_message_ids": ["source-a"],
                "distinct_turn_count": 1,
            }
        ],
        messages,
        profile=profile,
        window_end=WINDOW_END,
    )
    scan = scan_jargon_shadow(messages, profile=profile, window_end=WINDOW_END)
    jargon = build_jargon_quality_snapshots(
        [
            {
                "candidate_id": "jargon-candidate",
                "candidate_revision": 7,
                "canonical_form": "小火箭",
            }
        ],
        scan,
        profile=profile,
        created_at=WINDOW_END,
    )

    assert expression[0].candidate_revision == 4
    assert expression[0].profile_hash == profile.parameters_hash
    assert expression[0].speaker_message_count == 1
    assert jargon[0].candidate_revision == 7
    assert jargon[0].support_count == 2


def test_snapshot_builders_preserve_unknown_timestamps_and_recompute_counts():
    profile = LearningQualityProfile.quality_v1()
    messages = [
        {
            "source_id": "known",
            "content": "甲小火箭乙",
            "timestamp": WINDOW_END - 100,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": True,
            "speaker_scope_id": "scope:user-a",
        },
        {
            "source_id": "unknown",
            "content": "丙小火箭丁",
            "timestamp": None,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": True,
            "speaker_scope_id": "scope:user-a",
        },
    ]
    candidate = {
        "candidate_id": "candidate-unknown-time",
        "candidate_revision": 2,
        "speaker_scope_id": "scope:user-a",
        "source_message_ids": ["known", "unknown"],
        "distinct_turn_count": 99,
        "distinct_day_count": 99,
        "distinct_contributor_count": 99,
    }

    expression = build_expression_quality_snapshots(
        [candidate], messages, profile=profile, window_end=WINDOW_END
    )[0]
    scan = scan_jargon_shadow(messages, profile=profile, window_end=WINDOW_END)
    jargon = build_jargon_quality_snapshots(
        [{**candidate, "canonical_form": "小火箭"}],
        scan,
        profile=profile,
        created_at=WINDOW_END,
    )[0]

    assert expression.eligible_message_count == 2
    assert expression.group_message_count == 1
    assert expression.unknown_message_count == 1
    assert expression.distinct_turn_count == 1
    assert expression.distinct_day_count == 1
    assert expression.context_diversity == 1
    assert expression.feature_complete is False
    assert "timestamp_unavailable" in expression.missing_reasons
    assert scan["report"]["unknown_timestamps"] == 1
    assert jargon.eligible_message_count == 2
    assert jargon.group_message_count == 1
    assert jargon.unknown_message_count == 1
    assert jargon.feature_complete is False
    assert "timestamp_unavailable" in jargon.missing_reasons


def test_expression_snapshot_source_replay_is_idempotent_and_conflicts_fail_closed():
    profile = LearningQualityProfile.quality_v1()
    message = {
        "source_id": "source-a",
        "content": "same",
        "timestamp": WINDOW_END - 100,
        "eligible": True,
        "known_scope": True,
        "eligible_for_speaker_stats": False,
        "speaker_scope_id": "",
    }
    candidate = {
        "candidate_id": "candidate-a",
        "candidate_revision": 1,
        "source_message_ids": ["source-a"],
    }

    replay = build_expression_quality_snapshots(
        [candidate], [message, dict(message)], profile=profile, window_end=WINDOW_END
    )
    assert replay[0].eligible_message_count == 1
    assert replay[0].support_count == 1
    with pytest.raises(QualitySourceIdentityError, match="source_identity_conflict"):
        build_expression_quality_snapshots(
            [candidate],
            [message, {**message, "content": "different"}],
            profile=profile,
            window_end=WINDOW_END,
        )


def test_expression_snapshot_keeps_equal_values_from_different_identity_domains():
    profile = LearningQualityProfile.quality_v1()
    messages = [
        {
            "source_id": "event_id:1",
            "content": "event fact",
            "timestamp": WINDOW_END - 200,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": False,
        },
        {
            "source_id": "row:1",
            "content": "row fact",
            "timestamp": WINDOW_END - 100,
            "eligible": True,
            "known_scope": True,
            "eligible_for_speaker_stats": False,
        },
    ]
    snapshots = build_expression_quality_snapshots(
        [
            {
                "candidate_id": "typed-identity-candidate",
                "candidate_revision": 1,
                "source_message_ids": ["event_id:1", "row:1"],
            }
        ],
        messages,
        profile=profile,
        window_end=WINDOW_END,
    )

    assert snapshots[0].eligible_message_count == 2
    assert snapshots[0].group_message_count == 2
    assert snapshots[0].support_count == 2


class _FakeEnricher:
    def __init__(self):
        self.calls = 0
        self.last_provider_attempt = None

    async def enrich(self, _group_id, candidates):
        self.calls += 1
        return list(candidates)


def _miner_config(shadow_enabled: bool):
    return SimpleNamespace(
        evolution=SimpleNamespace(
            expression_min_count=2,
            expression_min_distinct_turns=3,
            expression_min_valid_messages=3,
            jargon_min_count=2,
            learning_quality_shadow_enabled=shadow_enabled,
            learning_enrichment_enabled=True,
        )
    )


def _shadow_messages(content: str):
    return [
        SimpleNamespace(
            id=index,
            event_id=f"event-{index}",
            platform_message_id=f"message-{index}",
            group_id="group-1",
            chat_kind="group",
            role="user",
            message_kind="text",
            sender_id="alice" if index <= 3 else "bob",
            sender_name="same-name",
            content=content,
            timestamp=WINDOW_END - 100 + index,
            learning_source_kind="user_said",
            learning_evidence_eligible=True,
        )
        for index in range(1, 5)
    ]


def test_unknown_profile_fails_closed_without_provider_calls():
    config = _miner_config(True)
    config.evolution.learning_quality_profile_version = "quality-v2-unknown"
    gateway = SimpleNamespace(config=config)
    expression = ExpressionMiner(gateway, config=config)
    jargon = JargonMiner(SimpleNamespace(gateway=gateway, config=config))

    expression_report = expression._quality_shadow([], _shadow_messages("嘿嘿"))
    jargon_report = jargon._quality_shadow(_shadow_messages("bigbird"))

    assert expression_report == {
        "status": "blocked",
        "reason": "unknown_profile",
        "profile_version": "quality-v2-unknown",
        "provider_call_count": 0,
    }
    assert jargon_report == expression_report


def test_expression_miner_shadow_maps_display_ids_to_typed_durable_ids():
    config = _miner_config(True)
    miner = ExpressionMiner(SimpleNamespace(config=config), config=config)
    messages = _shadow_messages("typed identity")
    for message in messages:
        message.group_id = "qq:group:1"
    report = miner._quality_shadow(
        [
            {
                "candidate_id": "candidate-typed",
                "candidate_revision": 1,
                "source_message_ids": ["event-1", "event-2"],
            }
        ],
        messages,
    )

    assert report["status"] == "completed"
    assert report["candidates"][0]["group_support"] == 2


def test_expression_miner_shadow_blocks_ambiguous_display_identity_domains():
    config = _miner_config(True)
    miner = ExpressionMiner(SimpleNamespace(config=config), config=config)
    messages = [
        SimpleNamespace(**{**vars(_shadow_messages("first")[0]), "id": 10, "event_id": "1"}),
        SimpleNamespace(**{**vars(_shadow_messages("second")[1]), "id": 1, "event_id": "", "platform_message_id": ""}),
    ]

    report = miner._quality_shadow([], messages)

    assert report["status"] == "blocked"
    assert report["reason"] == "source_identity_conflict"


@pytest.mark.parametrize("event_id", ["fallback_deadbeef", "evt_deadbeef"])
def test_quality_miners_block_fallback_event_without_authoritative_identity(event_id):
    config = _miner_config(True)
    gateway = SimpleNamespace(config=config)
    expression = ExpressionMiner(gateway, config=config)
    jargon = JargonMiner(SimpleNamespace(gateway=gateway, config=config))
    message = SimpleNamespace(
        id=None,
        event_id=event_id,
        platform_message_id="",
        group_id="qq:group:1",
        chat_kind="group",
        role="user",
        message_kind="text",
        sender_id="alice",
        sender_name="Alice",
        content="fallback evidence must not count",
        timestamp=WINDOW_END - 1,
        learning_source_kind="user_said",
        learning_evidence_eligible=True,
    )

    expression_report = expression._quality_shadow(
        [
            {
                "candidate_id": "fallback-candidate",
                "candidate_revision": 1,
                "source_message_ids": [event_id],
            }
        ],
        [message],
    )
    jargon_report = jargon._quality_shadow([message])

    for report in (expression_report, jargon_report):
        assert report["status"] == "blocked"
        assert report["reason"] == "source_identity_unavailable"
        assert report["provider_call_count"] == 0
        assert "eligible_message_count" not in report
        assert "eligible_documents" not in report


@pytest.mark.asyncio
async def test_expression_shadow_on_off_preserves_output_and_provider_calls():
    async def run(enabled: bool):
        config = _miner_config(enabled)
        gateway = SimpleNamespace(config=config)
        miner = ExpressionMiner(gateway, config=config)
        enricher = _FakeEnricher()
        miner.enricher = enricher
        output = await miner.mine("group-1", _shadow_messages("嘿嘿"))
        return output, enricher.calls, dict(miner.last_report)

    off_output, off_calls, off_report = await run(False)
    on_output, on_calls, on_report = await run(True)

    assert on_output == off_output
    assert on_calls == off_calls == 1
    assert "quality_shadow" not in off_report
    assert on_report["quality_shadow"]["status"] == "completed"
    assert on_report["quality_shadow"]["provider_call_count"] == 0


@pytest.mark.asyncio
async def test_jargon_shadow_on_off_preserves_output_and_provider_calls():
    async def run(enabled: bool):
        config = _miner_config(enabled)
        gateway = SimpleNamespace(config=config)
        expression_miner = SimpleNamespace(gateway=gateway, config=config)
        miner = JargonMiner(expression_miner)
        enricher = _FakeEnricher()
        miner.enricher = enricher
        output = await miner.mine("group-1", _shadow_messages("bigbird appears"))
        return output, enricher.calls, dict(miner.last_report)

    off_output, off_calls, off_report = await run(False)
    on_output, on_calls, on_report = await run(True)

    assert on_output == off_output
    assert on_calls == off_calls == 1
    assert "quality_shadow" not in off_report
    assert on_report["quality_shadow"]["status"] == "completed"
    assert on_report["quality_shadow"]["provider_call_count"] == 0
