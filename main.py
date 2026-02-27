import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# --- Config ---
from .config import AstrMaiConfig

# --- Phase 1: Infra ---
from .astrmai.infra.persistence import PersistenceManager
from .astrmai.infra.database import DatabaseService
from .astrmai.infra.gateway import GlobalModelGateway

# --- Phase 4: Memory ---
from .astrmai.memory.engine import MemoryEngine

# --- Phase 3: System 2 (Brain) ---
from .astrmai.Brain.context_engine import ContextEngine
from .astrmai.Brain.planner import Planner
from .astrmai.Brain.persona_summarizer import PersonaSummarizer

# --- Phase 5: Evolution & Expression ---
from .astrmai.evolution.processor import EvolutionManager
from .astrmai.meme_engine.meme_init import init_meme_storage 
from .astrmai.Brain.reply_engine import ReplyEngine 

# --- Phase 6: Proactive (Life) ---
from .astrmai.evolution.proactive_task import ProactiveTask  

# --- Phase 2: System 1 (Heart) ---
from .astrmai.Heart.state_engine import StateEngine
from .astrmai.Heart.judge import Judge
from .astrmai.Heart.sensors import PreFilters
from .astrmai.Heart.attention import AttentionGate

@register("astrmai", "Gemini Antigravity", "AstrMai: Dual-Process Architecture Plugin", "1.0.0", "https://github.com/astrmai")
class AstrMaiPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # 核心修改：反序列化传入的 dict 为 Pydantic 对象
        raw_config = config if config else context.get_config()
        self.config = AstrMaiConfig(**raw_config)
        
        sys1 = self.config.provider.system1_provider_id or 'Unconfigured'
        sys2 = self.config.provider.system2_provider_id or 'Unconfigured'
        emb_id = self.config.provider.embedding_provider_id or ''
        
        logger.info(f"[AstrMai] 🚀 Booting... Sys1: {sys1} | Sys2: {sys2}")

        # ==========================================
        # 🛠️ 架构层级挂载 (Layer Initialization)
        # ==========================================

        # --- Phase 1: Infrastructure Mount ---
        self.persistence = PersistenceManager()                 
        self.db_service = DatabaseService(self.persistence)     
        self.gateway = GlobalModelGateway(context, self.config) # 注入 AstrMaiConfig
        
        # --- Phase 4: Living Memory Mount ---
        self.memory_engine = MemoryEngine(context, self.gateway, embedding_provider_id=emb_id)

        # --- Phase 2: System 1 (Heart) Mount ---
        # (Fix: 将 Heart 初始化提前，解决向下游注入的依赖问题)
        self.state_engine = StateEngine(self.persistence, self.gateway)
        self.judge = Judge(self.gateway, self.state_engine) # Judge 和 Sensors 的 Config 注入将在 Step 3 适配，暂时保持旧签名或等待修改
        self.sensors = PreFilters(self.config) 

        # --- Phase 5: Expression Engine Mount ---
        self.reply_engine = ReplyEngine(self.state_engine, self.state_engine.mood_manager)
        self.evolution = EvolutionManager(self.db_service, self.gateway)

        # --- Phase 3 & 4: System 2 (Brain) Mount ---
        self.persona_summarizer = PersonaSummarizer(self.persistence, self.gateway)
        self.context_engine = ContextEngine(self.db_service, self.persona_summarizer)
        self.system2_planner = Planner(
            context, 
            self.gateway, 
            self.context_engine, 
            self.reply_engine,
            self.memory_engine, 
            self.evolution
        )

        # 组装 AttentionGate
        self.attention_gate = AttentionGate(
            state_engine=self.state_engine,
            judge=self.judge,
            sensors=self.sensors,
            system2_callback=self._system2_entry
        )
        
        # --- Phase 6: Proactive Task (Lifecycle) ---
        self.proactive_task = ProactiveTask(
            context=context,
            state_engine=self.state_engine,
            gateway=self.gateway,
            persistence=self.persistence
        )        
        
        logger.info("[AstrMai] ✅ Full Dual-Process Architecture Ready (Phases 1-6 Mounted).")

    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        logger.info("[AstrMai] 🏁 AstrBot Loaded. Starting System Initialization...")
        logger.info("[AstrMai] 🧠 Initializing Memory Engine...")
        await self._init_memory()
        init_meme_storage()        
        await self.sensors._load_foreign_commands()
        await self.proactive_task.start()

    async def _init_memory(self):
        await asyncio.sleep(1) 
        await self.memory_engine.initialize()
        await self.memory_engine.start_background_tasks()

    async def _system2_entry(self, main_event: AstrMessageEvent, queue_events: list):
        chat_id = main_event.unified_msg_origin
        pool = self.attention_gate.focus_pools.get(chat_id)
        queue_events = pool['queue'] if pool else [main_event]
        
        await self.state_engine.consume_energy(chat_id)
        await self.system2_planner.plan_and_execute(main_event, queue_events)

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        '''AstrMai 状态面板'''
        help_text = (
            "🤖 **AstrMai (v1.0.0)**\n"
            "-----------------------\n"
            "🧠 架构状态: Phase 6 (Lifecycle Active)\n"
            f"🔌 Sys1 Provider: {self.config.provider.system1_provider_id}\n"
            f"🔌 Sys2 Provider: {self.config.provider.system2_provider_id}\n"
            f"🔌 Emb Provider: {self.config.provider.embedding_provider_id}\n"
            "💾 SQLite & Faiss RAG: Connected\n"
            "🌀 Subconscious Miner: Running\n"
            "🌱 Proactive Life: Running"
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
        
        # [修改点 1] 兼容用户自定义前缀
        if any(msg.startswith(prefix) for prefix in self.config.global_settings.command_prefixes):
            return

        # [修改点 2] 接入群聊白名单机制
        group_id = event.get_group_id()
        enabled_groups = self.config.global_settings.enabled_groups
        if enabled_groups and group_id:
            if str(group_id) not in enabled_groups:
                return

        # ================= [Fix Start] =================
        self_id = None
        if hasattr(event.message_obj, 'self_id'):
            self_id = str(event.message_obj.self_id)
        if not self_id and hasattr(event, 'bot') and hasattr(event.bot, 'self_id'):
            self_id = str(event.bot.self_id)
        if not self_id:
            self_id = "unknown"
            
        if str(event.get_sender_id()) == self_id:
            return

        sender_name = event.get_sender_name()
        msg_str = event.message_str
        
        # [修改点 3] 接入 Config Debug Mode
        if self.config.global_settings.debug_mode:
            logger.info(f"[AstrMai-Sensor] 📡 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")
        
        user_id = event.get_sender_id()
        if user_id:
            asyncio.create_task(self._update_user_stats(user_id))
            
        await self.evolution.record_user_message(event)
        await self.attention_gate.process_event(event)

    async def _update_user_stats(self, user_id: str):
        profile = await self.state_engine.get_user_profile(user_id)
        profile.message_count_for_profiling += 1
        profile.is_dirty = True

    @filter.after_message_sent()
    async def after_message_sent_hook(self, event: AstrMessageEvent):
        is_command_res = getattr(event, "is_command_trigger", False)
        
        if self.config.global_settings.debug_mode:
            tag = "[指令回复]" if is_command_res else "[普通对话]"
            logger.info(f"[AstrMai-Subconscious]💡 消息发送完毕，触发后台状态机与反馈循环")
            
        await self.evolution.process_feedback(event, is_command=is_command_res)

    async def terminate(self):
        logger.info("[AstrMai] 🛑 Terminating processes and unmounting...")
        if hasattr(self, 'memory_engine') and self.memory_engine.summarizer:
            await self.memory_engine.summarizer.stop()
        
        if hasattr(self, 'proactive_task'):
            await self.proactive_task.stop()