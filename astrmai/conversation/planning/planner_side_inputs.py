from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import asdict
from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..contracts.turn_context import ensure_turn_context
from ...infrastructure.runtime.lane_manager import LaneKey
from ...shared.emotion_tags import build_emotion_tag_catalog
from ..contracts.prompt_envelope import PromptEnvelope
from .tool_contracts import (
    AUTONOMOUS_INTERACTION_TOOLS,
    build_explicit_invocation_plans,
    filter_tools_for_context,
    is_model_disclosure_requestable,
    normalize_tool_schemas,
    publish_invocation_plans,
    tool_display_name,
)
from .tool_disclosure import (
    DEFAULT_VISIBLE_TOOL_NAMES,
    ToolDisclosurePlanner,
    select_tools_by_names,
)
from .entity_domain_resolution import (
    EntityDomain,
    build_tool_intent_contracts,
    resolve_entity_domain,
)
from .tool_intent_resolution import (
    ToolIntentResolution,
    clarification_resolutions,
    ready_families,
    resolve_explicit_tool_intents,
)
from .member_action_intent import detect_member_action_candidate, normalize_member_action_purpose
from .tools.pfc_tools import (
    BotCapabilityLookupTool,
    ContactRouteSuggestTool,
    CrossChatMemoryQueryTool,
    CrossSessionReplyLookupTool,
    ConstructAtEventTool,
    GroupActivitySnapshotTool,
    LearnedLanguageLookupTool,
    MemeResonanceTool,
    MemoryWriteCorrectionTool,
    MessageEmojiLikeTool,
    MessageReactionTool,
    OmniPerceptionTool,
    PersonaFactCheckTool,
    ProactiveLikeTool,
    ProactiveMemeTool,
    ProactivePokeTool,
    QQFriendLookupTool,
    QQForwardMessageLookupTool,
    QQGroupMemberLookupTool,
    QQGroupPresenceLookupTool,
    QQMessageArtifactLookupTool,
    QQMessageRecallLookupTool,
    QQRecentContactLookupTool,
    QQUserIdentityLookupTool,
    QuoteReplyActionTool,
    RegretAndWithdrawTool,
    SelfLoreQueryTool,
    SpaceTransitionTool,
    TopicHijackTool,
    TopicThreadLookupTool,
    UnverifiedReportRecordTool,
    VisionMessageAnalyzeTool,
    WaitTool,
)

try:
    from .tools.pfc_tools import prepare_explicit_tool_fallbacks
except ImportError:  # Compatibility with lightweight host/test tool modules.
    async def prepare_explicit_tool_fallbacks(*args, **kwargs):
        del args, kwargs
        return []


class PlannerSideInputMixin:
    PENDING_MEME_INTENT_TTL_SECONDS = 60.0
    MEME_EMOTION_HINTS = {
        "happy": ("开心", "高兴", "快乐", "喜悦", "兴奋"),
        "sad": ("难过", "伤心", "悲伤", "委屈"),
        "angry": ("生气", "愤怒", "恼火", "暴躁"),
        "surprised": ("惊讶", "震惊", "吃惊"),
        "cute": ("可爱", "卖萌", "萌"),
        "comfort": ("安慰", "鼓励", "加油"),
    }
    FOLLOW_UP_ALLOWED_INTENTS = {"comfort", "tease", "inquire", "answer"}
    FOLLOW_UP_COOLDOWN_SECONDS = {
        "comfort": 300.0,
        "tease": 600.0,
        "inquire": 600.0,
        "answer": 600.0,
    }
    FOLLOW_UP_STANCE_MULTIPLIERS = {
        "guarded": 0.35,
        "cool": 0.60,
    }
    TOOL_INTENT_KEYWORDS = {
        "查一下",
        "搜一下",
        "帮我看看",
        "帮我查",
        "你还记得",
        "你记得",
        "好友",
        "共同群",
        "别的群",
        "哪个群",
        "最近联系人",
        "那条消息",
        "那张图",
        "图片",
        "文件",
        "授权",
        "撤回",
        "转移话题",
        "换个话题",
        "look up",
        "search",
        "check",
        "withdraw",
        "do you remember",
        "still remember",
        "change topic",
        "switch topic",
    }
    QQ_ACTION_INTENT_KEYWORDS = {
        "poke": ("戳一下", "戳我一下", "戳一戳", "戳戳", "poke"),
        "at": ("艾特", "帮我@", "@一下"),
        "qq_reaction": ("消息表情", "表情回应", "加个表情", "给这条消息点赞", "点赞这条消息"),
        "withdraw": ("撤回", "删掉上一条", "撤回上一条", "withdraw", "delete last reply", "remove last reply"),
        "meme": ("发个表情包", "发表情包", "来个表情包"),
        "quote_reply": ("引用回复", "引用那条", "回这条消息"),
    }
    GENERAL_EXPLICIT_TOOL_KEYWORDS = {
        "wait": ("先别回复", "先等等", "等一下再说", "wait a moment"),
        "query": ("查一下", "搜一下", "帮我看看", "帮我查", "你还记得", "你记得", "do you remember"),
        "self_lore": ("你的设定", "你的人设", "你的世界观", "你的经历", "你是谁"),
        "friend_fact": ("好友", "朋友列表", "联系人", "是不是你好友", "是否为好友"),
        "group_fact": ("共同群", "别的群", "哪个群", "群里见过", "所在群", "棉花娃娃群"),
        "recent_contact": ("最近联系人", "刚才谁", "最近联系", "联系过谁", "跨会话"),
        "message_artifact": ("那条消息", "刚才那条", "那张图", "这张图", "图片", "文件", "转发消息"),
        "group_member": ("群成员", "群名片", "管理员", "在群里吗", "群里有没有"),
        "user_identity": ("他是谁", "我是谁", "这个人是谁", "身份", "昵称", "备注"),
        "forward_message": ("合并转发", "转发消息", "聊天记录", "转发里"),
        "vision_message": (
            "看图", "这张图", "那张图", "刚才那张", "前面那张", "图里", "图片里",
            "照片里", "截图里", "这个表情", "帮我看", "看一下", "表情包什么意思", "图片是什么意思",
        ),
        "cross_reply": ("对方回了吗", "有没有回复", "他回了什么", "跨会话回复"),
        "quote_reply": ("引用回复", "回这条", "引用那条"),
        "message_recall": ("上一条消息", "刚才发的", "可撤回", "消息id"),
        "topic_thread": ("刚才说的", "那个", "这件事", "话题线索"),
        "capability": ("你能做什么", "你有什么工具", "能不能查", "工具列表"),
        "memory_correction": ("你记错了", "不是这样", "改成", "纠正记忆"),
        # OPT-12/TL-06: 旧触发词（听说/据说/有人说/不确定）过于日常，普通闲聊会被
        # 升级 task tier 并强制 required 工具轮；收紧为明确的"登记/记录"组合意图
        "unverified_report": ("记录一下听说", "帮我记下传闻", "登记未核实", "记录未确认", "备案传闻"),
        "persona_fact": ("你有没有授权", "你的设定", "官方", "人格事实"),
        "group_activity": ("群里刚才", "谁在聊天", "群活跃", "最近群消息"),
        "route_suggest": ("该发哪里", "要不要私聊", "怎么联系", "路由建议"),
        "cross_memory": ("别的群见过", "跨群记忆", "你的记忆", "你记不记得", "授权", "官方周边"),
        "resonance": ("复读这句", "跟着复读", "原样复读"),
        "topic": ("转移话题", "换个话题", "别聊这个了", "change topic", "switch topic"),
        "private": (
            "转到私聊",
            "私聊说",
            "去私聊",
            "发私聊",
            "发私信",
            "传话",
            "转告",
            "带话",
            "talk in private",
            "send a private message",
        ),
        "reaction": ("用文字回应", "用语气回应"),
        "like": ("夸夸我", "表扬我", "夸我一下"),
    }
    POKE_INTENT_KEYWORDS = {
        "戳一下",
        "戳我一下",
        "戳一戳",
        "戳戳",
        "poke",
    }
    AT_INTENT_KEYWORDS = {
        "艾特",
        "帮我@",
        "@一下",
    }
    CHAT_TOOL_NAMES = {
        "proactive_meme",
        "message_emoji_like_action",
        "vision_message_analyze_tool",
        "quote_reply_action",
        "regret_and_withdraw_action",
    }
    GUARDED_CHAT_TOOL_NAMES = {
        "proactive_poke",
        "construct_at_event",
    }
    FULL_ONLY_TOOL_NAMES = {
        "wait_and_listen",
        "omni_perception_query",
        "self_lore_query",
        "topic_hijack_action",
        "space_transition_action",
        "meme_resonance_action",
    }
    QQ_NATIVE_TOOL_NAMES = {
        "proactive_poke",
        "construct_at_event",
        "message_emoji_like_action",
        "regret_and_withdraw_action",
        "quote_reply_action",
    }
    TOOL_NAME_ALIASES = {
        "WaitTool": "wait_and_listen",
        "OmniPerceptionTool": "omni_perception_query",
        "SelfLoreQueryTool": "self_lore_query",
        "QQFriendLookupTool": "qq_friend_lookup",
        "QQGroupMemberLookupTool": "qq_group_member_lookup",
        "QQUserIdentityLookupTool": "qq_user_identity_lookup",
        "QQForwardMessageLookupTool": "qq_forward_message_lookup",
        "QQGroupPresenceLookupTool": "qq_group_presence_lookup",
        "QQRecentContactLookupTool": "qq_recent_contact_lookup",
        "QQMessageArtifactLookupTool": "qq_message_artifact_lookup",
        "VisionMessageAnalyzeTool": "vision_message_analyze_tool",
        "CrossSessionReplyLookupTool": "cross_session_reply_lookup",
        "QuoteReplyActionTool": "quote_reply_action",
        "QQMessageRecallLookupTool": "qq_message_recall_lookup",
        "TopicThreadLookupTool": "topic_thread_lookup",
        "BotCapabilityLookupTool": "bot_capability_lookup",
        "MemoryWriteCorrectionTool": "memory_write_correction_tool",
        "UnverifiedReportRecordTool": "unverified_report_record_tool",
        "PersonaFactCheckTool": "persona_fact_check_tool",
        "GroupActivitySnapshotTool": "group_activity_snapshot_tool",
        "ContactRouteSuggestTool": "contact_route_suggest_tool",
        "CrossChatMemoryQueryTool": "cross_chat_memory_query",
        "ConstructAtEventTool": "construct_at_event",
        "ProactivePokeTool": "proactive_poke",
        "ProactiveMemeTool": "proactive_meme",
        "MemeResonanceTool": "meme_resonance_action",
        "TopicHijackTool": "topic_hijack_action",
        "SpaceTransitionTool": "space_transition_action",
        "RegretAndWithdrawTool": "regret_and_withdraw_action",
        "MessageReactionTool": "message_reaction_action",
        "MessageEmojiLikeTool": "message_emoji_like_action",
        "ProactiveLikeTool": "proactive_like_action",
        "LearnedLanguageLookupTool": "learned_language_lookup",
    }
    TOOL_FAMILIES = {
        "wait_and_listen": {"wait"},
        "omni_perception_query": {"query"},
        "self_lore_query": {"self_lore", "query"},
        "learned_language_lookup": {"learned_language", "query"},
        "qq_friend_lookup": {"friend_fact", "query"},
        "qq_group_member_lookup": {"group_member", "query"},
        "qq_user_identity_lookup": {"user_identity", "query"},
        "qq_forward_message_lookup": {"forward_message", "message_artifact", "query"},
        "qq_group_presence_lookup": {"group_fact", "query"},
        "qq_recent_contact_lookup": {"recent_contact", "query"},
        "qq_message_artifact_lookup": {"message_artifact", "query"},
        "vision_message_analyze_tool": {"vision_message", "message_artifact", "query"},
        "cross_session_reply_lookup": {"cross_reply", "recent_contact", "query"},
        "quote_reply_action": {"quote_reply"},
        "qq_message_recall_lookup": {"message_recall", "message_artifact", "query"},
        "topic_thread_lookup": {"topic_thread", "query"},
        "bot_capability_lookup": {"capability", "query"},
        "memory_write_correction_tool": {"memory_correction", "query"},
        "unverified_report_record_tool": {"unverified_report", "query"},
        "persona_fact_check_tool": {"persona_fact", "self_lore", "query"},
        "group_activity_snapshot_tool": {"group_activity", "query"},
        "contact_route_suggest_tool": {"route_suggest", "query"},
        "cross_chat_memory_query": {"cross_memory", "query"},
        "construct_at_event": {"at"},
        "proactive_poke": {"poke"},
        "proactive_meme": {"meme"},
        "meme_resonance_action": {"resonance"},
        "topic_hijack_action": {"topic"},
        "space_transition_action": {"private"},
        "regret_and_withdraw_action": {"withdraw"},
        "message_reaction_action": {"reaction"},
        "message_emoji_like_action": {"qq_reaction"},
        "proactive_like_action": {"reaction", "like"},
    }
    MODE_INSTRUCTION_MAX_CHARS = 240
    PRIVATE_JUMP_CONTEXT_MAX_CHARS = 360
    PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS = 480
    PRIVATE_JUMP_MAX_HISTORY_MESSAGES = 2
    PRIVATE_JUMP_MAX_MESSAGE_CHARS = 72
    PRIVATE_JUMP_MAX_PRIVATE_MESSAGE_CHARS = 90
    MODE_INSTRUCTION_MAX_LINES = 3

    @staticmethod
    def _truncate_runtime_instruction_text(text: str, limit: int) -> str:
        cleaned = " ".join(str(text or "").split())
        budget = max(0, int(limit or 0))
        if not cleaned or budget <= 0:
            return ""
        if len(cleaned) <= budget:
            return cleaned
        if budget <= 3:
            return cleaned[:budget]
        return cleaned[: budget - 3].rstrip() + "..."

    @classmethod
    def _clamp_runtime_instruction_block(cls, prompt_envelope: PromptEnvelope | None) -> None:
        if not isinstance(prompt_envelope, PromptEnvelope):
            return
        text = str(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or "").strip()
        prompt_envelope.planner_runtime_instruction_block = cls._truncate_runtime_instruction_text(
            text,
            cls.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,
        )

    @staticmethod
    def _planner_side_input_text(
        prompt_envelope: PromptEnvelope,
        window_lines: List[str],
        *,
        recent_only: bool = False,
    ) -> str:
        if isinstance(prompt_envelope, PromptEnvelope):
            sections = [
                prompt_envelope.focus_message_text,
                prompt_envelope.direct_context_text,
                prompt_envelope.related_context_text,
                prompt_envelope.ambient_background_text,
            ]
            text = "\n".join(section for section in sections if str(section or "").strip()).strip()
            if text:
                return text
        lines = window_lines[-3:] if recent_only and window_lines else window_lines
        return "\n".join(line for line in lines if str(line or "").strip())

    async def _load_planning_side_inputs(self, chat_id: str, prompt_envelope: PromptEnvelope, window_lines: List[str], is_fast_mode: bool):
        if is_fast_mode:
            return {
                # Deprecated compatibility mirrors for legacy readers outside the main reply chain.
                "slang_context": "",
                "goal_text": "",
                "expression_habits": "",
                "jargon_explanation": "",
                "stable_expression_habits": "",
                "situational_style_cues": "",
                "stable_jargon_explanation": "",
                "planner_reasoning": "",
            }

        async def _load_slang():
            return await self.evolution_manager.get_active_patterns_canonical_async(chat_id)

        async def _load_goals():
            window_text = self._planner_side_input_text(prompt_envelope, window_lines)
            result = await self.goal_manager.analyze_and_update(chat_id, window_text)
            logger.debug(f"[{chat_id}] 当前主目标: {result}")
            return result

        async def _load_expressions():
            recent_text = self._planner_side_input_text(prompt_envelope, window_lines, recent_only=True)
            think_level = 1 if len(recent_text) >= 40 and len(window_lines) >= 2 else 0
            return await self.expression_selector.select(
                chat_id=chat_id,
                context_text=recent_text,
                think_level=think_level,
                shared_scope=chat_id,
            )

        async def _load_jargons():
            return ""

        slang_context, expression_habits, jargon_explanation = await asyncio.gather(
            _load_slang(),
            _load_expressions(),
            _load_jargons(),
            return_exceptions=True,
        )
        goal_text = await _load_goals()
        return {
            "slang_context": slang_context,
            "goal_text": goal_text,
            "expression_habits": expression_habits,
            "jargon_explanation": jargon_explanation,
            "stable_expression_habits": expression_habits,
            "situational_style_cues": slang_context,
            "stable_jargon_explanation": jargon_explanation,
            "planner_reasoning": goal_text,
        }

    def _has_tool_intent(self, event: AstrMessageEvent) -> bool:
        msg = self._tool_intent_text(event)
        if not msg:
            return False
        lowered = msg.lower()
        if any(keyword in msg or keyword in lowered for keyword in self.TOOL_INTENT_KEYWORDS):
            return True
        return bool(self._explicit_tool_families(event))

    def _explicit_tool_families(self, event: AstrMessageEvent) -> set[str]:
        message = self._tool_intent_text(event)
        if not message:
            return set()
        lowered = message.lower()
        families = self._explicit_qq_action_families(event)
        member_families = self._member_action_families(event, message)
        member_candidate = event.get_extra("astrmai_member_action_candidate", {}) if hasattr(event, "get_extra") else {}
        member_purpose = str(event.get_extra("astrmai_member_action_effective_purpose", "") or "") if hasattr(event, "get_extra") else ""
        if member_candidate and member_purpose in {"discuss_member", "unclear"}:
            families.discard("at")
        families.update(member_families)
        families.update(
            family
            for family, keywords in self.GENERAL_EXPLICIT_TOOL_KEYWORDS.items()
            if any(keyword in message or keyword in lowered for keyword in keywords)
        )
        if self._looks_like_cross_session_relay_request(message):
            families.add("private")
        return families

    @staticmethod
    def _raw_tool_intent_text(event: AstrMessageEvent) -> str:
        current = str(getattr(event, "message_str", "") or "").strip()
        if not hasattr(event, "get_extra"):
            return current
        merged = str(event.get_extra("astrmai_private_batch_text", "") or "").strip()
        if not merged:
            merged = str(event.get_extra("astrmai_rich_text", "") or "").strip()
        if merged and current and current not in merged:
            return f"{merged}\n{current}".strip()
        return merged or current

    @classmethod
    def _tool_intent_text(cls, event: AstrMessageEvent) -> str:
        if hasattr(event, "get_extra"):
            prepared = str(event.get_extra("astrmai_tool_intent_text", "") or "").strip()
            if prepared:
                return prepared
        return cls._raw_tool_intent_text(event)

    @classmethod
    def _meme_emotion_from_text(cls, message: str) -> str:
        text = str(message or "").strip().lower()
        for tag, hints in cls.MEME_EMOTION_HINTS.items():
            if tag in text or any(hint in text for hint in hints):
                return tag
        return ""

    @classmethod
    def _extract_meme_topic(cls, message: str) -> str:
        topic = " ".join(str(message or "").strip().split())
        topic = re.sub(r"^(?:请|麻烦)?(?:你)?(?:给我)?(?:发|来|整)(?:一个|个|一张|张)?", "", topic)
        topic = re.sub(r"(?:给我|给一下|一下|一个|一张|张)$", "", topic)
        topic = re.sub(r"(?:表情包|表情图|梗图|能表示|用来表示|表达|的图|图片)", " ", topic)
        topic = topic.replace("给我", " ")
        for hints in cls.MEME_EMOTION_HINTS.values():
            for hint in hints:
                topic = topic.replace(hint, " ")
        return " ".join(topic.strip(" ，,。！？!~～的").split())[:80]

    @classmethod
    def _looks_like_meme_supplement(cls, message: str) -> bool:
        text = str(message or "").strip(" ，,。！？!~～")
        if not text or len(text) > 24 or cls._looks_like_meme_request(text):
            return False
        return bool(cls._meme_emotion_from_text(text)) and bool(
            re.fullmatch(r"[\u4e00-\u9fffA-Za-z_-]{1,12}(?:一点|点)?(?:的)?", text)
        )

    def _prepare_pending_meme_intent(
        self,
        chat_id: str,
        user_id,
        event: AstrMessageEvent,
    ) -> str:
        text = self._raw_tool_intent_text(event)
        now = time.monotonic()
        store = getattr(self, "_pending_explicit_meme_intents", None)
        if not isinstance(store, dict):
            store = {}
            setattr(self, "_pending_explicit_meme_intents", store)
        for stale_key, payload in list(store.items()):
            if float(payload.get("expires_at", 0.0) or 0.0) <= now:
                store.pop(stale_key, None)

        key = (str(chat_id or ""), str(user_id or ""))
        pending = store.get(key)
        if pending and self._looks_like_meme_supplement(text):
            text = f"{pending.get('text', '')}\n{text}".strip()
            store.pop(key, None)
            event.set_extra("astrmai_pending_meme_intent_inherited", True)
            event.set_extra("astrmai_pending_meme_intent_consumed", True)

        if self._looks_like_meme_request(text):
            intent = {
                "action": "meme",
                "topic": self._extract_meme_topic(text),
                "emotion": self._meme_emotion_from_text(text),
                "text": text,
                "chat_id": key[0],
                "sender_id": key[1],
                "created_at": now,
                "expires_at": now + self.PENDING_MEME_INTENT_TTL_SECONDS,
            }
            store[key] = intent
            event.set_extra("astrmai_pending_meme_intent", dict(intent))
            event.set_extra(
                "astrmai_meme_intent",
                {name: intent[name] for name in ("action", "topic", "emotion")},
            )
        return text

    def _consume_pending_meme_intent(self, chat_id: str, user_id, event: AstrMessageEvent) -> None:
        store = getattr(self, "_pending_explicit_meme_intents", None)
        if isinstance(store, dict):
            store.pop((str(chat_id or ""), str(user_id or "")), None)
        event.set_extra("astrmai_pending_meme_intent_consumed", True)

    @staticmethod
    def _member_action_families(event: AstrMessageEvent, message: str) -> set[str]:
        candidate = detect_member_action_candidate(message)
        if candidate is None:
            return set()
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_member_action_candidate", candidate.to_dict())

        confirmed = normalize_member_action_purpose(
            event.get_extra("astrmai_member_action_purpose", "") if hasattr(event, "get_extra") else ""
        )
        if confirmed:
            purpose = confirmed
            source = "cognitive_confirmation"
        elif candidate.strong_explicit:
            purpose = candidate.proposed_purpose
            source = "strong_explicit_fallback"
        elif candidate.proposed_purpose == "lookup_member":
            purpose = "lookup_member"
            source = "deterministic_lookup"
        else:
            purpose = "unclear"
            source = "soft_candidate_unconfirmed"

        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_member_action_effective_purpose", purpose)
            event.set_extra("astrmai_member_action_effective_target", candidate.target_name)
            event.set_extra("astrmai_member_action_resolution_source", source)
        if purpose == "mention_member" and candidate.target_name:
            return {"at"}
        if purpose == "lookup_member" and candidate.target_name:
            return {"group_member"}
        return set()

    @staticmethod
    def _looks_like_cross_session_relay_request(message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        patterns = (
            r"^(?:(?:帮我|替我|麻烦你|请你|你去|去)[，, ]*)?(?:给|向|跟).{1,40}?(?:发(?:个|一条)?消息|问问|问一下|说一声|说|告诉|转告|带话)",
            r"^(?:帮我|替我|麻烦你|请你|你去|去)[，, ]*(?:问问|问一下|询问|联系)(?:你(?:的)?好友|好友|朋友|联系人|\d{5,12}|(?!我(?:们|的)?|你).{1,20})",
            r"^(?:帮我|替我|麻烦你|请你)[，, ]*(?:告诉|转告)(?!我(?:们|的)?|你).{1,30}",
        )
        return any(re.search(pattern, text) for pattern in patterns)

    def _explicit_qq_action_families(self, event: AstrMessageEvent) -> set[str]:
        message = self._tool_intent_text(event)
        if not message:
            return set()
        lowered = message.lower()
        families = {
            family
            for family, keywords in self.QQ_ACTION_INTENT_KEYWORDS.items()
            if any(keyword in message or keyword in lowered for keyword in keywords)
        }
        if self._looks_like_meme_request(message):
            families.add("meme")
        return families

    @staticmethod
    def _looks_like_meme_request(message: str) -> bool:
        text = " ".join(str(message or "").strip().split())
        if not text:
            return False
        action_requested = any(
            token in text
            for token in (
                "发",
                "来一个",
                "来个",
                "给我",
                "整一个",
                "整张",
                "想要",
                "要一个",
                "要张",
            )
        )
        if not action_requested:
            return False
        if any(token in text for token in ("表情包", "表情图", "梗图")):
            return True
        return bool(re.search(r"(?:能|可以|用来)?(?:表示|表达).{1,24}(?:的)?(?:图|图片)", text))

    def _conversation_flag(self, name: str, default: bool) -> bool:
        config = getattr(self.gateway, "config", None)
        conversation = getattr(config, "conversation", None)
        return bool(getattr(conversation, name, default))

    def _conversation_int(self, name: str, default: int) -> int:
        config = getattr(self.gateway, "config", None)
        conversation = getattr(config, "conversation", None)
        try:
            return int(getattr(conversation, name, default) or default)
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _event_components(event: AstrMessageEvent) -> list:
        message_obj = getattr(event, "message_obj", None)
        components = getattr(message_obj, "message", None)
        return list(components or []) if isinstance(components, list) else []

    def _event_has_component_hint(self, event: AstrMessageEvent, hints: tuple[str, ...]) -> bool:
        for component in self._event_components(event):
            class_name = component.__class__.__name__.lower()
            type_name = str(getattr(component, "type", "") or "").lower()
            text = f"{class_name} {type_name}"
            if any(hint in text for hint in hints):
                return True
        return False

    def _has_poke_intent(self, message: str) -> bool:
        if not message:
            return False
        lowered = message.lower()
        return any(keyword in message or keyword in lowered for keyword in self.POKE_INTENT_KEYWORDS)

    def _has_at_intent(self, message: str) -> bool:
        if not message:
            return False
        candidate = detect_member_action_candidate(message)
        return bool(
            any(keyword in message for keyword in self.AT_INTENT_KEYWORDS)
            or (candidate and candidate.proposed_purpose == "mention_member")
        )

    def _has_guarded_chat_intent(self, event: AstrMessageEvent) -> bool:
        msg = str(getattr(event, "message_str", "") or "").strip()
        return self._has_poke_intent(msg) or self._has_at_intent(msg)

    @staticmethod
    def _set_tool_tier(event: AstrMessageEvent, tier: str) -> None:
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_tool_tier", tier)
            ensure_turn_context(event).tools.final_tier = str(tier or "")
        else:
            setattr(event, "astrmai_tool_tier", tier)

    def _emotion_mapping_for_meme_tool(self) -> list:
        return build_emotion_tag_catalog(self.reply_engine.config).mapping_entries()

    def _build_full_pfc_tools(self, chat_id: str, user_id, sender_name: str, event: AstrMessageEvent | None = None):
        target_persona_id = getattr(self.gateway.config.persona, "persona_id", "") if hasattr(self.gateway.config, "persona") else ""
        memory_tool_service = getattr(self.memory_engine, "tool_service", None)
        tools = [
            WaitTool(),
            SelfLoreQueryTool(
                memory_engine=self.memory_engine,
                memory_tool_service=memory_tool_service,
                persona_id=target_persona_id,
            ),
            LearnedLanguageLookupTool(memory_engine=self.memory_engine, chat_id=chat_id),
            QQFriendLookupTool(),
            QQGroupMemberLookupTool(),
            QQUserIdentityLookupTool(),
            QQForwardMessageLookupTool(),
            QQGroupPresenceLookupTool(),
            QQRecentContactLookupTool(),
            QQMessageArtifactLookupTool(),
            VisionMessageAnalyzeTool(
                db_service=self.context_engine.db,
                visual_cortex=getattr(self, "visual_cortex", None),
                image_resolver=getattr(self, "image_resolver", None),
            ),
            CrossSessionReplyLookupTool(
                db_service=self.context_engine.db,
                history_service=getattr(self, "conversation_history_service", None),
            ),
            QuoteReplyActionTool(),
            QQMessageRecallLookupTool(),
            TopicThreadLookupTool(history_service=getattr(self, "conversation_history_service", None)),
            BotCapabilityLookupTool(),
            MemoryWriteCorrectionTool(memory_engine=self.memory_engine),
            UnverifiedReportRecordTool(memory_engine=self.memory_engine),
            PersonaFactCheckTool(
                memory_engine=self.memory_engine,
                memory_tool_service=memory_tool_service,
                persona_id=target_persona_id,
            ),
            GroupActivitySnapshotTool(history_service=getattr(self, "conversation_history_service", None)),
            ContactRouteSuggestTool(db_service=self.context_engine.db),
            CrossChatMemoryQueryTool(
                memory_engine=self.memory_engine,
                memory_tool_service=memory_tool_service,
                persona_id=target_persona_id,
            ),
            OmniPerceptionTool(
                memory_engine=self.memory_engine,
                memory_tool_service=memory_tool_service,
                db_service=self.context_engine.db,
                chat_id=chat_id,
                current_sender_id=str(user_id) if user_id is not None else "",
                current_sender_name=sender_name,
            ),
            ConstructAtEventTool(db_service=self.context_engine.db),
            ProactivePokeTool(db_service=self.context_engine.db),
            ProactiveMemeTool(emotion_mapping=self._emotion_mapping_for_meme_tool()),
            MemeResonanceTool(),
            TopicHijackTool(),
            SpaceTransitionTool(
                db_service=self.context_engine.db,
                handoff_store=getattr(self, "cross_session_handoff_store", None),
                runtime_coordinator=getattr(self, "runtime_coordinator", None),
            ),
            RegretAndWithdrawTool(),
            MessageReactionTool(),
            MessageEmojiLikeTool(),
            ProactiveLikeTool(db_service=self.context_engine.db),
        ]
        if not self._qq_native_tools_available(event):
            tools = [tool for tool in tools if self._canonical_tool_name(tool) not in self.QQ_NATIVE_TOOL_NAMES]
        return tools

    def _build_chat_tools(self, event: AstrMessageEvent):
        tools = [
            VisionMessageAnalyzeTool(
                db_service=self.context_engine.db,
                visual_cortex=getattr(self, "visual_cortex", None),
                image_resolver=getattr(self, "image_resolver", None),
            ),
            QuoteReplyActionTool(),
            RegretAndWithdrawTool(),
            ProactiveMemeTool(emotion_mapping=self._emotion_mapping_for_meme_tool()),
            MessageEmojiLikeTool(),
            ProactivePokeTool(db_service=self.context_engine.db),
            ConstructAtEventTool(db_service=self.context_engine.db),
        ]
        if self._conversation_flag("autonomous_chat_tools_enabled", True):
            target_persona_id = getattr(self.gateway.config.persona, "persona_id", "") if hasattr(self.gateway.config, "persona") else ""
            memory_tool_service = getattr(self.memory_engine, "tool_service", None)
            tools = [
                QQFriendLookupTool(),
                QQGroupMemberLookupTool(),
                QQUserIdentityLookupTool(),
                QQForwardMessageLookupTool(),
                QQGroupPresenceLookupTool(),
                QQRecentContactLookupTool(),
                QQMessageArtifactLookupTool(),
                LearnedLanguageLookupTool(
                    memory_engine=self.memory_engine,
                    chat_id=str(getattr(event, "unified_msg_origin", "") or ""),
                ),
                CrossSessionReplyLookupTool(
                    db_service=self.context_engine.db,
                    history_service=getattr(self, "conversation_history_service", None),
                ),
                QQMessageRecallLookupTool(),
                TopicThreadLookupTool(history_service=getattr(self, "conversation_history_service", None)),
                BotCapabilityLookupTool(),
                MemoryWriteCorrectionTool(memory_engine=self.memory_engine),
                UnverifiedReportRecordTool(memory_engine=self.memory_engine),
                PersonaFactCheckTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    persona_id=target_persona_id,
                ),
                GroupActivitySnapshotTool(history_service=getattr(self, "conversation_history_service", None)),
                ContactRouteSuggestTool(db_service=self.context_engine.db),
                CrossChatMemoryQueryTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    persona_id=target_persona_id,
                ),
                MessageReactionTool(),
                ProactiveLikeTool(db_service=self.context_engine.db),
                SpaceTransitionTool(
                    db_service=self.context_engine.db,
                    handoff_store=getattr(self, "cross_session_handoff_store", None),
                    runtime_coordinator=getattr(self, "runtime_coordinator", None),
                ),
                *tools,
            ]
        if not self._qq_native_tools_available(event):
            tools = [tool for tool in tools if self._canonical_tool_name(tool) not in self.QQ_NATIVE_TOOL_NAMES]
        return tools

    def _qq_native_tools_available(self, event: AstrMessageEvent | None) -> bool:
        if not self._conversation_flag("qq_native_tools_enabled", True) or not self._conversation_flag(
            "qq_deferred_action_commit_enabled",
            True,
        ):
            return False
        if event is None or not hasattr(event, "bot"):
            return True
        api = getattr(getattr(event, "bot", None), "api", None)
        return callable(getattr(api, "call_action", None))

    @classmethod
    def _canonical_tool_name(cls, tool: object) -> str:
        raw_name = getattr(tool, "name", "")
        if not raw_name and isinstance(tool, str):
            raw_name = tool
        name = str(raw_name or "").strip()
        return cls.TOOL_NAME_ALIASES.get(name, name)

    @staticmethod
    def _event_string_list(event: AstrMessageEvent, key: str) -> list[str]:
        value = event.get_extra(key, []) if hasattr(event, "get_extra") else []
        if isinstance(value, str):
            raw_items = re.split(r"[,，、\s]+", value)
        elif isinstance(value, list):
            raw_items = value
        else:
            raw_items = []
        items: list[str] = []
        for item in raw_items:
            text = str(item or "").strip().lower()
            if text and text not in items:
                items.append(text)
        return items

    def _persona_entity_catalog_text(self) -> str:
        summarizer = getattr(self.context_engine, "summarizer", None)
        cache = getattr(summarizer, "cache", {})
        if not isinstance(cache, dict):
            return ""
        parts: list[str] = []
        for payload in cache.values():
            if not isinstance(payload, dict):
                continue
            for key in ("summary", "first_person_rewrite", "style", "raw_persona"):
                value = str(payload.get(key, "") or "").strip()
                if value:
                    parts.append(value)
            shards = payload.get("shards", {})
            if isinstance(shards, dict):
                parts.extend(
                    str(value or "").strip()
                    for value in shards.values()
                    if str(value or "").strip()
                )
        return "\n".join(parts)[:40000]

    def _families_for_social_intent(self, event: AstrMessageEvent, social_intent: str) -> set[str] | None:
        if not social_intent:
            return None
        if social_intent == "comfort":
            return {"reaction", "qq_reaction", "like"}
        if social_intent == "tease":
            families = {"meme", "reaction", "qq_reaction", "like"}
            if self._has_poke_intent(str(getattr(event, "message_str", "") or "")):
                families.add("poke")
            if self._has_at_intent(str(getattr(event, "message_str", "") or "")):
                families.add("at")
            return families
        if social_intent in {"pushback", "boundary", "observe"}:
            return set()
        if social_intent in {"inquire", "recall"}:
            return {"query"}
        if social_intent == "redirect":
            return {"topic"}
        return None

    def _filter_tools_by_families(self, tools: list, allowed_families: set[str] | None) -> list:
        if allowed_families is None:
            return tools
        if not allowed_families:
            return []
        filtered = []
        for tool in tools:
            tool_name = self._canonical_tool_name(tool)
            families = self.TOOL_FAMILIES.get(tool_name, set())
            if families & allowed_families:
                filtered.append(tool)
        return filtered

    async def _build_execution_tools(
        self,
        chat_id: str,
        event: AstrMessageEvent,
        user_id,
        sender_name: str,
        ctx,
        *,
        is_all_mode: bool,
        is_fast_mode: bool,
        is_tool_call_mode: bool,
        tool_state=None,
    ):
        if is_tool_call_mode:
            sys3_light_tools = (await self.sys3_router.get_light_tools_for_planner()).tools
            # ponytail: M5 — seed seen_names with built-in tool names to prevent SubAgent collision
            seen_names: set[str] = {
                self._canonical_tool_name(WaitTool()),
                self._canonical_tool_name(OmniPerceptionTool(memory_engine=None, memory_tool_service=None, db_service=None, chat_id="", current_sender_id="", current_sender_name="")),
                self._canonical_tool_name(SelfLoreQueryTool(memory_engine=None, memory_tool_service=None, persona_id="")),
                self._canonical_tool_name(LearnedLanguageLookupTool(memory_engine=None, chat_id="")),
                self._canonical_tool_name(QQFriendLookupTool()),
                self._canonical_tool_name(QQGroupPresenceLookupTool()),
                self._canonical_tool_name(QQRecentContactLookupTool()),
                self._canonical_tool_name(QQMessageArtifactLookupTool()),
                self._canonical_tool_name(CrossChatMemoryQueryTool(memory_engine=None, memory_tool_service=None, persona_id="")),
            }
            deduped_sys3_tools: list = []
            for tool in sys3_light_tools:
                name = getattr(tool, "name", "")
                if name in seen_names:
                    logger.warning(f"[Planner] duplicate tool name '{name}' in sys3 tools, skipped")
                    continue
                seen_names.add(name)
                deduped_sys3_tools.append(tool)
            target_persona_id = getattr(self.gateway.config.persona, "persona_id", "") if hasattr(self.gateway.config, "persona") else ""
            memory_tool_service = getattr(self.memory_engine, "tool_service", None)
            self._set_disable_rag_injection(ctx, True)
            self._set_tool_tier(event, "sys3")
            tools = [
                WaitTool(),
                OmniPerceptionTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    db_service=self.context_engine.db,
                    chat_id=chat_id,
                    current_sender_id=str(user_id) if user_id is not None else "",
                    current_sender_name=sender_name,
                ),
                SelfLoreQueryTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    persona_id=target_persona_id,
                ),
                LearnedLanguageLookupTool(memory_engine=self.memory_engine, chat_id=chat_id),
                QQFriendLookupTool(),
                QQGroupPresenceLookupTool(),
                QQRecentContactLookupTool(),
                QQMessageArtifactLookupTool(),
                CrossChatMemoryQueryTool(
                    memory_engine=self.memory_engine,
                    memory_tool_service=memory_tool_service,
                    persona_id=target_persona_id,
                ),
            ] + deduped_sys3_tools
            turn_tools = ensure_turn_context(event).tools
            tool_names = [
                self._canonical_tool_name(tool)
                for tool in tools or []
                if self._canonical_tool_name(tool)
            ]
            turn_tools.requested_tier = "sys3"
            turn_tools.final_tier = "sys3"
            turn_tools.explicit_tool_intent = True
            turn_tools.initial_tools = list(tool_names)
            turn_tools.available_tools = list(tool_names)
            turn_tools.family_filtered_tools = list(tool_names)
            turn_tools.filtered_tools = list(tool_names)
            logger.info(f"[{chat_id}] [TOOL_CALL 模式] 加载 Sys3 SubAgent 索引，工具总数: {len(tools)}")
            return tools

        if is_all_mode or is_fast_mode:
            self._set_disable_rag_injection(ctx, True)
        else:
            self._set_disable_rag_injection(ctx, False)

        tool_intent_text = self._prepare_pending_meme_intent(chat_id, user_id, event)
        event.set_extra("astrmai_tool_intent_text", tool_intent_text)
        explicit_qq_families = self._explicit_qq_action_families(event)
        explicit_tool_families = self._explicit_tool_families(event)
        persona_catalog_text = self._persona_entity_catalog_text()
        entity_domain, _, _, entity_reason = resolve_entity_domain(
            tool_intent_text,
            explicit_families=explicit_tool_families,
            persona_text=persona_catalog_text,
        )
        if entity_domain == EntityDomain.PERSONA_LORE:
            explicit_tool_families.discard("friend_fact")
            explicit_tool_families.discard("query")
            explicit_tool_families.add("self_lore")
        elif entity_domain == EntityDomain.PLATFORM_FRIEND:
            explicit_tool_families.discard("self_lore")
            explicit_tool_families.discard("query")
            explicit_tool_families.add("friend_fact")
        explicit_override_enabled = self._conversation_flag("qq_explicit_intent_override_enabled", True)
        explicit_tool_intent = bool(explicit_tool_families) or self._has_tool_intent(event)
        requested_tier = str(event.get_extra("astrmai_action_tier", "") if hasattr(event, "get_extra") else "").strip().lower()
        social_intent = str(event.get_extra("astrmai_social_intent", "") if hasattr(event, "get_extra") else "").strip().lower()
        stance = str(event.get_extra("astrmai_stance", "") if hasattr(event, "get_extra") else "").strip().lower()
        think_level = None
        if hasattr(event, "get_extra"):
            try:
                think_level = int(event.get_extra("astrmai_think_level", None))
            except (TypeError, ValueError):
                think_level = None
        turn_tools = ensure_turn_context(event).tools
        turn_tools.requested_tier = requested_tier
        turn_tools.explicit_tool_intent = bool(explicit_tool_intent)
        turn_tools.social_intent = social_intent
        turn_tools.filter_steps = []
        turn_tools.filter_reasons = []
        turn_tools.removed_by_energy = []
        turn_tools.removed_by_mood = []
        turn_tools.removed_by_hostility = []
        turn_tools.removed_by_cooldown = []
        turn_tools.removed_by_caution = []
        turn_tools.removed_by_social_intent = []
        turn_tools.removed_by_stance = []
        turn_tools.disclosure_enabled = False
        turn_tools.disclosure_tier = ""
        turn_tools.disclosure_packages = []
        turn_tools.disclosure_reasons = []
        turn_tools.disclosure_second_pass_packages = []
        turn_tools.disclosure_expanded_packages = []
        turn_tools.disclosure_decisions = []
        turn_tools.preselected_tools = []
        turn_tools.hidden_requestable_tools = []
        turn_tools.disclosure_request_source = ""
        turn_tools.disclosure_requested_tools = []
        turn_tools.disclosure_rejected_requests = []
        turn_tools.second_pass_added_tools = []
        turn_tools.second_pass_tool_executed = False
        turn_tools.intent_contracts = []
        turn_tools.contract_outcomes = []
        turn_tools.contract_unsatisfied = []
        turn_tools.correction_pass_used = False
        turn_tools.correction_packages = []
        turn_tools.correction_reason = ""
        if entity_domain != EntityDomain.UNKNOWN:
            turn_tools.record_step(
                "planner.entity_domain_resolution",
                [],
                [entity_domain.value],
                entity_reason,
                category="tool_domain",
            )
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            if requested_tier in {"full", "sys3"} or explicit_tool_intent:
                turn_tools.record_step(
                    "planner.proactive_tool_guard",
                    [requested_tier or "tool_intent"],
                    ["chat"],
                    "proactive_event_blocks_heavy_tools",
                    category="social_intent",
                )
            explicit_tool_intent = False
            turn_tools.explicit_tool_intent = False
            if requested_tier != "none":
                requested_tier = "chat"
                turn_tools.requested_tier = "chat"
        allowed_families = set(self._event_string_list(event, "astrmai_allowed_action_families"))
        intent_families = self._families_for_social_intent(event, social_intent)
        if intent_families is not None:
            allowed_families = (allowed_families & intent_families) if allowed_families else intent_families
        explicit_filter_families = set(explicit_tool_families)
        if explicit_override_enabled and explicit_filter_families:
            allowed_families.update(explicit_filter_families)
        turn_tools.allowed_families = sorted(allowed_families if allowed_families else (intent_families or set()))

        state = None
        profile = None
        relationship_vec = None
        if tool_state is not None:
            if isinstance(tool_state, dict):
                state = tool_state.get("state")
                profile = tool_state.get("profile")
                relationship_vec = tool_state.get("relationship_vec")
            else:
                state = getattr(tool_state, "state", None)
                profile = getattr(tool_state, "profile", None)
                relationship_vec = getattr(tool_state, "relationship_vec", None)
        elif self.state_engine:
            try:
                state = await self.state_engine.get_state(chat_id)
            except Exception:
                pass
            if user_id:
                try:
                    profile = await self.state_engine.get_user_profile(str(user_id))
                except Exception:
                    pass
                if hasattr(self.state_engine, "relationship_engine"):
                    relationship_vec = self.state_engine.relationship_engine.get_or_create(str(user_id))

        if social_intent in {"pushback", "boundary", "observe"} and requested_tier not in {"full", "sys3"}:
            turn_tools.record_step(
                "planner.tier_guard",
                [],
                [],
                f"social_intent({social_intent})_forces_none",
                category="social_intent",
            )
            requested_tier = "none"
        if think_level is not None and think_level < 3 and requested_tier == "full" and not explicit_tool_intent:
            reason = f"think_level_{think_level}_prevents_full_tier"
            turn_tools.record_step(
                "planner.think_level_guard",
                ["full_tier"],
                ["chat_tier"],
                reason,
            )
            requested_tier = "chat"
        if requested_tier == "full" and not explicit_tool_intent:
            try:
                energy = float(getattr(state, "energy", 0.6) if state is not None else 0.6)
            except (TypeError, ValueError):
                energy = 0.6
            if energy < 0.25:
                reason = f"low_energy_tier_downgrade({energy:.2f})"
                turn_tools.record_step(
                    "planner.tier_state_guard",
                    ["full_tier"],
                    ["chat_tier"],
                    reason,
                    category="energy",
                )
                requested_tier = "chat"

        progressive_enabled = self._conversation_flag("tool_progressive_disclosure_enabled", True)
        disclosure_plan = None
        if requested_tier == "none":
            self._set_tool_tier(event, "none")
            tools = []
            setattr(event, "_astrmai_disclosure_hidden_tools", [])
            turn_tools.record_step(
                "planner.tier_select",
                [],
                [],
                "requested_tier_none",
                category="social_intent" if social_intent in {"pushback", "boundary", "observe"} else "",
            )
        elif progressive_enabled:
            self._set_tool_tier(event, "full" if requested_tier == "full" or explicit_tool_intent else "chat")
            candidate_tools = self._build_full_pfc_tools(chat_id, user_id, sender_name, event)
            candidate_tools = filter_tools_for_context(
                candidate_tools,
                is_group=bool(event.get_group_id()),
                name_resolver=self._canonical_tool_name,
            )
            if not explicit_tool_intent and not self._conversation_flag("autonomous_chat_tools_enabled", True):
                candidate_tools = [
                    tool
                    for tool in candidate_tools
                    if self._canonical_tool_name(tool) not in AUTONOMOUS_INTERACTION_TOOLS
                    or self._canonical_tool_name(tool) in DEFAULT_VISIBLE_TOOL_NAMES
                ]
            has_image = bool(
                event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))
                or event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", []))
                or (
                    self._conversation_flag("autonomous_vision_tool_enabled", True)
                    and event.get_extra("astrmai_recent_media_candidates", [])
                )
            ) or self._event_has_component_hint(event, ("image",))
            has_forward = self._event_has_component_hint(event, ("forward", "node"))
            has_reply = self._event_has_component_hint(event, ("reply",))
            requested_packages = self._event_string_list(event, "astrmai_requested_tool_packages")
            disclosure_plan = ToolDisclosurePlanner().plan(
                message=tool_intent_text,
                requested_tier=requested_tier,
                explicit_tool_intent=explicit_tool_intent,
                explicit_tool_families=explicit_tool_families,
                social_intent=social_intent,
                has_image=has_image,
                has_forward=has_forward,
                has_reply=has_reply,
                requested_packages=requested_packages,
                max_chat_tools=max(1, self._conversation_int("tool_disclosure_max_tools_chat", 8)),
                max_task_tools=max(1, self._conversation_int("tool_disclosure_max_tools_task", 16)),
            )
            selected_tool_names = set(disclosure_plan.tool_names)
            tools = select_tools_by_names(
                candidate_tools,
                selected_tool_names,
                name_resolver=self._canonical_tool_name,
            )
            hidden_tools = [
                tool
                for tool in candidate_tools
                if self._canonical_tool_name(tool) not in selected_tool_names
            ]
            setattr(event, "_astrmai_disclosure_hidden_tools", hidden_tools)
            hidden_requestable_tools = [
                self._canonical_tool_name(tool)
                for tool in hidden_tools
                if is_model_disclosure_requestable(self._canonical_tool_name(tool))
            ]
            event.set_extra("astrmai_hidden_requestable_tools", hidden_requestable_tools)
            second_pass_packages = (
                list(disclosure_plan.second_pass_packages)
                if self._conversation_flag("tool_disclosure_allow_second_pass", True)
                else []
            )
            event.set_extra("astrmai_disclosure_second_pass_packages", second_pass_packages)
            turn_tools.disclosure_enabled = True
            turn_tools.disclosure_tier = disclosure_plan.tier
            turn_tools.disclosure_packages = list(disclosure_plan.packages)
            turn_tools.disclosure_reasons = list(disclosure_plan.package_reasons)
            turn_tools.disclosure_second_pass_packages = list(second_pass_packages)
            turn_tools.disclosure_decisions = [asdict(item) for item in disclosure_plan.decisions]
            turn_tools.preselected_tools = list(disclosure_plan.preselected_tool_names)
            turn_tools.hidden_requestable_tools = list(hidden_requestable_tools)
            disclosure_families: set[str] = set()
            for tool_name in disclosure_plan.tool_names:
                disclosure_families.update(self.TOOL_FAMILIES.get(tool_name, set()))
            if explicit_tool_intent or (not allowed_families and intent_families is None):
                allowed_families.update(disclosure_families)
                turn_tools.allowed_families = sorted(allowed_families)
            else:
                # OPT-12/TL-02: 披露层为图片/引用轮特意加的只读查证能力（artifact/
                # vision/wait/capability）不得被 tease/comfort 等 intent 家族白名单
                # 静默剥除——否则图片轮无查图工具只能臆测（trace 1785050973 实证）
                protected_families = disclosure_families & {
                    "message_artifact",
                    "vision_message",
                    "quote_reply",
                    "topic_thread",
                    "wait",
                    "capability",
                }
                if protected_families:
                    allowed_families.update(protected_families)
                    turn_tools.allowed_families = sorted(allowed_families)
            turn_tools.record_step(
                "planner.tool_disclosure",
                [self._canonical_tool_name(tool) for tool in candidate_tools],
                [self._canonical_tool_name(tool) for tool in tools],
                "packages(" + ",".join(disclosure_plan.packages) + ")",
            )
        elif requested_tier == "full" or explicit_tool_intent:
            self._set_tool_tier(event, "full")
            tools = self._build_full_pfc_tools(chat_id, user_id, sender_name, event)
            setattr(event, "_astrmai_disclosure_hidden_tools", [])
        else:
            self._set_tool_tier(event, "chat")
            tools = self._build_chat_tools(event)
            setattr(event, "_astrmai_disclosure_hidden_tools", [])

        tools = filter_tools_for_context(
            tools,
            is_group=bool(event.get_group_id()),
            name_resolver=self._canonical_tool_name,
        )

        member_candidate = event.get_extra("astrmai_member_action_candidate", {}) if hasattr(event, "get_extra") else {}
        member_purpose = str(event.get_extra("astrmai_member_action_effective_purpose", "") or "") if hasattr(event, "get_extra") else ""
        if member_candidate and member_purpose in {"discuss_member", "unclear"}:
            tools = [tool for tool in tools if self._canonical_tool_name(tool) != "construct_at_event"]

        if not explicit_tool_intent and not self._conversation_flag("autonomous_chat_tools_enabled", True):
            tools = [
                tool
                for tool in tools
                if self._canonical_tool_name(tool) not in AUTONOMOUS_INTERACTION_TOOLS
                or self._canonical_tool_name(tool) in DEFAULT_VISIBLE_TOOL_NAMES
            ]

        built_tool_names = [
            self._canonical_tool_name(tool)
            for tool in tools or []
            if self._canonical_tool_name(tool)
        ]
        turn_tools.initial_tools = list(built_tool_names)
        turn_tools.available_tools = list(built_tool_names)

        if tools:
            before_family_names = list(built_tool_names)
            tools_before_family = list(tools)
            tools = self._filter_tools_by_families(tools, allowed_families if allowed_families else intent_families)
            filtered_names = {self._canonical_tool_name(tool) for tool in tools or []}
            default_names = set(DEFAULT_VISIBLE_TOOL_NAMES)
            if default_names - filtered_names:
                tools = [
                    tool
                    for tool in tools_before_family
                    if self._canonical_tool_name(tool) in filtered_names | default_names
                ]
            after_family_names = [
                self._canonical_tool_name(tool)
                for tool in tools or []
                if self._canonical_tool_name(tool)
            ]
            turn_tools.family_filtered_tools = list(after_family_names)
            if before_family_names != after_family_names:
                reason = "allowed_families(" + ",".join(turn_tools.allowed_families) + ")"
                turn_tools.record_step(
                    "planner.family_filter",
                    before_family_names,
                    after_family_names,
                    reason,
                    category="social_intent",
                )
        else:
            turn_tools.family_filtered_tools = []

        tools_before_modifier = list(tools or [])
        tools = self.action_modifier.modify_tools(
            tools,
            state=state,
            profile=profile,
            relationship_vec=relationship_vec,
            tool_tier=event.get_extra("astrmai_tool_tier", "full") if hasattr(event, "get_extra") else getattr(event, "astrmai_tool_tier", "full"),
            social_intent=social_intent,
            stance=stance,
            cooldown_tags=event.get_extra("astrmai_agency_cooldown_tags", []) if hasattr(event, "get_extra") else [],
            trace=turn_tools,
        )
        modifier_names = {self._canonical_tool_name(tool) for tool in tools or []}
        default_names = set(DEFAULT_VISIBLE_TOOL_NAMES)
        if default_names - modifier_names:
            before_names = [self._canonical_tool_name(tool) for tool in tools or []]
            tools = [
                tool
                for tool in tools_before_modifier
                if self._canonical_tool_name(tool) in modifier_names | default_names
            ]
            turn_tools.record_step(
                "planner.default_actions_restore",
                before_names,
                [self._canonical_tool_name(tool) for tool in tools],
                "default_visible_tools_bypass_action_modifier",
                category="tool_disclosure",
            )
        incompatible_domain_tools: set[str] = set()
        if entity_domain == EntityDomain.PLATFORM_FRIEND:
            incompatible_domain_tools.add("self_lore_query")
        elif entity_domain == EntityDomain.PERSONA_LORE:
            incompatible_domain_tools.add("qq_friend_lookup")
        if incompatible_domain_tools:
            before_names = [self._canonical_tool_name(tool) for tool in tools or []]
            tools = [
                tool
                for tool in tools or []
                if self._canonical_tool_name(tool) not in incompatible_domain_tools
            ]
            after_names = [self._canonical_tool_name(tool) for tool in tools]
            if before_names != after_names:
                turn_tools.record_step(
                    "planner.entity_domain_tool_guard",
                    before_names,
                    after_names,
                    f"entity_domain({entity_domain.value})",
                    category="tool_domain",
                )
        if explicit_override_enabled and explicit_filter_families:
            filtered_names = {self._canonical_tool_name(tool) for tool in tools or []}
            protected_names = {
                self._canonical_tool_name(tool)
                for tool in tools_before_modifier
                if self.TOOL_FAMILIES.get(self._canonical_tool_name(tool), set()) & explicit_filter_families
            }
            if protected_names - filtered_names:
                before_names = [self._canonical_tool_name(tool) for tool in tools or []]
                tools = [
                    tool
                    for tool in tools_before_modifier
                    if self._canonical_tool_name(tool) in filtered_names | protected_names
                ]
                turn_tools.record_step(
                    "planner.explicit_qq_action_restore",
                    before_names,
                    [self._canonical_tool_name(tool) for tool in tools],
                    "explicit_user_tool_request",
                    category="social_intent",
                )
        if requested_tier != "none" and not any(
            self._canonical_tool_name(tool) == "bot_capability_lookup"
            for tool in tools or []
        ):
            before_names = [self._canonical_tool_name(tool) for tool in tools or []]
            tools = [*(tools or []), BotCapabilityLookupTool()]
            turn_tools.record_step(
                "planner.capability_fallback_restore",
                before_names,
                [self._canonical_tool_name(tool) for tool in tools],
                "global_progressive_disclosure_fallback",
                category="tool_disclosure",
            )
        tools = normalize_tool_schemas(tools)
        turn_tools.filtered_tools = [
            self._canonical_tool_name(tool)
            for tool in tools or []
            if self._canonical_tool_name(tool)
        ]
        reliable_explicit_enabled = self._conversation_flag("explicit_tool_execution_enabled", True)
        contract_families = set(explicit_tool_families)
        if disclosure_plan is not None:
            contract_families.update(disclosure_plan.required_families)
        intent_resolutions = list(
            resolve_explicit_tool_intents(
                contract_families,
                message=tool_intent_text,
                available_tool_names=turn_tools.filtered_tools,
            )
            if reliable_explicit_enabled
            else []
        )
        if reliable_explicit_enabled and disclosure_plan is not None:
            visible_names = set(turn_tools.filtered_tools)
            existing_families = {item.family for item in intent_resolutions}
            for decision in disclosure_plan.decisions:
                if (
                    decision.source != "explicit_user_intent"
                    or decision.family in existing_families
                    or decision.tool_name in visible_names
                ):
                    continue
                intent_resolutions.append(
                    ToolIntentResolution(
                        family=decision.family,
                        tool_name=decision.tool_name,
                        required_state="clarify_needed",
                        missing_slots=("tool_unavailable",),
                        reason="tool_unavailable_in_current_context",
                        clarification_prompt=(
                            f"当前会话或平台不支持{tool_display_name(decision.tool_name)}，"
                            "所以这次不能可靠执行。"
                        ),
                    )
                )
        clarification_items = clarification_resolutions(intent_resolutions)
        if clarification_items:
            prompt = "；".join(
                item.clarification_prompt
                for item in clarification_items
                if item.clarification_prompt
            )
            missing_slots = []
            for item in clarification_items:
                missing_slots.extend(f"{item.family}.{slot}" for slot in item.missing_slots)
            event.set_extra("astrmai_tool_clarification_needed", True)
            event.set_extra("astrmai_tool_clarification_prompt", prompt)
            event.set_extra("astrmai_tool_clarification_missing_slots", missing_slots)
            turn_tools.record_step(
                "planner.tool_slot_clarification",
                list(contract_families),
                sorted(ready_families(intent_resolutions)),
                "missing_slots(" + ",".join(missing_slots) + ")",
                category="tool_slots",
            )
        else:
            event.set_extra("astrmai_tool_clarification_needed", False)
            event.set_extra("astrmai_tool_clarification_prompt", "")
            event.set_extra("astrmai_tool_clarification_missing_slots", [])
        executable_families = ready_families(intent_resolutions) if reliable_explicit_enabled else set()
        intent_contracts = (
            build_tool_intent_contracts(
                executable_families,
                message=tool_intent_text,
                available_tool_names=turn_tools.filtered_tools,
                persona_text=persona_catalog_text,
            )
            if reliable_explicit_enabled
            else []
        )
        serialized_contracts = [asdict(contract) for contract in intent_contracts]
        event.set_extra("astrmai_tool_intent_contracts", serialized_contracts)
        turn_tools.intent_contracts = serialized_contracts
        plans = (
            build_explicit_invocation_plans(
                executable_families,
                turn_tools.filtered_tools,
                intent_contracts=intent_contracts,
            )
            if reliable_explicit_enabled
            else []
        )
        publish_invocation_plans(event, plans)
        turn_tools.invocation_mode = "required" if plans else "auto"
        turn_tools.required_tools = [plan.tool_name for plan in plans if plan.required]
        turn_tools.invocation_plans = [asdict(plan) for plan in plans]
        if plans:
            prepared_tools = await prepare_explicit_tool_fallbacks(
                event,
                turn_tools.required_tools,
                emotion_mapping=self._emotion_mapping_for_meme_tool(),
            )
            event.set_extra("astrmai_prepared_required_tools", prepared_tools)
            if "proactive_meme" in turn_tools.required_tools and any(
                isinstance(item, dict) and item.get("action") == "meme"
                for item in event.get_extra("astrmai_pending_actions", []) or []
            ):
                self._consume_pending_meme_intent(chat_id, user_id, event)
        elif clarification_items:
            event.set_extra("astrmai_prepared_required_tools", [])
            turn_tools.invocation_mode = "clarify"
            return []
        return tools

    @staticmethod
    def _append_planner_runtime_instruction(prompt_envelope: PromptEnvelope | None, text: str) -> None:
        if not isinstance(prompt_envelope, PromptEnvelope):
            return
        existing = str(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or "").strip()
        addition = str(text or "").strip()
        if not addition:
            return
        prompt_envelope.planner_runtime_instruction_block = "\n\n".join(part for part in [existing, addition] if part).strip()
        PlannerSideInputMixin._clamp_runtime_instruction_block(prompt_envelope)

    async def _apply_private_jump_context(
        self,
        ctx,
        event: AstrMessageEvent,
        user_id,
        *,
        prompt_envelope: PromptEnvelope | None,
    ) -> None:
        if event.get_group_id() or not isinstance(prompt_envelope, PromptEnvelope):
            return
        sender_id = str(user_id)
        source_umo = str(getattr(event, "unified_msg_origin", "") or "")
        platform_id = source_umo.split(":", 1)[0] if ":" in source_umo else "default"
        handoff_store = getattr(self, "cross_session_handoff_store", None)
        handoff_id = ""
        jumps = None
        jump_info = None
        if handoff_store is not None:
            try:
                handoff = await handoff_store.peek_for_recipient(platform_id, sender_id)
            except Exception as exc:
                logger.warning(f"[Planner] cross-session handoff lookup degraded: {exc}")
                handoff = None
            if handoff is not None:
                handoff_id = str(handoff.handoff_id or "")
                jump_info = {
                    "timestamp": handoff.created_at,
                    "source_umo": handoff.source_umo,
                    "source_sender_id": handoff.source_sender_id,
                    "source_sender_name": handoff.source_sender_name,
                    "target_id": handoff.target_id,
                    "target_name": handoff.target_name,
                    "private_message": handoff.outbound_message,
                    "context_summary": handoff.context_summary,
                    "delivery_mode": handoff.delivery_mode,
                }
        if jump_info is None and ctx is not None:
            shared_dict = getattr(ctx, "shared_dict", {})
            jumps = shared_dict.get("astrmai_space_jumps", {})
            jump_info = jumps.get(sender_id)
        if jump_info is None:
            return

        injected = False
        try:
            if time.time() - float(jump_info.get("timestamp") or 0.0) < 1800:
                source_group_id = jump_info.get("group_id")
                source_umo = str(jump_info.get("source_umo") or "").strip()
                source_sender_id = str(jump_info.get("source_sender_id") or "").strip()
                source_sender_name = str(jump_info.get("source_sender_name") or "").strip()
                context_summary = self._truncate_runtime_instruction_text(
                    str(jump_info.get("context_summary") or ""),
                    self.PRIVATE_JUMP_MAX_PRIVATE_MESSAGE_CHARS,
                )
                delivery_mode = str(jump_info.get("delivery_mode") or "").strip().lower()
                group_context_str = ""
                if source_group_id and not context_summary:
                    try:
                        conv_mgr = ctx.conversation_manager
                        uid = f"default:GroupMessage:{source_group_id}"
                        curr_cid = await conv_mgr.get_curr_conversation_id(uid)
                        conversation = await conv_mgr.get_conversation(uid, curr_cid)
                        history = json.loads(conversation.history) if conversation and conversation.history else []
                        recent_msgs = []
                        for msg in history[-5:]:
                            role = msg.get("role", "")
                            text_parts = [
                                item.get("text", "")
                                for item in (msg.get("content") or [])
                                if isinstance(item, dict) and item.get("type") == "text"
                            ]
                            content = " ".join(text_parts) if text_parts else ""
                            if content:
                                speaker = "群友" if role == "user" else "你"
                                clipped_content = self._truncate_runtime_instruction_text(
                                    content,
                                    self.PRIVATE_JUMP_MAX_MESSAGE_CHARS,
                                )
                                recent_msgs.append(f"[{speaker}]: {clipped_content}")
                        if recent_msgs:
                            group_context_str = "\n".join(recent_msgs[-self.PRIVATE_JUMP_MAX_HISTORY_MESSAGES :])
                    except Exception as exc:
                        logger.error(f"[Planner] 溯源群聊历史失败: {exc}")

                private_message = self._truncate_runtime_instruction_text(
                    str(jump_info.get("private_message") or ""),
                    self.PRIVATE_JUMP_MAX_PRIVATE_MESSAGE_CHARS,
                )
                if source_umo or context_summary or delivery_mode:
                    source_type = "群聊" if source_group_id or ":GroupMessage:" in source_umo else "另一个私聊"
                    action_text = "受对方委托传话" if delivery_mode == "relay" else "主动联系当前对方"
                    # OPT-16/TL-09: 三方消歧指令移到块首——旧版排在块尾，长摘要+长
                    # 消息时最先被 360 字符截断吃掉，传话场景更易把三方混为一人
                    sys_inject = (
                        f"\n\n我刚才从{source_type}跨会话来到当前私聊，执行的是：{action_text}。\n"
                        "【三方区分】当前发消息给我的人是收件人，不一定是上一会话的发起人；"
                        "回复时必须区分发起人、机器人自己和当前收件人，不要把三者混为一人。\n"
                        + (
                            f"【发起人】：{source_sender_name}（QQ：{source_sender_id}）\n"
                            if source_sender_name and source_sender_id
                            else f"【发起人】：{source_sender_name or source_sender_id}\n"
                            if source_sender_name or source_sender_id
                            else ""
                        )
                        + (f"【跨会话摘要】：{context_summary}\n" if context_summary else "")
                        + f"【已经发给当前对方的消息】：{private_message}\n"
                        "自然承接已经发送的消息和对方现在的回应。"
                    )
                else:
                    sys_inject = (
                        "\n\n刚才我还在群聊"
                        + (f" (群号:{source_group_id})" if source_group_id else "")
                        + "里和大家说话，随后又主动私下对 ta 说了一句：\n"
                        f"【我刚才的悄悄话】：{private_message}\n"
                    )
                    if group_context_str:
                        sys_inject += f"\n【我切出来前群里的话题回顾】：\n{group_context_str}\n"
                    sys_inject += (
                        "\n对方现在这句，多半就是接着我刚才那次跨界私聊在回我。"
                        "我得把群里的前置话题和这句悄悄话一起接住，顺着私下交流的自然感继续聊下去。"
                    )
                sys_inject = self._truncate_runtime_instruction_text(
                    sys_inject,
                    self.PRIVATE_JUMP_CONTEXT_MAX_CHARS,
                )
                self._append_planner_runtime_instruction(prompt_envelope, sys_inject)
                injected = True
                logger.info(f"[Planner] 已触发跨会话语境补偿，并注入到 {sender_id} 的私聊思考中。")
        finally:
            if injected and handoff_id and handoff_store is not None:
                try:
                    await handoff_store.acknowledge(handoff_id)
                except Exception as exc:
                    logger.warning(f"[Planner] cross-session handoff acknowledge degraded: {exc}")
            elif jumps is not None:
                jumps.pop(sender_id, None)
        return

    def _append_mode_instructions(
        self,
        event: AstrMessageEvent,
        *,
        prompt_envelope: PromptEnvelope | None,
        is_tool_call_mode: bool,
        is_all_mode: bool,
        is_fast_mode: bool,
    ) -> None:
        lines: list[str] = []
        if is_tool_call_mode:
            lines.append(
                "对方这次是在让我帮忙办事。"
                "我先看看手边有哪些对应的子智能体工具能真去执行，"
                "等拿到结果后，再用我自己的语气告诉 ta。"
            )
        if is_all_mode:
            user_message = PromptEnvelope.sanitize_inline_text(
                self._truncate_runtime_instruction_text(getattr(event, "message_str", ""), 80)
            )
            lines.append(f'对方刚才说的是：“{user_message}”。我这轮就先接住这一条来回。')
        if is_fast_mode:
            lines.append("有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。")
        if lines:
            merged_lines = "\n".join(lines[: self.MODE_INSTRUCTION_MAX_LINES])
            merged_lines = self._truncate_runtime_instruction_text(merged_lines, self.MODE_INSTRUCTION_MAX_CHARS)
            self._append_planner_runtime_instruction(prompt_envelope, merged_lines)
        return

    async def _should_follow_up_legacy(
        self,
        chat_id: str,
        last_reply: str,
        *,
        event: AstrMessageEvent | None = None,
        tools=None,
        decision=None,
    ) -> Optional[str]:
        if event is not None and hasattr(event, "get_extra"):
            try:
                if int(event.get_extra("astrmai_think_level", 1) or 0) < 1:
                    return None
            except (TypeError, ValueError):
                pass
            if event.get_extra("astrmai_lightweight_event", False):
                return None
            if event.get_extra("astrmai_agency_cooldown_tags", []):
                return None
            focus_reason = str(event.get_extra("astrmai_focus_reason", "") or "").lower()
            is_direct_focus = any(token in focus_reason for token in ("at", "reply", "direct", "wakeup", "private", "name"))
            group_getter = getattr(event, "get_group_id", None)
            if callable(group_getter) and group_getter() and not is_direct_focus:
                return None
        if tools:
            return None
        social_intent = str(getattr(decision, "social_intent", "") if decision else "").strip()
        if social_intent in {"boundary", "pushback", "observe"}:
            return None

        if self.state_engine:
            state = await self.state_engine.get_state(chat_id)
            if state and state.energy < 0.3:
                return None

        clean_reply = last_reply.strip()
        if len(clean_reply) < 15:
            return None
        if clean_reply.endswith("？") or clean_reply.endswith("?"):
            return None

        reply_cfg = getattr(self.gateway.config, "reply", None)
        follow_up_probability = getattr(reply_cfg, "follow_up_probability", 0.20)
        try:
            follow_up_probability = float(follow_up_probability)
        except (TypeError, ValueError):
            follow_up_probability = 0.20
        follow_up_probability = max(0.0, min(1.0, follow_up_probability))
        if follow_up_probability <= 0.0 or random.random() > follow_up_probability:
            return None

        prompt = (
            f'你刚回复:"{clean_reply[:100]}"\n'
            "需要紧接着追发第二句吗？(补充/追问/表情/吐槽)\n"
            'JSON: {"follow": true/false, "reason": "原因"}'
        )

        try:
            import json as _json
            import re

            result = await self.gateway.call_data_process_task(
                prompt,
                system_prompt=self.FOLLOW_UP_SYSTEM_PROMPT,
                is_json=True,
                lane_key=LaneKey(subsystem="sys2", task_family="followup", scope_id=chat_id),
                base_origin=chat_id,
            )
            data = result if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                match = re.search(r"\{.*?\}", str(result), re.DOTALL)
                if match:
                    data = _json.loads(match.group(0))
            if data.get("follow") or data.get("should_follow"):
                return data.get("reason", "补充细节")
        except Exception as exc:
            logger.debug(f"[Planner] Follow-up 判定异常: {exc}")
        return None
    @staticmethod
    def _is_poke_event(event: AstrMessageEvent | None) -> bool:
        if event is None or not hasattr(event, "get_extra"):
            return False
        return bool(
            event.get_extra("is_virtual_poke", False)
            or str(event.get_extra("astrmai_interaction_kind", "") or "").lower() in {"poke", "peer_poke"}
        )

    def _record_follow_up_decision(
        self,
        event: AstrMessageEvent | None,
        *,
        eligible: bool = False,
        skipped_reason: str = "",
        signals: list[str] | None = None,
        probability: float = 0.0,
        llm_checked: bool = False,
        followed: bool = False,
        reason: str = "",
        cooldown_until: float = 0.0,
    ) -> None:
        if event is None:
            return
        turn_context = ensure_turn_context(event)
        snapshot = turn_context.follow_up
        snapshot.eligible = bool(eligible)
        snapshot.skipped_reason = str(skipped_reason or "")
        snapshot.signals = [str(signal) for signal in (signals or []) if str(signal or "").strip()]
        snapshot.probability = float(probability or 0.0)
        snapshot.llm_checked = bool(llm_checked)
        snapshot.followed = bool(followed)
        snapshot.reason = str(reason or "")
        snapshot.cooldown_until = float(cooldown_until or 0.0)

    async def _wait_for_post_reply_feedback(
        self,
        event: AstrMessageEvent | None,
        delay_seconds: float,
    ) -> bool:
        if event is None or not hasattr(event, "get_extra"):
            await asyncio.sleep(max(0.0, float(delay_seconds)))
            return False
        feedback_event = event.get_extra("astrmai_post_reply_feedback_event", None)
        if not isinstance(feedback_event, asyncio.Event):
            await asyncio.sleep(max(0.0, float(delay_seconds)))
            return False
        try:
            await asyncio.wait_for(
                feedback_event.wait(),
                timeout=max(0.0, float(delay_seconds)),
            )
            return True
        except asyncio.TimeoutError:
            return False

    async def _settle_no_send_relationship_event(
        self,
        event: AstrMessageEvent | None,
        chat_id: str,
        *,
        skipped_reason: str,
    ) -> None:
        if event is None or not hasattr(event, "get_extra"):
            return
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            return
        if self.state_engine is None or not hasattr(self.state_engine, "settle_no_send_affection"):
            return
        sender_getter = getattr(event, "get_sender_id", None)
        sender_id = str(sender_getter() or "").strip() if callable(sender_getter) else ""
        if not sender_id:
            return
        focus_event = event.get_extra("astrmai_focus_event", None)
        anchor_event = event.get_extra("astrmai_anchor_event", None)
        for candidate in [focus_event, anchor_event, event]:
            message_text = str(getattr(candidate, "message_str", "") or "").strip() if candidate is not None else ""
            if message_text:
                break
        else:
            message_text = ""
        if not message_text:
            return
        risk_flags = event.get_extra("astrmai_risk_flags", []) or []
        try:
            attack_confidence = float(event.get_extra("astrmai_attack_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            attack_confidence = 0.0
        try:
            await self.state_engine.settle_no_send_affection(
                user_id=sender_id,
                group_id=chat_id,
                message_text=message_text,
                skipped_reason=skipped_reason,
                attack_confidence=attack_confidence,
                risk_flags=risk_flags,
            )
        except Exception as exc:
            logger.debug(f"[Planner] no-send relationship settlement skipped: {exc}")

    def _follow_up_cooldown_until(self, chat_id: str, now: float | None = None) -> float:
        cooldowns = getattr(self, "follow_up_cooldowns", None)
        if not isinstance(cooldowns, dict):
            return 0.0
        current_time = time.time() if now is None else float(now)
        until = float(cooldowns.get(str(chat_id or ""), 0.0) or 0.0)
        if until and until <= current_time:
            cooldowns.pop(str(chat_id or ""), None)
            return 0.0
        return until

    def _set_follow_up_cooldown(self, chat_id: str, social_intent: str) -> float:
        cooldowns = getattr(self, "follow_up_cooldowns", None)
        if not isinstance(cooldowns, dict):
            cooldowns = {}
            setattr(self, "follow_up_cooldowns", cooldowns)
        duration = self.FOLLOW_UP_COOLDOWN_SECONDS.get(str(social_intent or "answer"), 600.0)
        until = time.time() + float(duration)
        cooldowns[str(chat_id or "")] = until
        return until

    @classmethod
    def _follow_up_probability_for_intent(cls, base_probability: float, social_intent: str) -> float:
        base = max(0.0, min(1.0, float(base_probability or 0.0)))
        if social_intent == "comfort":
            return min(base * 1.5, 0.35)
        if social_intent in {"tease", "inquire"}:
            return min(base, 0.18)
        return min(base * 0.4, 0.08)

    @staticmethod
    def _looks_complete_for_follow_up(reply_text: str) -> bool:
        text = str(reply_text or "").strip()
        if len(text) < 20:
            return False
        return text.endswith(("。", ".", "！", "!", "～", "~"))

    async def _should_follow_up(
        self,
        chat_id: str,
        last_reply: str,
        *,
        event: AstrMessageEvent | None = None,
        tools=None,
        decision=None,
    ) -> Optional[str]:
        signals: list[str] = []

        def _skip(reason: str, *, probability: float = 0.0, cooldown_until: float = 0.0) -> None:
            self._record_follow_up_decision(
                event,
                eligible=False,
                skipped_reason=reason,
                signals=signals,
                probability=probability,
                llm_checked=False,
                followed=False,
                cooldown_until=cooldown_until,
            )

        if event is not None and hasattr(event, "get_extra"):
            try:
                if int(event.get_extra("astrmai_think_level", 1) or 0) < 1:
                    signals.append("think_level_below_1")
                    _skip("think_level_below_1")
                    return None
            except (TypeError, ValueError):
                pass
            if event.get_extra("astrmai_lightweight_event", False):
                signals.append("lightweight_event")
                _skip("lightweight_event")
                return None
            if self._is_poke_event(event):
                signals.append("poke_event")
                _skip("poke_event")
                return None
            cooldown_tags = {
                str(tag or "").strip()
                for tag in (event.get_extra("astrmai_agency_cooldown_tags", []) or [])
                if str(tag or "").strip()
            }
            blocking_cooldowns = cooldown_tags & {"meme", "poke", "like", "sharp_reply", "long_reply"}
            if blocking_cooldowns:
                signals.extend(sorted(blocking_cooldowns))
                _skip("agency_cooldown")
                return None
            focus_reason = str(event.get_extra("astrmai_focus_reason", "") or "").lower()
            is_direct_focus = any(token in focus_reason for token in ("at", "reply", "direct", "wakeup", "private", "name"))
            group_getter = getattr(event, "get_group_id", None)
            if callable(group_getter) and group_getter() and not is_direct_focus:
                signals.append("group_non_direct")
                _skip("group_non_direct")
                return None
            reply_need = str(event.get_extra("astrmai_reply_need", "") or "").strip()
            action_tier = str(event.get_extra("astrmai_action_tier", "") or "").strip()
            if reply_need in {"wait", "ignore"}:
                signals.append(f"reply_need_{reply_need}")
                _skip("reply_need_blocked")
                return None
            if action_tier == "none":
                signals.append("action_tier_none")
                _skip("action_tier_none")
                return None
        if tools:
            signals.append("tools_used")
            _skip("tools_used")
            return None
        social_intent = str(getattr(decision, "social_intent", "") if decision else "").strip()
        if not social_intent and event is not None and hasattr(event, "get_extra"):
            social_intent = str(event.get_extra("astrmai_social_intent", "") or "").strip()
        social_intent = social_intent or "answer"
        if social_intent in {"boundary", "pushback", "observe"}:
            signals.append(f"social_intent_{social_intent}")
            _skip("social_intent_blocked")
            return None
        if social_intent not in self.FOLLOW_UP_ALLOWED_INTENTS:
            signals.append(f"social_intent_{social_intent}")
            _skip("social_intent_not_allowed")
            return None

        if self.state_engine:
            state = await self.state_engine.get_state(chat_id)
            if state and state.energy < 0.3:
                signals.append("low_energy")
                _skip("low_energy")
                return None

        clean_reply = last_reply.strip()
        if not (8 <= len(clean_reply) <= 60):
            signals.append("reply_length_out_of_range")
            _skip("reply_length_out_of_range")
            return None
        if clean_reply.endswith(("？", "?")):
            signals.append("reply_already_invites_response")
            _skip("reply_already_invites_response")
            return None
        if social_intent != "comfort" and self._looks_complete_for_follow_up(clean_reply):
            signals.append("complete_reply")
            _skip("complete_reply")
            return None

        now = time.time()
        cooldown_until = self._follow_up_cooldown_until(chat_id, now)
        if cooldown_until > now:
            signals.append("follow_up_cooldown")
            _skip("follow_up_cooldown", cooldown_until=cooldown_until)
            return None

        reply_cfg = getattr(self.gateway.config, "reply", None)
        follow_up_probability = getattr(reply_cfg, "follow_up_probability", 0.20)
        try:
            follow_up_probability = float(follow_up_probability)
        except (TypeError, ValueError):
            follow_up_probability = 0.20
        follow_up_probability = max(0.0, min(1.0, follow_up_probability))
        effective_probability = self._follow_up_probability_for_intent(follow_up_probability, social_intent)
        stance = str(getattr(decision, "stance", "") if decision else "").strip().lower()
        if not stance and event is not None and hasattr(event, "get_extra"):
            stance = str(event.get_extra("astrmai_stance", "") or "").strip().lower()
        stance_multiplier = self.FOLLOW_UP_STANCE_MULTIPLIERS.get(stance, 1.0)
        if stance_multiplier < 1.0:
            signals.append(f"stance_{stance}")
            effective_probability = max(0.0, min(1.0, effective_probability * stance_multiplier))
            signals.append(f"follow_up_probability_scaled:{stance_multiplier:.2f}")
        if effective_probability <= 0.0:
            signals.append("follow_up_disabled")
            _skip("follow_up_disabled", probability=effective_probability)
            return None
        if random.random() > effective_probability:
            signals.append("probability_gate")
            _skip("probability_gate", probability=effective_probability)
            return None

        if social_intent == "comfort" and len(clean_reply) <= 24:
            cooldown_until = self._set_follow_up_cooldown(chat_id, social_intent)
            self._record_follow_up_decision(
                event,
                eligible=True,
                signals=[*signals, "comfort_short_reply"],
                probability=effective_probability,
                llm_checked=False,
                followed=True,
                reason="gentle_support",
                cooldown_until=cooldown_until,
            )
            return "gentle_support"

        prompt = (
            f'Last reply: "{clean_reply[:100]}"\n'
            "Should the bot send one very short natural follow-up now? "
            "Only say yes for a genuinely useful supplement, question, or gentle support.\n"
            'JSON: {"follow": true/false, "reason": "reason"}'
        )

        try:
            import json as _json
            import re

            result = await self.gateway.call_data_process_task(
                prompt,
                system_prompt=self.FOLLOW_UP_SYSTEM_PROMPT,
                is_json=True,
                lane_key=LaneKey(subsystem="sys2", task_family="followup", scope_id=chat_id),
                base_origin=chat_id,
            )
            data = result if isinstance(result, dict) else {}
            if not isinstance(data, dict):
                match = re.search(r"\{.*?\}", str(result), re.DOTALL)
                if match:
                    data = _json.loads(match.group(0))
            if data.get("follow") or data.get("should_follow"):
                reason = str(data.get("reason", "extra_detail") or "extra_detail")
                cooldown_until = self._set_follow_up_cooldown(chat_id, social_intent)
                self._record_follow_up_decision(
                    event,
                    eligible=True,
                    signals=signals,
                    probability=effective_probability,
                    llm_checked=True,
                    followed=True,
                    reason=reason,
                    cooldown_until=cooldown_until,
                )
                return reason
            self._record_follow_up_decision(
                event,
                eligible=True,
                skipped_reason="llm_rejected",
                signals=signals,
                probability=effective_probability,
                llm_checked=True,
                followed=False,
            )
        except Exception as exc:
            logger.debug(f"[Planner] Follow-up 判定异常: {exc}")
            self._record_follow_up_decision(
                event,
                eligible=True,
                skipped_reason="llm_error",
                signals=signals,
                probability=effective_probability,
                llm_checked=True,
                followed=False,
            )
        return None


__all__ = ["Planner"]
