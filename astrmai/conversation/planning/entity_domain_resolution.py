from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from .tool_contracts import FAMILY_TO_TOOL


class EntityDomain(str, Enum):
    PERSONA_LORE = "persona_lore"
    PLATFORM_FRIEND = "platform_friend"
    PLATFORM_GROUP_MEMBER = "platform_group_member"
    CONVERSATION_PERSON = "conversation_person"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class ToolOperation(str, Enum):
    LIST = "list"
    COUNT = "count"
    MATCH = "match"
    DESCRIBE = "describe"
    VERIFY_RELATION = "verify_relation"


@dataclass(frozen=True, slots=True)
class ToolIntentContract:
    family: str
    required_tool: str
    entity_domain: str
    operation: str
    target: str = ""
    prepared_arguments: dict[str, object] = field(default_factory=dict)
    acceptable_statuses: tuple[str, ...] = ("success",)
    acceptable_source_domains: tuple[str, ...] = ()
    required: bool = True
    reason: str = ""
    clarification_prompt: str = ""


QQ_NUMBER_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
_TRAILING_PARTICLES_RE = re.compile(r"(?:吗|呢|呀|啊|嘛|么|？|\?|。|！|!)+$")
_PERSONA_MARKERS = (
    "人设中的",
    "人设里的",
    "设定中的",
    "设定里的",
    "角色卡中的",
    "角色卡里的",
    "世界观中的",
    "世界观里的",
    "你的设定",
    "你的人设",
)
_FRIEND_MARKERS = (
    "好友列表",
    "朋友列表",
    "联系人列表",
    "你的好友",
    "你好友",
    "机器人好友",
    "QQ好友",
    "qq好友",
)
_GROUP_MARKERS = ("群成员", "群友", "群名片", "这个群里", "当前群里")


def _clean(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _extract_target(message: str) -> str:
    text = _clean(message)
    qq_match = QQ_NUMBER_RE.search(text)
    if qq_match:
        return qq_match.group(0)
    patterns = (
        r"(?:好友|朋友|联系人)(?:列表)?(?:里|里面|中)?(?:的)?([^，。！？?]{1,32})",
        r"(?:查看|查一下|查查|看看|查询)(?:一下)?([^，。！？?]{1,32})",
        r"(?:认识|知道|记得)([^，。！？?]{1,32})",
        r"([^，。！？?]{1,32})(?:是谁|是不是(?:你)?好友|是否为(?:你)?好友|在不在(?:你)?好友列表)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        target = _TRAILING_PARTICLES_RE.sub("", match.group(1).strip(" ，,：:的"))
        target = re.sub(
            r"^(?:人设中的|人设里的|设定中的|设定里的|角色卡中的|角色卡里的|世界观中的|世界观里的)",
            "",
            target,
        ).strip()
        target = re.split(
            r"(?:是谁|是不是(?:你)?的?好友|是否为(?:你)?的?好友|在不在(?:你)?的?好友列表|有没有)",
            target,
            maxsplit=1,
        )[0].strip()
        target = re.sub(r"^(?:有|有没有|是否有|是不是|是否为)", "", target).strip()
        if target and target not in {"谁", "哪些人", "什么人", "一下", "列表"}:
            return target[:80]
    return ""


def _friend_operation(message: str, target: str) -> ToolOperation:
    text = _clean(message)
    if any(token in text for token in ("多少好友", "好友数量", "几个好友", "好友有多少")):
        return ToolOperation.COUNT
    if any(token in text for token in ("好友列表", "朋友列表", "联系人列表", "好友都有谁", "有哪些好友")):
        return ToolOperation.LIST
    if target and any(token in text for token in ("是不是", "是否", "有没有", "在不在", "好友吗")):
        return ToolOperation.MATCH
    if target:
        return ToolOperation.DESCRIBE
    return ToolOperation.LIST


def _persona_contains_target(persona_text: str, target: str) -> bool:
    return bool(target and target.casefold() in _clean(persona_text).casefold())


def resolve_entity_domain(
    message: str,
    *,
    explicit_families: Iterable[str] = (),
    persona_text: str = "",
) -> tuple[EntityDomain, ToolOperation, str, str]:
    text = _clean(message)
    families = {str(item or "").strip() for item in explicit_families}
    target = _extract_target(text)
    if any(marker in text for marker in _PERSONA_MARKERS):
        return EntityDomain.PERSONA_LORE, ToolOperation.DESCRIBE, target, "persona_lore_marker"
    if "friend_fact" in families or any(marker in text for marker in _FRIEND_MARKERS):
        operation = _friend_operation(text, target)
        if operation in {ToolOperation.LIST, ToolOperation.COUNT}:
            target = ""
        return EntityDomain.PLATFORM_FRIEND, operation, target, "platform_friend_marker"
    if "group_member" in families or any(marker in text for marker in _GROUP_MARKERS):
        operation = ToolOperation.MATCH if target else ToolOperation.LIST
        return EntityDomain.PLATFORM_GROUP_MEMBER, operation, target, "platform_group_marker"
    if "self_lore" in families:
        return EntityDomain.PERSONA_LORE, ToolOperation.DESCRIBE, target, "persona_lore_marker"
    if any(marker in text for marker in ("你认识", "你知道", "你记得")) and target:
        if _persona_contains_target(persona_text, target):
            return EntityDomain.PERSONA_LORE, ToolOperation.DESCRIBE, target, "persona_catalog_match"
        return EntityDomain.CONVERSATION_PERSON, ToolOperation.DESCRIBE, target, "conversation_person_reference"
    return EntityDomain.UNKNOWN, ToolOperation.DESCRIBE, target, "no_entity_domain_signal"


def build_tool_intent_contracts(
    families: Iterable[str],
    *,
    message: str,
    available_tool_names: Iterable[str],
    persona_text: str = "",
) -> list[ToolIntentContract]:
    available = {str(name or "").strip() for name in available_tool_names}
    requested = {str(item or "").strip() for item in families if str(item or "").strip()}
    domain, operation, target, reason = resolve_entity_domain(
        message,
        explicit_families=requested,
        persona_text=persona_text,
    )
    if domain == EntityDomain.PERSONA_LORE:
        requested.discard("friend_fact")
        requested.add("self_lore")
    elif domain == EntityDomain.PLATFORM_FRIEND:
        requested.discard("self_lore")
        requested.add("friend_fact")

    contracts: list[ToolIntentContract] = []
    for family in requested:
        tool_name = FAMILY_TO_TOOL.get(family, "")
        if not tool_name or tool_name not in available:
            continue
        entity_domain = domain.value if family in {"friend_fact", "self_lore", "group_member"} else ""
        tool_operation = operation.value if entity_domain else ""
        prepared: dict[str, object] = {}
        acceptable_statuses = ("success",)
        acceptable_domains: tuple[str, ...] = ()
        clarification = ""
        if family == "friend_fact":
            if operation in {ToolOperation.LIST, ToolOperation.COUNT}:
                target = ""
            prepared = {
                "mode": tool_operation or ToolOperation.LIST.value,
                "target": target,
            }
            acceptable_statuses = ("success", "not_found") if target else ("success",)
            acceptable_domains = (EntityDomain.PLATFORM_FRIEND.value,)
        elif family == "self_lore":
            prepared = {"query": target or _clean(message)[:240]}
            acceptable_statuses = ("success", "not_found")
            acceptable_domains = (EntityDomain.PERSONA_LORE.value,)
        elif family == "group_member":
            acceptable_domains = (EntityDomain.PLATFORM_GROUP_MEMBER.value,)
        if entity_domain == EntityDomain.AMBIGUOUS.value:
            clarification = "你想查现实 QQ 好友/群成员，还是角色设定里的人物？"
        contracts.append(
            ToolIntentContract(
                family=family,
                required_tool=tool_name,
                entity_domain=entity_domain,
                operation=tool_operation,
                target=target,
                prepared_arguments=prepared,
                acceptable_statuses=acceptable_statuses,
                acceptable_source_domains=acceptable_domains,
                reason=reason if entity_domain else f"explicit_{family}_intent",
                clarification_prompt=clarification,
            )
        )
    return contracts


__all__ = [
    "EntityDomain",
    "ToolIntentContract",
    "ToolOperation",
    "build_tool_intent_contracts",
    "resolve_entity_domain",
]
