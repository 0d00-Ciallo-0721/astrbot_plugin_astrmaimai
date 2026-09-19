from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, Mapping


_PLATFORM_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_GROUP_KINDS = {"group", "groupmessage"}
_PRIVATE_KINDS = {"private", "friend", "friendmessage"}
_GENERATED_TYPES = {"bot_echo", "plugin_output", "synthetic", "replay"}
_NON_SUPPORT_TYPES = {
    "quoted",
    "forwarded",
    "bot_echo",
    "plugin_output",
    "retracted",
    "synthetic",
    "replay",
    "unknown",
}


def _value(message: Any, name: str, default: Any = "") -> Any:
    if isinstance(message, Mapping):
        return message.get(name, default)
    return getattr(message, name, default)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _json_string_list(value: Any, *, field_name: str) -> tuple[tuple[str, ...], str]:
    if value in (None, "", (), []):
        return (), ""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return (), f"invalid_{field_name}"
    if not isinstance(parsed, (list, tuple)):
        return (), f"invalid_{field_name}"
    result: list[str] = []
    for item in parsed:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result), ""


def _canonical_scope(value: Any, explicit_kind: Any) -> tuple[str | None, str | None, str | None]:
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) != 3 or not all(part.strip() for part in parts):
        return None, None, None
    platform, raw_kind, chat_id = (part.strip() for part in parts)
    if not _PLATFORM_RE.fullmatch(platform):
        return None, None, None
    normalized_kind = raw_kind.lower()
    if normalized_kind in _GROUP_KINDS:
        chat_kind = "group"
    elif normalized_kind in _PRIVATE_KINDS:
        chat_kind = "private"
    else:
        return None, None, None
    requested_kind = str(explicit_kind or "").strip().lower()
    if requested_kind:
        requested_kind = (
            "group"
            if requested_kind in _GROUP_KINDS
            else "private"
            if requested_kind in _PRIVATE_KINDS
            else ""
        )
        if not requested_kind or requested_kind != chat_kind:
            return None, None, None
    return platform, chat_kind, chat_id


def _source_type(message: Any) -> str:
    provenance = str(_value(message, "provenance", "") or "").strip().lower()
    role = str(_value(message, "role", "") or "").strip().lower()
    if bool(_value(message, "recalled", False)):
        return "retracted"
    if provenance in {"external_plugin", "plugin_output"}:
        return "plugin_output"
    if bool(_value(message, "is_bot", False)) or role in {
        "assistant",
        "bot",
        "system",
        "tool",
    } or provenance in {"bot_echo", "proactive"}:
        return "bot_echo"
    if provenance in {"forward", "forwarded"}:
        return "forwarded"
    if provenance == "synthetic":
        return "synthetic"
    if provenance == "replay":
        return "replay"
    if provenance in {"quoted", "quote"}:
        return "quoted"
    if provenance in {"", "original", "legacy"}:
        return "user_said"
    return "unknown"


@dataclass(frozen=True, slots=True)
class LearningAttribution:
    source_row_id: int | None
    source_message_id: str
    event_id: str
    identity_source: str
    platform_message_id: str
    platform_id: str | None
    chat_kind: str | None
    chat_id: str | None
    scope_id: str | None
    speaker_id: str | None
    speaker_scope_id: str | None
    pairwise_id: str | None
    topic_epoch: int | None
    source_type: str
    evidence_quality: str
    attribution_confidence: float | None
    relation_payload: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    is_generated: bool = False
    evidence_eligible: bool = False
    eligible_for_group_shadow: bool = False
    eligible_for_speaker_stats: bool = False
    unknown_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "relation_payload",
            MappingProxyType({
                key: tuple(value) if isinstance(value, (list, tuple)) else value
                for key, value in self.relation_payload.items()
            }),
        )
        object.__setattr__(self, "unknown_reasons", tuple(self.unknown_reasons))

    def to_dict(self) -> dict[str, Any]:
        result = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in {"relation_payload", "unknown_reasons"}
        }
        result["relation_payload"] = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in self.relation_payload.items()
        }
        result["unknown_reasons"] = list(self.unknown_reasons)
        return result


class LearningAttributionAdapter:
    """Build a fail-closed attribution view from one durable MessageLog-like row."""

    def attribute(self, message: Any) -> LearningAttribution:
        unknown: list[str] = []
        source_row_id = _positive_int(_value(message, "id", None))
        event_id = str(_value(message, "event_id", "") or "").strip()
        platform_message_id = str(
            _value(message, "platform_message_id", "") or ""
        ).strip()
        source_message_id = event_id or platform_message_id
        if event_id.lower().startswith(("fallback_", "evt_")):
            identity_source = "fallback_hash"
        elif event_id:
            identity_source = "event_id"
        elif platform_message_id:
            identity_source = "platform_message_id"
        elif source_row_id is not None:
            identity_source = "messagelog.id"
        else:
            identity_source = "unknown"
            unknown.append("message_identity_missing")

        platform_id, chat_kind, chat_id = _canonical_scope(
            _value(message, "group_id", ""),
            _value(message, "chat_kind", ""),
        )
        scope_id = (
            f"{platform_id}:{chat_kind}:{chat_id}"
            if platform_id and chat_kind and chat_id
            else None
        )
        if scope_id is None:
            unknown.append("scope_invalid")

        speaker_id = str(_value(message, "sender_id", "") or "").strip() or None
        if speaker_id is None:
            unknown.append("speaker_missing")
        speaker_scope_id = (
            f"{scope_id}:{speaker_id}" if scope_id and speaker_id else None
        )

        reply_target_actor_id = str(
            _value(message, "reply_target_actor_id", "") or ""
        ).strip()
        target_speaker_scope_id = (
            f"{scope_id}:{reply_target_actor_id}"
            if scope_id and reply_target_actor_id
            else None
        )
        pairwise_id = (
            f"pair:{speaker_scope_id}:{target_speaker_scope_id}"
            if speaker_scope_id and target_speaker_scope_id
            else None
        )
        at_actor_ids, at_error = _json_string_list(
            _value(message, "at_actor_ids", ()), field_name="at_actor_ids"
        )
        source_event_ids, source_error = _json_string_list(
            _value(message, "source_event_ids", ()), field_name="source_event_ids"
        )
        image_refs, image_error = _json_string_list(
            _value(message, "image_refs", ()), field_name="image_refs"
        )
        unknown.extend(
            item for item in (at_error, source_error, image_error) if item
        )
        topic_epoch = _positive_int(_value(message, "topic_epoch", None))
        source_type = _source_type(message)
        if source_type == "unknown":
            unknown.append("source_type_unknown")
        content = str(_value(message, "content", "") or "").strip()
        if not re.search(r"[A-Za-z0-9\u4e00-\u9fff]", content):
            unknown.append("text_missing")

        schema_version = _positive_int(
            _value(message, "event_schema_version", None)
        )
        authoritative_identity = identity_source in {
            "event_id",
            "platform_message_id",
        }
        relation_signal = bool(
            reply_target_actor_id
            or str(_value(message, "reply_target_event_id", "") or "").strip()
            or str(_value(message, "quote_event_id", "") or "").strip()
            or at_actor_ids
            or topic_epoch
        )
        core_complete = bool(scope_id and speaker_id and content)
        if unknown:
            evidence_quality = "unknown"
        elif not schema_version or identity_source == "fallback_hash":
            evidence_quality = "low"
        elif authoritative_identity and core_complete and relation_signal:
            evidence_quality = "high"
        elif authoritative_identity and core_complete:
            evidence_quality = "medium"
        else:
            evidence_quality = "unknown"

        is_generated = source_type in _GENERATED_TYPES
        source_allowed = source_type not in _NON_SUPPORT_TYPES
        policy_eligible = bool(
            _value(message, "learning_evidence_eligible", True)
        )
        eligible_for_group_shadow = bool(
            source_allowed
            and policy_eligible
            and content
            and scope_id
            and chat_kind == "group"
            and evidence_quality in {"high", "medium", "low"}
        )
        eligible_for_speaker_stats = bool(
            eligible_for_group_shadow
            and speaker_scope_id
            and evidence_quality in {"high", "medium"}
        )
        evidence_eligible = eligible_for_group_shadow
        relation_payload = {
            "reply_target_event_id": str(
                _value(message, "reply_target_event_id", "") or ""
            ).strip(),
            "reply_target_actor_id": reply_target_actor_id,
            "quote_event_id": str(
                _value(message, "quote_event_id", "") or ""
            ).strip(),
            "at_actor_ids": list(at_actor_ids),
            "causal_parent_event_id": str(
                _value(message, "causal_parent_event_id", "") or ""
            ).strip(),
            "source_event_ids": list(source_event_ids),
            "image_ref_count": len(image_refs),
        }
        return LearningAttribution(
            source_row_id=source_row_id,
            source_message_id=source_message_id,
            event_id=event_id,
            identity_source=identity_source,
            platform_message_id=platform_message_id,
            platform_id=platform_id,
            chat_kind=chat_kind,
            chat_id=chat_id,
            scope_id=scope_id,
            speaker_id=speaker_id,
            speaker_scope_id=speaker_scope_id,
            pairwise_id=pairwise_id,
            topic_epoch=topic_epoch,
            source_type=source_type,
            evidence_quality=evidence_quality,
            attribution_confidence=None,
            relation_payload=relation_payload,
            is_generated=is_generated,
            evidence_eligible=evidence_eligible,
            eligible_for_group_shadow=eligible_for_group_shadow,
            eligible_for_speaker_stats=eligible_for_speaker_stats,
            unknown_reasons=tuple(dict.fromkeys(unknown)),
        )


__all__ = ["LearningAttribution", "LearningAttributionAdapter"]
