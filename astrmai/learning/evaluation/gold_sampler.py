from __future__ import annotations

import hashlib
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import EvaluationUnit, GoldSetManifest, canonical_hash, canonical_json


STRATA: tuple[tuple[str, int], ...] = (
    ("image/context-only or content missing", 40),
    ("historical rejected/revision_needed", 60),
    ("expression candidate", 80),
    ("jargon candidate", 80),
    ("expression hard negative", 60),
    ("jargon hard negative", 60),
    ("structured relation-rich", 50),
    ("legacy relation-poor", 50),
)


def _rows(source_reader: Any) -> list[Mapping[str, Any]]:
    if callable(source_reader):
        source_reader = source_reader()
    elif hasattr(source_reader, "read") and callable(source_reader.read):
        source_reader = source_reader.read()
    elif hasattr(source_reader, "iter_units") and callable(source_reader.iter_units):
        source_reader = source_reader.iter_units()
    return [item for item in source_reader or () if isinstance(item, Mapping)]


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _durable_source_rows(row: Mapping[str, Any]) -> tuple[int, ...]:
    if "source_row_ids" not in row or row.get("source_row_ids") is None:
        return ()
    raw = row.get("source_row_ids")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("source_row_ids must be a list or tuple")
    if any(type(item) is not int or item <= 0 for item in raw):
        raise ValueError("source_row_ids must contain strict positive integers")
    return tuple(sorted(set(raw)))


def _identity(rows: Mapping[str, Any], snapshot_hash: str, pipeline_version: str, seed: int) -> str:
    candidate = str(rows.get("candidate_id") or "").strip()
    source_rows = list(_durable_source_rows(rows))
    if not candidate and not source_rows:
        raise ValueError("gold sample requires durable candidate_id or source_row_ids")
    identity = {
        "snapshot_hash": snapshot_hash,
        "candidate_id_or_sorted_source_row_ids": candidate or source_rows,
        "pipeline_version": pipeline_version,
        "seed": seed,
    }
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()


def _stratum(row: Mapping[str, Any]) -> str:
    source_type = str(row.get("source_type") or "").lower()
    if row.get("content_missing") or source_type in {"image", "context-only", "context_only"}:
        return STRATA[0][0]
    if str(row.get("review_status") or "").lower() in {"rejected", "revision_needed"}:
        return STRATA[1][0]
    family = str(row.get("candidate_family") or "").lower()
    hard_negative = bool(row.get("hard_negative"))
    if family == "expression" and not hard_negative:
        return STRATA[2][0]
    if family == "jargon" and not hard_negative:
        return STRATA[3][0]
    if family == "expression" and hard_negative:
        return STRATA[4][0]
    if family == "jargon" and hard_negative:
        return STRATA[5][0]
    if row.get("relation_rich") or row.get("structured"):
        return STRATA[6][0]
    return STRATA[7][0]


def _unit(row: Mapping[str, Any], *, snapshot_hash: str, pipeline_version: str, extractor_version: str, seed: int) -> EvaluationUnit:
    source_rows = _durable_source_rows(row)
    sample_id = _identity(row, snapshot_hash, pipeline_version, seed)
    return EvaluationUnit(
        sample_id=sample_id,
        source_row_ids=source_rows,
        source_message_ids=tuple(sorted(str(item) for item in (row.get("source_message_ids") or ()) if str(item).strip())),
        identity_source=str(row.get("identity_source") or ("row" if source_rows else "unknown")),
        candidate_id=str(row.get("candidate_id") or "") or None,
        candidate_family=str(row.get("candidate_family") or "uncertain"),
        scope_id=str(row.get("scope_id") or "unknown"),
        speaker_id=str(row.get("speaker_id") or "unknown"),
        evidence_quality=str(row.get("evidence_quality") or "unknown"),
        source_type=str(row.get("source_type") or "unknown"),
        pipeline_version=pipeline_version,
        extractor_version=extractor_version,
        snapshot_hash=snapshot_hash,
        primary_stratum=_stratum(row),
        secondary_tags=tuple(sorted(str(item) for item in (row.get("secondary_tags") or ()) if str(item).strip())),
    )


def build_gold_manifest(
    source_reader: Any,
    *,
    seed: int = 20260915,
    target_size: int = 480,
    annotation_status: str = "not_collected",
    human_gold_status: str = "not_required_for_discovery",
) -> GoldSetManifest:
    if type(seed) is not int or seed < 0 or type(target_size) is not int or target_size < 0:
        raise ValueError("seed and target_size must be strict non-negative integers")
    raw = _rows(source_reader)
    supplied_snapshot_hashes = set()
    if raw:
        for item in raw:
            snapshot_hash = item.get("snapshot_hash")
            if not isinstance(snapshot_hash, str) or not _SHA256_RE.fullmatch(snapshot_hash):
                raise ValueError("every gold sample requires a valid snapshot_hash")
            supplied_snapshot_hashes.add(snapshot_hash.lower())
    if len(supplied_snapshot_hashes) > 1:
        raise ValueError("mixed source snapshot hashes cannot share a gold manifest")
    supplied_pipeline_versions = {str(item.get("pipeline_version") or "") for item in raw}
    supplied_extractor_versions = {str(item.get("extractor_version") or "") for item in raw}
    if len(supplied_pipeline_versions) > 1 or len(supplied_extractor_versions) > 1:
        raise ValueError("mixed pipeline or extractor versions cannot share a gold manifest")
    snapshot_hash = next(iter(supplied_snapshot_hashes), "") if raw else ""
    if not snapshot_hash:
        snapshot_hash = canonical_hash([dict(item) for item in raw])
    pipeline_version = next(iter(supplied_pipeline_versions), "evaluation-v1") if raw else "evaluation-v1"
    extractor_version = next(iter(supplied_extractor_versions), "unknown") if raw else "unknown"
    units = [_unit(item, snapshot_hash=snapshot_hash, pipeline_version=pipeline_version, extractor_version=extractor_version, seed=seed) for item in raw]
    by_stratum: dict[str, list[EvaluationUnit]] = defaultdict(list)
    for unit in units:
        by_stratum[unit.primary_stratum].append(unit)
    selected: dict[str, list[EvaluationUnit]] = defaultdict(list)
    remaining = max(0, target_size)
    selected_ids: set[str] = set()
    groups: dict[str, list[EvaluationUnit]] = defaultdict(list)
    for unit in units:
        if unit.scope_id != "unknown":
            groups[unit.scope_id].append(unit)
    floor_candidates = sorted(
        (unit for group in groups.values() for unit in sorted(group, key=lambda item: item.sample_id)[:2]),
        key=lambda item: item.sample_id,
    )
    for unit in floor_candidates[: min(62, remaining)]:
        selected[unit.primary_stratum].append(unit)
        selected_ids.add(unit.sample_id)
    remaining -= len(selected_ids)
    for name, quota in STRATA:
        candidates = [item for item in by_stratum[name] if item.sample_id not in selected_ids]
        take = min(quota, remaining, len(candidates))
        for unit in sorted(candidates, key=lambda item: item.sample_id)[:take]:
            selected[name].append(unit)
            selected_ids.add(unit.sample_id)
        remaining -= take
    if remaining > 0:
        pools = {name: [item for item in values if item.sample_id not in selected_ids] for name, values in by_stratum.items()}
        while remaining and any(pools.values()):
            ordered = sorted(
                ((name, values) for name, values in pools.items() if values),
                key=lambda pair: (-math.sqrt(len(pair[1])), next(index for index, (n, _) in enumerate(STRATA) if n == pair[0])),
            )
            for name, values in ordered:
                if not remaining or not values:
                    continue
                unit = sorted(values, key=lambda item: item.sample_id)[0]
                selected[name].append(unit)
                selected_ids.add(unit.sample_id)
                values.remove(unit)
                remaining -= 1
    chosen = tuple(sorted((item for values in selected.values() for item in values), key=lambda item: item.sample_id))
    unavailable = tuple(name for name, quota in STRATA if len(selected.get(name, ())) < quota)
    strata = {
        name: {"population": len(by_stratum.get(name, ())), "selected": len(selected.get(name, ())), "target": quota}
        for name, quota in STRATA
    }
    return GoldSetManifest(
        seed=seed,
        target_size=target_size,
        units=chosen,
        strata=strata,
        unavailable_strata=unavailable,
        snapshot_hash=snapshot_hash,
        pipeline_version=pipeline_version,
        extractor_version=extractor_version,
        annotation_status=annotation_status,
        human_gold_status=human_gold_status,
        business_gate_eligible=False,
    )
