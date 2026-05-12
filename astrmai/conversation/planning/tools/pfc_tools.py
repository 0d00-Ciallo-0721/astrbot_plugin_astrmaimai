from __future__ import annotations

import asyncio
from typing import Any, Optional

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext


def _get_current_event(context: ContextWrapper[AstrAgentContext]):
    return context.context.event


def _get_pending_actions(event) -> list[dict[str, Any]]:
    pending_actions = event.get_extra("astrmai_pending_actions", [])
    if isinstance(pending_actions, list):
        return pending_actions
    return []


def _set_pending_actions(event, pending_actions: list[dict[str, Any]]) -> None:
    event.set_extra("astrmai_pending_actions", pending_actions)


def _append_pending_action(event, action: dict[str, Any]) -> None:
    pending_actions = _get_pending_actions(event)
    pending_actions.append(action)
    _set_pending_actions(event, pending_actions)


def _append_once(event, *, matcher, action: dict[str, Any]) -> bool:
    pending_actions = _get_pending_actions(event)
    if any(matcher(item) for item in pending_actions):
        return False
    pending_actions.append(action)
    _set_pending_actions(event, pending_actions)
    return True


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
            if not self.memory_engine or not query or not self.chat_id:
                return None
            search_query = f"{target_name} {query}".strip() if target_name else query
            try:
                if hasattr(self.memory_engine, "query"):
                    return await self.memory_engine.query(query=search_query, session_id=self.chat_id)
                if hasattr(self.memory_engine, "search"):
                    return await self.memory_engine.search(query=search_query, session_id=self.chat_id)
                if hasattr(self.memory_engine, "recall"):
                    return await self.memory_engine.recall(query=search_query, session_id=self.chat_id)
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] memory lookup failed: {exc}")
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
            if not self.db_service or not query:
                return None
            try:
                if hasattr(self.db_service, "get_jargon"):
                    jargon = self.db_service.get_jargon(self.chat_id, query)
                    if jargon:
                        return f"{query}: {jargon}"
            except Exception as exc:
                logger.debug(f"[OmniPerceptionTool] jargon lookup failed: {exc}")
            return None

        memory_result, profile_result, node_result, reflection_result, jargon_result = await asyncio.gather(
            _fetch_memory(),
            _fetch_profile(),
            _fetch_nodes(),
            _fetch_reflection(),
            _fetch_jargon(),
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
            matcher=lambda item: item.get("action") == "at" and str(item.get("target_id")) == target_id,
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
            target_id, _ = resolved
        else:
            target_id = str(current_event.get_sender_id())

        if target_id == str(current_event.get_self_id()):
            return "动作取消：不能戳自己。"

        try:
            client = getattr(current_event, "bot", None)
            api = getattr(client, "api", None)
            if api is None:
                return "动作取消：底层 poke API 不可用。"
            if group_id:
                await api.call_action("send_poke", user_id=int(target_id), group_id=int(group_id))
            else:
                await api.call_action("send_poke", user_id=int(target_id))
            logger.info(f"[ProactivePokeTool] poked target={target_id} group={group_id}")
            return f"已主动戳了 {display_name}，请继续生成自然的文字回应。"
        except Exception as exc:
            logger.error(f"[ProactivePokeTool] execution failed: {exc}")
            return f"动作执行失败：{exc}"


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
        _append_once(
            current_event,
            matcher=lambda item: item.get("action") == "withdraw",
            action={"action": "withdraw"},
        )
        return "已记录撤回动作，请继续生成简短自然的补救文本。"


@dataclass
class MessageReactionTool(FunctionTool[AstrAgentContext]):
    name: str = "message_reaction_action"
    description: str = "为当前消息补一个简短互动反应。"
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
        current_event = _get_current_event(context)
        reaction = str(kwargs.get("reaction", "") or "").strip()
        if not reaction:
            return "执行失败：reaction 不能为空。"
        _append_pending_action(current_event, {"action": "reaction", "reaction": reaction})
        return f"已记录互动反应 {reaction}，请继续生成最终回复。"


@dataclass
class ProactiveLikeTool(FunctionTool[AstrAgentContext]):
    name: str = "proactive_like_action"
    description: str = "表达明确的点赞、夸奖或正向偏爱。"
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
        _append_pending_action(
            current_event,
            {
                "action": "like",
                "target_id": target_id,
                "target_name": target_name or (current_event.get_sender_name() or ""),
            },
        )
        return "已记录正向偏好动作，请继续生成带一点明确好感的回复。"


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
            elif hasattr(self.memory_engine, "recall_persona_lore"):
                result = await self.memory_engine.recall_persona_lore(query=query, persona_id=self.persona_id)
            elif hasattr(self.memory_engine, "query_persona_lore"):
                result = await self.memory_engine.query_persona_lore(query=query, persona_id=self.persona_id)
            else:
                result = None
        except Exception as exc:
            logger.debug(f"[SelfLoreQueryTool] recall failed: {exc}")
            result = None
        if not result:
            return "系统提示：当前没有检索到相关自我设定。"
        return str(result)


__all__ = [
    "ConstructAtEventTool",
    "MemeResonanceTool",
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
