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
        
        # 🟢 [深层修复 Bug 4] 引入 MPSC 有界缓冲队列与消费者状态，防止风暴击穿事件循环
        self._event_queue = asyncio.Queue(maxsize=1000)
        self._workers_started = False
        self._background_tasks = set()

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
        # 保留 .set() 以防部分兼容旧代码依赖原生 asyncio.Event (如未及升级的挂起锁)
        self.affection_changed.set()
        
        # 直接使用强大的混合引用路由发布事件，不执行带有让出性质的 sleep 和容易吞噬事件的 clear()
        await self.publish("affection_changed")

    def trigger_knowledge_update(self):
        self.knowledge_updated.set()
        self.knowledge_updated.clear()        

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
        
        while True:
            try:
                topic, data = await self._event_queue.get()
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
                            task = asyncio.create_task(callback(data))
                            # 挂载异常钩子，防止背景任务静默崩溃
                            task.add_done_callback(
                                lambda t, cb=callback: logger.error(f"[EventBus] Topic '{topic}' 异步回调异常 {cb}: {t.exception()}") if t.exception() else None
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
                task = asyncio.create_task(self._worker_loop())
                self._background_tasks.add(task)
        
        try:
            # 使用 nowait 防止高频事件反向阻塞关键请求链路，溢出时告警
            self._event_queue.put_nowait((topic, data))
        except asyncio.QueueFull:
            from astrbot.api import logger
            logger.warning(f"[EventBus] 🚨 事件积压超限 (1000)，为保护事件循环正丢弃主题: {topic}")