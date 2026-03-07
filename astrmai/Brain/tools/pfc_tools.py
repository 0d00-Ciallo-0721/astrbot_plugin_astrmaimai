from typing import Any, Optional
from pydantic import Field
from pydantic.dataclasses import dataclass 

from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

@dataclass
class WaitTool(FunctionTool[AstrAgentContext]):
    name: str = "wait_and_listen"
    # 【修复】增加强指令约束，防止大模型在 Agent Loop 的最后一步不输出挂起信号
    description: str = (
        "当你认为对方话没说完，需要等待用户补充；或者你能量过低不想立即生成回复时调用此工具。"
        "⚠️注意：一旦调用此工具，你必须在最终的回复内容中原样输出 '[SYSTEM_WAIT_SIGNAL]'，并且不要带有任何其他解释性文字。"
    )
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        # 这个结果仅作为 observation 返回给大模型，最终的截断依赖大模型的严格复述
        return "动作执行成功：请在最终回复中原样输出 '[SYSTEM_WAIT_SIGNAL]'"

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

    # 依赖注入，使用 exclude=True 避免被序列化到 LLM 的 Schema 中
    memory_engine: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)
    # 【新增】预留 persona_id 进行物理隔离
    persona_id: Optional[str] = Field(default=None, exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        query = kwargs.get("query", "")
        
        # 接入真实的 Memory 混合检索
        if self.memory_engine and self.chat_id:
            try:
                # 【修复】同时透传 persona_id 以保证未来多个人设隔离时的检索准确性
                result = await self.memory_engine.recall(
                    query=query, 
                    session_id=self.chat_id,
                    persona_id=self.persona_id
                )
                return result
            except Exception as e:
                return f"[Knowledge] 检索记忆时发生底层异常: {str(e)}"
                
        # 兜底：如果外部未正确挂载引擎
        return f"[Knowledge] 无法检索 '{query}' 的记忆，记忆引擎离线或丢失会话 ID。"


@dataclass
class QueryJargonTool(FunctionTool[AstrAgentContext]):
    """查询黑话/网络用语工具"""
    name: str = "query_jargon"
    description: str = "查询黑话/网络用语/群组特定术语的含义。当你在对话中遇到不理解的词语时使用。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "需要查询含义的黑话或词汇"}
            },
            "required": ["keyword"]
        }
    )

    db_service: Optional[Any] = Field(default=None, exclude=True)
    chat_id: str = Field(default="", exclude=True)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        keyword = kwargs.get("keyword", "")
        if not keyword:
            return "未提供查询关键词。"
            
        if self.db_service and self.chat_id and hasattr(self.db_service, 'get_jargons'):
            try:
                # 获取群组内已确认的黑话 (同步调用在少量查询下没问题)
                jargons = self.db_service.get_jargons(self.chat_id, limit=50, only_confirmed=True)
                for j in jargons:
                    if keyword in j.content or j.content in keyword:
                        meaning = j.meaning if j.meaning else '含义正在推断中...'
                        return f"查找到黑话记录 -> 「{j.content}」的含义是: {meaning}"
                return f"数据库中暂未收录关于「{keyword}」的黑话记录，请基于上下文自行推断或询问用户。"
            except Exception as e:
                return f"查询黑话库失败: {e}"
                
        return "黑话数据库服务当前不可用，请根据上下文推断含义。"