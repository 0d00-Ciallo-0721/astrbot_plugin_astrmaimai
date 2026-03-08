import asyncio
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from ..infra.database import DatabaseService
from ..infra.gateway import GlobalModelGateway
from .miner import ExpressionMiner
from typing import List

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
        self.mining_lock = asyncio.Lock()

    async def process_feedback(self, event: AstrMessageEvent, is_command: bool = False):
        """
        消息发送后的回调 (Subconscious Feedback Loop)
        """
        # 1. 安全获取 bot_id (保持你的修复逻辑)
        bot_id = getattr(event.message_obj, 'self_id', 'SELF_BOT')
        if hasattr(event, 'bot') and getattr(event, 'bot', None):
            bot_id = getattr(event.bot, 'self_id', bot_id)

        # 2. 内容修饰：根据是否为指令回复，给内容打上认知标签
        # 这样做是为了在 ContextEngine 召回记忆时，AI 能意识到这是系统行为
        raw_content = event.message_str
        processed_content = raw_content
        
        if is_command:
            # 注入“元认知”前缀，防止 AI 以后模仿这些死板的指令格式
            processed_content = f"(系统指令执行结果): {raw_content}"

        # 3. 记录当前消息到短期日志
        self.db.add_message_log(
            group_id=event.unified_msg_origin,
            sender_id=str(bot_id),
            sender_name="SELF",
            content=processed_content # 记录带标签的内容
        )
        
        # 4. 触发后台挖掘任务 (Fire-and-Forget)
        # 如果是指令消息，通常不包含情感模式，可以在挖掘逻辑里进一步过滤
        asyncio.create_task(self._try_trigger_mining(event.unified_msg_origin))

    async def record_user_message(self, event: AstrMessageEvent):
        """记录用户消息 (在 System 1 阶段调用)"""
        self.db.add_message_log(
            group_id=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            content=event.message_str
        )

    async def process_logs_and_mine(self, group_id: str, logs: List['MessageLog']):
        """
        [修改] 执行综合挖掘任务（表达模式 + 群组黑话）
        （注：如果原文件中该函数名为 _run_mining 或类似名称，请直接替换对应逻辑）
        """
        if not logs:
            return

        async with self.mining_lock:
            try:
                # 1. 挖掘用户的表达模式 (Expression Pattern)
                patterns = await self.miner.mine(group_id, logs)
                for p in patterns:
                    self.db.save_pattern(p)
                    logger.debug(f"[Evolution] Learned Pattern: {p.situation} -> {p.expression}")

                # 2. [新增] 挖掘群组黑话 (Jargon)
                if hasattr(self.db, 'save_jargon'):
                    jargons = await self.miner.mine_jargons(group_id, logs)
                    for j in jargons:
                        self.db.save_jargon(j)
                        if j.is_jargon and j.is_complete:
                            logger.info(f"[Evolution] Learned Jargon: {j.content} -> {j.meaning}")
                            
                            # 触发新知识事件总线，通知系统进行认知刷新
                            from ..infra.event_bus import EventBus
                            EventBus().trigger_knowledge_update()

                # 3. 标记已处理
                self.db.mark_logs_processed([l.id for l in logs])

            except Exception as e:
                logger.error(f"[Evolution] 综合挖掘任务执行失败: {e}")


    async def analyze_and_get_goal(self, chat_id: str, recent_messages: str) -> str:
        """
        目标分析器 (Reference: pfc.py GoalAnalyzer)
        动态分析当前的短期对话意图或目标。
        """
        prompt = f"""
        作为对话意图分析器，请根据最近的对话上下文，用一句话（不超过20个字）总结当前对话的核心目标或主要话题。
        对话上下文:
        {recent_messages}

        严格返回 JSON 格式: {{"goal": "string"}}
        """
        try:
            # [修改点] 调用数据处理专项模型，由于原代码调用的是 self.miner.gateway，这里保持一致
            result = await self.miner.gateway.call_data_process_task(prompt=prompt, is_json=True)
            if isinstance(result, dict):
                return result.get("goal", "陪伴用户，提供有趣且连贯的对话")
            else:
                import json, re
                match = re.search(r'\{.*\}', str(result), re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    return data.get("goal", "陪伴用户，提供有趣且连贯的对话")
                return "陪伴用户，提供有趣且连贯的对话"
        except Exception as e:
            logger.error(f"[Evolution] 目标分析异常: {e}")
            return "陪伴用户，提供有趣且连贯的对话"


    def get_active_patterns(self, chat_id: str, limit: int = 5) -> str:
        """获取当前群组高频/活跃的黑话和表达句式"""
        patterns = self.db.get_patterns(chat_id, limit)
        if not patterns:
            return "暂无特殊语言风格记录。"
        
        lines = []
        for p in patterns:
            lines.append(f"- 当【{p.situation}】时 -> 习惯使用表达/黑话：【{p.expression}】")
        return "\n".join(lines)                    
    

    async def _try_trigger_mining(self, group_id: str):
        """
        私有方法：尝试触发异步挖掘 [新增]
        逻辑：检查未处理日志数量，达到阈值则执行 process_logs_and_mine
        """
        try:
            # 从数据库获取未处理的日志
            unprocessed_logs = self.db.get_unprocessed_logs(group_id, limit=100)
            
            # 设置触发阈值（例如：累积 20 条消息触发一次进化挖掘）
            threshold = getattr(self.config.evolution, 'mining_threshold', 20)
            
            if len(unprocessed_logs) >= threshold:
                logger.info(f"[Evolution] 群组 {group_id} 积攒日志达标 ({len(unprocessed_logs)}条)，启动进化挖掘...")
                # 调用已有的综合挖掘任务
                await self.process_logs_and_mine(group_id, unprocessed_logs)
            else:
                logger.debug(f"[Evolution] 群组 {group_id} 当前日志数: {len(unprocessed_logs)}，未达阈值 {threshold}。")
                
        except Exception as e:
            logger.error(f"[Evolution] _try_trigger_mining 异常: {e}")    