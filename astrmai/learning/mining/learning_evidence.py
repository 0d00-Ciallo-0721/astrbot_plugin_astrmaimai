from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .learning_attribution import LearningAttributionAdapter


EVIDENCE_VERSION = 3


def message_evidence_id(message: Any, *, fallback_index: int) -> str:
    for field in ("event_id", "platform_message_id", "id"):
        value = str(_value(message, field, "") or "").strip()
        if value:
            return value
    payload = "|".join(
        (
            str(_value(message, "group_id", "") or ""),
            str(_value(message, "sender_id", "") or ""),
            str(_value(message, "timestamp", "") or ""),
            str(_value(message, "content", "") or ""),
            str(fallback_index),
        )
    )
    return f"synthetic:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _clean_text(value: Any, *, limit: int = 240) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _value(message: Any, name: str, default: Any = "") -> Any:
    if isinstance(message, dict):
        return message.get(name, default)
    return getattr(message, name, default)


def _context_text(message: Any) -> str:
    return _clean_text(
        _value(message, "learning_context_content", "")
        or _value(message, "content", "")
        or "[图片]"
    )


def _source_kind(message: Any) -> str:
    return str(
        _value(message, "learning_source_kind", "")
        or _value(message, "message_kind", "")
        or "human_text"
    ).strip()


def _message_flags(message: Any) -> dict[str, bool]:
    return {
        "is_bot": bool(_value(message, "is_bot", False)),
        "is_echo": str(_value(message, "provenance", "") or "").strip().lower() == "bot_echo",
        "is_plugin_output": str(_value(message, "provenance", "") or "").strip().lower() == "external_plugin",
        "is_recalled": bool(_value(message, "recalled", False)),
        "evidence_eligible": bool(_value(message, "learning_evidence_eligible", True)),
    }


def _timestamp(message: Any) -> float:
    try:
        return float(_value(message, "timestamp", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ordered_unique(values: Iterable[Any], *, limit: int = 24) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def build_evidence_bundle(
    *,
    group_id: str,
    messages: list[Any],
    matched_indexes: Iterable[int],
    source_examples: Iterable[str] = (),
    source_spans: Iterable[dict[str, Any]] = (),
    context_radius: int = 2,
    attribution_enabled: bool = False,
) -> dict[str, Any]:
    matched = sorted(
        {int(index) for index in matched_indexes if 0 <= int(index) < len(messages)}
    )
    adapter = LearningAttributionAdapter() if attribution_enabled else None
    indexes = [
        index
        for index in matched
        if bool(_value(messages[index], "learning_evidence_eligible", True))
        and (
            not attribution_enabled
            or bool(adapter.attribute(messages[index]).evidence_eligible)
        )
    ]
    source_attributions: list[dict[str, Any]] = []
    attribution_by_index: dict[int, dict[str, Any]] = {}
    if attribution_enabled:
        attribution_by_index = {
            index: adapter.attribute(messages[index]).to_dict()
            for index in matched
        }
        source_attributions = [attribution_by_index[index] for index in matched]
        eligible_attributions = [
            item for item in source_attributions if bool(item["evidence_eligible"])
        ]
        source_message_ids = _ordered_unique(
            item["source_message_id"] for item in eligible_attributions
        )
        source_row_ids = sorted(
            {
                int(item["source_row_id"])
                for item in eligible_attributions
                if item["source_row_id"] is not None
            }
        )
        contributor_ids = _ordered_unique(
            item["speaker_id"] for item in eligible_attributions
        )
    else:
        source_message_ids = [
            message_evidence_id(messages[index], fallback_index=index)
            for index in indexes
        ]
        source_row_ids = []
        contributor_ids = _ordered_unique(
            _value(messages[index], "sender_id", "")
            or _value(messages[index], "sender_name", "")
            for index in indexes
        )
    context_windows: list[dict[str, Any]] = []
    reply_relations: list[dict[str, str]] = []
    for index in matched:
        start = max(0, index - max(int(context_radius or 0), 0))
        end = min(len(messages), index + max(int(context_radius or 0), 0) + 1)
        window_messages = []
        for offset in range(start, end):
            message = messages[offset]
            window_messages.append(
                {
                    "message_id": message_evidence_id(message, fallback_index=offset),
                    "actor": str(_value(message, "sender_name", "") or _value(message, "sender_id", "") or "群友"),
                    "timestamp": _timestamp(message),
                    "content": _context_text(message),
                    "source_kind": _source_kind(message),
                    "flags": _message_flags(message),
                    "is_evidence": (
                        offset == index
                        and bool(_value(message, "learning_evidence_eligible", True))
                        and (
                            not attribution_enabled
                                or bool(adapter.attribute(message).evidence_eligible)
                        )
                    ),
                }
            )
        attribution = attribution_by_index.get(index) if attribution_enabled else None
        context_windows.append(
            {
                "evidence_message_id": message_evidence_id(messages[index], fallback_index=index),
                "messages": window_messages,
                **(
                    {"source_attribution": attribution}
                    if attribution is not None
                    else {}
                ),
            }
        )
        message = messages[index]
        target_event_id = str(
            _value(message, "reply_target_event_id", "")
            or _value(message, "quote_event_id", "")
            or _value(message, "causal_parent_event_id", "")
            or ""
        ).strip()
        if target_event_id and index in indexes:
            reply_relations.append(
                {
                    "source_message_id": message_evidence_id(message, fallback_index=index),
                    "target_message_id": target_event_id,
                }
            )

    real_examples = _ordered_unique(
        (
            _value(messages[index], "content", "")
            for index in indexes
        )
        if len(indexes) != len(matched)
        else source_examples,
        limit=12,
    )
    eligible_message_ids = {
        message_evidence_id(messages[index], fallback_index=index)
        for index in indexes
    }
    normalized_spans: list[dict[str, Any]] = []
    for span in source_spans:
        if not isinstance(span, dict):
            continue
        message_id = str(span.get("message_id") or "").strip()
        text = str(span.get("text") or "").strip()
        try:
            start = max(0, int(span.get("start", 0) or 0))
            end = max(start, int(span.get("end", start + len(text)) or start + len(text)))
        except (TypeError, ValueError):
            continue
        if (
            not message_id
            or not text
            or (len(indexes) != len(matched) and message_id not in eligible_message_ids)
        ):
            continue
        normalized = {
            "message_id": message_id,
            "start": start,
            "end": end,
            "text": text[:60],
        }
        if normalized not in normalized_spans:
            normalized_spans.append(normalized)
        if len(normalized_spans) >= 48:
            break
    digest_payload = {
        "group_id": str(group_id or ""),
        "source_message_ids": sorted(source_message_ids),
        "source_row_ids": source_row_ids,
        "source_examples": real_examples,
        "reply_relations": reply_relations,
        "source_spans": normalized_spans,
        "source_attributions": sorted(
            source_attributions,
            key=lambda item: (
                int(item["source_row_id"] or 0),
                str(item["source_message_id"] or ""),
            ),
        ),
    }
    evidence_digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    support_identities = {
        (
            f"row:{item['source_row_id']}"
            if item["source_row_id"] is not None
            else f"message:{item['source_message_id']}"
        )
        for item in source_attributions
        if item["evidence_eligible"]
        and (item["source_row_id"] is not None or item["source_message_id"])
    } if attribution_enabled else set(source_message_ids)
    return {
        "evidence_version": EVIDENCE_VERSION,
        "source_examples": real_examples,
        "source_message_ids": _ordered_unique(source_message_ids, limit=48),
        "source_row_ids": source_row_ids,
        "source_group_ids": [str(group_id)] if str(group_id or "").strip() else [],
        "source_attributions": source_attributions,
        "source_types": sorted(
            {str(item["source_type"]) for item in source_attributions}
        ),
        "evidence_qualities": sorted(
            {str(item["evidence_quality"]) for item in source_attributions}
        ),
        "attribution_unknown_reasons": sorted(
            {
                str(reason)
                for item in source_attributions
                for reason in item["unknown_reasons"]
            }
        ),
        "context_windows": context_windows[:12],
        "reply_relations": reply_relations[:12],
        "source_spans": normalized_spans,
        "support_count": len(support_identities),
        "contradiction_count": 0,
        "contributor_count": len(contributor_ids),
        "model_examples": [],
        "evidence_digest": evidence_digest,
    }


def merge_evidence_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing or {})
    result["evidence_version"] = max(
        int(result.get("evidence_version") or 1),
        int(incoming.get("evidence_version") or EVIDENCE_VERSION),
    )
    for key, limit in (
        ("source_examples", 12),
        ("source_message_ids", 48),
        ("source_group_ids", 64),
        ("model_examples", 12),
        ("source_types", 16),
        ("evidence_qualities", 4),
        ("attribution_unknown_reasons", 32),
        ("attribution_scope_ids", 64),
        ("attribution_speaker_ids", 64),
        ("attribution_speaker_scope_ids", 64),
    ):
        if key in {
            "source_types", "evidence_qualities", "attribution_unknown_reasons",
            "attribution_scope_ids", "attribution_speaker_ids",
            "attribution_speaker_scope_ids",
        } and key not in result and key not in incoming:
            continue
        result[key] = _ordered_unique(
            [*(result.get(key) or []), *(incoming.get(key) or [])],
            limit=limit,
        )
    if "source_row_ids" in result or "source_row_ids" in incoming:
        result["source_row_ids"] = sorted(
            {
                int(item)
                for item in [
                    *(result.get("source_row_ids") or []),
                    *(incoming.get("source_row_ids") or []),
                ]
                if isinstance(item, int) or str(item or "").isdigit()
            }
        )[:48]
    for key, limit in (
        ("context_windows", 12),
        ("reply_relations", 24),
        ("source_spans", 48),
        ("source_attributions", 48),
    ):
        if key == "source_attributions" and key not in result and key not in incoming:
            continue
        merged: list[Any] = []
        seen: set[str] = set()
        for item in [*(result.get(key) or []), *(incoming.get(key) or [])]:
            marker = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(item)
            if len(merged) >= limit:
                break
        result[key] = merged
    attributions = [
        item
        for item in (result.get("source_attributions") or [])
        if isinstance(item, dict) and bool(item.get("evidence_eligible"))
    ]
    if "source_attributions" in result:
        result["support_count"] = len(
            {
                (
                    f"row:{item['source_row_id']}"
                    if item.get("source_row_id") is not None
                    else f"message:{item.get('source_message_id')}"
                )
                for item in attributions
                if item.get("source_row_id") is not None or item.get("source_message_id")
            }
        )
    else:
        result["support_count"] = len(set(result.get("source_message_ids") or []))
    result["contradiction_count"] = max(
        int(result.get("contradiction_count") or 0),
        int(incoming.get("contradiction_count") or 0),
    )
    result["contributor_count"] = max(
        int(result.get("contributor_count") or 0),
        int(incoming.get("contributor_count") or 0),
    )
    result["evidence_digest"] = str(incoming.get("evidence_digest") or result.get("evidence_digest") or "")
    if "personal_attribution_eligible" in result or "personal_attribution_eligible" in incoming:
        result["personal_attribution_eligible"] = bool(
            incoming.get(
                "personal_attribution_eligible",
                result.get("personal_attribution_eligible", False),
            )
        )
    return result


__all__ = [
    "EVIDENCE_VERSION",
    "build_evidence_bundle",
    "merge_evidence_metadata",
    "message_evidence_id",
]
