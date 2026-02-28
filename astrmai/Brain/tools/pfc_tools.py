from typing import Any, Optional
from pydantic import Field
from pydantic.dataclasses import dataclass 

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_context import AstrAgentContext

@dataclass
class WaitTool(FunctionTool[AstrAgentContext]):
    name: str = "wait_and_listen"
    description: str = "当你认为对方话没说完，需要等待用户补充；或者你能量过低，不想立即生成长回复时调用此工具。"
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        # 触发执行器的挂起逻辑
        return "[SYSTEM_WAIT_SIGNAL] 已挂起，等待用户后续输入。"

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

    # [新增] 依赖注入，使用 exclude=True 避免被序列化到 LLM 的 Schema 中
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> str:
        query = kwargs.get("query", "")
        
        # [完善] 接入真实的 Memory 混合检索
        if self.memory_engine and self.chat_id:
            try:
                # 调用 memory_engine 的 recall 方法 (它已内置向量/BM25双路检索和时间衰减)
                result = await self.memory_engine.recall(query=query, session_id=self.chat_id)
                return result
            except Exception as e:
                return f"[Knowledge] 检索记忆时发生底层异常: {str(e)}"
                
        # 兜底：如果外部未正确挂载引擎
        return f"[Knowledge] 无法检索 '{query}' 的记忆，记忆引擎离线或丢失会话 ID。"