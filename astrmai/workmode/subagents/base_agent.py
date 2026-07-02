"""
AstrMai SubAgent 抽象基类
职责：
1. 屏蔽 ContextWrapper 三层穿透的样板代码
2. 统一工具可用性健康检查与优雅退回（Edge Case 闭环）
3. 拉起独立 tool_loop_agent 循环并返回结果
"""
from pydantic import Field
from pydantic.dataclasses import dataclass
# ponytail: these are internal AstrBot APIs — may break on major version upgrades.
# Monitor AstrBot changelog for FunctionTool/CallableTool/ContextWrapper migration.
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
                    "description": "需要此子智能体执行的任务描述，越具体越好",
                }
            },
            "required": ["query"],
        }
    )

    async def get_system_prompt(self, ctx, event) -> str:
        return "你是一个专业的任务执行助手，请认真完成交给你的任务。"

    async def get_tool_set(self, ctx, event) -> ToolSet:
        return ToolSet([])

    def get_max_steps(self) -> int:
        return 12

    def get_timeout(self) -> int:
        return 60

    async def _get_decline_reason(self) -> str:
        return "所需工具或执行环境未激活（如沙盒未启动、API 未配置）"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        query = kwargs.get("query", "（无任务描述）")

        astr_agent_ctx = context.context
        if astr_agent_ctx is None:
            logger.error(f"[Sys3/{self.name}] ContextWrapper.context is None; AstrBot API may have changed")
            return "[SUBAGENT_ERROR] 系统上下文不可用，请联系管理员检查 AstrBot 版本兼容性。"
        ctx = astr_agent_ctx.context
        event = astr_agent_ctx.event
        if ctx is None or event is None:
            logger.error(f"[Sys3/{self.name}] astr_agent_ctx.context={ctx}, .event={event}; AstrBot API may have changed")
            return "[SUBAGENT_ERROR] 系统上下文不完整，无法执行任务。"

        try:
            provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception as exc:
            logger.error(f"[Sys3/{self.name}] 无法获取 Provider ID: {exc}")
            return f"[SUBAGENT_ERROR] 无法连接到语言模型服务：{exc}。请告知用户检查 Provider 配置。"

        try:
            tools = await self.get_tool_set(ctx, event)
        except Exception as exc:
            logger.error(f"[Sys3/{self.name}] 获取工具集失败: {exc}")
            tools = ToolSet([])

        active_tools = [tool for tool in tools.tools if getattr(tool, "active", True)]
        if not active_tools:
            reason = await self._get_decline_reason()
            logger.warning(f"[Sys3/{self.name}] 无可用工具，优雅退回。原因: {reason}")
            return (
                f"[SUBAGENT_DECLINE] 我当前无法执行这个任务。\n"
                f"原因：{reason}\n"
                "请如实告知用户此功能暂时不可用，并建议他联系管理员检查相关配置。"
            )

        system_prompt = await self.get_system_prompt(ctx, event)
        logger.info(f"[Sys3/{self.name}] 接受任务，ReAct 启动: {query[:60]}...")

        try:
            # 优先走 Gateway（享受模型路由/重试/冷却）
            gateway = getattr(ctx, "gateway", None)
            if gateway is not None:
                from ...infrastructure.runtime.lane_manager import LaneKey
                models = gateway.get_agent_models() if hasattr(gateway, "get_agent_models") else []
                result = await gateway.tool_chat_in_lane_result(
                    lane_key=LaneKey(subsystem="sys3", task_family=self.name, scope_id=str(event.unified_msg_origin)),
                    base_origin=str(event.unified_msg_origin),
                    event=event,
                    prompt=query,
                    system_prompt=system_prompt,
                    tools=ToolSet(active_tools),
                    models=models,
                    max_steps=self.get_max_steps(),
                    timeout=self.get_timeout(),
                )
                result_text = result.text if hasattr(result, "text") else str(result)
                logger.info(f"[Sys3/{self.name}] 任务完成 (via Gateway)，结果长度: {len(result_text)} 字")
                return result_text
            # 回退：裸 AstrBot provider
            llm_resp = await ctx.tool_loop_agent(
                event=event,
                chat_provider_id=provider_id,
                prompt=query,
                system_prompt=system_prompt,
                tools=ToolSet(active_tools),
                max_steps=self.get_max_steps(),
                tool_call_timeout=self.get_timeout(),
            )
            result = getattr(llm_resp, "completion_text", None) or "任务已执行完毕，但无文字输出。"
            logger.info(f"[Sys3/{self.name}] 任务完成 (raw provider)，结果长度: {len(result)} 字")
            return result
        except Exception as exc:
            logger.error(f"[Sys3/{self.name}] 内部 ReAct 循环异常: {exc}", exc_info=True)
            return (
                f"[SUBAGENT_ERROR] 任务执行过程中发生错误：{str(exc)[:150]}\n"
                "请告知用户稍后重试，并在日志中查看详细错误信息。"
            )


__all__ = ["AstrMaiBaseSubAgent"]
