import json
import re
from typing import List


PROVIDER_FAILURE_MARKERS = (
    "request_id",
    "request id",
    "status code",
    "http 状态码",
    "http status",
    "json 响应",
    "完整 api 响应",
    "usagemetadata",
    "prompttokencount",
    "totaltokencount",
    "finishreason",
    "safety_ratings",
    "safetyratings",
    "safety filter",
    "安全过滤",
    "安全限制",
    "内容可能已被过滤",
    "被安全过滤器拦截",
    "模型没有生成任何内容",
    "没有生成任何文本",
    "没有生成任何内容",
    "没有生成有效回复",
    "api 没有生成任何内容",
    "api 没有返回任何内容",
    "response:",
)

PROMPT_SCAFFOLD_MARKERS = (
    "[rollingsummary]",
    "[本轮主线程",
    "本轮主线程（优先围绕它回答）",
    "[本轮必须优先回应的消息",
    "本轮优先回应消息",
    "[与焦点直接相关的上下文",
    "相关上下文：",
    "[同线程补充消息",
    "同线程补充：",
    "[环境背景",
    "环境背景，仅供参考：",
    "环境背景消息，仅供参考：",
    "[上一轮你的回复",
    "上一轮你的回复：",
)

TOOL_PROTOCOL_MARKERS = (
    "wait_and_listen",
    "[system_wait_signal]",
    "[terminal_yield]",
    "请调用 wait_and_listen",
)

ROLE_PREFIX_RE = re.compile(r"^(user|assistant|system)\s*:\s*", re.IGNORECASE)
TIME_PREFIX_RE = re.compile(r"^\[[0-2]?\d:[0-5]\d(?::[0-5]\d)?\]\s*")
PUNCT_FRAGMENT_RE = re.compile(r"^[\s'\"`{}\[\]():,._-]+$")
REQUEST_ID_LINE_RE = re.compile(r"^\(?\s*request[_\s-]*id\s*[:：]", re.IGNORECASE)
STATUS_LINE_RE = re.compile(r"^\(?\s*(http\s*)?status\s*code\s*[:：]", re.IGNORECASE)
HTTP_STATUS_CN_RE = re.compile(r"^http\s*状态码\s*[:：]", re.IGNORECASE)
SAFETY_JSON_RE = re.compile(r"(finishreason|usagemetadata|prompttokencount|totaltokencount|safety_ratings)", re.IGNORECASE)


def normalize_guard_text(text: str) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text.replace("\ufeff", "").replace("\r\n", "\n").strip()


def _strip_common_prefixes(line: str) -> str:
    cleaned = TIME_PREFIX_RE.sub("", line.strip())
    cleaned = ROLE_PREFIX_RE.sub("", cleaned)
    return cleaned.strip()


def looks_like_provider_failure_text(text: str) -> bool:
    normalized = normalize_guard_text(text)
    if not normalized:
        return False
    lowered = normalized.lower()
    if any(marker in lowered for marker in PROVIDER_FAILURE_MARKERS):
        return True
    if SAFETY_JSON_RE.search(lowered):
        return True
    if normalized.startswith("{") or normalized.startswith("["):
        try:
            parsed = json.loads(normalized)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            if any(key in parsed for key in ("candidates", "usageMetadata", "usage_metadata")):
                return True
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            if any(key in parsed[0] for key in ("finishReason", "safetyRatings", "usageMetadata")):
                return True
    return False


def looks_like_prompt_scaffold_text(text: str) -> bool:
    normalized = normalize_guard_text(text)
    if not normalized:
        return False
    lowered = normalized.lower()
    if any(marker in lowered for marker in PROMPT_SCAFFOLD_MARKERS):
        return True
    return bool(ROLE_PREFIX_RE.match(normalized))


def looks_like_tool_protocol_text(text: str) -> bool:
    lowered = normalize_guard_text(text).lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in TOOL_PROTOCOL_MARKERS)


def is_noise_line(line: str) -> bool:
    stripped = normalize_guard_text(line)
    if not stripped:
        return True
    lowered = stripped.lower()
    if REQUEST_ID_LINE_RE.match(stripped):
        return True
    if STATUS_LINE_RE.match(stripped) or HTTP_STATUS_CN_RE.match(stripped):
        return True
    if stripped in {"A", "All", "'}", "\"}", "}", "]"}:
        return True
    if PUNCT_FRAGMENT_RE.match(stripped):
        return True
    if looks_like_provider_failure_text(stripped):
        return True
    if looks_like_prompt_scaffold_text(stripped):
        return True
    if looks_like_tool_protocol_text(stripped):
        return True
    if lowered.startswith("原因可能是：") or lowered.startswith("详细内容:"):
        return True
    return False


def sanitize_visible_reply_text(text: str, fallback_text: str = "") -> str:
    normalized = normalize_guard_text(text)
    if not normalized:
        return ""
    lines: List[str] = []
    for raw_line in normalized.splitlines():
        raw_cleaned = TIME_PREFIX_RE.sub("", raw_line.strip())
        role_match = ROLE_PREFIX_RE.match(raw_cleaned)
        if role_match and role_match.group(1).lower() in {"user", "system"}:
            continue
        cleaned = _strip_common_prefixes(raw_line)
        if is_noise_line(cleaned):
            continue
        lines.append(cleaned)
    candidate = "\n".join(line for line in lines if line).strip()
    if candidate:
        if looks_like_provider_failure_text(candidate) or looks_like_tool_protocol_text(candidate):
            return fallback_text.strip()
        return candidate
    if looks_like_provider_failure_text(normalized) or looks_like_tool_protocol_text(normalized):
        return fallback_text.strip()
    return ""


def is_safe_visible_text(text: str) -> bool:
    sanitized = sanitize_visible_reply_text(text, "")
    return bool(sanitized)


def is_sendable_segment(text: str) -> bool:
    normalized = normalize_guard_text(text)
    if not normalized:
        return False
    if is_noise_line(normalized):
        return False
    sanitized = sanitize_visible_reply_text(normalized, "")
    return bool(sanitized)
