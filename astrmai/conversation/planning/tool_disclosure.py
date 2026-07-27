from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .tool_contracts import FAMILY_TO_TOOL, TOOL_CAPABILITIES


TOOL_PACKAGES: dict[str, tuple[str, ...]] = {
    "core": (
        "wait_and_listen",
        "omni_perception_query",
        "self_lore_query",
        "cross_chat_memory_query",
        "persona_fact_check_tool",
        "bot_capability_lookup",
    ),
    "identity": (
        "qq_user_identity_lookup",
        "qq_friend_lookup",
        "qq_group_member_lookup",
    ),
    "relationship": (
        "qq_friend_lookup",
        "qq_group_presence_lookup",
        "qq_recent_contact_lookup",
        "contact_route_suggest_tool",
    ),
    "artifact": (
        "qq_message_artifact_lookup",
        "qq_message_recall_lookup",
        "qq_forward_message_lookup",
        "vision_message_analyze_tool",
        "topic_thread_lookup",
    ),
    "cross_session": (
        "contact_route_suggest_tool",
        "qq_recent_contact_lookup",
        "qq_user_identity_lookup",
        "qq_friend_lookup",
        "space_transition_action",
        "cross_session_reply_lookup",
    ),
    "memory_governance": (
        "memory_write_correction_tool",
        "unverified_report_record_tool",
    ),
    "fun": (
        "proactive_meme",
        "custom_face_catalog_query",
        "qq_custom_face_send_tool",
        "message_reaction_action",
        "message_emoji_like_action",
        "proactive_like_action",
    ),
    "native_action": (
        "proactive_poke",
        "construct_at_event",
        "quote_reply_action",
        "group_sign_action",
    ),
    "conversation_control": (
        "wait_and_listen",
        "topic_hijack_action",
        "meme_resonance_action",
        "regret_and_withdraw_action",
    ),
}


PACKAGE_ORDER: tuple[str, ...] = (
    "core",
    "identity",
    "relationship",
    "artifact",
    "cross_session",
    "memory_governance",
    "fun",
    "native_action",
    "conversation_control",
)


FAMILY_TO_PACKAGES: dict[str, tuple[str, ...]] = {
    "wait": ("conversation_control",),
    "query": ("core",),
    "self_lore": ("core",),
    "friend_fact": ("identity", "relationship"),
    "group_member": ("identity",),
    "user_identity": ("identity",),
    "forward_message": ("artifact",),
    "group_fact": ("relationship",),
    "recent_contact": ("relationship", "cross_session"),
    "message_artifact": ("artifact",),
    "vision_message": ("artifact",),
    "cross_reply": ("cross_session",),
    "custom_face": ("fun",),
    # OPT-12/TL-08: quote_reply 属 PRECISION_ONLY（plan() 剔除包映射），此处映射
    # 为死配置且与 TOOL_PACKAGES 矛盾——引用能力只经 exact_tool_names 显式注入
    "message_recall": ("artifact",),
    "topic_thread": ("artifact", "conversation_control"),
    "capability": ("core",),
    "memory_correction": ("memory_governance",),
    "unverified_report": ("memory_governance",),
    "persona_fact": ("core",),
    "group_activity": ("relationship",),
    "route_suggest": ("relationship", "cross_session"),
    "cross_memory": ("core",),
    "at": ("native_action",),
    "poke": ("native_action",),
    "meme": ("fun",),
    "resonance": ("conversation_control",),
    "topic": ("conversation_control",),
    "private": ("cross_session",),
    "withdraw": ("conversation_control",),
    "reaction": ("fun",),
    "qq_reaction": ("fun",),
    "like": ("fun",),
    "qq_query": ("fun",),
    "sign": ("native_action",),
}


SECOND_PASS_ALLOWED_PACKAGES: tuple[str, ...] = (
    "identity",
    "relationship",
    "artifact",
    "memory_governance",
)


PRECISION_ONLY_FAMILIES: frozenset[str] = frozenset(
    {
        "custom_face",
        "quote_reply",
        "at",
        "poke",
        "meme",
        "resonance",
        "topic",
        "withdraw",
        "reaction",
        "qq_reaction",
        "like",
        "qq_query",
        "sign",
        "wait",
    }
)


@dataclass(frozen=True, slots=True)
class ToolDisclosurePlan:
    enabled: bool
    tier: str
    packages: tuple[str, ...]
    package_reasons: tuple[str, ...]
    tool_names: tuple[str, ...]
    second_pass_packages: tuple[str, ...]


def _ordered_unique(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return tuple(result)


def normalize_requested_packages(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raw_items = re.split(r"[,，\s]+", value)
    elif isinstance(value, Iterable):
        raw_items = list(value)
    else:
        raw_items = []
    return _ordered_unique(
        item
        for item in raw_items
        if str(item or "").strip() in TOOL_PACKAGES
    )


def package_names_for_families(families: Iterable[str]) -> tuple[str, ...]:
    packages: list[str] = []
    for family in families or []:
        packages.extend(FAMILY_TO_PACKAGES.get(str(family or "").strip(), ()))
    return _ordered_unique(package for package in packages if package in TOOL_PACKAGES)


def tool_names_for_packages(
    packages: Iterable[str],
    *,
    max_tools: int = 0,
) -> tuple[str, ...]:
    names: list[str] = []
    package_set = set(packages or [])
    for package in PACKAGE_ORDER:
        if package not in package_set:
            continue
        names.extend(TOOL_PACKAGES.get(package, ()))
    selected = _ordered_unique(names)
    if max_tools and max_tools > 0:
        return selected[:max_tools]
    return selected


def select_tools_by_names(
    tools: Iterable[Any],
    tool_names: Iterable[str],
    *,
    name_resolver: Callable[[Any], str] | None = None,
) -> list[Any]:
    wanted = set(_ordered_unique(tool_names))
    selected: list[Any] = []
    for tool in tools or []:
        raw_name = name_resolver(tool) if callable(name_resolver) else getattr(tool, "name", "")
        if str(raw_name or "").strip() in wanted:
            selected.append(tool)
    return selected


def select_tools_by_packages(
    tools: Iterable[Any],
    packages: Iterable[str],
    *,
    name_resolver: Callable[[Any], str] | None = None,
    max_tools: int = 0,
) -> list[Any]:
    tool_names = tool_names_for_packages(packages, max_tools=max_tools)
    return select_tools_by_names(tools, tool_names, name_resolver=name_resolver)


class ToolDisclosurePlanner:
    QQ_NUMBER_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")

    IDENTITY_KEYWORDS = (
        "我是谁",
        "他是谁",
        "她是谁",
        "这个人是谁",
        "身份",
        "昵称",
        "备注",
        "qq",
        "QQ",
    )
    RELATIONSHIP_KEYWORDS = (
        "好友",
        "朋友",
        "共同群",
        "哪个群",
        "别的群",
        "群里见过",
        "最近联系人",
        "联系过谁",
    )
    ARTIFACT_KEYWORDS = (
        "图片",
        "表情包",
        "文件",
        "转发",
        "聊天记录",
        "那条消息",
        "上一条",
        "刚才那条",
        "刚才那张",
        "引用",
        "那个呢",
    )
    CROSS_SESSION_KEYWORDS = (
        "发消息",
        "发个消息",
        "发一条消息",
        "私聊",
        "私信",
        "传话",
        "转告",
        "带话",
        "问问你好友",
        "问一下你好友",
        "对方回了吗",
        "有没有回复",
    )
    MEMORY_GOVERNANCE_KEYWORDS = (
        "你记错了",
        "不是这样",
        "改成",
        "纠正记忆",
        "听说",
        "据说",
        "有人说",
        "不确定",
        "未确认",
    )
    FUN_KEYWORDS = (
        "表情包",
        "自定义表情",
        "点赞",
        "夸夸我",
        "夸我",
        "发表情",
        "来个表情",
    )
    NATIVE_ACTION_KEYWORDS = (
        "戳一下",
        "戳一戳",
        "戳戳",
        "艾特",
        "帮我@",
        "@一下",
        "引用回复",
        "群签到",
        "群打卡",
    )
    CONTROL_KEYWORDS = (
        "撤回",
        "删掉上一条",
        "转移话题",
        "换个话题",
        "别聊这个",
        "复读",
        "原样复读",
        "先等等",
        "先别回复",
    )

    @staticmethod
    def _contains_any(message: str, keywords: Iterable[str]) -> bool:
        lowered = message.lower()
        return any(keyword in message or keyword.lower() in lowered for keyword in keywords)

    @staticmethod
    def _append_package(
        packages: list[str],
        reasons: list[str],
        package: str,
        reason: str,
    ) -> None:
        if package not in TOOL_PACKAGES or package in packages:
            return
        packages.append(package)
        reasons.append(f"{package}:{reason}")

    # G5/TL-01: 语义意图 → 只读查询包。复用 OPT-06 在 prompt_refiner 引入的同款
    # QueryIntentClassifier，保持"记忆检索门"与"工具披露"对身份类问句的判定一致。
    # 只映射"查得到答案"的意图：identity/location 对应 QQ 身份查询工具。
    # 刻意不映射 recent_reference（"你还记得我之前说的吗"）——那是记忆回想，
    # 由记忆注入链路（OPT-06）服务，并包联系人路由工具属于语义错配
    # （既有 test_agency_tier_none_and_social_intent_constrain_tools 抓出过此错）。
    _SEMANTIC_INTENT_PACKAGES = {
        "identity": "identity",
        "location": "identity",
    }

    @classmethod
    def _semantic_intent_packages(cls, text: str, existing: list[str]) -> list[str]:
        query = str(text or "").strip()
        if not query:
            return []
        try:
            from ...memory.services.memory_query_builder import QueryIntentClassifier

            _primary, intents, _confidence = QueryIntentClassifier().classify(query)
        except Exception:
            return []
        resolved: list[str] = []
        for intent in intents or []:
            package = cls._SEMANTIC_INTENT_PACKAGES.get(str(intent or "").strip())
            if package and package not in existing and package not in resolved:
                resolved.append(package)
        return resolved

    def plan(
        self,
        *,
        message: str,
        requested_tier: str,
        explicit_tool_intent: bool,
        explicit_tool_families: Iterable[str],
        social_intent: str = "",
        has_image: bool = False,
        has_forward: bool = False,
        has_reply: bool = False,
        requested_packages: Iterable[str] = (),
        max_chat_tools: int = 8,
        max_task_tools: int = 16,
    ) -> ToolDisclosurePlan:
        text = str(message or "").strip()
        packages: list[str] = []
        reasons: list[str] = []
        self._append_package(packages, reasons, "core", "default")

        explicit_families = tuple(str(family or "").strip() for family in (explicit_tool_families or []) if str(family or "").strip())
        exact_tool_names = [
            tool_name
            for family in explicit_families
            for tool_name in (FAMILY_TO_TOOL.get(family),)
            if tool_name
        ]
        package_families = [
            family
            for family in explicit_families
            if family not in PRECISION_ONLY_FAMILIES
        ]
        for package in package_names_for_families(package_families):
            self._append_package(packages, reasons, package, "explicit_family")

        for package in normalize_requested_packages(requested_packages):
            self._append_package(packages, reasons, package, "requested_second_pass")

        if has_image or self._contains_any(text, self.ARTIFACT_KEYWORDS):
            self._append_package(packages, reasons, "artifact", "message_artifact")
        if has_forward:
            self._append_package(packages, reasons, "artifact", "forward_message")
        if has_reply:
            self._append_package(packages, reasons, "artifact", "reply_message")
        if self.QQ_NUMBER_RE.search(text) or self._contains_any(text, self.IDENTITY_KEYWORDS):
            self._append_package(packages, reasons, "identity", "identity_signal")
        if self._contains_any(text, self.RELATIONSHIP_KEYWORDS):
            self._append_package(packages, reasons, "relationship", "relationship_signal")
        # G5/TL-01 后半：关键词未命中时用语义意图兜底并包只读查询工具。
        # 二段披露（模型自检调 bot_capability_lookup）16h 零触发，不能只靠它；
        # 这里让"我叫什么名字/我们是什么关系"这类问句即使不含关键词也拿到工具。
        for package in self._semantic_intent_packages(text, packages):
            self._append_package(packages, reasons, package, f"{package}_semantic_intent")
        if self._contains_any(text, self.CROSS_SESSION_KEYWORDS):
            self._append_package(packages, reasons, "cross_session", "cross_session_signal")
        if self._contains_any(text, self.MEMORY_GOVERNANCE_KEYWORDS):
            self._append_package(packages, reasons, "memory_governance", "memory_governance_signal")
        if str(social_intent or "").strip().lower() in {"comfort", "tease"}:
            self._append_package(packages, reasons, "fun", f"social_intent_{social_intent}")
        if not explicit_tool_intent and self._contains_any(text, self.FUN_KEYWORDS):
            self._append_package(packages, reasons, "fun", "fun_signal")
        if not explicit_tool_intent and self._contains_any(text, self.NATIVE_ACTION_KEYWORDS):
            self._append_package(packages, reasons, "native_action", "native_action_signal")
        if str(social_intent or "").strip().lower() == "redirect":
            self._append_package(packages, reasons, "conversation_control", "social_intent_redirect")
        if not explicit_tool_intent and self._contains_any(text, self.CONTROL_KEYWORDS):
            self._append_package(packages, reasons, "conversation_control", "control_signal")

        task_like = explicit_tool_intent or len(packages) > 1 or str(requested_tier or "").lower() in {"full", "sys3"}
        max_tools = max_task_tools if task_like else max_chat_tools
        selected_tool_names = _ordered_unique([*tool_names_for_packages(packages), *exact_tool_names])
        if max_tools and max_tools > 0:
            protected = _ordered_unique(exact_tool_names)
            selected_tool_names = _ordered_unique(
                [*selected_tool_names[:max_tools], *protected]
            )
        second_pass_packages = tuple(
            package
            for package in SECOND_PASS_ALLOWED_PACKAGES
            if package not in packages
        )
        return ToolDisclosurePlan(
            enabled=True,
            tier="task" if task_like else "chat",
            packages=tuple(packages),
            package_reasons=tuple(reasons),
            tool_names=selected_tool_names,
            second_pass_packages=second_pass_packages,
        )


__all__ = [
    "FAMILY_TO_PACKAGES",
    "PACKAGE_ORDER",
    "SECOND_PASS_ALLOWED_PACKAGES",
    "TOOL_PACKAGES",
    "ToolDisclosurePlan",
    "ToolDisclosurePlanner",
    "normalize_requested_packages",
    "package_names_for_families",
    "select_tools_by_names",
    "select_tools_by_packages",
    "tool_names_for_packages",
]
