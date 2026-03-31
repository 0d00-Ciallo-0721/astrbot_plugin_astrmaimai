"""
AstrMai SubAgent 抽象基类
职责：
1. 屏蔽 ContextWrapper 三层穿透的样板代码
2. 统一工具可用性健康检查与优雅退回（Edge Case 闭环）
3. 拉起独立 tool_loop_agent 循环并返回结果
"""
from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.api import logger


@dataclass
class AstrMaiBaseSubAgent(FunctionTool[AstrAgentContext]):
    """
    所有 Sys3 子智能体的抽象基类
    子类需要重写：name, description, get_system_prompt(), get_tool_set()
    """
    name: str = "base_agent"
    description: str = "基础子智能体（请勿直接使用）"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "需要此子智能体执行的任务描述，越具体越好"
                }
            },
            "required": ["query"]
        }
    )

    # ─── 子类重写区域 ───────────────────────────────────────────────────────

    async def get_system_prompt(self, ctx, event) -> str:
        """子类必须重写：此 SubAgent 的独立角色提示词"""
        return "你是一个专业的任务执行助手，请认真完成交给你的任务。"

    async def get_tool_set(self, ctx, event) -> ToolSet:
        """子类必须重写：此 SubAgent 拥有的专属工具集"""
        return ToolSet([])

    def get_max_steps(self) -> int:
        """子类可选重写：ReAct 循环最大步数（默认 12）"""
        return 12

    def get_timeout(self) -> int:
        """子类可选重写：单次工具调用超时秒数（默认 60）"""
        return 60

    async def _get_decline_reason(self) -> str:
        """子类可重写：无工具时提供更具体的退回原因"""
        return "所需工具或执行环境未激活（如沙盒未启动、API 未配置）"

    # ─── 框架核心层（子类通常不重写）─────────────────────────────────────────

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        query = kwargs.get("query", "（无任务描述）")

        # ━━━ 三层上下文穿透 ━━━
        # context                    → ContextWrapper[AstrAgentContext]
        # context.context            → AstrAgentContext
        # context.context.context    → astrbot Context（全局资源入口）
        # context.context.event      → AstrMessageEvent
        astr_agent_ctx = context.context
        ctx = astr_agent_ctx.context
        event = astr_agent_ctx.event

        # ━━━ 获取 LLM Provider ID ━━━
        try:
            provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception as e:
            logger.error(f"[Sys3/{self.name}] 无法获取 Provider ID: {e}")
            return f"[SUBAGENT_ERROR] 无法连接到语言模型服务：{e}。请告知用户检查 Provider 配置。"

        # ━━━ 工具健康检查 ━━━
        try:
            tools = await self.get_tool_set(ctx, event)
        except Exception as e:
            logger.error(f"[Sys3/{self.name}] 获取工具集失败: {e}")
            tools = ToolSet([])

        active_tools = [t for t in tools.tools if getattr(t, 'active', True)]

        if not active_tools:
            reason = await self._get_decline_reason()
            logger.warning(f"[Sys3/{self.name}] 无可用工具，优雅退回。原因: {reason}")
            return (
                f"[SUBAGENT_DECLINE] 我当前无法执行这个任务。\n"
                f"原因：{reason}\n"
                f"请如实告知用户此功能暂时不可用，并建议他联系管理员检查相关配置。"
            )

        # ━━━ 构建提示词 ━━━
        system_prompt = await self.get_system_prompt(ctx, event)

        # ━━━ 启动独立 ReAct 循环 ━━━
        logger.info(f"[Sys3/{self.name}] ▶ 接受任务，ReAct 启动: {query[:60]}...")
        try:
            llm_resp = await ctx.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=query,
                system_prompt=system_prompt,
                tools=ToolSet(active_tools),
                max_steps=self.get_max_steps(),
                tool_call_timeout=self.get_timeout()
            )
            result = getattr(llm_resp, 'completion_text', None) or "任务已执行完毕，但无文字输出。"
            logger.info(f"[Sys3/{self.name}] ✅ 任务完成，结果长度: {len(result)} 字")
            return result

        except Exception as e:
            logger.error(f"[Sys3/{self.name}] 内部 ReAct 循环异常: {e}", exc_info=True)
            return (
                f"[SUBAGENT_ERROR] 任务执行过程中发生错误：{str(e)[:150]}\n"
                f"请告知用户稍后重试，并在日志中查看详细错误信息。"
            )