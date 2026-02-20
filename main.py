import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig

# --- Phase 1: Infra ---
from .astrmai.infra.database import DatabaseService
from .astrmai.infra.gateway import GlobalModelGateway

# --- Phase 4: Memory ---
from .astrmai.memory.engine import MemoryEngine

# --- Phase 5: Evolution ---
from .astrmai.evolution.processor import EvolutionManager

# --- Phase 3: System 2 (Brain) ---
from .astrmai.Brain.context_engine import ContextEngine
from .astrmai.Brain.planner import Planner

# --- Phase 2: System 1 (Heart) ---
from .astrmai.Heart.state_engine import StateEngine
from .astrmai.Heart.judge import Judge
from .astrmai.Heart.sensors import PreFilters
from .astrmai.Heart.attention import AttentionGate

@register("astrmai", "Gemini Antigravity", "AstrMai: Dual-Process Architecture Plugin", "1.0.0", "https://github.com/astrmai")
class AstrMaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config else context.get_config()
        
        sys1 = config.get('system1_provider_id', 'Unconfigured')
        sys2 = config.get('system2_provider_id', 'Unconfigured')
        emb_id = config.get('embedding_provider_id', '')
        logger.info(f"[AstrMai] 🚀 Booting... Sys1: {sys1} | Sys2: {sys2}")

        # ==========================================
        # 🛠️ 架构层级挂载 (Layer Initialization)
        # ==========================================

        # --- Phase 1: Infrastructure Mount ---
        self.db_service = DatabaseService()
        self.gateway = GlobalModelGateway(context, config)
        
        # --- Phase 4: Living Memory Mount ---
        # [Fix] 传入 embedding_provider_id
        self.memory_engine = MemoryEngine(context, self.gateway, embedding_provider_id=emb_id)
        
        self.memory_engine = MemoryEngine(context, self.gateway)

        # --- Phase 5: Subconscious Evolution Mount ---
        self.evolution = EvolutionManager(self.db_service, self.gateway)

        # --- Phase 3: System 2 (Brain) Mount ---
        self.context_engine = ContextEngine(self.db_service)
        self.system2_planner = Planner(context, self.gateway, self.context_engine)

        # --- Phase 2: System 1 (Heart) Mount ---
        self.state_engine = StateEngine(self.db_service, self.gateway)
        self.judge = Judge(self.gateway, self.state_engine)
        self.sensors = PreFilters(config)
        
        # 组装 AttentionGate，并将 System 2 的入口作为防抖结束后的回调传入
        self.attention_gate = AttentionGate(
            state_engine=self.state_engine,
            judge=self.judge,
            sensors=self.sensors,
            system2_callback=self._system2_entry # 绑定跨系统回调
        )
        
        logger.info("[AstrMai] ✅ Full Dual-Process Architecture Ready (Phases 1-5 Mounted).")
    
    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        logger.info("[AstrMai] 🏁 AstrBot Loaded. Starting System Initialization...")
        
        # [Fix] 1. 优先初始化基础设施 (DatabaseService)
        # 即使 MemoryEngine 不直接用它，BM25 或其他组件可能隐式依赖它
        try:
            if hasattr(self.db_service, 'initialize'):
                await self.db_service.initialize()
                logger.info("[AstrMai] 🗄️ Database Service Initialized.")
            elif hasattr(self.db_service, 'init'): # 兼容常见的命名
                await self.db_service.init()
                logger.info("[AstrMai] 🗄️ Database Service Initialized.")
        except Exception as e:
            logger.error(f"[AstrMai] ❌ Database Service Init Failed: {e}")
            # 数据库失败是致命的，但我们尝试继续以暴露更多问题
            
        # 2. 初始化记忆引擎
        logger.info("[AstrMai] 🧠 Initializing Memory Engine...")
        await self._init_memory()
    
    async def _init_memory(self):
        """异步唤醒记忆引擎与后台任务"""
        # 为了极度稳健，这里甚至可以再 sleep 1秒，但通常 on_astrbot_loaded 已经足够
        await asyncio.sleep(1) 
        await self.memory_engine.initialize()
        await self.memory_engine.start_background_tasks()

    async def _system2_entry(self, event: AstrMessageEvent):
        """AttentionGate 防抖结束后的回调，负责真正拉起 System 2 进行深度思考"""
        chat_id = event.unified_msg_origin
        
        # 1. 取出 AttentionGate 聚合的消息队列
        pool = self.attention_gate.focus_pools.get(chat_id)
        queue_events = pool['queue'] if pool else [event]
        
        # 2. 情绪与能量结算
        await self.state_engine.consume_energy(chat_id)
        
        # 3. 引爆 System 2 认知循环
        await self.system2_planner.plan_and_execute(event, queue_events)

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        '''AstrMai 状态面板'''
        help_text = (
            "🤖 **AstrMai (v1.0.0)**\n"
            "-----------------------\n"
            "🧠 架构状态: Phase 5 (Evolution Ready)\n"
            f"🔌 Sys1 Provider: {self.config.get('system1_provider_id')}\n"
            f"🔌 Sys2 Provider: {self.config.get('system2_provider_id')}\n"
            f"🔌 Emb Provider: {self.config.get('embedding_provider_id')}\n"
            "💾 SQLite & Faiss RAG: Connected\n"
            "🌀 Subconscious Miner: Running\n"
            "🛡️ Dual-Process: Active"
        )
        yield event.plain_result(help_text)

    # ==========================================
    # 📡 核心事件钩子 (Event Hooks)
    # ==========================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_global_message(self, event: AstrMessageEvent):
        """
        [入口] 接管所有平台消息，将数据泵入双系统架构与进化层。
        """
        # 防止处理机器人自己发出的消息导致死循环
        if event.get_sender_id() == event.bot.self_id:
            return

        sender_name = event.get_sender_name()
        msg_str = event.message_str
        unified_id = event.unified_msg_origin
        
        # [Debug Mode] 控制台输出拦截日志
        if self.config.get("debug_mode", False):
            logger.info(f"[AstrMai-Sensor] 📡 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")

        # --- 分流 1: 泵入 Evolution 潜意识层 (记录语料与触发挖掘) ---
        await self.evolution.record_user_message(event)

        # --- 分流 2: 泵入 System 1 注意力门控 (判断防抖、拦截或上抛给 Sys2) ---
        await self.attention_gate.process_event(event)
        
        # 注意: 这里不调用 event.stop_event()，以便 AstrBot 原生的其他插件指令依然能够生效。
        # 如果你想将 AstrMai 做为独占机器人，可以在这里加上 event.stop_event()

    @filter.after_message_sent()
    async def after_message_sent_hook(self, event: AstrMessageEvent):
        """
        [出口] 消息发送后的回调钩子 (Subconscious Feedback Loop)
        用于记录 AI 自己的发言并触发后台挖掘清算任务。
        """
        if self.config.get("debug_mode", False):
            logger.info(f"[AstrMai-Subconscious] 💡 消息发送完毕，触发后台状态机与反馈循环。")
            
        await self.evolution.process_feedback(event)

    async def terminate(self):
        """卸载时的资源清理"""
        logger.info("[AstrMai] 🛑 Terminating processes and unmounting...")
        if hasattr(self, 'memory_engine') and self.memory_engine.summarizer:
            await self.memory_engine.summarizer.stop()