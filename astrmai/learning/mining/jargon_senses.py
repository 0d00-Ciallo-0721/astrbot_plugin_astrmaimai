from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any

from .learning_evidence import merge_evidence_metadata


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def jargon_sense_id(meaning: str, scene: str = "") -> str:
    payload = f"{_normalize(meaning)}|{_normalize(scene)}"
    return f"sense:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _review_status(value: Any) -> str:
    normalized = str(value or "review_pending").strip().lower()
    if normalized in {"approved", "review_pending", "revision_needed", "rejected", "stale"}:
        return normalized
    if normalized in {"pending", "pending_human"}:
        return "review_pending"
    return "review_pending"


def _sense_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_meaning = _normalize(left.get("meaning"))
    right_meaning = _normalize(right.get("meaning"))
    if not left_meaning or not right_meaning:
        return 0.0
    meaning_score = SequenceMatcher(None, left_meaning, right_meaning).ratio()
    if left_meaning in right_meaning or right_meaning in left_meaning:
        meaning_score = max(meaning_score, 0.93)
    left_scene = _normalize(left.get("scene"))
    right_scene = _normalize(right.get("scene"))
    if not left_scene or not right_scene:
        return meaning_score
    scene_score = SequenceMatcher(None, left_scene, right_scene).ratio()
    return meaning_score * 0.85 + scene_score * 0.15


def _sense_payload(payload: dict[str, Any], *, group_id: str) -> dict[str, Any]:
    meaning = str(payload.get("meaning") or "").strip()
    scene = str(payload.get("scene") or "").strip()
    evidence = merge_evidence_metadata({}, payload)
    return {
        "sense_id": jargon_sense_id(meaning, scene),
        "meaning": meaning,
        "scene": scene,
        "review_status": _review_status(payload.get("review_status")),
        "confidence": float(payload.get("confidence") or 0.0),
        "examples": list(dict.fromkeys(str(item).strip() for item in (payload.get("source_examples") or payload.get("examples") or []) if str(item).strip()))[:12],
        "model_examples": list(dict.fromkeys(str(item).strip() for item in (payload.get("model_examples") or []) if str(item).strip()))[:12],
        "source_groups": [str(group_id)] if str(group_id or "").strip() else [],
        **evidence,
    }


def _legacy_sense(metadata: dict[str, Any], *, record_status: str) -> dict[str, Any] | None:
    meaning = str(metadata.get("meaning") or "").strip()
    if not meaning:
        return None
    scene = str(metadata.get("scene") or "").strip()
    review_status = str(metadata.get("review_status") or "").strip().lower()
    if not review_status:
        review_status = "approved" if record_status == "active" else _review_status(record_status)
    return {
        "sense_id": jargon_sense_id(meaning, scene),
        "meaning": meaning,
        "scene": scene,
        "review_status": _review_status(review_status),
        "confidence": float(metadata.get("confidence") or 0.0),
        "examples": list(metadata.get("examples") or [])[:12],
        "model_examples": list(metadata.get("model_examples") or [])[:12],
        "source_groups": list(metadata.get("source_groups") or [])[:64],
        **merge_evidence_metadata({}, metadata),
        "legacy": True,
    }


def merge_jargon_senses(
    existing_metadata: dict[str, Any],
    incoming_payload: dict[str, Any],
    *,
    group_id: str,
    record_status: str,
) -> tuple[list[dict[str, Any]], str, bool, bool]:
    senses = [dict(item) for item in (existing_metadata.get("senses") or []) if isinstance(item, dict)]
    if not senses:
        legacy = _legacy_sense(existing_metadata, record_status=record_status)
        if legacy:
            senses.append(legacy)

    incoming = _sense_payload(incoming_payload, group_id=group_id)
    incoming_id = str(incoming["sense_id"])
    matched_index = next(
        (index for index, sense in enumerate(senses) if str(sense.get("sense_id") or "") == incoming_id),
        None,
    )
    if matched_index is None:
        scored = [(_sense_similarity(sense, incoming), index) for index, sense in enumerate(senses)]
        best_score, best_index = max(scored, default=(0.0, None))
        if best_index is not None and best_score >= 0.9:
            matched_index = best_index
            incoming_id = str(senses[best_index].get("sense_id") or incoming_id)
    revision_reopened = False
    is_new_sense = matched_index is None
    if matched_index is None:
        senses.append(incoming)
    else:
        current = dict(senses[matched_index])
        merged_evidence = merge_evidence_metadata(current, incoming)
        old_status = _review_status(current.get("review_status"))
        old_digest = str(current.get("evidence_digest") or "")
        new_digest = str(incoming.get("evidence_digest") or "")
        revision_reopened = (
            old_status == "rejected"
            and bool(new_digest)
            and new_digest != old_digest
            and int(incoming.get("support_count") or 0) >= max(int(current.get("support_count") or 0) + 1, 2)
        )
        merged_status = "revision_needed" if revision_reopened else (
            old_status if old_status in {"approved", "rejected"} else _review_status(incoming.get("review_status"))
        )
        senses[matched_index] = {
            **current,
            **merged_evidence,
            "meaning": str(current.get("meaning") or incoming.get("meaning") or ""),
            "scene": str(current.get("scene") or incoming.get("scene") or ""),
            "review_status": merged_status,
            "confidence": max(float(current.get("confidence") or 0.0), float(incoming.get("confidence") or 0.0)),
            "examples": list(dict.fromkeys([*(current.get("examples") or []), *(incoming.get("examples") or [])]))[:12],
            "model_examples": list(dict.fromkeys([*(current.get("model_examples") or []), *(incoming.get("model_examples") or [])]))[:12],
            "source_groups": list(dict.fromkeys([*(current.get("source_groups") or []), *(incoming.get("source_groups") or [])]))[:64],
        }

    senses.sort(
        key=lambda item: (
            str(item.get("review_status") or "") == "approved",
            float(item.get("confidence") or 0.0),
            int(item.get("support_count") or 0),
        ),
        reverse=True,
    )
    return senses[:12], incoming_id, is_new_sense, revision_reopened


def select_jargon_senses(metadata: dict[str, Any], query_text: str, *, limit: int = 2) -> list[dict[str, Any]]:
    approved = [
        dict(item)
        for item in (metadata.get("senses") or [])
        if isinstance(item, dict) and _review_status(item.get("review_status")) == "approved"
    ]
    if not approved:
        legacy = _legacy_sense(metadata, record_status="active")
        return [legacy] if legacy else []
    query_terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", _normalize(query_text)))

    def score(sense: dict[str, Any]) -> tuple[float, float, int]:
        haystack = _normalize(" ".join((
            str(sense.get("meaning") or ""),
            str(sense.get("scene") or ""),
            " ".join(str(item) for item in (sense.get("examples") or [])),
        )))
        overlap = sum(1.0 for term in query_terms if term and term in haystack)
        return overlap, float(sense.get("confidence") or 0.0), int(sense.get("support_count") or 0)

    approved.sort(key=score, reverse=True)
    return approved[:max(int(limit or 1), 1)]


__all__ = ["jargon_sense_id", "merge_jargon_senses", "select_jargon_senses"]
