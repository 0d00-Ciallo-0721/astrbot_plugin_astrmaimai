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
from .astrmai.memory.engine import MemoryEngine

# --- Phase 3: System 2 (Brain) ---
from .astrmai.Brain.context_engine import ContextEngine
from .astrmai.Brain.planner import Planner

# --- Phase 5: Evolution & Expression ---
from .astrmai.evolution.processor import EvolutionManager
from .astrmai.meme_engine.meme_init import init_meme_storage # [新增]
from .astrmai.Brain.reply_engine import ReplyEngine # [新增]

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
        
        # [Fix] 必须使用 self.config.get() 而不是局部的 config.get()
        sys1 = self.config.get('system1_provider_id', 'Unconfigured')
        sys2 = self.config.get('system2_provider_id', 'Unconfigured')
        emb_id = self.config.get('embedding_provider_id', '')
        
        logger.info(f"[AstrMai] 🚀 Booting... Sys1: {sys1} | Sys2: {sys2}")

        # ==========================================
        # 🛠️ 架构层级挂载 (Layer Initialization)
        # ==========================================

        # --- Phase 1: Infrastructure Mount ---
        self.persistence = PersistenceManager()                 # [核心修改]: 初始化底座
        self.db_service = DatabaseService(self.persistence)     # [核心修改]: 兼容代理包装
        self.gateway = GlobalModelGateway(context, config)
        
        # --- Phase 4: Living Memory Mount ---
        # [Fix] 传入 embedding_provider_id
        self.memory_engine = MemoryEngine(context, self.gateway, embedding_provider_id=emb_id)


        # --- [新增] Phase 5: Expression Engine Mount ---
        # 需要 StateEngine 和 MoodManager (StateEngine 中已包含 MoodManager 逻辑或实例)
        # 这里的 StateEngine.mood_manager 是在 Phase 3 添加的
        self.reply_engine = ReplyEngine(self.state_engine, self.state_engine.mood_manager)

        # --- Phase 3: System 2 (Brain) Mount ---
        self.context_engine = ContextEngine(self.db_service)
        self.system2_planner = Planner(context, self.gateway, self.context_engine)

        # --- Phase 2: System 1 (Heart) Mount ---
        self.state_engine = StateEngine(self.persistence, self.gateway)
        # [修改] 传入 self.config
        self.judge = Judge(self.gateway, self.state_engine, self.config) 
        self.sensors = PreFilters(self.config) 
        self.system2_planner = Planner(context, self.gateway, self.context_engine, self.reply_engine)

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
            
        # 2. 初始化记忆引擎
        logger.info("[AstrMai] 🧠 Initializing Memory Engine...")
        await self._init_memory()
        init_meme_storage()        
        #提前唤醒并构建指令黑名单防火墙，减少 System 1 误判的概率    
        await self.sensors._load_foreign_commands()

    async def _init_memory(self):
        """异步唤醒记忆引擎与后台任务"""
        # 为了极度稳健，这里甚至可以再 sleep 1秒，但通常 on_astrbot_loaded 已经足够
        await asyncio.sleep(1) 
        await self.memory_engine.initialize()
        await self.memory_engine.start_background_tasks()

    async def _system2_entry(self, main_event: AstrMessageEvent, queue_events: list):
        """AttentionGate 防抖结束后的回调，负责真正拉起 System 2 进行深度思考"""
        chat_id = main_event.unified_msg_origin
        
        # 1. 取出 AttentionGate 聚合的消息队列
        pool = self.attention_gate.focus_pools.get(chat_id)
        queue_events = pool['queue'] if pool else [event]
        
        # 2. 情绪与能量结算
        await self.state_engine.consume_energy(chat_id)
        
        # 3. 引爆 System 2 认知循环
        await self.system2_planner.plan_and_execute(main_event, queue_events)

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
        msg = event.message_str.strip()
        if msg.startswith("/") or msg.startswith("！") or msg.startswith("!"):
            return

        # ================= [Fix Start] =================
        # 修复 QQ/OneBot 平台下 self_id 获取失败导致自回复的问题
        self_id = None
        
        # 1. 尝试从 message_obj 获取 (兼容 WebChat)
        if hasattr(event.message_obj, 'self_id'):
            self_id = str(event.message_obj.self_id)
        
        # 2. 尝试从 bot 平台实例获取 (兼容 Aiocqhttp/OneBot)
        # event.bot 通常是平台适配器的 Client 实例，它一定知道自己是谁
        if not self_id and hasattr(event, 'bot') and hasattr(event.bot, 'self_id'):
            self_id = str(event.bot.self_id)
            
        # 3. 兜底
        if not self_id:
            self_id = "unknown"
            
        # 4. 执行过滤
        if str(event.get_sender_id()) == self_id:
            return

        sender_name = event.get_sender_name()
        msg_str = event.message_str
        
        # [Debug Mode] 控制台输出拦截日志
        if self.config.get("debug_mode", False):
            logger.info(f"[AstrMai-Sensor] 📡 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")

        # --- 分流 1: 泵入 Evolution 潜意识层 (记录语料与触发挖掘) ---
        await self.evolution.record_user_message(event)

        # --- 分流 2: 泵入 System 1 注意力门控 (判断防抖、拦截或上抛给 Sys2) ---
        await self.attention_gate.process_event(event)

    @filter.after_message_sent()
    async def after_message_sent_hook(self, event: AstrMessageEvent):
        """
        [出口] 消息发送后的回调钩子
        """
        # 检查是否携带指令触发标签
        is_command_res = getattr(event, "is_command_trigger", False)
        
        if self.config.get("debug_mode", False):
            tag = "[指令回复]" if is_command_res else "[普通对话]"
            logger.info(f"[AstrMai-Subconscious]💡 消息发送完毕，触发后台状态机与反馈循环")
            
        # 将标签传递给进化模块，以便在存入数据库时进行区分
        await self.evolution.process_feedback(event, is_command=is_command_res)

    async def terminate(self):
        """卸载时的资源清理"""
        logger.info("[AstrMai] 🛑 Terminating processes and unmounting...")
        if hasattr(self, 'memory_engine') and self.memory_engine.summarizer:
            await self.memory_engine.summarizer.stop()