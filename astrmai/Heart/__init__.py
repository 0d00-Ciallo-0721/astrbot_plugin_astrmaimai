from .state_engine import StateEngine
from .judge import Judge
from .attention import AttentionGate
from .sensors import PreFilters

class System1:
    """
    System 1 Facade
    """
    def __init__(self, infra, system2_callback):
        self.state_engine = StateEngine(infra.db, infra.gateway)
        self.judge = Judge(infra.gateway, self.state_engine)
        self.sensors = PreFilters(None) # Config passed later
        self.attention = AttentionGate(self.state_engine, self.judge, self.sensors, system2_callback)

    async def process(self, event):
        await self.attention.process_event(event)