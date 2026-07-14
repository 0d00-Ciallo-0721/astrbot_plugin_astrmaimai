from __future__ import annotations

import json
from typing import Any


FACT_LOG_PREFIX = "[事实]"
LEGACY_FACT_LOG_PREFIX = "[fact]"


def normalize_dream_fact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    subject_id = str(value.get("subject_id") or value.get("sender_id") or value.get("session_id") or "").strip()
    entity = str(value.get("entity") or "").strip()
    attribute = str(value.get("attribute") or "").strip()
    fact_value = str(value.get("value") or "").strip()
    if not subject_id or not entity or not attribute or not fact_value:
        return None
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence_score", 0.9))))
    except (TypeError, ValueError):
        confidence = 0.0
    signal = str(value.get("confidence_signal") or "high").strip().lower()
    if signal not in {"low", "medium", "high", "very_high"}:
        signal = "medium"
    evidence = value.get("evidence") or {}
    if not isinstance(evidence, dict):
        evidence = {"text": str(evidence or "")[:200]}
    return {
        "subject_id": subject_id,
        "entity": entity,
        "attribute": attribute,
        "value": fact_value,
        "confidence_score": confidence,
        "confidence_signal": signal,
        "evidence": {
            "turn_id": str(evidence.get("turn_id") or value.get("turn_id") or ""),
            "text": str(evidence.get("text") or fact_value)[:200],
            "memory_id": str(evidence.get("memory_id") or value.get("memory_id") or ""),
        },
    }


def normalize_dream_facts(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    normalized = []
    for value in values:
        fact = normalize_dream_fact(value)
        if fact is not None:
            normalized.append(fact)
    return normalized


def format_dream_fact_log(fact: dict[str, Any]) -> str:
    return f"{FACT_LOG_PREFIX} {json.dumps(fact, ensure_ascii=False, sort_keys=True)}"


def parse_dream_fact_log(line: str) -> dict[str, Any] | None:
    normalized = str(line or "").strip()
    prefix = next(
        (candidate for candidate in (FACT_LOG_PREFIX, LEGACY_FACT_LOG_PREFIX) if normalized.startswith(candidate)),
        "",
    )
    if not prefix:
        return None
    try:
        payload = json.loads(normalized.removeprefix(prefix).strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return normalize_dream_fact(payload)


__all__ = [
    "FACT_LOG_PREFIX",
    "format_dream_fact_log",
    "normalize_dream_fact",
    "normalize_dream_facts",
    "parse_dream_fact_log",
]
