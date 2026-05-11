class DreamMaintenanceService:
    def __init__(self, memory_engine):
        self.memory_engine = memory_engine

    async def apply_decay(self, decay_rate: float, days: int = 1) -> int:
        return await self.memory_engine.apply_daily_decay(decay_rate=decay_rate, days=days)
