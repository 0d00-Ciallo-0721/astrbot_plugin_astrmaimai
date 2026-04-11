class System1:
    """
    System 1 Facade
    """

    def __init__(self, infra, system2_callback):
        from .attention import AttentionGate
        from .judge import Judge
        from .sensors import PreFilters
        from .state_engine import StateEngine

        self.state_engine = StateEngine(infra.db, infra.gateway)
        self.judge = Judge(infra.gateway, self.state_engine)
        self.sensors = PreFilters(None)
        self.attention = AttentionGate(self.state_engine, self.judge, self.sensors, system2_callback)

    async def process(self, event):
        await self.attention.process_event(event)


__all__ = ["System1"]
