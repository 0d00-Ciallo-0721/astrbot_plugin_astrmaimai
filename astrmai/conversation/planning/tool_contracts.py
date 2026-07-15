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
    plans: list[ToolInvocationPlan] = []
    for family in dict.fromkeys(str(item or "").strip() for item in families):
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
