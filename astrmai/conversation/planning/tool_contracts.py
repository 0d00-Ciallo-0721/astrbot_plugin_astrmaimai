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
    max_calls_per_turn: int = 1


@dataclass(frozen=True, slots=True)
class ToolInvocationPlan:
    tool_name: str
    family: str
    source: str
    required: bool
    deterministic_fallback: bool
    reason: str


TOOL_CAPABILITIES: dict[str, ToolCapabilitySpec] = {
    "wait_and_listen": ToolCapabilitySpec("wait_and_listen", "wait", "control", explicit_policy="required", autonomous_allowed=True),
    "omni_perception_query": ToolCapabilitySpec("omni_perception_query", "query", "query", explicit_policy="required", autonomous_allowed=True),
    "self_lore_query": ToolCapabilitySpec("self_lore_query", "self_lore", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_friend_lookup": ToolCapabilitySpec("qq_friend_lookup", "friend_fact", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_group_member_lookup": ToolCapabilitySpec("qq_group_member_lookup", "group_member", "query", contexts=("group",), explicit_policy="required", autonomous_allowed=True),
    "qq_user_identity_lookup": ToolCapabilitySpec("qq_user_identity_lookup", "user_identity", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_forward_message_lookup": ToolCapabilitySpec("qq_forward_message_lookup", "forward_message", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_group_presence_lookup": ToolCapabilitySpec("qq_group_presence_lookup", "group_fact", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_recent_contact_lookup": ToolCapabilitySpec("qq_recent_contact_lookup", "recent_contact", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_message_artifact_lookup": ToolCapabilitySpec("qq_message_artifact_lookup", "message_artifact", "query", explicit_policy="required", autonomous_allowed=True),
    "vision_message_analyze_tool": ToolCapabilitySpec("vision_message_analyze_tool", "vision_message", "query", explicit_policy="required", autonomous_allowed=True),
    "cross_session_reply_lookup": ToolCapabilitySpec("cross_session_reply_lookup", "cross_reply", "query", explicit_policy="required", autonomous_allowed=True),
    "qq_custom_face_send_tool": ToolCapabilitySpec("qq_custom_face_send_tool", "custom_face", "qq_side_effect", explicit_policy="required", autonomous_allowed=True, deterministic_fallback=True),
    "quote_reply_action": ToolCapabilitySpec("quote_reply_action", "quote_reply", "qq_side_effect", explicit_policy="required", autonomous_allowed=True, deterministic_fallback=True),
    "qq_message_recall_lookup": ToolCapabilitySpec("qq_message_recall_lookup", "message_recall", "query", explicit_policy="required", autonomous_allowed=True),
    "topic_thread_lookup": ToolCapabilitySpec("topic_thread_lookup", "topic_thread", "query", explicit_policy="required", autonomous_allowed=True),
    "bot_capability_lookup": ToolCapabilitySpec("bot_capability_lookup", "capability", "query", explicit_policy="required", autonomous_allowed=True),
    "memory_write_correction_tool": ToolCapabilitySpec("memory_write_correction_tool", "memory_correction", "memory_write", explicit_policy="required"),
    "unverified_report_record_tool": ToolCapabilitySpec("unverified_report_record_tool", "unverified_report", "memory_write", explicit_policy="required"),
    "persona_fact_check_tool": ToolCapabilitySpec("persona_fact_check_tool", "persona_fact", "query", explicit_policy="required", autonomous_allowed=True),
    "group_activity_snapshot_tool": ToolCapabilitySpec("group_activity_snapshot_tool", "group_activity", "query", contexts=("group",), explicit_policy="required", autonomous_allowed=True),
    "contact_route_suggest_tool": ToolCapabilitySpec("contact_route_suggest_tool", "route_suggest", "query", explicit_policy="required", autonomous_allowed=True),
    "cross_chat_memory_query": ToolCapabilitySpec("cross_chat_memory_query", "cross_memory", "query", explicit_policy="required", autonomous_allowed=True),
    "construct_at_event": ToolCapabilitySpec("construct_at_event", "at", "message", contexts=("group",), explicit_policy="required", autonomous_allowed=True),
    "proactive_poke": ToolCapabilitySpec("proactive_poke", "poke", "qq_side_effect", explicit_policy="required", autonomous_allowed=True, deterministic_fallback=True),
    "proactive_meme": ToolCapabilitySpec("proactive_meme", "meme", "message", explicit_policy="required", autonomous_allowed=True, deterministic_fallback=True),
    "meme_resonance_action": ToolCapabilitySpec("meme_resonance_action", "resonance", "control", explicit_policy="required", autonomous_allowed=True),
    "topic_hijack_action": ToolCapabilitySpec("topic_hijack_action", "topic", "control", explicit_policy="required", autonomous_allowed=True),
    "space_transition_action": ToolCapabilitySpec("space_transition_action", "private", "cross_session_message", explicit_policy="required", autonomous_allowed=True),
    "regret_and_withdraw_action": ToolCapabilitySpec("regret_and_withdraw_action", "withdraw", "qq_side_effect", explicit_policy="required", deterministic_fallback=True),
    "message_reaction_action": ToolCapabilitySpec("message_reaction_action", "reaction", "text", explicit_policy="required", autonomous_allowed=True),
    "message_emoji_like_action": ToolCapabilitySpec("message_emoji_like_action", "qq_reaction", "qq_side_effect", explicit_policy="required", autonomous_allowed=True, deterministic_fallback=True),
    "proactive_like_action": ToolCapabilitySpec("proactive_like_action", "like", "text", explicit_policy="required", autonomous_allowed=True),
    "custom_face_catalog_query": ToolCapabilitySpec("custom_face_catalog_query", "qq_query", "query", explicit_policy="required"),
    "group_sign_action": ToolCapabilitySpec("group_sign_action", "sign", "qq_side_effect", contexts=("group",), explicit_policy="required", deterministic_fallback=True),
}


FAMILY_TO_TOOL: dict[str, str] = {
    "wait": "wait_and_listen",
    "query": "omni_perception_query",
    "self_lore": "self_lore_query",
    "friend_fact": "qq_friend_lookup",
    "group_member": "qq_group_member_lookup",
    "user_identity": "qq_user_identity_lookup",
    "forward_message": "qq_forward_message_lookup",
    "group_fact": "qq_group_presence_lookup",
    "recent_contact": "qq_recent_contact_lookup",
    "message_artifact": "qq_message_artifact_lookup",
    "vision_message": "vision_message_analyze_tool",
    "cross_reply": "cross_session_reply_lookup",
    "custom_face": "qq_custom_face_send_tool",
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
    "reaction": "message_reaction_action",
    "qq_reaction": "message_emoji_like_action",
    "like": "proactive_like_action",
    "qq_query": "custom_face_catalog_query",
    "sign": "group_sign_action",
}


AUTONOMOUS_INTERACTION_TOOLS = {
    name
    for name, spec in TOOL_CAPABILITIES.items()
    if spec.autonomous_allowed and spec.effect_type in {"text", "message", "qq_side_effect", "cross_session_message"}
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
        tool_name = str(name_resolver(tool) if callable(name_resolver) else raw_name)
        spec = TOOL_CAPABILITIES.get(tool_name, ToolCapabilitySpec(tool_name, "", ""))
        if context_name in spec.contexts:
            filtered.append(tool)
    return filtered


def build_explicit_invocation_plans(
    families: Iterable[str],
    available_tool_names: Iterable[str],
) -> list[ToolInvocationPlan]:
    available = {str(name or "").strip() for name in available_tool_names}
    requested = {str(item or "").strip() for item in families}
    ordered_families = [family for family in FAMILY_TO_TOOL if family in requested]
    ordered_families.extend(family for family in requested if family and family not in FAMILY_TO_TOOL)
    plans: list[ToolInvocationPlan] = []
    for family in ordered_families:
        tool_name = FAMILY_TO_TOOL.get(family)
        spec = TOOL_CAPABILITIES.get(tool_name or "")
        if not spec or tool_name not in available:
            continue
        plans.append(
            ToolInvocationPlan(
                tool_name=tool_name,
                family=family,
                source="explicit_user_request",
                required=True,
                deterministic_fallback=spec.deterministic_fallback,
                reason=f"explicit_{family}_intent",
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
    "ToolCapabilitySpec",
    "ToolInvocationPlan",
    "build_explicit_invocation_plans",
    "filter_tools_for_context",
    "normalize_tool_schema",
    "normalize_tool_schemas",
    "publish_invocation_plans",
    "record_tool_lifecycle",
]
