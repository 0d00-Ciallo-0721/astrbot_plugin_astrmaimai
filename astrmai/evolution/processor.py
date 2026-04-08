import asyncio
from astrbot.api import logger
from ..infra.lane_manager import LaneKey
from astrbot.api.event import AstrMessageEvent
from ..infra.database import DatabaseService
from ..infra.gateway import GlobalModelGateway
from .miner import ExpressionMiner
from typing import Dict, List

class EvolutionManager:
    """
    进化管理器 (Evolution Layer Facade)
    职责: 
    1. 监听消息发送后事件 -> 记录 Log
    2. 触发异步挖掘任务
    """
    def __init__(self, db: DatabaseService, gateway: GlobalModelGateway, config=None):
        self.db = db
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.miner = ExpressionMiner(gateway, self.config)
        # [核心修复 Bug 4] 废除会导致全局排队阻塞的单实例锁，重构为群组级分片哈希锁
        self._mining_locks: Dict[str, asyncio.Lock] = {}
        self._lock_mutex = asyncio.Lock()

    # [新增] 位置: astrmai/evolution/processor.py -> EvolutionManager 类下
    async def _get_mining_lock(self, group_id: str) -> asyncio.Lock:
        """[新增] 安全获取或创建细粒度群组锁"""
        async with self._lock_mutex:
            if group_id not in self._mining_locks:
                self._mining_locks[group_id] = asyncio.Lock()
            return self._mining_locks[group_id]

    # [新增]
    def _fire_background_task(self, coro):
        """安全触发后台任务，接管游离 Task 防止 GC 销毁与静默崩溃"""
        if not hasattr(self, '_background_tasks'):
            self._background_tasks = set()
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

    # [新增]
    def _handle_task_result(self, task: asyncio.Task):
        """清理已完成的任务并暴露异常"""
        if hasattr(self, '_background_tasks'):
            self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[Evolution Task Error] 潜意识挖掘任务发生异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass    

    async def process_feedback(self, event: AstrMessageEvent, is_command: bool = False):
        """
        消息发送后的回调 (Subconscious Feedback Loop)
        """
        # 1. 安全获取 bot_id
        bot_id = getattr(event.message_obj, 'self_id', 'SELF_BOT')
        if hasattr(event, 'bot') and getattr(event, 'bot', None):
            bot_id = getattr(event.bot, 'self_id', bot_id)

        raw_content = event.message_str
        processed_content = raw_content
        
        if is_command:
            processed_content = f"(系统指令执行结果): {raw_content}"

        # 3. [修改] 使用异步方法记录当前消息，避免阻塞发消息流程
        if hasattr(self.db, 'add_message_log_async'):
            await self.db.add_message_log_async(
                group_id=event.unified_msg_origin,
                sender_id=str(bot_id),
                sender_name="SELF",
                content=processed_content
            )
        else:
            self.db.add_message_log(group_id=event.unified_msg_origin, sender_id=str(bot_id), sender_name="SELF", content=processed_content)
        
        # 4. 🟢 [核心修复 Bug 1] 触发后台挖掘任务，使用安全托管池代替裸奔的 create_task
        self._fire_background_task(self._try_trigger_mining(event.unified_msg_origin))
    
    async def record_user_message(self, event: AstrMessageEvent):
        """记录用户消息 (在 System 1 阶段调用)"""
        # ✨ 【修改此行】：获取富文本
        rich_text = event.get_extra("astrmai_rich_text", event.message_str)
        
        # [修改] 使用异步方法记录用户消息
        if hasattr(self.db, 'add_message_log_async'):
            await self.db.add_message_log_async(
                group_id=event.unified_msg_origin,
                sender_id=event.get_sender_id(),
                sender_name=event.get_sender_name(),
                content=rich_text # ✨ 【修改此行】
            )
        else:
            self.db.add_message_log(
                group_id=event.unified_msg_origin, 
                sender_id=event.get_sender_id(), 
                sender_name=event.get_sender_name(), 
                content=rich_text # ✨ 【修改此行】
            )
            
    async def process_logs_and_mine(self, group_id: str, logs: List['MessageLog']):
        """
        [修正] 使用群组级细粒度锁进行二次校验，极大解放多群并发挖掘吞吐量
        """
        if not logs:
            return

        # [核心修复 Bug 4] 动态申请当前群组专属锁
        group_lock = await self._get_mining_lock(group_id)
        async with group_lock:
            try:
                # [修正] 二次校验：确保传入的这批 logs 在数据库中仍是“未处理”状态
                if hasattr(self.db, 'get_unprocessed_logs_async'):
                    current_unprocessed = await self.db.get_unprocessed_logs_async(group_id, limit=999)
                else:
                    current_unprocessed = self.db.get_unprocessed_logs(group_id, limit=999)
                    
                current_unprocessed_ids = {l.id for l in current_unprocessed}
                
                # 如果传入的第一条 log 的 ID 已经不在未处理池中，说明被前一个竞争的协程消费了，立刻短路抛弃
                if not logs or logs[0].id not in current_unprocessed_ids:
                    logger.debug(f"[Evolution] 拦截到过期快照，避免重复挖掘任务。")
                    return

                # 1. 挖掘用户的表达模式
                patterns = await self.miner.mine(group_id, logs)
                for p in patterns:
                    if hasattr(self.db, 'save_pattern_async'):
                        await self.db.save_pattern_async(p)
                    else:
                        self.db.save_pattern(p)
                    logger.debug(f"[Evolution] Learned Pattern: {p.situation} -> {p.expression}")

                # 2. 挖掘群组黑话
                if hasattr(self.db, 'save_jargon_async'):
                    jargons = await self.miner.mine_jargons(group_id, logs)
                    for j in jargons:
                        await self.db.save_jargon_async(j)
                        if j.is_jargon and j.is_complete:
                            logger.info(f"[Evolution] Learned Jargon: {j.content} -> {j.meaning}")
                            
                            from ..infra.event_bus import EventBus
                            EventBus().trigger_knowledge_update()

                # 3. 标记已处理
                if hasattr(self.db, 'mark_logs_processed_async'):
                    await self.db.mark_logs_processed_async([l.id for l in logs])
                else:
                    self.db.mark_logs_processed([l.id for l in logs])

            except Exception as e:
                logger.error(f"[Evolution] 综合挖掘任务执行失败: {e}")

    async def analyze_and_get_goal(self, chat_id: str, recent_messages: str) -> str:
        """
        目标分析器 (混合模式重构版)
        动态分析当前的短期对话意图或目标。
        Token 优化: Prompt 极限压缩 (~50字) + 输入截断 (200字)
        """
        # 输入为空则直接返回默认值（零 Token）
        if not recent_messages or not recent_messages.strip():
            return "陪伴用户，提供有趣且连贯的对话"
        
        # 截断输入，最多 200 字（降低 input token）
        trimmed = recent_messages.strip()[:200]
        
        # 极短 Prompt（~50 字，原版 ~120 字）
        prompt = f"""根据对话总结当前核心话题(≤15字):
{trimmed}
JSON: {{"goal": "string"}}"""
        
        try:
            result = await self.miner.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=LaneKey(subsystem="sys2", task_family="goal", scope_id=chat_id),
                base_origin=chat_id,
            )
            
            # 安全拆包
            if isinstance(result, dict):
                return str(result.get("goal", "陪伴用户，提供有趣且连贯的对话"))
            elif isinstance(result, str):
                import json, re
                match = re.search(r'\{.*?\}', result, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(0))
                        if isinstance(data, dict):
                            return str(data.get("goal", "陪伴用户，提供有趣且连贯的对话"))
                    except json.JSONDecodeError:
                        pass
            
            return "陪伴用户，提供有趣且连贯的对话"
            
        except Exception as e:
            logger.error(f"[Evolution-Processor] ❌ 目标分析异常: {e}")
            return "陪伴用户，提供有趣且连贯的对话"


    def get_active_patterns(self, chat_id: str, limit: int = 5) -> str:
        """此方法由于是旧版同步签名且可能被其他同步代码调用，建议保持现状，但在调用方侧应尽量重构。
        (如果 ContextEngine 中需要，应当在 ContextEngine 里 await db.get_patterns_async，然后自行格式化)"""
        patterns = self.db.get_patterns(chat_id, limit)
        if not patterns:
            return "暂无特殊语言风格记录。"
        
        lines = []
        for p in patterns:
            lines.append(f"- 当【{p.situation}】时 -> 习惯使用表达/黑话：【{p.expression}】")
        return "\n".join(lines)
    

    async def _try_trigger_mining(self, group_id: str):
        """
        私有方法：尝试触发异步挖掘 
        [修改] 将原本同步的日志检查动作完全异步化，防止拖慢主事件循环
        """
        try:
            # [核心修复] 使用之前添加的异步接口，安全地在后台查询未处理日志
            if hasattr(self.db, 'get_unprocessed_logs_async'):
                unprocessed_logs = await self.db.get_unprocessed_logs_async(group_id, limit=100)
            else:
                unprocessed_logs = self.db.get_unprocessed_logs(group_id, limit=100)
            
            threshold = getattr(self.config.evolution, 'mining_trigger', 20)
            
            if len(unprocessed_logs) >= threshold:
                logger.info(f"[Evolution] 群组 {group_id} 积攒日志达标 ({len(unprocessed_logs)}条)，启动进化挖掘...")
                await self.process_logs_and_mine(group_id, unprocessed_logs)
            else:
                logger.debug(f"[Evolution] 群组 {group_id} 当前日志数: {len(unprocessed_logs)}，未达阈值 {threshold}。")
                
        except Exception as e:
            logger.error(f"[Evolution] _try_trigger_mining 异常: {e}") 

    async def process_bot_reply(self, chat_id: str, bot_id: str, reply_text: str):
        """
        [修改] 主动接收 executor 传来的、真正属于 AI 的生成文本并入库。
        增加污点拦截规则，防止报错信息污染潜意识记忆。
        """
        if not reply_text or not reply_text.strip():
            return
            
        # ==========================================
        # 🟢 [新增] 污点拦截规则 (Memory Pollution Purification)
        # ==========================================
        fallback_msg = getattr(self.config.reply, 'fallback_text', "（陷入了短暂的沉默...）")
        error_keywords = ["Exception", "failed", "Traceback", "请求失败", "APITimeoutError", "All chat models fail"]
        
        if reply_text.strip() == fallback_msg or any(keyword in reply_text for keyword in error_keywords):
            logger.warning(f"[Evolution-Processor] 🚨 拦截到受污染的 Bot 回复，已抛弃该条潜意识记忆！内容: {reply_text[:30]}...")
            return

        logger.info(f"[Evolution-Processor] 🧠 正在将真实的 Bot 回复计入潜意识日志: {reply_text[:20]}...")
            
        # 1. 使用异步方法记录真实消息
        if hasattr(self.db, 'add_message_log_async'):
            await self.db.add_message_log_async(
                group_id=chat_id,
                sender_id=bot_id,
                sender_name="SELF",
                content=reply_text
            )
        else:
            self.db.add_message_log(group_id=chat_id, sender_id=bot_id, sender_name="SELF", content=reply_text)
        
        # 2. 触发后台挖掘任务
        self._fire_background_task(self._try_trigger_mining(chat_id))
