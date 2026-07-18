from __future__ import annotations

import re
from dataclasses import asdict, dataclass


_TRIM_CHARS = " \t\r\n，,。！？!?：:；;~～\"'“”‘’【】[]()（）"
_AMBIGUOUS_TARGETS = {"他", "她", "它", "ta", "TA", "对方", "那个人", "这个人", "谁", "人"}
_TARGET_PREFIXES = (
    "之前和你聊天的",
    "之前跟你聊天的",
    "之前聊天的",
    "刚才和你聊天的",
    "刚才跟你聊天的",
    "刚才聊天的",
    "群里的那个",
    "群里的",
    "那个叫",
    "那个",
)


@dataclass(frozen=True, slots=True)
class MemberActionCandidate:
    proposed_purpose: str
    target_name: str
    strong_explicit: bool
    pattern: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _clean_target(value: str) -> str:
    target = " ".join(str(value or "").strip(_TRIM_CHARS).split())
    for prefix in _TARGET_PREFIXES:
        if target.startswith(prefix):
            target = target[len(prefix):].strip(_TRIM_CHARS)
            break
    target = re.sub(r"^(?:名叫|叫做|昵称是)", "", target).strip(_TRIM_CHARS)
    target = re.split(r"(?:，|,|。|！|!|？|\?|然后|并且|顺便)", target, maxsplit=1)[0].strip(_TRIM_CHARS)
    if not target or target in _AMBIGUOUS_TARGETS or len(target) > 40:
        return ""
    return target


def detect_member_action_candidate(message: str) -> MemberActionCandidate | None:
    text = " ".join(str(message or "").strip().split())
    if not text:
        return None

    discussion_patterns = (
        r"^(?:别|不要|不用|不必)(?:再)?(?:叫|喊|艾特|@)",
        r"^(?:为什么|为啥|怎么)(?:要|又要|还要)?(?:叫|喊|艾特|@)",
        r"^(?:我没|不是我)(?:叫|喊|艾特|@)",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in discussion_patterns):
        return MemberActionCandidate("discuss_member", "", False, "discussion_guard")

    lookup_patterns = (
        (r"^(?P<target>.{1,40}?)(?:还|现在)?在(?:当前|这个)?群里吗[？?]?$", "member_presence"),
        (r"^(?:当前|这个)?群里(?:有没有|有没)(?P<target>.{1,40}?)[？?]?$", "group_has_member"),
    )
    for pattern, name in lookup_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            target = _clean_target(match.group("target"))
            if target:
                return MemberActionCandidate("lookup_member", target, True, name)

    command_patterns = (
        (
            r"^(?:麻烦你|请你|帮我|替我)?(?:再)?(?:帮我)?把(?P<target>.{1,40}?)(?:叫|喊)(?:一下|一声)?(?:出来|过来|来|冒个泡|冒泡)[吧呀啊！!。.]?$",
            "ba_call_out",
            True,
        ),
        (
            r"^(?:麻烦你|请你|帮我|替我)?(?:再)?(?:帮我)?让(?P<target>.{1,40}?)(?:出来|过来|来)(?:冒个泡|冒泡)?[吧呀啊！!。.]?$",
            "let_call_out",
            True,
        ),
        (
            r"^(?P<prefix>(?:麻烦你|请你|帮我|替我|再帮我|麻烦|请)?)(?:叫|喊|呼叫)(?:一下|一声)?(?P<target>.{1,40}?)(?:出来|过来|来|冒个泡|冒泡)?[吧呀啊！!。.]?$",
            "direct_call",
            True,
        ),
        (
            r"^(?:麻烦你|麻烦|请你|请|帮我|替我|再帮我)?(?:艾特|@一下)(?P<target>.{1,40}?)[吧呀啊！!。.]?$",
            "explicit_at",
            True,
        ),
        (
            r"^(?:麻烦你|请你|帮我|替我|再帮我)@(?P<target>[^\s，,。！？!?]{1,40})[吧呀啊！!。.]?$",
            "explicit_raw_at",
            True,
        ),
    )
    for pattern, name, always_strong in command_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        target = _clean_target(match.group("target"))
        if not target:
            continue
        prefix = str(match.groupdict().get("prefix", "") or "")
        strong = always_strong or bool(prefix) or "出来" in text or "冒泡" in text
        return MemberActionCandidate("mention_member", target, strong, name)
    return None


def normalize_member_action_purpose(value: object) -> str:
    purpose = str(value or "").strip().lower()
    aliases = {
        "mention": "mention_member",
        "at": "mention_member",
        "call": "mention_member",
        "lookup": "lookup_member",
        "presence": "lookup_member",
        "discuss": "discuss_member",
        "unknown": "unclear",
        "ambiguous": "unclear",
    }
    purpose = aliases.get(purpose, purpose)
    if purpose not in {"mention_member", "lookup_member", "discuss_member", "unclear"}:
        return ""
    return purpose


__all__ = [
    "MemberActionCandidate",
    "detect_member_action_candidate",
    "normalize_member_action_purpose",
]
