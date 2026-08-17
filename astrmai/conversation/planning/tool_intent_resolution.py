from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .tool_contracts import FAMILY_TO_TOOL
from .member_action_intent import detect_member_action_candidate


@dataclass(frozen=True, slots=True)
class ToolIntentResolution:
    family: str
    tool_name: str
    required_state: str
    missing_slots: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    clarification_prompt: str = ""

    @property
    def ready(self) -> bool:
        return self.required_state == "ready_required"


@dataclass(frozen=True, slots=True)
class ToolNeedResolution:
    family: str
    tool_name: str
    confidence: float
    reason: str


QQ_NUMBER_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")

_SEND_VERBS = (
    "发消息",
    "发个消息",
    "发一条消息",
    "私聊",
    "私信",
    "转告",
    "告诉",
    "带话",
    "问问",
    "问一下",
    "询问",
    "联系",
    "说一声",
)

_AMBIGUOUS_TARGETS = {
    "他",
    "她",
    "ta",
    "TA",
    "它",
    "对方",
    "那个人",
    "这个人",
    "朋友",
    "好友",
    "联系人",
}


_CAPABILITY_NEED_HINTS: dict[str, tuple[str, ...]] = {
    "group_member": ("群成员", "群名片", "管理员", "群主", "在群里吗", "是不是群员"),
    "group_fact": ("共同群", "哪些群", "哪个群", "群里见过", "同一个群", "群关系"),
    "forward_message": ("合并转发", "转发消息", "转发记录", "转发里", "聊天记录里"),
    "cross_reply": ("对方回了吗", "有没有回复", "他回了什么", "她回了什么", "跨会话回复"),
    "recent_contact": ("最近联系人", "最近联系", "联系过谁", "刚联系过", "最近聊过"),
    "message_recall": ("上一条消息", "刚才发的", "消息id", "消息 ID", "可撤回消息"),
    "message_artifact": ("消息附件", "那条消息", "刚才那条", "消息里的文件", "消息里的图片"),
    "topic_thread": ("刚才说的", "前面说的", "那个话题", "话题线索", "之前聊的"),
    "group_activity": ("群里刚才", "谁在聊天", "群活跃", "最近群消息", "群里发生了什么"),
    "route_suggest": ("该发哪里", "要不要私聊", "怎么联系", "路由建议", "在哪联系"),
    "persona_fact": ("有没有授权", "官方设定", "人格事实", "设定事实", "是否官方"),
    "self_lore": ("你的设定", "你的人设", "你的世界观", "你的经历", "你是谁"),
    "user_identity": ("我是谁", "他是谁", "她是谁", "这个人是谁", "用户身份", "昵称", "备注", "名字"),
    "friend_fact": ("好友", "朋友列表", "联系人", "是不是朋友", "是否为好友", "好友关系"),
}


def resolve_capability_need(
    message: str,
    *,
    available_tool_names: Iterable[str] = (),
) -> ToolNeedResolution | None:
    """Resolve a natural-language capability request without another LLM call."""
    text = _clean(message)
    if not text:
        return None
    lowered = text.lower()
    allowed = {
        str(name or "").strip()
        for name in available_tool_names
        if str(name or "").strip()
    }

    for family, tool_name in FAMILY_TO_TOOL.items():
        if allowed and tool_name not in allowed:
            continue
        if family.lower() in lowered or tool_name.lower() in lowered:
            return ToolNeedResolution(
                family=family,
                tool_name=tool_name,
                confidence=1.0,
                reason="explicit_capability_identifier",
            )

    best: ToolNeedResolution | None = None
    for family, hints in _CAPABILITY_NEED_HINTS.items():
        tool_name = FAMILY_TO_TOOL.get(family, "")
        if not tool_name or (allowed and tool_name not in allowed):
            continue
        matches = [hint for hint in hints if hint.lower() in lowered]
        if not matches:
            continue
        confidence = min(0.98, 0.86 + 0.04 * (len(matches) - 1))
        candidate = ToolNeedResolution(
            family=family,
            tool_name=tool_name,
            confidence=confidence,
            reason="natural_language_capability_match:" + ",".join(matches[:3]),
        )
        if best is None or candidate.confidence > best.confidence:
            best = candidate
    return best


def _clean(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _has_private_target(message: str) -> bool:
    text = _clean(message)
    if QQ_NUMBER_RE.search(text):
        return True
    for marker in ("给", "向", "跟"):
        if marker not in text:
            continue
        tail = text.split(marker, 1)[1].strip()
        if not tail:
            continue
        stop_positions = [
            pos
            for verb in _SEND_VERBS
            for pos in [tail.find(verb)]
            if pos >= 0
        ]
        target = tail[: min(stop_positions)].strip(" ，,。！？!~～") if stop_positions else tail[:20].strip()
        if target and target not in _AMBIGUOUS_TARGETS and len(target) <= 30:
            return True
    if "你好友" in text or "你的好友" in text:
        suffix = text.split("好友", 1)[1].strip(" ，,。！？!~～")
        if QQ_NUMBER_RE.search(suffix):
            return True
    return False


def _has_private_message_body(message: str) -> bool:
    text = _clean(message)
    if not text:
        return False
    for verb in _SEND_VERBS:
        if verb not in text:
            continue
        tail = text.split(verb, 1)[1].strip(" ，,。！？!~～")
        if len(tail) >= 2:
            return True
    if QQ_NUMBER_RE.search(text) and any(word in text for word in ("吃饭", "开心", "在吗", "睡了吗", "怎么了")):
        return True
    return False


def _resolve_private(message: str) -> ToolIntentResolution:
    missing: list[str] = []
    if not _has_private_target(message):
        missing.append("target_name")
    if not _has_private_message_body(message):
        missing.append("message")
    if not missing:
        return ToolIntentResolution(
            family="private",
            tool_name=FAMILY_TO_TOOL["private"],
            required_state="ready_required",
            reason="cross_session_slots_ready",
        )
    if missing == ["target_name"]:
        prompt = "你想让我发给谁？请给我对方的 QQ 号、好友备注或昵称。"
    elif missing == ["message"]:
        prompt = "你想让我转达什么内容？把要发给对方的话告诉我。"
    else:
        prompt = "你想让我发给谁、具体转达什么内容？请把目标和要说的话都告诉我。"
    return ToolIntentResolution(
        family="private",
        tool_name=FAMILY_TO_TOOL["private"],
        required_state="clarify_needed",
        missing_slots=tuple(missing),
        reason="cross_session_slots_missing",
        clarification_prompt=prompt,
    )


def _has_at_target(message: str) -> bool:
    text = _clean(message)
    candidate = detect_member_action_candidate(text)
    if candidate and candidate.proposed_purpose == "mention_member" and candidate.target_name:
        return True
    if QQ_NUMBER_RE.search(text):
        return True
    if "@" in text:
        tail = text.split("@", 1)[1].strip(" ，,。！？!~～")
        return bool(tail and tail not in _AMBIGUOUS_TARGETS)
    for marker in ("艾特", "@一下"):
        if marker in text:
            tail = text.split(marker, 1)[1].strip(" ，,。！？!~～")
            return bool(tail and tail not in _AMBIGUOUS_TARGETS)
    return False


def _simple_slot_resolution(
    family: str,
    message: str,
    *,
    required_tokens: tuple[str, ...],
    prompt: str,
) -> ToolIntentResolution:
    tool_name = FAMILY_TO_TOOL.get(family, "")
    text = _clean(message)
    ready = any(token and token in text for token in required_tokens)
    return ToolIntentResolution(
        family=family,
        tool_name=tool_name,
        required_state="ready_required" if ready else "clarify_needed",
        missing_slots=() if ready else ("target_or_content",),
        reason="simple_slots_ready" if ready else "simple_slots_missing",
        clarification_prompt="" if ready else prompt,
    )


def resolve_explicit_tool_intents(
    families: Iterable[str],
    *,
    message: str,
    available_tool_names: Iterable[str],
) -> list[ToolIntentResolution]:
    available = {str(name or "").strip() for name in available_tool_names}
    resolutions: list[ToolIntentResolution] = []
    for family in [str(item or "").strip() for item in families if str(item or "").strip()]:
        tool_name = FAMILY_TO_TOOL.get(family, "")
        if not tool_name or tool_name not in available:
            continue
        if family == "private":
            resolutions.append(_resolve_private(message))
        elif family == "at":
            ready = _has_at_target(message)
            resolutions.append(
                ToolIntentResolution(
                    family=family,
                    tool_name=tool_name,
                    required_state="ready_required" if ready else "clarify_needed",
                    missing_slots=() if ready else ("target_name",),
                    reason="at_target_ready" if ready else "at_target_missing",
                    clarification_prompt="" if ready else "你要我 @ 谁？请告诉我目标昵称或 QQ 号。",
                )
            )
        elif family == "memory_correction":
            ready = ("不是" in message or "记错" in message) and ("改成" in message or "应该是" in message or "正确" in message)
            resolutions.append(
                ToolIntentResolution(
                    family=family,
                    tool_name=tool_name,
                    required_state="ready_required" if ready else "clarify_needed",
                    missing_slots=() if ready else ("wrong_fact", "correct_fact"),
                    reason="memory_correction_slots_ready" if ready else "memory_correction_slots_missing",
                    clarification_prompt="" if ready else "你想纠正哪条记忆？正确内容是什么？",
                )
            )
        elif family in {"unverified_report", "persona_fact"}:
            resolutions.append(
                _simple_slot_resolution(
                    family,
                    message,
                    required_tokens=("听说", "据说", "有人说", "授权", "官方", "事实"),
                    prompt="你想让我核查或记录哪条说法？请把具体说法发给我。",
                )
            )
        elif family == "meme":
            resolutions.append(
                _simple_slot_resolution(
                    family,
                    message,
                    required_tokens=("开心", "难过", "生气", "表情", "表情包", "自定义"),
                    prompt="你想要哪类表情？可以告诉我是开心、吐槽、安慰还是其它情绪。",
                )
            )
        else:
            resolutions.append(
                ToolIntentResolution(
                    family=family,
                    tool_name=tool_name,
                    required_state="ready_required",
                    reason="no_slot_guard",
                )
            )
    return resolutions


def ready_families(resolutions: Iterable[ToolIntentResolution]) -> set[str]:
    return {item.family for item in resolutions if item.ready}


def clarification_resolutions(resolutions: Iterable[ToolIntentResolution]) -> list[ToolIntentResolution]:
    return [item for item in resolutions if item.required_state == "clarify_needed"]


__all__ = [
    "ToolNeedResolution",
    "ToolIntentResolution",
    "clarification_resolutions",
    "ready_families",
    "resolve_capability_need",
    "resolve_explicit_tool_intents",
]
