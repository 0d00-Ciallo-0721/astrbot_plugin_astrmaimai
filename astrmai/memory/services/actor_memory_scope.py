from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..contracts.memory_query import MemoryCandidate, MemoryQuery


_ACTOR_METADATA_KEYS = (
    "subject_id",
    "actor_id",
    "user_id",
    "sender_id",
    "promotion_subject_id",
    "target_actor_id",
)
_SENSITIVE_KINDS = {
    "affection",
    "emotion",
    "identity",
    "profile",
    "preference",
    "relation",
    "relationship",
    "social_stance",
    "stance",
    "user_profile",
}
_GLOBAL_SAFE_KINDS = {
    "expression_pattern",
    "jargon",
    "persona_lore",
    "world_view",
}
_SENSITIVE_METADATA_TOKENS = {
    "address",
    "affection",
    "exclusive",
    "hostility",
    "identity",
    "kinship",
    "nickname",
    "profile",
    "relation",
    "relationship",
    "romantic",
    "stance",
}
_EXCLUSIVE_RELATION_RE = re.compile(
    r"(唯一|专属|恋人|老婆|老公|妻子|丈夫|哥哥|妹妹|欧尼酱|亲属|情侣|敌人|仇人|讨厌|憎恨)"
)


def _ordered_unique(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _metadata_values(metadata: Mapping[str, Any], key: str) -> list[str]:
    value = metadata.get(key)
    if isinstance(value, (list, tuple, set)):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    normalized = str(value or "").strip()
    return [normalized] if normalized else []


@dataclass(frozen=True, slots=True)
class ActorMemoryScope:
    is_group: bool = False
    group_id: str = ""
    current_actor_id: str = ""
    allowed_actor_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_sources: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_group": self.is_group,
            "group_id": self.group_id,
            "current_actor_id": self.current_actor_id,
            "allowed_actor_ids": list(self.allowed_actor_ids),
            "evidence_sources": list(self.evidence_sources),
        }

    @classmethod
    def from_value(cls, value: Any) -> "ActorMemoryScope":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return cls()
        return cls(
            is_group=bool(value.get("is_group", False)),
            group_id=str(value.get("group_id", "") or "").strip(),
            current_actor_id=str(value.get("current_actor_id", "") or "").strip(),
            allowed_actor_ids=_ordered_unique(value.get("allowed_actor_ids", ()) or ()),
            evidence_sources=_ordered_unique(value.get("evidence_sources", ()) or ()),
        )


def build_actor_memory_scope(event: Any) -> ActorMemoryScope:
    group_id = ""
    get_group_id = getattr(event, "get_group_id", None)
    if callable(get_group_id):
        try:
            group_id = str(get_group_id() or "").strip()
        except Exception:
            group_id = ""
    if not group_id:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        if ":GroupMessage:" in origin:
            group_id = origin.rsplit(":", 1)[-1].strip()
    if not group_id:
        return ActorMemoryScope()

    current_actor_id = ""
    get_sender_id = getattr(event, "get_sender_id", None)
    if callable(get_sender_id):
        try:
            current_actor_id = str(get_sender_id() or "").strip()
        except Exception:
            current_actor_id = ""

    actor_ids: list[str] = [current_actor_id]
    evidence_sources: list[str] = ["current_actor"] if current_actor_id else []
    get_extra = getattr(event, "get_extra", None)
    turn_context = get_extra("astrmai_turn_context", None) if callable(get_extra) else None
    attention = getattr(turn_context, "attention", None)
    actor_set = getattr(attention, "actor_set", None)
    if actor_set is not None:
        for attribute, source in (
            ("explicit_target_actor_ids", "turn_target"),
            ("at_actor_ids", "at"),
            ("quoted_actor_ids", "quote"),
            ("recent_topic_actor_ids", "topic_evidence"),
        ):
            values = getattr(actor_set, attribute, ())
            normalized = [str(item or "").strip() for item in values or ()]
            if any(normalized):
                actor_ids.extend(normalized)
                evidence_sources.append(source)
        actor_current = str(getattr(actor_set, "current_actor_id", "") or "").strip()
        if actor_current:
            actor_ids.append(actor_current)

    turn_target = getattr(attention, "turn_target", None)
    target_actor_id = str(getattr(turn_target, "target_actor_id", "") or "").strip()
    if target_actor_id:
        actor_ids.append(target_actor_id)
        evidence_sources.append("turn_target")

    referenced_entities = (
        get_extra("astrmai_referenced_entities", []) if callable(get_extra) else []
    )
    for entity in referenced_entities or []:
        resolved_id = str(
            getattr(entity, "resolved_id", "")
            or (entity.get("resolved_id", "") if isinstance(entity, Mapping) else "")
            or ""
        ).strip()
        ambiguous = bool(
            getattr(entity, "ambiguous", False)
            or (entity.get("ambiguous", False) if isinstance(entity, Mapping) else False)
        )
        if resolved_id and not ambiguous:
            actor_ids.append(resolved_id)
            evidence_sources.append("resolved_reference")

    return ActorMemoryScope(
        is_group=True,
        group_id=group_id,
        current_actor_id=current_actor_id,
        allowed_actor_ids=_ordered_unique(actor_ids),
        evidence_sources=_ordered_unique(evidence_sources),
    )


def _candidate_actor_ids(candidate: MemoryCandidate) -> tuple[str, ...]:
    metadata = dict(candidate.metadata or {})
    values: list[str] = [str(candidate.sender_id or "").strip()]
    for key in _ACTOR_METADATA_KEYS:
        values.extend(_metadata_values(metadata, key))
    return _ordered_unique(values)


def _is_sensitive(candidate: MemoryCandidate) -> bool:
    kind = str(candidate.kind or "").strip().lower()
    if kind in _SENSITIVE_KINDS:
        return True
    metadata = dict(candidate.metadata or {})
    for key in ("attribute", "entity", "category", "memory_type", "relation_type"):
        value = str(metadata.get(key, "") or "").strip().lower()
        if any(token in value for token in _SENSITIVE_METADATA_TOKENS):
            return True
    return False


def _is_group_shared(candidate: MemoryCandidate) -> bool:
    metadata = dict(candidate.metadata or {})
    declared_scope = str(
        metadata.get("scope")
        or metadata.get("memory_scope")
        or metadata.get("visibility_scope")
        or ""
    ).strip().lower()
    if declared_scope in {"group", "group_shared", "public_group"}:
        return True
    return len(set(_metadata_values(metadata, "speaker_ids"))) >= 2


def _contains_exclusive_relation(candidate: MemoryCandidate) -> bool:
    if _is_sensitive(candidate):
        return True
    text = " ".join(
        (
            str(candidate.summary or ""),
            str(candidate.content or ""),
            str((candidate.metadata or {}).get("attribute", "") or ""),
            str((candidate.metadata or {}).get("value", "") or ""),
        )
    )
    return bool(_EXCLUSIVE_RELATION_RE.search(text))


def filter_candidates_for_actor_scope(
    query: MemoryQuery,
    candidates: list[MemoryCandidate],
) -> list[MemoryCandidate]:
    scope = ActorMemoryScope.from_value((query.metadata or {}).get("actor_memory_scope"))
    if not scope.is_group:
        return list(candidates)

    allowed = set(scope.allowed_actor_ids)
    kept: list[MemoryCandidate] = []
    suppressed_reasons: dict[str, str] = {}
    group_shared_count = 0
    for candidate in candidates:
        candidate_id = str(candidate.id or "")
        kind = str(candidate.kind or "").strip().lower()
        actor_ids = set(_candidate_actor_ids(candidate))
        actor_ids.discard(scope.group_id)
        is_shared = _is_group_shared(candidate)

        if kind in _GLOBAL_SAFE_KINDS:
            kept.append(candidate)
            continue
        if is_shared:
            if _contains_exclusive_relation(candidate):
                suppressed_reasons[candidate_id] = "group_shared_sensitive_memory"
                continue
            group_shared_count += 1
            kept.append(candidate)
            continue
        if actor_ids:
            if actor_ids & allowed:
                kept.append(candidate)
            else:
                suppressed_reasons[candidate_id] = "actor_not_allowed"
            continue
        if _is_sensitive(candidate):
            suppressed_reasons[candidate_id] = "actorless_sensitive_memory"
            continue
        kept.append(candidate)

    trace = query.metadata.setdefault("_trace", {})
    trace["actor_scope_filter"] = {
        "is_group": True,
        "group_id": scope.group_id,
        "current_actor_id": scope.current_actor_id,
        "allowed_actor_ids": list(scope.allowed_actor_ids),
        "evidence_sources": list(scope.evidence_sources),
        "before_count": len(candidates),
        "after_count": len(kept),
        "suppressed_count": len(suppressed_reasons),
        "suppressed_ids": list(suppressed_reasons),
        "suppressed_reasons": suppressed_reasons,
        "group_shared_count": group_shared_count,
    }
    return kept


__all__ = [
    "ActorMemoryScope",
    "build_actor_memory_scope",
    "filter_candidates_for_actor_scope",
]
