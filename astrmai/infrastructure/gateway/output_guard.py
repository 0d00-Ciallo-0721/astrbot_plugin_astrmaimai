import json
import re
from typing import Iterable, List


PROVIDER_FAILURE_MARKERS = (
    "request_id",
    "request id",
    "status code",
    "http status",
    "http 状态码",
    "json 响应",
    "完整 api 响应",
    "完整api响应",
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
    "没有生成任何文本",
    "没有生成任何内容",
    "没有生成有效回复",
    "api 没有生成任何内容",
    "api 没有返回任何内容",
    "response:",
)

PROMPT_SCAFFOLD_MARKERS = (
    "[rollingsummary]",
    "较早对话摘要",
    "最近真实对话",
    "最近几轮对话",
    "本轮主线程",
    "请优先接住这条对话线索并回答",
    "优先处理这条消息",
    "本轮优先回应消息",
    "相关上下文",
    "和它直接相关的上下文",
    "同线程补充",
    "环境背景",
    "其他背景只作参考，不必逐条回应",
    "背景消息，仅供参考",
    "上一轮你的回复",
    "你上一句刚说过",
    "你的想法",
    "你的对话目标",
    "你此刻的直觉",
    "此刻你的直觉：",
    "这一轮你想达成：",
    "你脑海里闪过的想法：",
    "当前心情:",
    "当前状态：",
    "请顺着刚才的话继续回应，不要另起话题",
    "---对话记录---",
    "---眼前正在对我说的---",
    "---前因---",
    "---补充---",
    "---旁边在聊的---",
    "---记忆闪回",
    "---本轮指引---",
    "内心浮现的印象",
    "仅供我自己判断当下",
    "仅供内心参考",
    "不要出现在回复正文中",
    "印象结束",
    "绝不照搬原文",
    "主动记忆闪回",
    "记忆到此为止",
    "不会直接复述给对方",
    "记忆内容只帮我理解当下",
)

TOOL_PROTOCOL_MARKERS = (
    "wait_and_listen",
    "[system_wait_signal]",
    "[terminal_yield]",
    "请调用 wait_and_listen",
)

MOJIBAKE_MARKERS = (
    '鍥剧墖',
    '鏈疆',
    '鐜',
    '浣犵殑鎯虫硶',
    '瀵硅瘽鐩爣',
    '褰撳墠蹇冩儏',
)

ROLE_PREFIX_RE = re.compile(r"^(user|assistant|system)\s*:\s*", re.IGNORECASE)
TIME_PREFIX_RE = re.compile(r"^\[[0-2]?\d:[0-5]\d(?::[0-5]\d)?\]\s*")
SECTION_SEPARATOR_RE = re.compile(r"^---[^\r\n]{1,80}---$")
PUNCT_FRAGMENT_RE = re.compile(r"^[\s'\"`{}\[\]():,._-]+$")
REQUEST_ID_LINE_RE = re.compile(r"^\(?\s*request[_\s-]*id\s*[:：]", re.IGNORECASE)
STATUS_LINE_RE = re.compile(r"^\(?\s*(http\s*)?status\s*code\s*[:：]", re.IGNORECASE)
HTTP_STATUS_CN_RE = re.compile(r"^http\s*状态码\s*[:：]", re.IGNORECASE)
SAFETY_JSON_RE = re.compile(
    r"(finishreason|usagemetadata|prompttokencount|totaltokencount|safety_ratings)",
    re.IGNORECASE,
)
JSON_FRAGMENT_RE = re.compile(r"^[\[\{].*[\}\]]$", re.DOTALL)
SINGLE_LATIN_FRAGMENT_RE = re.compile(r"^[A-Za-z]$")


def normalize_guard_text(text: str) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text.replace("\ufeff", "").replace("\r\n", "\n").strip()


def _normalize_speaker_names(speaker_names: Iterable[str] | None) -> List[str]:
    names: List[str] = []
    for raw_name in speaker_names or []:
        name = str(raw_name or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _strip_named_speaker_prefix(line: str, speaker_names: Iterable[str] | None = None) -> str:
    cleaned = line.strip()
    for name in sorted(_normalize_speaker_names(speaker_names), key=len, reverse=True):
        if not name:
            continue
        match = re.match(rf"^{re.escape(name)}\s*[:：]\s*", cleaned)
        if match:
            return cleaned[match.end():].strip()
    return cleaned


def _strip_common_prefixes(line: str, speaker_names: Iterable[str] | None = None) -> str:
    cleaned = TIME_PREFIX_RE.sub("", line.strip())
    cleaned = ROLE_PREFIX_RE.sub("", cleaned)
    cleaned = _strip_named_speaker_prefix(cleaned, speaker_names)
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
    if JSON_FRAGMENT_RE.match(normalized):
        try:
            parsed = json.loads(normalized)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and any(key in parsed for key in ("candidates", "usageMetadata", "usage_metadata")):
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
    if any(marker.lower() in lowered for marker in MOJIBAKE_MARKERS):
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
    if SECTION_SEPARATOR_RE.match(stripped):
        return True
    lowered = stripped.lower()
    if REQUEST_ID_LINE_RE.match(stripped):
        return True
    if STATUS_LINE_RE.match(stripped) or HTTP_STATUS_CN_RE.match(stripped):
        return True
    if stripped in {"A", "a", "All", "'}", "\"}", "}", "]"}:
        return True
    if PUNCT_FRAGMENT_RE.match(stripped):
        return True
    if SINGLE_LATIN_FRAGMENT_RE.match(stripped):
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


def sanitize_visible_reply_text(text: str, fallback_text: str = "", speaker_names: Iterable[str] | None = None) -> str:
    normalized = normalize_guard_text(text)
    if not normalized:
        return ""
    if looks_like_provider_failure_text(normalized) or looks_like_tool_protocol_text(normalized):
        return fallback_text.strip()

    lines: List[str] = []
    for raw_line in normalized.splitlines():
        raw_cleaned = TIME_PREFIX_RE.sub("", raw_line.strip())
        role_match = ROLE_PREFIX_RE.match(raw_cleaned)
        if role_match and role_match.group(1).lower() in {"user", "system"}:
            continue
        cleaned = _strip_common_prefixes(raw_line, speaker_names)
        if is_noise_line(cleaned):
            continue
        lines.append(cleaned)

    candidate = "\n".join(line for line in lines if line).strip()
    if candidate:
        if looks_like_provider_failure_text(candidate) or looks_like_tool_protocol_text(candidate):
            return fallback_text.strip()
        return candidate

    if looks_like_prompt_scaffold_text(normalized):
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
