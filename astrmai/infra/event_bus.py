# astrmai/infra/event_bus.py
import asyncio
from typing import Callable, List, Dict, Any

class EventBus:
    """
    事件总线 (Infrastructure Layer)
    用于 System 1 (Heart) 和 System 2 (Brain) 之间的解耦通信 (单例模式)
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EventBus, cls).__new__(cls)
            cls._instance._init_bus()
        return cls._instance

    def _init_bus(self):
        # 基础信号定义
        self.abort_signal = asyncio.Event()     # 打断思考
        self.response_sent = asyncio.Event()    # 回复已发送
        
        # 状态变更广播信号
        self.affection_changed = asyncio.Event() # 好感度/社交数值变更
        self.knowledge_updated = asyncio.Event() # 黑话/表达模式挖掘完成
        
        # 真正的发布-订阅路由器
        self.subscribers: Dict[str, List[Callable]] = {}

    # ==========================
    # 基础 Event 触发机制 (遗留兼容)
    # ==========================
    def trigger_abort(self):
        self.abort_signal.set()

    def reset_abort(self):
        self.abort_signal.clear()

    async def wait_for_abort(self):
        await self.abort_signal.wait()

    def trigger_affection_change(self):
        self.affection_changed.set()
        self.affection_changed.clear()

    def trigger_knowledge_update(self):
        self.knowledge_updated.set()
        self.knowledge_updated.clear()        

    # ==========================
    # 真正的 Pub/Sub 机制 (新增)
    # ==========================
    def subscribe(self, topic: str, callback: Callable):
        """订阅一个主题"""
        if topic not in self.subscribers:
            self.subscribers[topic] = []
        if callback not in self.subscribers[topic]:
            self.subscribers[topic].append(callback)

    def unsubscribe(self, topic: str, callback: Callable):
        """取消订阅一个主题"""
        if topic in self.subscribers and callback in self.subscribers[topic]:
            self.subscribers[topic].remove(callback)

    async def publish(self, topic: str, data: dict = None):
        """发布一个主题及负载数据"""
        if data is None:
            data = {}
        
        if topic in self.subscribers:
            for callback in self.subscribers[topic]:
                # 如果是异步回调，则 await 执行；否则同步执行
                if asyncio.iscoroutinefunction(callback):
                    await callback(data)
                else:
                    callback(data)