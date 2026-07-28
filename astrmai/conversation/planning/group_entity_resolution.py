from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_NAME_REJECTS = {
    "机器人",
    "群友",
    "大家",
    "有人",
    "自己",
    "你们",
    "我们",
    "欧尼酱",
    "欧尼",
    "哥哥",
}
_NAME_CLEAN_RE = re.compile(r"[\s\u200b\u200c\u200d]+")
_PUNCTUATION_RE = re.compile(r"""[\s,，。.!！?？:：;；"'“”‘’()（）\[\]【】<>《》/@#~～、\\|]+""")
_HONORIFIC_ALIAS_RE = re.compile(
    r"^([A-Za-z0-9_\-\u4e00-\u9fff·]{1,12}?(?:酱|哥哥|姐姐|哥|姐|老师|同学|君|桑|宝|爷|总))"
)


@dataclass(frozen=True)
class EntityEvidence:
    user_id: str
    display_name: str
    source: str
    timestamp: float = 0.0


@dataclass(frozen=True)
class ReferencedEntity:
    mention_text: str
    group_id: str
    candidate_ids: tuple[str, ...] = ()
    candidate_names: tuple[str, ...] = ()
    source: str = ""
    confidence: str = "unresolved"
    ambiguous: bool = False

    @property
    def resolved_id(self) -> str:
        if self.ambiguous or len(self.candidate_ids) != 1:
            return ""
        return self.candidate_ids[0]

    def as_dict(self) -> dict[str, Any]:
        return {
            "mention_text": self.mention_text,
            "group_id": self.group_id,
            "candidate_ids": list(self.candidate_ids),
            "candidate_names": list(self.candidate_names),
            "source": self.source,
            "confidence": self.confidence,
            "ambiguous": self.ambiguous,
            "resolved_id": self.resolved_id,
        }


def _normalize(value: Any) -> str:
    return _NAME_CLEAN_RE.sub("", str(value or "")).strip()


def _clean_candidate_name(value: Any) -> str:
    name = _normalize(value)
    name = _PUNCTUATION_RE.sub("", name)
    return name[:80]


def _event_extra(event: Any, key: str, default: Any = None) -> Any:
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def event_group_id(event: Any) -> str:
    try:
        group_id = str(event.get_group_id() or "").strip()
    except Exception:
        group_id = ""
    if group_id:
        return group_id
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    match = re.search(r"(?:^|:)GroupMessage:(\d+)", origin)
    return match.group(1) if match else ""


def _event_identity(event: Any, *, source: str) -> EntityEvidence | None:
    if event is None:
        return None
    try:
        user_id = str(event.get_sender_id() or "").strip()
    except Exception:
        user_id = ""
    try:
        display_name = str(event.get_sender_name() or "").strip()
    except Exception:
        display_name = ""
    display_name = _clean_candidate_name(display_name)
    if not user_id or not display_name:
        return None
    timestamp = float(_event_extra(event, "astrmai_timestamp", getattr(event, "timestamp", 0.0)) or 0.0)
    return EntityEvidence(user_id=user_id, display_name=display_name, source=source, timestamp=timestamp)


def collect_event_identity_evidence(
    events: Iterable[Any],
    *,
    group_id: str = "",
) -> list[EntityEvidence]:
    """Collect sender identities, optionally restricted to one group.

    The planner may provide a mixed focus/thread event collection during
    recovery or compaction.  When resolving a group mention, an event without
    a verifiable group origin is unsafe to use because it could be private or
    from another group, so it is excluded when ``group_id`` is supplied.
    """
    evidence: list[EntityEvidence] = []
    seen: set[tuple[str, str]] = set()
    scoped_group_id = str(group_id or "").strip()
    for event in events:
        if scoped_group_id:
            candidate_group_id = event_group_id(event)
            if not candidate_group_id or candidate_group_id != scoped_group_id:
                continue
        item = _event_identity(event, source="recent_event")
        if item is None:
            continue
        key = (item.user_id, _normalize(item.display_name))
        if key in seen:
            continue
        seen.add(key)
        evidence.append(item)
    return evidence


def _message_text(event: Any) -> str:
    rich_text = _event_extra(event, "astrmai_rich_text", "")
    if str(rich_text or "").strip():
        return str(rich_text)
    return str(getattr(event, "message_str", "") or "")


def _candidate_names(evidence: Iterable[EntityEvidence], *, excluded_names: set[str]) -> list[str]:
    names = {
        _normalize(item.display_name)
        for item in evidence
        if _normalize(item.display_name)
        and len(_normalize(item.display_name)) >= 2
        and _normalize(item.display_name) not in _NAME_REJECTS
        and _normalize(item.display_name) not in excluded_names
    }
    return sorted(names, key=lambda value: (-len(value), value))


def _expand_alias_evidence(evidence: Iterable[EntityEvidence]) -> list[EntityEvidence]:
    expanded: list[EntityEvidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        normalized_name = _normalize(item.display_name)
        if not normalized_name:
            continue
        candidates = [(normalized_name, item.source)]
        match = _HONORIFIC_ALIAS_RE.match(normalized_name)
        if match:
            alias = _normalize(match.group(1))
            if alias and alias != normalized_name and alias not in _NAME_REJECTS:
                candidates.append((alias, f"{item.source}_alias"))
        for display_name, source in candidates:
            key = (item.user_id, display_name, source)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(
                EntityEvidence(
                    user_id=item.user_id,
                    display_name=display_name,
                    source=source,
                    timestamp=item.timestamp,
                )
            )
    return expanded


def _build_evidence_map(evidence: Iterable[EntityEvidence]) -> dict[str, list[EntityEvidence]]:
    result: dict[str, list[EntityEvidence]] = {}
    for item in evidence:
        name = _normalize(item.display_name)
        if not name:
            continue
        bucket = result.setdefault(name, [])
        if not any(existing.user_id == item.user_id for existing in bucket):
            bucket.append(item)
    return result


def build_referenced_entities(
    text: str,
    *,
    group_id: str,
    current_sender_id: str = "",
    current_sender_name: str = "",
    evidence: Iterable[EntityEvidence] = (),
) -> list[ReferencedEntity]:
    """Resolve third-party names using only evidence scoped to one group.

    A name is never treated as a user identity merely because the model saw it
    in an old reply. It must be present in a recent event or persisted group
    message log. Multiple QQ IDs remain ambiguous instead of being guessed.
    """
    normalized_text = str(text or "").strip()
    if not normalized_text or not group_id:
        return []
    current_id = str(current_sender_id or "").strip()
    excluded_names = {_normalize(current_sender_name)} if current_sender_name else set()
    all_evidence = _expand_alias_evidence(
        item
        for item in evidence
        if item
        and item.user_id
        and item.display_name
        and (not current_id or str(item.user_id).strip() != current_id)
    )
    by_name = _build_evidence_map(all_evidence)
    entities: list[ReferencedEntity] = []
    for name in _candidate_names(all_evidence, excluded_names=excluded_names):
        if name not in normalized_text:
            continue
        candidates = by_name.get(name, [])
        candidate_ids = tuple(sorted({item.user_id for item in candidates}))
        candidate_names = tuple(sorted({item.display_name for item in candidates}))
        sources = sorted({item.source for item in candidates if item.source})
        ambiguous = len(candidate_ids) > 1
        confidence = "ambiguous" if ambiguous else "high" if candidate_ids else "unresolved"
        if len(candidate_ids) == 1 and any(source.startswith("recent_event") for source in sources):
            confidence = "high"
        elif len(candidate_ids) == 1:
            confidence = "medium"
        entities.append(
            ReferencedEntity(
                mention_text=name,
                group_id=group_id,
                candidate_ids=candidate_ids,
                candidate_names=candidate_names,
                source=",".join(sources) or "unresolved",
                confidence=confidence,
                ambiguous=ambiguous,
            )
        )
    return entities[:8]


def render_referenced_entity_block(entities: Iterable[ReferencedEntity]) -> str:
    resolved = list(entities)
    if not resolved:
        return ""
    lines = [
        "本轮提及对象边界（只用于区分第三方，不改变默认人格关系）：",
        "以下对象与当前发言人是不同概念；不要把提及对象的身份、称呼或关系套给当前发言人。",
    ]
    for entity in resolved:
        lines.append(f"- 名称：{entity.mention_text}")
        if entity.resolved_id:
            lines.append(f"  群内身份：QQ {entity.resolved_id}")
            lines.append(f"  证据：{entity.source}；可信度：{entity.confidence}")
            lines.append("  处理：可把该名称与这个 QQ 绑定，但不要根据昵称推断固定关系。")
        elif entity.ambiguous:
            lines.append(f"  候选身份：QQ {', '.join(entity.candidate_ids)}")
            lines.append("  证据：同一群内存在多个同名对象；可信度：歧义")
            lines.append("  处理：不要猜测具体是哪一位；涉及 @、传话或身份事实时先查询或澄清。")
        else:
            lines.append("  群内身份：未确认")
            lines.append("  处理：不要猜测；涉及 @、传话或身份事实时先查询身份工具或向用户澄清。")
    return "\n".join(lines)


async def resolve_group_references(
    text: str,
    *,
    group_id: str,
    current_sender_id: str = "",
    current_sender_name: str = "",
    events: Iterable[Any] = (),
    db_service: Any = None,
    max_log_count: int = 200,
    max_age_seconds: float = 7 * 24 * 60 * 60,
) -> tuple[list[ReferencedEntity], str]:
    evidence = collect_event_identity_evidence(events, group_id=group_id)
    if db_service is not None and group_id and hasattr(db_service, "get_recent_message_logs"):
        try:
            logs = await asyncio.to_thread(
                db_service.get_recent_message_logs,
                group_id,
                max(1, min(int(max_log_count or 200), 500)),
                max_age_seconds,
                True,
            )
            for row in logs or []:
                user_id = str(getattr(row, "sender_id", "") or "").strip()
                display_name = _clean_candidate_name(getattr(row, "sender_name", ""))
                if user_id and display_name:
                    evidence.append(
                        EntityEvidence(
                            user_id=user_id,
                            display_name=display_name,
                            source="group_message_log",
                            timestamp=float(getattr(row, "timestamp", 0.0) or 0.0),
                        )
                    )
        except Exception:
            # Identity enrichment must never block the normal reply path.
            pass
    entities = build_referenced_entities(
        text,
        group_id=group_id,
        current_sender_id=current_sender_id,
        current_sender_name=current_sender_name,
        evidence=evidence,
    )
    return entities, render_referenced_entity_block(entities)


__all__ = [
    "EntityEvidence",
    "ReferencedEntity",
    "build_referenced_entities",
    "collect_event_identity_evidence",
    "event_group_id",
    "render_referenced_entity_block",
    "resolve_group_references",
]
