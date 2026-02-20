import asyncio
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrmai.infra.database import DatabaseService
from astrmai.infra.gateway import GlobalModelGateway
from .miner import ExpressionMiner

class EvolutionManager:
    """
    进化管理器 (Evolution Layer Facade)
    职责: 
    1. 监听消息发送后事件 -> 记录 Log
    2. 触发异步挖掘任务
    """
    def __init__(self, db: DatabaseService, gateway: GlobalModelGateway):
        self.db = db
        self.miner = ExpressionMiner(gateway)
        self.mining_lock = asyncio.Lock()

    async def process_feedback(self, event: AstrMessageEvent):
        """
        消息发送后的回调 (Subconscious)
        """
        # 1. 记录当前消息 (Self)
        self.db.add_message_log(
            group_id=event.unified_msg_origin,
            sender_id=event.get_self_id(),
            sender_name="SELF",
            content=event.message_str
        )
        
        # 2. 检查是否触发学习 (例如: 每积压 20 条消息)
        # 这里使用 Fire-and-Forget 方式启动任务
        asyncio.create_task(self._try_trigger_mining(event.unified_msg_origin))

    async def record_user_message(self, event: AstrMessageEvent):
        """记录用户消息 (在 System 1 阶段调用)"""
        self.db.add_message_log(
            group_id=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            content=event.message_str
        )

    async def _try_trigger_mining(self, group_id: str):
        if self.mining_lock.locked():
            return

        async with self.mining_lock:
            # 1. 获取未处理消息
            logs = self.db.get_unprocessed_logs(group_id, limit=50)
            
            # 阈值检测 (Self_Learning 默认 25，这里设为 20)
            if len(logs) < 20:
                return

            logger.info(f"[Evolution] Triggering pattern mining for {group_id} ({len(logs)} msgs)...")
            
            # 2. 执行挖掘
            patterns = await self.miner.mine(group_id, logs)
            
            # 3. 保存结果
            for p in patterns:
                self.db.save_pattern(p)
                logger.debug(f"[Evolution] Learned: {p.situation} -> {p.expression}")
            
            # 4. 标记已处理
            self.db.mark_logs_processed([l.id for l in logs])