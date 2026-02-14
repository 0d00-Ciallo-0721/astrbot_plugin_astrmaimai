### 📄 main.py
# heartflow/main.py
# HeartCore 2.0 - The Digital Being Entry Point

import asyncio
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter as event_filter

# 引入新架构组件
from .config import HeartflowConfig
from .persistence import PersistenceManager
from .core.state_manager import StateManager
from .core.mood_manager import MoodManager
from .core.reply_engine import ReplyEngine
from .core.mind_scheduler import MindScheduler
from .utils.prompt_builder import PromptBuilder

# 特性模块 (保持原样，稍后通过 MindScheduler 协调)
from .features.proactive_task import ProactiveTask
from .features.poke_handler import PokeHandler
from .features.command_handler import CommandHandler
from .features.persona_summarizer import PersonaSummarizer
from .features.maintenance_task import MaintenanceTask
from .meme_engine.meme_init import init_meme_storage

@register("heartcore", "Soulter", "HeartCore 2.0: Digital Being", "2.0.0", "https://github.com/Soulter/astrbot_plugin_heartcore")
class HeartCorePlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.context = context
        
        # 1. 加载配置
        self.cfg = HeartflowConfig.from_astrbot_config(config)
        if not self.cfg.enable_heartflow:
            logger.warning("HeartCore: 插件已加载，但主开关 (enable_heartflow) 未开启。")

        # 2. 初始化基础设施
        self.persistence = PersistenceManager(context)
        self.state_manager = StateManager(self.persistence, self.cfg)
        init_meme_storage() # 初始化表情包

        # 3. 初始化核心组件
        self.mood_manager = MoodManager(context, self.cfg)
        self.persona_summarizer = PersonaSummarizer(context, self.cfg)
        self.prompt_builder = PromptBuilder(context, self.cfg, self.state_manager)
        self.prompt_builder.set_persona_summarizer(self.persona_summarizer) # 注入依赖
        
        # 初始化回复引擎 (The Mouth)
        self.reply_engine = ReplyEngine(
            context, 
            self.cfg, 
            self.prompt_builder, 
            self.state_manager, 
            self.persistence, 
            self.mood_manager
        )

        # 4. 初始化神经中枢 (MindScheduler)
        # 这是 2.0 的核心变更：所有消息调度由它接管
        self.scheduler = MindScheduler(
            context=context,
            config=self.cfg,
            state_manager=self.state_manager,
            prompt_builder=self.prompt_builder,
            mood_manager=self.mood_manager,
            reply_engine=self.reply_engine
        )

        # 5. 初始化功能特性 (指令、戳一戳、后台任务)
        self.command_handler = CommandHandler(
            context, self.cfg, self.state_manager, self.persistence, self.prompt_builder
        )
        self.poke_handler = PokeHandler(
            context, self.cfg, self.state_manager, self.reply_engine
        )
        
        # 启动后台任务
        self.proactive_task = ProactiveTask(
            context, self.cfg, self.state_manager, self.reply_engine
        )
        self.maintenance_task = MaintenanceTask(
            context, self.state_manager, self.persistence
        )
        
        asyncio.create_task(self.proactive_task.run())
        asyncio.create_task(self.maintenance_task.run())
        
        logger.info("HeartCore 2.0: MindScheduler is online. Digital Being is breathing.")

    # --- 事件监听 ---

    @event_filter.on_decorating_event("message")
    async def on_group_message(self, event: AstrMessageEvent):
        """
        监听群聊/私聊消息 -> 转发给 MindScheduler
        """
        if not self.cfg.enable_heartflow:
            return
            
        # 让指令处理器先过 (优先级最高)
        # (AstrBot 框架通常会先处理 command，这里是兜底)
        
        # 转发给神经中枢
        await self.scheduler.on_message(event)

    @event_filter.event_message_type(event_filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """戳一戳事件"""
        if not self.cfg.enable_heartflow: return
        await self.poke_handler.handle_poke(event)

    # --- 指令注册 (保持原样，通过 CommandHandler 处理) ---
    
    @event_filter.command("heartcore")
    async def cmd_heartcore(self, event: AstrMessageEvent):
        """HeartCore 主菜单"""
        async for result in self.command_handler.cmd_menu(event):
            yield result

    # (其他指令省略，保持与 v4.14 一致，只需调用 self.command_handler)
    
    async def terminate(self):
        """插件卸载清理"""
        self.proactive_task.cancel()
        self.maintenance_task.stop()
        # 保存数据
        await self.persistence.save_all_states(self.state_manager)
        logger.info("HeartCore: System shutdown.")