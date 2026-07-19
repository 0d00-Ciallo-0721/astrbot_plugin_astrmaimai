from __future__ import annotations

import re
from typing import Any


FEEDBACK_SCHEMA_VERSION = 2

SOURCE_LABELS = {
    "agency": "行为节奏",
    "diary": "日记连续性",
    "memory_summary": "记忆总结",
    "heartflow": "主动参与反馈",
    "dream": "记忆整理反馈",
    "maintenance": "记忆维护反馈",
}

INTENT_LABELS = {
    "answer": "回答问题",
    "reply": "回应交流",
    "wait": "等待观察",
    "ignore": "暂不参与",
    "tease": "轻松互动",
}

ACTION_LABELS = {
    "reply": "普通回复",
    "wait": "等待",
    "ignore": "忽略",
    "none": "无特殊动作",
}

TIER_LABELS = {
    "chat": "聊天动作",
    "full": "完整动作",
    "sys3": "工作模式",
    "none": "无特殊层级",
}

TAG_LABELS = {
    "long_reply": "长回复",
    "meme": "表情包",
    "sharp_reply": "强硬回应",
    "like": "点赞互动",
    "poke": "戳一戳",
    "tool": "工具调用",
}

_AGENCY_PATTERN = re.compile(
    r"Recent agency pattern:\s*(?P<turns>\d+)\s*turns,\s*"
    r"main_intent=(?P<intent>[^,\.]+),\s*"
    r"main_tier=(?P<tier>[^,\.]+),\s*"
    r"main_action=(?P<action>[^\.]+)\."
    r"(?:\s*Cooldowns observed:\s*(?P<tags>[^\.]+)\.)?",
    re.IGNORECASE,
)


def source_label(source: str) -> str:
    clean = str(source or "unknown").strip().lower() or "unknown"
    return SOURCE_LABELS.get(clean, clean)


def tag_label(tag: str) -> str:
    clean = str(tag or "").strip().lower()
    return TAG_LABELS.get(clean, clean)


def _label(mapping: dict[str, str], value: Any, fallback: str) -> str:
    clean = str(value or "").strip().lower()
    return mapping.get(clean, clean or fallback)


def normalize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(payload or {})
    normalized: dict[str, Any] = {}
    if "turn_count" in raw:
        try:
            normalized["turn_count"] = max(0, int(raw.get("turn_count") or 0))
        except (TypeError, ValueError):
            pass
    for key in ("main_intent", "main_tier", "main_action"):
        clean = str(raw.get(key) or "").strip().lower()
        if clean:
            normalized[key] = clean
    tags = []
    for item in raw.get("cooldown_tags", []) or []:
        clean = str(item or "").strip().lower()
        if clean and clean not in tags:
            tags.append(clean)
    if tags:
        normalized["cooldown_tags"] = tags[:12]
    repeated = []
    for item in raw.get("repeated_tags", []) or []:
        clean = str(item or "").strip().lower()
        if clean and clean not in repeated:
            repeated.append(clean)
    if repeated:
        normalized["repeated_tags"] = repeated[:12]
    return normalized


def parse_legacy_agency(summary: str, guidance: str = "", tags: list[str] | None = None) -> dict[str, Any]:
    match = _AGENCY_PATTERN.search(str(summary or ""))
    if not match:
        return {}
    observed = [item.strip().lower() for item in str(match.group("tags") or "").split(",") if item.strip()]
    for item in tags or []:
        clean = str(item or "").strip().lower()
        if clean and clean not in observed:
            observed.append(clean)
    repeated = []
    repeated_match = re.search(r"Avoid repeating recently used actions:\s*([^\.]+)", str(guidance or ""), re.I)
    if repeated_match:
        repeated = [item.strip().lower() for item in repeated_match.group(1).split(",") if item.strip()]
    return normalize_payload(
        {
            "turn_count": match.group("turns"),
            "main_intent": match.group("intent"),
            "main_tier": match.group("tier"),
            "main_action": match.group("action"),
            "cooldown_tags": observed,
            "repeated_tags": repeated,
        }
    )


def render_agency(payload: dict[str, Any]) -> tuple[str, str]:
    data = normalize_payload(payload)
    if not data:
        return "", ""
    turns = int(data.get("turn_count") or 0)
    intent = _label(INTENT_LABELS, data.get("main_intent"), "一般交流")
    action = _label(ACTION_LABELS, data.get("main_action"), "普通回复")
    summary = f"近期 {turns} 轮主要以{intent}为主，常用动作是{action}。" if turns else f"近期主要以{intent}为主。"
    tags = list(data.get("cooldown_tags") or [])
    if tags:
        summary += " 已出现的表达动作包括：" + "、".join(tag_label(item) for item in tags) + "。"

    guidance: list[str] = []
    repeated = list(data.get("repeated_tags") or [])
    if repeated:
        guidance.append("避免立即重复使用" + "、".join(tag_label(item) for item in repeated) + "。")
    if "long_reply" in tags:
        guidance.append("除非用户明确要求细节，下一轮优先简短回应。")
    if "sharp_reply" in tags:
        guidance.append("除非再次受到明确攻击，不要延续强硬反驳。")
    if str(data.get("main_intent") or "") in {"wait", "ignore"}:
        guidance.append("近期对话更适合克制参与，先观察新的明确线索。")
    if not guidance:
        guidance.append("保持近期交流节奏，但不要机械重复上一轮表达。")
    return summary, "".join(guidance)


def render_feedback(
    *,
    source: str,
    summary: str,
    guidance: str = "",
    tags: list[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[str, str, list[str], dict[str, Any]]:
    clean_source = str(source or "unknown").strip().lower() or "unknown"
    data = normalize_payload(payload)
    if clean_source == "agency":
        data = data or parse_legacy_agency(summary, guidance, tags)
        rendered_summary, rendered_guidance = render_agency(data)
        if rendered_summary or rendered_guidance:
            return rendered_summary, rendered_guidance, [tag_label(item) for item in tags or []], data

    guidance_map = {
        "Use this diary only as quiet continuity, do not quote it or force old topics.": "仅将这篇日记作为安静的连续性背景，不要直接引用，也不要强行带回旧话题。",
        "Use this consolidated memory only when it is directly relevant; do not force older topics into the current reply.": "仅在与当前对话直接相关时使用这份记忆总结，不要把旧话题强行带入当前回复。",
        "Keep the next response consistent with the recent agency pattern without repeating it.": "保持近期交流节奏，但不要机械重复上一轮表达。",
    }
    display_guidance = guidance_map.get(str(guidance or "").strip(), str(guidance or "").strip())
    return str(summary or "").strip(), display_guidance, [tag_label(item) for item in tags or []], data


__all__ = [
    "FEEDBACK_SCHEMA_VERSION",
    "normalize_payload",
    "parse_legacy_agency",
    "render_agency",
    "render_feedback",
    "source_label",
    "tag_label",
]
