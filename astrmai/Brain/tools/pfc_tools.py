from pydantic import Field
from pydantic.dataclasses import dataclass
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

@dataclass
class WaitTool(FunctionTool[AstrAgentContext]):
    name: str = "wait_and_listen"
    description: str = "当你认为对方话没说完，需要等待用户补充；或者你能量过低，不想立即生成长回复时调用此工具。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        # 返回特定的中断信号，将在 Executor 中被捕获拦截
        return ToolExecResult(result="[SYSTEM_WAIT_SIGNAL] 已挂起，等待用户后续输入。", is_error=False)

@dataclass
class FetchKnowledgeTool(FunctionTool[AstrAgentContext]):
    name: str = "fetch_knowledge"
    description: str = "需要调取知识或记忆，当用户提到以前发生过的事情，或需要专业知识、特定信息时调用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "需要从记忆库中查询的关键词或问题"}
            },
            "required": ["query"]
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        query = kwargs.get("query", "")
        # 🚧 预留给阶段四 (Memory Engine) 接入使用
        return ToolExecResult(result=f"[Knowledge] 模拟检索关于 '{query}' 的记忆... (待 Memory 层接入)", is_error=False)