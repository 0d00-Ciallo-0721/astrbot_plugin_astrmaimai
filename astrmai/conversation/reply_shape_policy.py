from __future__ import annotations

import re
from typing import Any

from .contracts.focus_context import ReplyMode


_MICRO_UTTERANCES = {
    "行",
    "行吧",
    "好",
    "好吧",
    "好的",
    "可以",
    "收到",
    "知道了",
    "嗯",
    "嗯嗯",
    "哦",
    "噢",
    "对",
    "是",
    "算了",
    "哈哈",
    "哈哈哈",
    "嘿嘿",
    "嘿嘿嘿",
    "哼",
    "哼哼",
    "哼哼哼",
    "呃",
    "呃啊",
    "啊",
    "包包包",
    "累",
    "困",
    "累困",
    "好累",
    "好困",
}

_QUESTION_MARKERS = (
    "?",
    "？",
    "吗",
    "么",
    "什么",
    "谁",
    "哪",
    "怎么",
    "为何",
    "为什么",
    "多少",
    "几个",
)

_ACTION_MARKERS = (
    "帮我",
    "替我",
    "给我",
    "查一下",
    "查询",
    "搜索",
    "看看",
    "告诉",
    "解释",
    "总结",
    "分析",
    "提醒",
    "设置",
    "发送",
    "转发",
    "叫一下",
    "戳一下",
)

_NON_CHAT_EXTRA_KEYS = (
    "extracted_image_refs",
    "extracted_image_urls",
    "direct_image_refs",
    "direct_vision_urls",
    "astrmai_vision_records",
    "astrmai_pending_actions",
    "astrmai_required_tools",
    "astrmai_prepared_required_tools",
    "astrmai_tool_execution_trace",
    "astrmai_tool_lifecycle_trace",
)


def _event_extra(event: Any, key: str, default: Any = None) -> Any:
    getter = getattr(event, "get_extra", None)
    if callable(getter):
        return getter(key, default)
    extras = getattr(event, "_extras", None)
    if isinstance(extras, dict):
        return extras.get(key, default)
    extras = getattr(event, "_extra", None)
    if isinstance(extras, dict):
        return extras.get(key, default)
    return default


def set_reply_shape_policy(event: Any, policy: dict[str, Any]) -> None:
    setter = getattr(event, "set_extra", None)
    if callable(setter):
        setter("astrmai_reply_shape_policy", dict(policy))
        return
    for attr_name in ("_extras", "_extra"):
        extras = getattr(event, attr_name, None)
        if isinstance(extras, dict):
            extras["astrmai_reply_shape_policy"] = dict(policy)
            return


def _normalize_utterance(text: str) -> str:
    normalized = re.sub(r"^\s*@\S+\s*", "", str(text or "").strip())
    return re.sub(r"\s+", "", normalized)


def _has_non_chat_payload(event: Any) -> bool:
    if any(_event_extra(event, key) for key in _NON_CHAT_EXTRA_KEYS):
        return True
    if bool(_event_extra(event, "astrmai_tool_clarification_needed", False)):
        return True
    if str(_event_extra(event, "astrmai_interaction_kind", "") or "").strip():
        return True
    return int(_event_extra(event, "astrmai_private_batch_message_count", 0) or 0) > 1


def _looks_like_micro_utterance(text: str) -> tuple[bool, str]:
    normalized = _normalize_utterance(text)
    if not normalized or "\n" in str(text or "") or len(normalized) > 12:
        return False, "not_short"
    if any(marker in normalized for marker in _QUESTION_MARKERS):
        return False, "question"
    if any(marker in normalized for marker in _ACTION_MARKERS):
        return False, "action_request"
    if normalized.isdigit():
        return False, "entity_like"
    if normalized in _MICRO_UTTERANCES:
        return True, "known_micro_utterance"
    if re.fullmatch(r"([\u4e00-\u9fffA-Za-z])\1{1,5}", normalized):
        return True, "repeated_interjection"
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", normalized) and len(normalized) <= 8:
        return True, "emoji_or_emoticon"
    return False, "not_micro"


def resolve_reply_shape_policy(event: Any, text: str, reply_config: Any) -> dict[str, Any]:
    enabled = bool(getattr(reply_config, "humanlike_short_reply_enabled", True))
    base_policy = {
        "enabled": enabled,
        "mode": "default",
        "reason": "disabled" if not enabled else "not_micro",
    }
    if not enabled:
        return base_policy
    if _has_non_chat_payload(event):
        return {**base_policy, "reason": "non_chat_payload"}

    is_micro, reason = _looks_like_micro_utterance(text)
    if not is_micro:
        return {**base_policy, "reason": reason}
    return {
        "enabled": True,
        "mode": "micro",
        "reason": reason,
        "max_chars": int(getattr(reply_config, "short_reply_max_chars", 80) or 80),
        "max_sentences": int(getattr(reply_config, "short_reply_max_sentences", 2) or 2),
        "allow_followup_question": bool(
            getattr(reply_config, "short_reply_allow_followup_question", False)
        ),
    }


def should_apply_micro_reply_postprocess(
    event: Any,
    policy: dict[str, Any],
    *,
    reply_mode: ReplyMode,
    is_proactive: bool,
) -> bool:
    if not bool(policy.get("enabled", True)):
        return False
    if is_proactive or str(policy.get("mode", "")) != "micro":
        return False
    if reply_mode == ReplyMode.EMOTIONAL_SUPPORT:
        return False
    return not _has_non_chat_payload(event)


__all__ = [
    "resolve_reply_shape_policy",
    "set_reply_shape_policy",
    "should_apply_micro_reply_postprocess",
]
