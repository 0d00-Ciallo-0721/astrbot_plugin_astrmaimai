from typing import List
from astrbot.api.event import AstrMessageEvent

from ..infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine
from .executor import ConcurrentExecutor
from .tools.pfc_tools import WaitTool, FetchKnowledgeTool
from .reply_engine import ReplyEngine

class Planner:
    """
    认知总控 (System 2)
    职责: 统筹编排 System 2。将聚合的消息与环境状态拼装，定义原生工具栈，然后下发给 Executor 驱动智能体循环。
    """
    def __init__(self, context, gateway: GlobalModelGateway, context_engine: ContextEngine):
        self.gateway = gateway
        self.context_engine = context_engine
        self.executor = ConcurrentExecutor(context, gateway, reply_engine)

    async def plan_and_execute(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent]):
        """
        重构后的核心入口：跳过脆弱的手工 ReAct，全面基于 AstrBot Agent。
        """
        chat_id = event.unified_msg_origin
        
        # 1. 消息重组：将防抖队列中积压的消息合并为单个用户 Prompt
        prompt = "\n".join([f"{m.get_sender_name()}: {m.message_str}" for m in event_messages])
        
        # 2. 潜意识与动态状态注入 (System Prompt 构建)
        # [Fix] 传入 event_messages 给 ContextEngine
        system_prompt = await self.context_engine.build_prompt(
            chat_id=chat_id, 
            messages=event_messages
        )
        
        # 3. 装配前额叶基建工具 (PFC Actions)
        pfc_tools = [
            WaitTool(),
            FetchKnowledgeTool()
        ]
        
        # 4. 移交并发执行器引爆思考闭环
        await self.executor.execute(
            event=event,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=pfc_tools
        )