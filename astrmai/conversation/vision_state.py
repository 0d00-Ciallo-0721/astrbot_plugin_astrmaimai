from __future__ import annotations

import re
from typing import Any


_IMAGE_REFERENCE_RE = re.compile(
    r"(?:这|那|刚才|前面|上面)?(?:一)?(?:张|个)?(?:图(?:片)?|照片|截图|表情包|表情)"
    r"(?:里|中|上)?|(?:帮我)?(?:看|看看|看下|看一下|分析|识别|描述)"
    r".{0,8}(?:图(?:片)?|照片|截图|表情包|表情)",
    re.IGNORECASE,
)
_IMAGE_QUESTION_RE = re.compile(
    r"(?:什么|谁|哪|内容|意思|怎么样|好看|发生了什么|写了什么|是什么|干什么|在做什么)",
    re.IGNORECASE,
)
_IMAGE_INSPECTION_REQUEST_RE = re.compile(
    r"(?:(?:帮我|替我|给我).{0,4}(?:看|看看|看下|看一下|分析|识别|描述)"
    r"|(?:看看|看下|看一下|分析|识别|描述))"
    r".{0,16}(?:图(?:片)?|照片|截图|表情包|表情)",
    re.IGNORECASE,
)
_IMAGE_UNAVAILABLE_RE = re.compile(
    r"(?:(?:图(?:片)?|照片|截图|表情包|表情).{0,14}"
    r"(?:看不到|看不见|没看到|无法看到|看不清|加载|转圈|打不开|没显示|没有显示|不可用)"
    r"|(?:看不到|看不见|没看到|无法看到|看不清|加载|转圈|打不开|没显示|没有显示)"
    r".{0,14}(?:图(?:片)?|照片|截图|表情包|表情))",
    re.IGNORECASE,
)
_IMAGE_MENTION_RE = re.compile(r"(?:图(?:片)?|照片|截图|表情包|表情)", re.IGNORECASE)
_VISUAL_DEICTIC_RE = re.compile(
    r"(?:这(?:个|张|幅|里面|上面)?|那(?:个|张|幅|里面|上面)?|刚才(?:那个|那张)?|"
    r"前面(?:那个|那张)?|上面(?:那个|那张)?|图里|图片里|画面里|里面|它)"
    r".{0,18}(?:什么|谁|哪|怎么|为何|为什么|是不是|是否|好看|像|意思|情况|回事|评价|分析|看懂)",
    re.IGNORECASE,
)
_QUESTION_SIGNAL_RE = re.compile(
    r"(?:[?？]|什么|谁|哪|怎么|为何|为什么|是不是|是否|好看|像不像|意思|情况|回事|"
    r"评价|分析|看懂|觉得|感觉)",
    re.IGNORECASE,
)


def user_asked_about_image(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    reference = _IMAGE_REFERENCE_RE.search(normalized)
    if not reference:
        return False
    if _IMAGE_INSPECTION_REQUEST_RE.search(normalized):
        return True
    tail = normalized[max(0, reference.start() - 8) : reference.end() + 18]
    return bool(_IMAGE_QUESTION_RE.search(tail) or "?" in tail or "？" in tail)


def classify_autonomous_vision_need(
    text: str,
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
    *,
    explicit_request: bool = False,
) -> tuple[str, str]:
    """Classify whether the tool loop should inspect a recent image before replying."""

    safe_candidates = [item for item in candidates or [] if isinstance(item, dict)]
    if explicit_request:
        return "required", "explicit_image_request"
    if not safe_candidates:
        return "irrelevant", "no_image_candidate"

    normalized = str(text or "").strip()
    relations = {
        str(item.get("relation") or "recent").strip().lower()
        for item in safe_candidates
    }
    if "explicit_recent_reference" in relations:
        return "required", "explicit_recent_reference"
    if _VISUAL_DEICTIC_RE.search(normalized):
        return "required", "implicit_visual_reference"

    has_question = bool(_QUESTION_SIGNAL_RE.search(normalized))
    if has_question and relations.intersection({"current", "reply_target"}):
        return "required", "bound_image_question"
    if has_question and "same_sender_recent" in relations:
        return "optional", "same_sender_recent_question"
    if has_question:
        return "optional", "recent_image_question"
    return "irrelevant", "text_independent"


def select_autonomous_vision_candidate(
    candidates: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, Any]:
    safe_candidates = [dict(item) for item in candidates or [] if isinstance(item, dict)]
    if not safe_candidates:
        return {}
    relation_priority = {
        "explicit_recent_reference": 0,
        "reply_target": 1,
        "current": 2,
        "same_sender_recent": 3,
        "recent": 4,
    }

    def candidate_age(item: dict[str, Any]) -> float:
        try:
            return max(0.0, float(item.get("age_seconds") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    return min(
        safe_candidates,
        key=lambda item: (
            relation_priority.get(str(item.get("relation") or "recent").strip().lower(), 5),
            candidate_age(item),
        ),
    )


def derive_vision_state(*, raw_count: int, resolved_count: int) -> str:
    if resolved_count > 0:
        return "resolvable"
    if raw_count > 0:
        return "placeholder_only"
    return "none"


def vision_observation_facts(event: Any) -> dict[str, Any]:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return {}
    return {
        "vision_state": str(getter("astrmai_vision_state", "none") or "none"),
        "image_event_count": int(getter("astrmai_image_event_count", 0) or 0),
        "image_raw_component_count": int(getter("astrmai_image_raw_component_count", 0) or 0),
        "image_resolved_count": int(getter("astrmai_image_resolved_count", 0) or 0),
        "image_placeholder_count": int(getter("astrmai_image_placeholder_count", 0) or 0),
        "image_focus_reason": str(getter("astrmai_image_focus_reason", "") or ""),
        "image_focus_allowed": bool(getter("astrmai_image_focus_allowed", False)),
        "user_asked_about_image": bool(getter("astrmai_user_asked_about_image", False)),
    }


def has_valid_image_context(event: Any) -> bool:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return False
    if str(getter("astrmai_vision_state", "") or "") in {"analysis_ready", "cached_result"}:
        return True
    records = getter("astrmai_visual_records", []) or getter("astrmai_vision_records", []) or []
    if records:
        return True
    observation = getter("astrmai_vision_observation", {}) or getter("astrmai_vision_observability", {}) or {}
    return bool(
        isinstance(observation, dict)
        and int(observation.get("analyzed_count", 0) or 0) > 0
        and bool(observation.get("prompt_injected", False))
    )


def reply_mentions_unavailable_image(text: str) -> bool:
    return bool(_IMAGE_UNAVAILABLE_RE.search(str(text or "")))


def reply_mentions_image(text: str) -> bool:
    return bool(_IMAGE_MENTION_RE.search(str(text or "")))


def guard_unresolved_image_reply(
    text: str,
    *,
    user_text: str,
    has_valid_image_context: bool,
    enabled: bool,
) -> tuple[str, str, str]:
    original = str(text or "").strip()
    if not enabled or has_valid_image_context or not reply_mentions_unavailable_image(original):
        return original, "allowed", ""
    if user_asked_about_image(user_text):
        return original, "allowed", "explicit_image_question"

    parts = re.split(r"(?<=[。！？!?])|\n+", original)
    kept = [part.strip() for part in parts if part.strip() and not _IMAGE_UNAVAILABLE_RE.search(part)]
    repaired = "".join(kept).strip()
    if repaired:
        return repaired, "repaired", "unrequested_unresolved_image_claim"
    return "我在听，接着说吧。", "repaired", "unrequested_unresolved_image_claim"


__all__ = [
    "classify_autonomous_vision_need",
    "derive_vision_state",
    "guard_unresolved_image_reply",
    "has_valid_image_context",
    "reply_mentions_image",
    "reply_mentions_unavailable_image",
    "select_autonomous_vision_candidate",
    "user_asked_about_image",
    "vision_observation_facts",
]
