from .context_engine import ContextEngine
from .planner import Planner
from .executor import ConcurrentExecutor

class System2:
    """
    System 2 Facade
    """
    def __init__(self, infra):
        self.context_engine = ContextEngine(infra.db)
        self.planner = Planner(infra.gateway, self.context_engine)
        self.executor = ConcurrentExecutor(infra.gateway.context) # 需传入 context 用于发送

    async def process(self, chat_id: str, messages: list):
        """
        Cognitive Loop Entry
        """
        # 1. Plan
        # TODO: 传入真实的 tools map
        plan = await self.planner.plan(chat_id, messages, tools_map={})
        
        # 2. Act (取第一条触发 System 2 的消息作为 event anchor)
        trigger_event = messages[-1]
        await self.executor.execute(plan, trigger_event)