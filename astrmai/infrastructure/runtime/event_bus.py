# astrmai/infra/event_bus.py
import asyncio
from typing import Callable, List, Dict, Any

from ...shared.helpers.plugin_helpers import safe_create_task
from .turn_call_ledger import detach_turn_telemetry

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
        self.TOPIC_AFFECTION_CHANGED = "affection_changed"
        self.TOPIC_KNOWLEDGE_UPDATED = "knowledge_updated"
        self.TOPIC_LEARNING_MESSAGE_RECORDED = "learning.message_recorded"
        self.TOPIC_LEARNING_BOT_REPLY_RECORDED = "learning.bot_reply_recorded"
        self.TOPIC_LEARNING_MINING_COMPLETED = "learning.mining_completed"
        self.TOPIC_MEMORY_TURN_COMMITTED = "memory.turn_committed"

        # 基础信号定义
        self.abort_signal = asyncio.Event()     # 打断思考
        self.response_sent = asyncio.Event()    # 回复已发送
        
        # 状态变更广播信号
        self.affection_changed = asyncio.Event() # 好感度/社交数值变更
        self.knowledge_updated = asyncio.Event() # 黑话/表达模式挖掘完成
        
        # 真正的发布-订阅路由器
        self.subscribers: Dict[str, List[Callable]] = {}
        
        # 🟢 [深层修复 Bug 4] 引入 MPSC 有界缓冲队列与消费者状态，防止风暴击穿事件循环
        self._event_queue = asyncio.Queue(maxsize=1000)
        self._dropped_count = 0
        self._workers_started = False
        self._background_tasks = set()
        self._worker_tasks: set = set()  # ponytail: worker-only tracking (R8)
        self._generation = 0

    # ==========================
    # 基础 Event 触发机制 (遗留兼容)
    # ==========================
    def trigger_abort(self):
        self.abort_signal.set()

    def reset_abort(self):
        self.abort_signal.clear()

    async def wait_for_abort(self):
        await self.abort_signal.wait()

    async def trigger_affection_change(self):
        """
        [彻底修复 Bug 4] 剔除高并发下导致竞态条件的 asyncio.Event .clear()，
        防止事件丢失，直接依赖稳定的 pub/sub 路由广播。
        """
        self.affection_changed.set()
        await self.publish(self.TOPIC_AFFECTION_CHANGED)
        self.affection_changed.clear()

    def trigger_knowledge_update(self):
        self.knowledge_updated.set()
        try:
            safe_create_task(
                self._clear_knowledge_after_publish(),
                name="eventbus:knowledge_update",
                track_set=self._background_tasks,
            )
        except RuntimeError:
            pass

    async def _clear_knowledge_after_publish(self):
        await self.publish(self.TOPIC_KNOWLEDGE_UPDATED)
        await asyncio.sleep(0.1)
        self.knowledge_updated.clear()

    async def publish_learning_message_recorded(self, payload: dict):
        await self.publish(self.TOPIC_LEARNING_MESSAGE_RECORDED, payload)

    async def publish_learning_bot_reply_recorded(self, payload: dict):
        await self.publish(self.TOPIC_LEARNING_BOT_REPLY_RECORDED, payload)

    async def publish_learning_mining_completed(self, payload: dict):
        await self.publish(self.TOPIC_LEARNING_MINING_COMPLETED, payload)

    async def publish_memory_turn_committed(self, payload: dict):
        await self.publish(self.TOPIC_MEMORY_TURN_COMMITTED, payload)

    # ==========================
    # 真正的 Pub/Sub 机制 (新增)
    # ==========================
    def subscribe(self, topic: str, callback: Callable): # [修改]
        """
        订阅一个主题
        [彻底修复 Bug 3] 引入智能引用机制：针对实例方法防循环引用，普通函数/闭包保持强引用防止被幽灵回收
        """
        import inspect
        import weakref
        
        if topic not in self.subscribers:
            self.subscribers[topic] = []
            
        # 针对绑定方法使用 WeakMethod，普通函数使用强引用
        if inspect.ismethod(callback):
            ref = weakref.WeakMethod(callback)
        else:
            ref = callback
            
        # 避免重复订阅 (解包混合引用验证真实目标)
        if not any((r() if isinstance(r, weakref.WeakMethod) else r) == callback for r in self.subscribers[topic] if (r() if isinstance(r, weakref.WeakMethod) else r) is not None):
            self.subscribers[topic].append(ref)


    def unsubscribe(self, topic: str, callback: Callable): # [修改]
        """
        取消订阅一个主题
        [彻底修复 Bug 3] 适配混合引用的退订逻辑
        """
        import weakref
        if topic in self.subscribers:
            self.subscribers[topic] = [
                r for r in self.subscribers[topic] 
                if ((r() if isinstance(r, weakref.WeakMethod) else r) is not None) and 
                   ((r() if isinstance(r, weakref.WeakMethod) else r) != callback)
            ]

    # [新增]
    async def _worker_loop(self):
        """专门从队列中消费事件的安全消费者协程，阻断无界限 Task 爆炸，同时实行异步派发防阻塞"""
        from astrbot.api import logger
        import weakref

        # OPT-02/RT-01: worker 在 publish() 内懒启动，会随 create_task 继承首个 turn 的
        # telemetry contextvar；不斩断则原轮预算耗尽后 worker 内所有 LLM 调用永久秒败
        detach_turn_telemetry()

        while True:
            try:
                generation, topic, data = await self._event_queue.get()
                if generation != self._generation:
                    self._event_queue.task_done()
                    continue
                if topic not in self.subscribers:
                    self._event_queue.task_done()
                    continue

                active_callbacks = []
                for item in list(self.subscribers[topic]):
                    if isinstance(item, weakref.WeakMethod):
                        cb = item()
                        if cb is not None:
                            active_callbacks.append(cb)
                        else:
                            self.subscribers[topic].remove(item)
                    else:
                        active_callbacks.append(item)

                for callback in active_callbacks:
                    try:
                        # 🟢 [核心修复 Bug 3] Fire-and-Forget 模式：绝对不要在此处 await 阻塞 Worker！
                        if asyncio.iscoroutinefunction(callback):
                            safe_create_task(
                                callback(data),
                                name=f"eventbus:{topic}",
                                track_set=self._background_tasks,
                            )
                        else:
                            callback(data)
                    except Exception as e:
                        logger.error(f"[EventBus] Topic '{topic}' callback dispatch error: {e}")

                self._event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[EventBus] Worker loop runtime error: {e}")

    async def _worker_health_check(self):
        """每 30s 检查 worker 数量，不足时自动补足至 3 个。"""
        # ponytail: _workers_started is never toggled False during normal operation,
        # but serves as a safety sentinel for clean shutdown via stop().
        while self._workers_started:
            await asyncio.sleep(30)
            # ponytail: only count _worker_tasks, not dispatch tasks (R8)
            active = sum(1 for t in list(self._worker_tasks) if not t.done())
            needed = 3 - active
            for _ in range(max(0, needed)):
                t = safe_create_task(self._worker_loop())
                self._background_tasks.add(t)
                self._worker_tasks.add(t)
            if needed > 0:
                logger.warning(f"[EventBus] restarted {needed} worker(s), now {active + needed} total")
    # [修改]
    async def publish(self, topic: str, data: dict = None): 
        """
        发布一个主题及负载数据 
        [深层修复 Bug 4] 纯异步非阻塞平滑入队，由后台 MPSC Worker 统一安全消化
        """
        if data is None:
            data = {}
            
        if topic not in self.subscribers:
            return
            
        # 懒加载启动后台消费 Worker 池
        if not self._workers_started:
            self._workers_started = True
            for _ in range(3):  # 拉起 3 个稳定常驻 Worker
                task = safe_create_task(self._worker_loop())
                self._background_tasks.add(task)
                self._worker_tasks.add(task)
            t = safe_create_task(self._worker_health_check())  # 健康检查
            self._background_tasks.add(t)
        
        try:
            # 使用 nowait 防止高频事件反向阻塞关键请求链路，溢出时告警
            self._event_queue.put_nowait((self._generation, topic, data))
        except asyncio.QueueFull:
            self._dropped_count += 1
            from astrbot.api import logger
            logger.warning(
                f"[EventBus] queue full (size={self._event_queue.qsize()}), "
                f"dropped topic={topic}, total_dropped={self._dropped_count}"
            )

    # ponytail: graceful shutdown — cancels all workers and health check
    async def stop(self):
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        self._worker_tasks.clear()
        self._workers_started = False
        self._generation += 1
        self._event_queue = asyncio.Queue(maxsize=1000)
        self.subscribers = {}
        self.abort_signal.clear()
        self.response_sent.clear()
        self.affection_changed.clear()
        self.knowledge_updated.clear()
    
    def get_dropped_count(self) -> int:
        """Return the number of events dropped due to queue full (R9)."""
        return self._dropped_count
