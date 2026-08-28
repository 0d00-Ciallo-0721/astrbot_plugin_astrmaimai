from __future__ import annotations

import copy
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class ToolCapabilitySpec:
    name: str
    family: str
    effect_type: str
    contexts: tuple[str, ...] = ("private", "group")
    explicit_policy: str = "optional"
    autonomous_allowed: bool = False
    deterministic_fallback: bool = False
    max_calls_per_turn: int | None = None
    requires_explicit_authorization: bool = False


@dataclass(frozen=True, slots=True)
class ToolInvocationPlan:
    tool_name: str
    family: str
    source: str
    required: bool
    deterministic_fallback: bool
    reason: str
    entity_domain: str = ""
    operation: str = ""
    target: str = ""
    prepared_arguments: dict[str, Any] | None = None
    acceptable_statuses: tuple[str, ...] = ("success",)
    acceptable_source_domains: tuple[str, ...] = ()


TOOL_CAPABILITIES: dict[str, ToolCapabilitySpec] = {
    "wait_and_listen": ToolCapabilitySpec("wait_and_listen", "wait", "control", explicit_policy="required", autonomous_allowed=True),
    "omni_perception_query": ToolCapabilitySpec("omni_perception_query", "query", "query", explicit_policy="required", autonomous_allowed=True),
    "self_lore_query": ToolCapabilitySpec("self_lore_query", "self_lore", "query", explicit_policy="required", autonomous_allowed=True),
    "learned_language_lookup": ToolCapabilitySpec("learned_language_lookup", "learned_language", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_friend_lookup": ToolCapabilitySpec("qq_friend_lookup", "friend_fact", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_group_member_lookup": ToolCapabilitySpec("qq_group_member_lookup", "group_member", "query", contexts=("group",), explicit_policy="required", autonomous_allowed=True),
    "qq_user_identity_lookup": ToolCapabilitySpec("qq_user_identity_lookup", "user_identity", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_forward_message_lookup": ToolCapabilitySpec("qq_forward_message_lookup", "forward_message", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_group_presence_lookup": ToolCapabilitySpec("qq_group_presence_lookup", "group_fact", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_recent_contact_lookup": ToolCapabilitySpec("qq_recent_contact_lookup", "recent_contact", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_message_artifact_lookup": ToolCapabilitySpec("qq_message_artifact_lookup", "message_artifact", "query", explicit_policy="required", autonomous_allowed=True),
    "vision_message_analyze_tool": ToolCapabilitySpec("vision_message_analyze_tool", "vision_message", "query", explicit_policy="required", autonomous_allowed=True),
    "cross_session_reply_lookup": ToolCapabilitySpec("cross_session_reply_lookup", "cross_reply", "query", explicit_policy="required", autonomous_allowed=True),
    "quote_reply_action": ToolCapabilitySpec("quote_reply_action", "quote_reply", "qq_side_effect", explicit_policy="required", autonomous_allowed=True, deterministic_fallback=True),
    "qq_message_recall_lookup": ToolCapabilitySpec("qq_message_recall_lookup", "message_recall", "query", explicit_policy="required", autonomous_allowed=True),
    "topic_thread_lookup": ToolCapabilitySpec("topic_thread_lookup", "topic_thread", "query", explicit_policy="required", autonomous_allowed=True),
    "bot_capability_lookup": ToolCapabilitySpec("bot_capability_lookup", "capability", "query", explicit_policy="required", autonomous_allowed=True),
    "memory_write_correction_tool": ToolCapabilitySpec("memory_write_correction_tool", "memory_correction", "memory_write", explicit_policy="optional", autonomous_allowed=True),
    "unverified_report_record_tool": ToolCapabilitySpec("unverified_report_record_tool", "unverified_report", "memory_write", explicit_policy="required"),
    "persona_fact_check_tool": ToolCapabilitySpec("persona_fact_check_tool", "persona_fact", "query", explicit_policy="required", autonomous_allowed=True),
    "group_activity_snapshot_tool": ToolCapabilitySpec("group_activity_snapshot_tool", "group_activity", "query", contexts=("group",), explicit_policy="required", autonomous_allowed=True),
    "contact_route_suggest_tool": ToolCapabilitySpec("contact_route_suggest_tool", "route_suggest", "query", explicit_policy="required", autonomous_allowed=True),
    "cross_chat_memory_query": ToolCapabilitySpec("cross_chat_memory_query", "cross_memory", "query", explicit_policy="required", autonomous_allowed=True),
    "construct_at_event": ToolCapabilitySpec("construct_at_event", "at", "message", contexts=("group",), explicit_policy="optional", autonomous_allowed=True),
    "proactive_poke": ToolCapabilitySpec("proactive_poke", "poke", "qq_side_effect", explicit_policy="optional", autonomous_allowed=True, deterministic_fallback=True),
    "proactive_meme": ToolCapabilitySpec("proactive_meme", "meme", "message", explicit_policy="optional", autonomous_allowed=True, deterministic_fallback=True),
    "meme_resonance_action": ToolCapabilitySpec("meme_resonance_action", "resonance", "control", contexts=("group",), explicit_policy="optional", autonomous_allowed=True),
    "topic_hijack_action": ToolCapabilitySpec("topic_hijack_action", "topic", "control", explicit_policy="optional", autonomous_allowed=True),
    "space_transition_action": ToolCapabilitySpec("space_transition_action", "private", "cross_session_message", explicit_policy="optional", autonomous_allowed=True),
    "regret_and_withdraw_action": ToolCapabilitySpec("regret_and_withdraw_action", "withdraw", "qq_side_effect", explicit_policy="optional", autonomous_allowed=True, deterministic_fallback=True),
    "message_emoji_reaction_action": ToolCapabilitySpec("message_emoji_reaction_action", "emoji_reaction", "qq_side_effect", explicit_policy="optional", autonomous_allowed=True, deterministic_fallback=True),
    "proactive_like_action": ToolCapabilitySpec("proactive_like_action", "like", "qq_side_effect", explicit_policy="optional", autonomous_allowed=True),
}


TOOL_DISPLAY_NAMES: dict[str, str] = {
    "wait_and_listen": "等待并继续倾听",
    "omni_perception_query": "综合感知查询",
    "self_lore_query": "人格设定查询",
    "learned_language_lookup": "已学习语言查询",
    "qq_friend_lookup": "QQ 好友查询",
    "qq_group_member_lookup": "QQ群成员查询",
    "qq_user_identity_lookup": "QQ 用户身份查询",
    "qq_forward_message_lookup": "QQ 合并转发查询",
    "qq_group_presence_lookup": "QQ群关系查询",
    "qq_recent_contact_lookup": "QQ 最近联系人查询",
    "qq_message_artifact_lookup": "QQ 消息资料查询",
    "vision_message_analyze_tool": "图片消息分析",
    "cross_session_reply_lookup": "跨会话回复查询",
    "quote_reply_action": "引用回复",
    "qq_message_recall_lookup": "消息回溯查询",
    "topic_thread_lookup": "话题线索查询",
    "bot_capability_lookup": "工具能力查询",
    "memory_write_correction_tool": "记忆纠正",
    "unverified_report_record_tool": "未核实说法记录",
    "persona_fact_check_tool": "人格事实核查",
    "group_activity_snapshot_tool": "群聊活动快照",
    "contact_route_suggest_tool": "联系人路由建议",
    "cross_chat_memory_query": "跨聊天记忆查询",
    "construct_at_event": "群聊成员提醒",
    "proactive_poke": "QQ 戳一戳",
    "proactive_meme": "发送表情包",
    "meme_resonance_action": "表情共鸣复读",
    "topic_hijack_action": "话题切换",
    "space_transition_action": "跨会话发送",
    "regret_and_withdraw_action": "撤回机器人消息",
    "message_emoji_reaction_action": "贴表情",
    "proactive_like_action": "QQ 点赞",
}


TOOL_NAME_ALIASES: dict[str, str] = {
    "message_reaction_action": "message_emoji_reaction_action",
    "message_emoji_like_action": "message_emoji_reaction_action",
}


def canonical_tool_name(tool_name: str) -> str:
    name = str(tool_name or "").strip()
    return TOOL_NAME_ALIASES.get(name, name)


def get_tool_capability(tool_name: str) -> ToolCapabilitySpec | None:
    """Resolve a capability from the live registry using canonical aliases."""
    return TOOL_CAPABILITIES.get(canonical_tool_name(tool_name))


def is_model_disclosure_requestable(tool_name: str) -> bool:
    """Only read-only tools may be opened from a model-originated request."""
    spec = get_tool_capability(tool_name)
    return bool(spec and spec.effect_type == "query")


def requires_explicit_disclosure(tool_name: str) -> bool:
    spec = get_tool_capability(tool_name)
    return bool(spec and spec.effect_type in {"memory_write", "cross_session_message"})


def requires_explicit_authorization(tool_name: str) -> bool:
    spec = get_tool_capability(tool_name)
    return bool(spec and spec.requires_explicit_authorization)


def is_autonomous_interaction(tool_name: str) -> bool:
    spec = get_tool_capability(tool_name)
    return bool(
        spec
        and spec.autonomous_allowed
        and spec.effect_type in {"text", "message", "control", "qq_side_effect", "cross_session_message"}
    )


def tool_display_name(tool_name: str) -> str:
    name = canonical_tool_name(tool_name)
    return TOOL_DISPLAY_NAMES.get(name, name)


FAMILY_TO_TOOL: dict[str, str] = {
    "wait": "wait_and_listen",
    "query": "omni_perception_query",
    "self_lore": "self_lore_query",
    "learned_language": "learned_language_lookup",
    "friend_fact": "qq_friend_lookup",
    "group_member": "qq_group_member_lookup",
    "user_identity": "qq_user_identity_lookup",
    "forward_message": "qq_forward_message_lookup",
    "group_fact": "qq_group_presence_lookup",
    "recent_contact": "qq_recent_contact_lookup",
    "message_artifact": "qq_message_artifact_lookup",
    "vision_message": "vision_message_analyze_tool",
    "cross_reply": "cross_session_reply_lookup",
    "quote_reply": "quote_reply_action",
    "message_recall": "qq_message_recall_lookup",
    "topic_thread": "topic_thread_lookup",
    "capability": "bot_capability_lookup",
    "memory_correction": "memory_write_correction_tool",
    "unverified_report": "unverified_report_record_tool",
    "persona_fact": "persona_fact_check_tool",
    "group_activity": "group_activity_snapshot_tool",
    "route_suggest": "contact_route_suggest_tool",
    "cross_memory": "cross_chat_memory_query",
    "at": "construct_at_event",
    "poke": "proactive_poke",
    "meme": "proactive_meme",
    "resonance": "meme_resonance_action",
    "topic": "topic_hijack_action",
    "private": "space_transition_action",
    "withdraw": "regret_and_withdraw_action",
    "emoji_reaction": "message_emoji_reaction_action",
    "reaction": "message_emoji_reaction_action",
    "qq_reaction": "message_emoji_reaction_action",
    "like": "proactive_like_action",
}


AUTONOMOUS_INTERACTION_TOOLS = {
    name
    for name, spec in TOOL_CAPABILITIES.items()
    if spec.autonomous_allowed and spec.effect_type in {"text", "message", "control", "qq_side_effect", "cross_session_message"}
}


def normalize_tool_schema(tool: Any) -> Any:
    schema = copy.deepcopy(getattr(tool, "parameters", None))
    if not isinstance(schema, dict):
        schema = {}
    schema["type"] = "object"
    properties = schema.get("properties")
    schema["properties"] = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    if required is not None:
        schema["required"] = [
            str(name)
            for name in required
            if str(name) in schema["properties"]
        ]
    schema["additionalProperties"] = False
    tool.parameters = schema
    return tool


def normalize_tool_schemas(tools: Iterable[Any]) -> list[Any]:
    return [normalize_tool_schema(tool) for tool in tools or []]


def filter_tools_for_context(
    tools: Iterable[Any],
    *,
    is_group: bool,
    name_resolver=None,
) -> list[Any]:
    context_name = "group" if is_group else "private"
    filtered: list[Any] = []
    for tool in tools or []:
        raw_name = str(getattr(tool, "name", "") or "")
        tool_name = canonical_tool_name(str(name_resolver(tool) if callable(name_resolver) else raw_name))
        spec = get_tool_capability(tool_name) or ToolCapabilitySpec(tool_name, "", "")
        if context_name in spec.contexts:
            filtered.append(tool)
    return filtered


def build_explicit_invocation_plans(
    families: Iterable[str],
    available_tool_names: Iterable[str],
    *,
    intent_contracts: Iterable[Any] | None = None,
) -> list[ToolInvocationPlan]:
    available = {str(name or "").strip() for name in available_tool_names}
    requested = {str(item or "").strip() for item in families}
    ordered_families = [family for family in FAMILY_TO_TOOL if family in requested]
    ordered_families.extend(family for family in requested if family and family not in FAMILY_TO_TOOL)
    contracts_by_family = {
        str(getattr(contract, "family", "") or ""): contract
        for contract in intent_contracts or []
        if str(getattr(contract, "family", "") or "")
    }
    plans: list[ToolInvocationPlan] = []
    for family in ordered_families:
        tool_name = canonical_tool_name(FAMILY_TO_TOOL.get(family) or "")
        spec = get_tool_capability(tool_name or "")
        if not spec or tool_name not in available:
            continue
        contract = contracts_by_family.get(family)
        plans.append(
            ToolInvocationPlan(
                tool_name=tool_name,
                family=family,
                source="explicit_user_request",
                required=True,
                deterministic_fallback=spec.deterministic_fallback,
                reason=str(getattr(contract, "reason", "") or f"explicit_{family}_intent"),
                entity_domain=str(getattr(contract, "entity_domain", "") or ""),
                operation=str(getattr(contract, "operation", "") or ""),
                target=str(getattr(contract, "target", "") or ""),
                prepared_arguments=dict(getattr(contract, "prepared_arguments", {}) or {}),
                acceptable_statuses=tuple(
                    getattr(contract, "acceptable_statuses", ("success",)) or ("success",)
                ),
                acceptable_source_domains=tuple(
                    getattr(contract, "acceptable_source_domains", ()) or ()
                ),
            )
        )
    return plans


def record_tool_lifecycle(
    event: Any,
    tool_name: str,
    phase: str,
    *,
    source: str = "",
    status: str = "",
    reason: str = "",
) -> None:
    if not hasattr(event, "get_extra") or not hasattr(event, "set_extra"):
        return
    trace = event.get_extra("astrmai_tool_lifecycle_trace", [])
    trace = list(trace) if isinstance(trace, list) else []
    trace.append(
        {
            "at": round(time.time(), 3),
            "tool": str(tool_name or ""),
            "phase": str(phase or ""),
            "source": str(source or ""),
            "status": str(status or ""),
            "reason": str(reason or "")[:120],
        }
    )
    event.set_extra("astrmai_tool_lifecycle_trace", trace[-64:])


def publish_invocation_plans(event: Any, plans: Iterable[ToolInvocationPlan]) -> None:
    serialized = [asdict(plan) for plan in plans]
    if hasattr(event, "set_extra"):
        event.set_extra("astrmai_tool_invocation_plans", serialized)
        event.set_extra(
            "astrmai_required_tools",
            [item["tool_name"] for item in serialized if item.get("required")],
        )
    for plan in plans:
        record_tool_lifecycle(
            event,
            plan.tool_name,
            "planned",
            source=plan.source,
            status="required" if plan.required else "optional",
            reason=plan.reason,
        )


__all__ = [
    "AUTONOMOUS_INTERACTION_TOOLS",
    "FAMILY_TO_TOOL",
    "TOOL_CAPABILITIES",
    "TOOL_DISPLAY_NAMES",
    "TOOL_NAME_ALIASES",
    "ToolCapabilitySpec",
    "ToolInvocationPlan",
    "build_explicit_invocation_plans",
    "canonical_tool_name",
    "get_tool_capability",
    "filter_tools_for_context",
    "is_model_disclosure_requestable",
    "is_autonomous_interaction",
    "normalize_tool_schema",
    "normalize_tool_schemas",
    "publish_invocation_plans",
    "record_tool_lifecycle",
    "requires_explicit_disclosure",
    "requires_explicit_authorization",
    "tool_display_name",
]
