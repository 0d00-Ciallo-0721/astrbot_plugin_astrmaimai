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
        
        # [新增] 状态变更广播信号 (用于通知 ContextEngine 清理缓存或实时注入)
        self.affection_changed = asyncio.Event() # 好感度/社交数值变更
        self.knowledge_updated = asyncio.Event() # 黑话/表达模式挖掘完成
        
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

    def trigger_affection_change(self):
        """[新增] 触发好感度/社交图谱变更信号"""
        self.affection_changed.set()
        # 广播后自动重置，支持下一次触发
        self.affection_changed.clear()

    def trigger_knowledge_update(self):
        """[新增] 触发新知识(黑话/句式)挖掘完成信号"""
        self.knowledge_updated.set()
        self.knowledge_updated.clear()        