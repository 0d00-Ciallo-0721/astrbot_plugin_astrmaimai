from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

from ...contracts.qq_action import PendingQQAction


QQ_MESSAGE_EMOJI_OPTIONS: dict[str, list[str]] = {
    "support": ["66", "124"],
    "approve": ["66", "76"],
    "laugh": ["63", "85"],
    "cute": ["124", "79"],
}


def _get_current_event(context: ContextWrapper[AstrAgentContext]):
    return context.context.event


def _get_pending_actions(event) -> list[dict[str, Any]]:
    pending_actions = event.get_extra("astrmai_pending_actions", [])
    if isinstance(pending_actions, list):
        return pending_actions
    return []


def _set_pending_actions(event, pending_actions: list[dict[str, Any]]) -> None:
    event.set_extra("astrmai_pending_actions", pending_actions)


def _record_tool_execution(event, tool_name: str, *, status: str = "success") -> None:
    trace = event.get_extra("astrmai_tool_execution_trace", [])
    trace = list(trace) if isinstance(trace, list) else []
    trace.append({"tool_name": str(tool_name or ""), "status": str(status or "success")})
    event.set_extra("astrmai_tool_execution_trace", trace[-32:])


def _append_pending_action(event, action: dict[str, Any]) -> None:
    pending_actions = _get_pending_actions(event)
    pending_actions.append(action)
    _set_pending_actions(event, pending_actions)


def _append_qq_action(event, action: PendingQQAction) -> None:
    _append_pending_action(event, action.to_dict())


def _append_once(event, *, matcher, action: dict[str, Any]) -> bool:
    pending_actions = _get_pending_actions(event)
    if any(matcher(item) for item in pending_actions):
        return False
    pending_actions.append(action)
    _set_pending_actions(event, pending_actions)
    tool_name = {
        "at": "construct_at_event",
        "meme": "proactive_meme",
        "terminal_reread": "meme_resonance_action",
        "withdraw": "regret_and_withdraw_action",
    }.get(str(action.get("action") or ""), "")
    if tool_name:
        _record_tool_execution(event, tool_name)
    return True


def _current_message_id(event) -> str:
    message_obj = getattr(event, "message_obj", None)
    return str(getattr(message_obj, "message_id", "") or "").strip()


def _history_messages(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            payload = data.get("messages", [])
        else:
            payload = data
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def _resolve_latest_bot_message_id(event) -> str:
    client = getattr(event, "bot", None)
    api = getattr(client, "api", None)
    if api is None:
        return ""
    group_id = str(event.get_group_id() or "").strip()
    sender_id = str(event.get_sender_id() or "").strip()
    self_id = str(event.get_self_id() or "").strip()
    try:
        if group_id:
            payload = await api.call_action(
                "get_group_msg_history",
                group_id=int(group_id) if group_id.isdigit() else group_id,
                message_seq=0,
                count=20,
            )
        elif sender_id:
            payload = await api.call_action(
                "get_friend_msg_history",
                user_id=int(sender_id) if sender_id.isdigit() else sender_id,
                message_seq=0,
                count=20,
            )
        else:
            return ""
    except Exception as exc:
        logger.warning(f"[RegretAndWithdrawTool] message history lookup failed: {exc}")
        return ""

    for message in reversed(_history_messages(payload)):
        message_sender = message.get("sender")
        author_id = message.get("user_id")
        if isinstance(message_sender, dict):
            author_id = message_sender.get("user_id", author_id)
        message_id = str(message.get("message_id") or "").strip()
        if message_id and str(author_id or "").strip() == self_id:
            return message_id
    return ""


async def _resolve_target(
    db_service: Any,
    *,
    target_name: str,
    current_event,
    astr_ctx,
) -> Optional[tuple[str, Optional[str]]]:
    if not db_service or not hasattr(db_service, "resolve_entity_spatio_temporal"):
        return None
    result = await db_service.resolve_entity_spatio_temporal(
        target_name=target_name,
        current_event=current_event,
        astr_ctx=astr_ctx,
    )
    if not result:
        return None
    target_id, group_id = result
    return str(target_id), str(group_id) if group_id is not None else None


@dataclass
class WaitTool(FunctionTool[AstrAgentContext]):
    name: str = "wait_and_listen"
    description: str = (
        "当你判断对方还没说完，或者当前更适合等待下一条消息时调用。"
        "调用后你最终必须只输出 [SYSTEM_WAIT_SIGNAL]。"
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        _record_tool_execution(_get_current_event(context), self.name)
        return "[SYSTEM_WAIT_SIGNAL]"


@dataclass
class OmniPerceptionTool(FunctionTool[AstrAgentContext]):
    name: str = "omni_perception_query"
    description: str = (
        "统一查询记忆、用户画像、黑话词条、节点与每日反思。"
        "至少提供 query、target_name、recall_date 之一。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的事件、概念或关键词。"},
                "target_name": {"type": "string", "description": "要检索的人物名称或 ID。"},
                "recall_date": {
                    "type": "string",
                    "description": "要检索的日期，格式 YYYY-MM-DD。",
                },
            },
        }
    )
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    memory_tool_service: Optional[Any] = Field(default=None, exclude=True)
    db_service: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)
    current_sender_id: str = Field(default="", exclude=True)
    current_sender_name: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query", "") or "").strip()
        target_name = str(kwargs.get("target_name", "") or "").strip()
        recall_date = str(kwargs.get("recall_date", "") or "").strip()
        if not (query or target_name or recall_date):
            return "执行失败：请至少提供 query、target_name、recall_date 之一。"

        current_event = _get_current_event(context)
        tool_service = self.memory_tool_service or getattr(self.memory_engine, "tool_service", None)
        if tool_service and hasattr(tool_service, "omni_query"):
            return await tool_service.omni_query(
                query=query,
                target_name=target_name,
                recall_date=recall_date,
                chat_id=self.chat_id,
                current_sender_id=self.current_sender_id,
                current_sender_name=self.current_sender_name,
                event=current_event,
            )

        async def _fetch_memory():
            return None

        async def _fetch_profile():
            if not self.db_service:
                return None
            entity = target_name or query
            if not entity:
                return None
            try:
                profile = None
                if entity == self.current_sender_name or entity in {"我", "自己", "当前用户"}:
                    getter = getattr(self.db_service, "get_user_profile", None)
                    if getter and self.current_sender_id:
                        profile = await getter(self.current_sender_id) if asyncio.iscoroutinefunction(getter) else getter(self.current_sender_id)
                else:
                    getter = getattr(self.db_service, "get_profile_by_name", None)
                    if getter:
                        profile = await getter(entity) if asyncio.iscoroutinefunction(getter) else getter(entity)
                if not profile:
                    return None
                social_score = float(getattr(profile, "social_score", 0.0) or 0.0)
                persona = getattr(profile, "persona_analysis", "") or "暂无稳定侧写。"
                name = getattr(profile, "name", entity)
                return f"对象: {name}\n好感度: {social_score:.1f}\n侧写: {persona}"
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] profile lookup failed: {exc}")
                return None

        async def _fetch_nodes():
            if not self.db_service or not hasattr(self.db_service, "search_nodes_async"):
                return None
            search_term = target_name or query
            if not search_term:
                return None
            try:
                nodes = await self.db_service.search_nodes_async(search_term, limit=3, include_description=True)
                if not nodes:
                    return None
                lines = []
                for node in nodes:
                    lines.append(
                        f"- {getattr(node, 'name', '')} ({getattr(node, 'type', '')}): "
                        f"{getattr(node, 'description', '')}"
                    )
                return "\n".join(lines)
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] node lookup failed: {exc}")
                return None

        async def _fetch_reflection():
            if not self.db_service or not recall_date or not hasattr(self.db_service, "get_reflection_async"):
                return None
            try:
                reflection = await self.db_service.get_reflection_async(recall_date)
                if not reflection:
                    return None
                return f"[{getattr(reflection, 'date', recall_date)}]\n{getattr(reflection, 'reflection', '')}"
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] reflection lookup failed: {exc}")
                return None

        async def _fetch_jargon():
            return None

        memory_result, profile_result, node_result, reflection_result, jargon_result = await asyncio.gather(
            _fetch_memory(),
            _fetch_profile(),
            _fetch_nodes(),
            _fetch_reflection(),
            _fetch_jargon(),
            return_exceptions=True,
        )

        sections = []
        if memory_result:
            sections.append(f"[记忆]\n{memory_result}")
        if jargon_result:
            sections.append(f"[黑话]\n{jargon_result}")
        if profile_result:
            sections.append(f"[画像]\n{profile_result}")
        if node_result:
            sections.append(f"[节点]\n{node_result}")
        if reflection_result:
            sections.append(f"[反思]\n{reflection_result}")
        if not sections:
            return "系统提示：当前没有检索到可用内部资料。"
        return "\n\n".join(sections)


@dataclass
class ConstructAtEventTool(FunctionTool[AstrAgentContext]):
    name: str = "construct_at_event"
    description: str = "为最终发送阶段追加 @ 某位群成员的动作。"
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "要 @ 的目标用户名或 ID。"}
            },
            "required": ["target_name"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_name = str(kwargs.get("target_name", "") or "").strip()
        if not target_name:
            return "执行失败：target_name 不能为空。"
        current_event = _get_current_event(context)
        astr_ctx = context.context.context
        resolved = await _resolve_target(
            self.db_service,
            target_name=target_name,
            current_event=current_event,
            astr_ctx=astr_ctx,
        )
        if not resolved:
            return f"动作取消：当前上下文里无法锁定 {target_name}。"
        target_id, group_id = resolved
        if target_id == str(current_event.get_self_id()):
            return "动作取消：不能 @ 自己。"
        added = _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "at" and str(item.get("target_id") or "") == target_id,
            action={
                "action": "at",
                "target_id": target_id,
                "target_name": target_name,
                "group_id": group_id,
            },
        )
        if not added:
            return f"已存在对 {target_name} 的 @ 动作，无需重复添加。"
        return f"已将 @{target_name} 加入待发送动作，请继续生成最终回复文本。"


@dataclass
class ProactivePokeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_poke"
    description: str = "主动戳一戳目标用户。未指定时默认戳当前触发用户。"
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "要戳的用户名或 ID，可为空。"}
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        astr_ctx = context.context.context
        target_name = str(kwargs.get("target_name", "") or "").strip()
        target_id: Optional[str] = None
        group_id = current_event.get_group_id()
        display_name = target_name or (current_event.get_sender_name() or "当前用户")

        if target_name:
            resolved = await _resolve_target(
                self.db_service,
                target_name=target_name,
                current_event=current_event,
                astr_ctx=astr_ctx,
            )
            if not resolved:
                return f"动作取消：当前上下文里无法锁定 {target_name}。"
            target_id, resolved_group_id = resolved
            current_group_id = str(group_id or "").strip()
            resolved_group_id = str(resolved_group_id or "").strip()
            if current_group_id:
                if resolved_group_id and resolved_group_id != current_group_id:
                    return f"动作取消：{target_name} 不在当前群聊上下文里。"
            else:
                current_peer_id = str(current_event.get_sender_id() or "").strip()
                if resolved_group_id or str(target_id or "").strip() != current_peer_id:
                    return f"动作取消：{target_name} 不在当前私聊上下文里。"
        else:
            target_id = str(current_event.get_sender_id())

        if target_id == str(current_event.get_self_id()):
            return "动作取消：不能戳自己。"

        _append_qq_action(
            current_event,
            PendingQQAction(
                action_type="poke",
                target_id=str(target_id or ""),
                target_name=display_name,
                group_id=str(group_id or ""),
            ),
        )
        return f"已将戳一戳 {display_name} 加入待执行动作；不要声称已经成功，继续生成自然的文字回应。"


@dataclass
class MessageEmojiLikeTool(FunctionTool[AstrAgentContext]):
    name: str = "message_emoji_like_action"
    description: str = "为当前焦点消息设置 QQ 原生表情回复。只会从内置的小型表情集合里挑选。"
    emoji_options: dict[str, list[str]] = Field(default_factory=lambda: dict(QQ_MESSAGE_EMOJI_OPTIONS), exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "tone": {
                    "type": "string",
                    "description": "表情语气，可选 support/approve/laugh/cute，留空则随机。",
                }
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        message_id = _current_message_id(current_event)
        if not message_id:
            return "动作取消：当前消息没有可定位的 message_id。"

        tone = str(kwargs.get("tone", "") or "").strip().lower()
        selected_pool = list(self.emoji_options.get(tone) or [])
        selected_tone = tone if selected_pool else ""
        if not selected_pool:
            fallback_items = [(bucket, values) for bucket, values in self.emoji_options.items() if values]
            if not fallback_items:
                return "动作取消：当前没有可用的内置表情回复集合。"
            selected_tone, selected_pool = random.choice(fallback_items)
        emoji_id = random.choice(selected_pool)

        _append_qq_action(
            current_event,
            PendingQQAction(
                action_type="message_emoji_like",
                message_id=message_id,
                payload={
                    "emoji_id": str(emoji_id),
                    "tone": selected_tone or tone or "random",
                },
            ),
        )
        return "已将 QQ 原生消息表情回复加入待执行动作；不要声称已经成功，继续生成最终回复。"


@dataclass
class GroupSignTool(FunctionTool[AstrAgentContext]):
    name: str = "group_sign_action"
    description: str = "对当前群聊执行一次群签到。只允许在当前群上下文里使用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        del kwargs
        current_event = _get_current_event(context)
        group_id = str(current_event.get_group_id() or "").strip()
        if not group_id:
            return "动作取消：当前不是群聊，无法执行群签到。"
        _append_qq_action(
            current_event,
            PendingQQAction(action_type="group_sign", group_id=group_id),
        )
        return "已将当前群签到加入待执行动作；不要声称已经成功，继续生成最终回复。"


@dataclass
class CustomFaceCatalogQueryTool(FunctionTool[AstrAgentContext]):
    name: str = "custom_face_catalog_query"
    description: str = "查询当前账号可用的 QQ 自定义表情目录，供后续聊天动作参考。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "最多返回多少个表情，默认 24。"}
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        client = getattr(current_event, "bot", None)
        api = getattr(client, "api", None)
        if api is None:
            return "系统提示：当前无法访问 QQ 自定义表情目录。"
        try:
            count = int(kwargs.get("count", 24) or 24)
        except (TypeError, ValueError):
            count = 24
        count = max(1, min(count, 96))
        try:
            payload = await api.call_action("fetch_custom_face", count=count)
        except Exception as exc:
            logger.error(f"[CustomFaceCatalogQueryTool] execution failed: {exc}")
            return f"系统提示：查询自定义表情失败：{exc}"

        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            faces = payload["data"]
        else:
            faces = payload if isinstance(payload, list) else []
        normalized = [str(item).strip() for item in faces if str(item or "").strip()]
        if not normalized:
            return "系统提示：当前没有查询到可用的 QQ 自定义表情。"
        preview = "\n".join(f"- {item}" for item in normalized[:count])
        return f"可用的 QQ 自定义表情如下：\n{preview}"


@dataclass
class ProactiveMemeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_meme"
    description: str = "指定当前回复应附带的表情包情绪标签。"
    parameters: dict = Field(default_factory=dict)
    emotion_mapping: list = Field(default_factory=list, exclude=True)

    def __post_init__(self) -> None:
        available = "\n".join(f"- {item}" for item in self.emotion_mapping) if self.emotion_mapping else "- neutral: 平静"
        self.description = (
            "选择一个表情包情绪标签，系统会在最终回复后自动按该标签补发表情包。\n"
            f"可用标签:\n{available}"
        )
        self.parameters = {
            "type": "object",
            "properties": {
                "emotion_tag": {"type": "string", "description": "要使用的情绪标签。"}
            },
            "required": ["emotion_tag"],
        }

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        emotion_tag = str(kwargs.get("emotion_tag", "neutral") or "neutral").strip().lower()
        valid_tags = [str(item).split(":")[0].strip().lower() for item in self.emotion_mapping if str(item).strip()]
        if valid_tags and emotion_tag not in valid_tags:
            emotion_tag = "neutral"
        current_event.set_extra("astrmai_bypass_mood_analysis", emotion_tag)
        current_event.set_extra("astrmai_force_meme", True)
        _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "meme",
            action={"action": "meme", "tag": emotion_tag},
        )
        return f"已锁定表情包标签 {emotion_tag}，请继续生成最终文本回复。"


@dataclass
class MemeResonanceTool(FunctionTool[AstrAgentContext]):
    name: str = "meme_resonance_action"
    description: str = "当需要 1:1 跟队复读时调用，系统会强制终止为固定输出。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_message": {"type": "string", "description": "要 1:1 复读的内容。"}
            },
            "required": ["target_message"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        target_message = str(kwargs.get("target_message", "") or "").strip()
        if not target_message:
            return "执行失败：target_message 不能为空。"
        current_event = _get_current_event(context)
        _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "terminal_reread",
            action={"action": "terminal_reread", "content": target_message},
        )
        return f"[TERMINAL_YIELD]:{target_message}"


@dataclass
class TopicHijackTool(FunctionTool[AstrAgentContext]):
    name: str = "topic_hijack_action"
    description: str = "强制转移话题，用一个生活化状态切断当前硬核话题。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "fake_status": {"type": "string", "description": "伪装成当前状态的短句。"}
            },
            "required": ["fake_status"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        fake_status = str(kwargs.get("fake_status", "") or "").strip() or "正忙着"
        _record_tool_execution(_get_current_event(context), self.name)
        return (
            "[SYSTEM OVERRIDE] 立刻停止回答原话题。"
            f"请以“我现在{fake_status}”为现实前提，简短、口语化地把话题拐开。"
        )


@dataclass
class SpaceTransitionTool(FunctionTool[AstrAgentContext]):
    name: str = "space_transition_action"
    description: str = "把敏感或更私密的话题转入私聊。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "private_message": {"type": "string", "description": "准备私发给对方的话。"},
                "cover_message": {"type": "string", "description": "准备留在当前空间的掩护话。"},
            },
            "required": ["private_message", "cover_message"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        private_message = str(kwargs.get("private_message", "") or "").strip()
        cover_message = str(kwargs.get("cover_message", "") or "").strip()
        if not private_message or not cover_message:
            return "执行失败：private_message 和 cover_message 都不能为空。"
        _record_tool_execution(_get_current_event(context), self.name)
        return (
            "[SYSTEM OVERRIDE] 你已经决定转入私聊。"
            f"请把群内最终话术写成：{cover_message}。"
            f"需要私聊的真实内容是：{private_message}。"
        )


@dataclass
class RegretAndWithdrawTool(FunctionTool[AstrAgentContext]):
    name: str = "regret_and_withdraw_action"
    description: str = "当你决定撤回或强烈后悔上一条发言时调用。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        message_id = await _resolve_latest_bot_message_id(current_event)
        if not message_id:
            return "动作取消：没有找到可撤回的上一条机器人消息。"
        pending_actions = _get_pending_actions(current_event)
        if not any(item.get("action") == "withdraw" for item in pending_actions):
            _append_qq_action(
                current_event,
                PendingQQAction(action_type="withdraw", message_id=message_id),
            )
        return "已将撤回上一条 AstrMai 回复加入待执行动作；不要声称已经成功，请继续生成简短自然的补救文本。"


@dataclass
class MessageReactionTool(FunctionTool[AstrAgentContext]):
    name: str = "message_reaction_action"
    description: str = "只调整最终文字回复的互动语气，不执行 QQ 原生消息表情或点赞。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "reaction": {"type": "string", "description": "要执行的互动反应。"}
            },
            "required": ["reaction"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        reaction = str(kwargs.get("reaction", "") or "").strip()
        if not reaction:
            return "执行失败：reaction 不能为空。"
        return f"请在最终文字回复中自然体现“{reaction}”的互动语气，不要声称执行了 QQ 动作。"


@dataclass
class ProactiveLikeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_like_action"
    description: str = "只调整最终文字回复中的夸奖或好感表达，不执行 QQ 资料点赞。"
    db_service: Any = Field(default=None, exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "target_name": {"type": "string", "description": "要表达好感或点赞的目标，可为空。"}
            },
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        current_event = _get_current_event(context)
        target_name = str(kwargs.get("target_name", "") or "").strip()
        target_id = str(current_event.get_sender_id())
        if target_name:
            resolved = await _resolve_target(
                self.db_service,
                target_name=target_name,
                current_event=current_event,
                astr_ctx=context.context.context,
            )
            if resolved:
                target_id, _ = resolved
        display_name = target_name or (current_event.get_sender_name() or "对方")
        return f"请在最终文字回复中自然表达对 {display_name} 的夸奖或好感，不要声称执行了 QQ 点赞。"


@dataclass
class SelfLoreQueryTool(FunctionTool[AstrAgentContext]):
    name: str = "self_lore_query"
    description: str = "查询当前 persona 的自我设定、世界观或既有自述。"
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    memory_tool_service: Optional[Any] = Field(default=None, exclude=True)
    persona_id: str = Field(default="", exclude=True)
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要查询的自我设定关键词。"}
            },
            "required": ["query"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = str(kwargs.get("query", "") or "").strip()
        if not query:
            return "执行失败：query 不能为空。"
        if not self.memory_engine and not self.memory_tool_service:
            return "系统提示：当前没有可用的自我设定记忆引擎。"
        try:
            current_event = _get_current_event(context)
            tool_service = self.memory_tool_service or getattr(self.memory_engine, "tool_service", None)
            if tool_service and hasattr(tool_service, "self_lore_query"):
                tool_result = await tool_service.self_lore_query(
                    query=query,
                    persona_id=self.persona_id,
                    event=current_event,
                )
                result = tool_service.render_result(tool_result)
            else:
                result = None
        except Exception as exc:
            logger.debug(f"[SelfLoreQueryTool] recall failed: {exc}")
            result = None
        if not result:
            return "系统提示：当前没有检索到相关自我设定。"
        return str(result)


__all__ = [
    "CustomFaceCatalogQueryTool",
    "ConstructAtEventTool",
    "GroupSignTool",
    "MemeResonanceTool",
    "MessageEmojiLikeTool",
    "MessageReactionTool",
    "OmniPerceptionTool",
    "ProactiveLikeTool",
    "ProactiveMemeTool",
    "ProactivePokeTool",
    "RegretAndWithdrawTool",
    "SelfLoreQueryTool",
    "SpaceTransitionTool",
    "TopicHijackTool",
    "WaitTool",
]
