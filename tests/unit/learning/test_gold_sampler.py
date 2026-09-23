from __future__ import annotations

from astrmai.learning.evaluation.gold_sampler import STRATA, build_gold_manifest
from astrmai.learning.evaluation.contracts import canonical_hash
import pytest


def _row(index: int, scope: str) -> dict:
    return {
        "source_row_ids": [index],
        "source_message_ids": [f"msg-{index}"],
        "identity_source": "row",
        "scope_id": scope,
        "speaker_id": f"speaker-{index % 3}",
        "candidate_family": "expression" if index % 2 else "jargon",
        "evidence_quality": "medium",
        "source_type": "message",
        "snapshot_hash": canonical_hash({"fixture": "gold"}),
        "pipeline_version": "p1",
        "extractor_version": "e1",
    }


def test_gold_sampling_is_stable_and_does_not_copy_units():
    rows = [_row(index, f"group-{index % 4}") for index in range(1, 45)]
    first = build_gold_manifest(rows, seed=20260915, target_size=480)
    second = build_gold_manifest(list(reversed(rows)), seed=20260915, target_size=480)
    assert first.manifest_hash == second.manifest_hash
    assert [unit.sample_id for unit in first.units] == [unit.sample_id for unit in second.units]
    assert len({unit.sample_id for unit in first.units}) == len(first.units)


def test_scope_floor_is_capped_at_62_and_covers_31_groups():
    rows = [_row(index, f"group-{(index - 1) // 3}") for index in range(1, 94)]
    manifest = build_gold_manifest(rows, seed=20260915, target_size=62)
    by_scope = {}
    for unit in manifest.units:
        by_scope.setdefault(unit.scope_id, 0)
        by_scope[unit.scope_id] += 1
    assert len(manifest.units) == 62
    assert len(by_scope) == 31
    assert all(count >= 2 for count in by_scope.values())


def test_insufficient_strata_keeps_all_without_duplication():
    rows = [_row(index, "one") for index in range(1, 6)]
    manifest = build_gold_manifest(rows, target_size=480)
    assert len(manifest.units) == 5
    assert len({unit.sample_id for unit in manifest.units}) == 5
    assert set(name for name, _ in STRATA) - set(manifest.strata) == set()
    assert manifest.unavailable_strata


def test_missing_durable_identity_is_rejected():
    row = _row(1, "group")
    row["source_row_ids"] = []
    row["candidate_id"] = None
    with pytest.raises(ValueError, match="durable"):
        build_gold_manifest([row])


@pytest.mark.parametrize("bad_rows", [["not-a-row-id"], [True], [1.5], [0], [-1], ["1"]])
def test_malformed_source_row_identity_is_rejected(bad_rows):
    row = _row(1, "group")
    row["candidate_id"] = None
    row["source_row_ids"] = bad_rows
    with pytest.raises(ValueError, match="source_row_ids"):
        build_gold_manifest([row])


def test_missing_snapshot_provenance_is_rejected():
    rows = [_row(1, "group"), _row(2, "group")]
    rows[1].pop("snapshot_hash")
    with pytest.raises(ValueError, match="snapshot_hash"):
        build_gold_manifest(rows)


def test_sampler_manifest_is_exploratory_and_not_business_gate_eligible():
    manifest = build_gold_manifest([_row(1, "group")], annotation_status="ai_exploratory")
    assert manifest.manifest_kind == "exploratory_sample"
    assert manifest.annotation_status == "ai_exploratory"
    assert manifest.human_gold_status == "not_required_for_discovery"
    assert manifest.business_gate_eligible is False
    assert manifest.to_dict()["human_gold_status"] == "not_required_for_discovery"


@pytest.mark.parametrize("field", ["snapshot_hash", "pipeline_version", "extractor_version"])
def test_mixed_manifest_identity_is_rejected(field):
    rows = [_row(1, "group"), _row(2, "group")]
    rows[1][field] = canonical_hash({"other": True}) if field == "snapshot_hash" else "other-version"
    with pytest.raises(ValueError, match="mixed"):
        build_gold_manifest(rows)
