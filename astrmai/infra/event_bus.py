import asyncio
from typing import Callable, List

class EventBus:
    """
    事件总线 (Infrastructure Layer)
    用于 System 1 (Heart) 和 System 2 (Brain) 之间的解耦通信
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EventBus, cls).__new__(cls)
            cls._instance._init_bus()
        return cls._instance

    def _init_bus(self):
        # 信号定义
        self.abort_signal = asyncio.Event()     # 打断思考
        self.response_sent = asyncio.Event()    # 回复已发送 (触发潜意识)
        
        # 简单的观察者列表 (如果需要复杂逻辑)
        self.subscribers: List[Callable] = []

    def trigger_abort(self):
        """触发打断信号"""
        self.abort_signal.set()

    def reset_abort(self):
        self.abort_signal.clear()

    async def wait_for_abort(self):
        """等待打断信号"""
        await self.abort_signal.wait()