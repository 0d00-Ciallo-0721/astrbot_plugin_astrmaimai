class System2:
    """
    System 2 Facade
    """

    def __init__(self, infra):
        from .context_engine import ContextEngine
        from .planner import Planner
        from .executor import ConcurrentExecutor

        self.context_engine = ContextEngine(infra.db)
        self.planner = Planner(infra.gateway, self.context_engine)
        self.executor = ConcurrentExecutor(infra.gateway.context)

    async def process(self, chat_id: str, messages: list):
        """
        Cognitive Loop Entry
        """
        plan = await self.planner.plan(chat_id, messages, tools_map={})
        trigger_event = messages[-1]
        await self.executor.execute(plan, trigger_event)


__all__ = ["System2"]
