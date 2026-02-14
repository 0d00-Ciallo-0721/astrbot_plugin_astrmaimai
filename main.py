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

# 特性模块 (已适配 2.0)
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
        
        # 1. 加载配置 & 基础设施
        self.cfg = HeartflowConfig.from_astrbot_config(config)
        
        if not self.cfg.enable_heartflow:
            logger.warning("HeartCore: 插件已加载，但主开关 (enable_heartflow) 未开启。")

        self.persistence = PersistenceManager(context, self.cfg)
        self.state_manager = StateManager(self.cfg, self.persistence)
        self.prompt_builder = PromptBuilder(context, self.cfg, self.state_manager)
        init_meme_storage()

        # 2. 初始化核心组件
        self.mood_manager = MoodManager(context, self.cfg)
        
        # PersonaSummarizer 现在作为工具类
        self.persona_summarizer = PersonaSummarizer(context, self.cfg, self.persistence, self.prompt_builder)
        self.prompt_builder.set_persona_summarizer(self.persona_summarizer) # 注入依赖
        
        # 初始化回复引擎 (The Mouth - 纯执行器)
        self.reply_engine = ReplyEngine(
            context, 
            self.cfg, 
            self.prompt_builder, 
            self.state_manager, 
            self.persistence, 
            self.mood_manager
        )

        # 3. 初始化神经中枢 (MindScheduler)
        # 这是 2.0 的核心：所有消息调度由它接管
        self.scheduler = MindScheduler(
            context=context,
            config=self.cfg,
            state_manager=self.state_manager,
            prompt_builder=self.prompt_builder,
            mood_manager=self.mood_manager,
            reply_engine=self.reply_engine
        )

        # 4. 初始化功能特性 (依赖注入更新)
        
        # CommandHandler 获取 Impulse, Memory, Evolution 引用
        self.command_handler = CommandHandler(
            context, self.cfg, self.state_manager,
            self.scheduler.impulse,      # ImpulseEngine
            self.scheduler.memory,       # MemoryGlands
            self.scheduler.evolution     # EvolutionCortex
        )
        
        # PokeHandler 传入 scheduler (发送触觉信号)
        self.poke_handler = PokeHandler(
            context, self.cfg, self.scheduler
        )
        
        # ProactiveTask 传入 scheduler (发送无聊信号)
        self.proactive_task = ProactiveTask(
            context, self.cfg, self.state_manager, self.scheduler
        )
        
        # MaintenanceTask 保持不变 (清理底层缓存)
        self.maintenance_task = MaintenanceTask(
            self.state_manager, self.persistence, context
        )
        
        # 启动后台任务
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
            
        # 转发给神经中枢
        await self.scheduler.on_message(event)

    @event_filter.event_message_type(event_filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """戳一戳事件"""
        if not self.cfg.enable_heartflow: return
        await self.poke_handler.on_poke(event)

    # --- 指令注册 ---
    
    @event_filter.command("heartcore")
    async def cmd_heartcore(self, event: AstrMessageEvent):
        """HeartCore 主菜单"""
        async for result in self.command_handler.cmd_menu(event):
            yield result

    @event_filter.command("遗忘")
    async def cmd_reset_memory(self, event: AstrMessageEvent):
        """[管理] 清空当前会话记忆"""
        async for result in self.command_handler.cmd_reset_memory(event):
            yield result
            
    @event_filter.command("突变")
    async def cmd_force_mutation(self, event: AstrMessageEvent):
        """[管理] 强制触发人格突变"""
        async for result in self.command_handler.cmd_force_mutation(event):
            yield result

    # (保留原有的查看/重载人格指令，如果需要)
    @event_filter.command("重载人格")
    async def cmd_reload_persona(self, event: AstrMessageEvent):
        # 简单透传给 PersonaSummarizer 处理，或者在 CommandHandler 中实现
        yield event.plain_result("指令已迁移，请使用 /heartcore 查看最新菜单。")

    async def terminate(self):
        """插件卸载清理"""
        if hasattr(self, 'proactive_task'):
            self.proactive_task.cancel()
        if hasattr(self, 'maintenance_task'):
            self.maintenance_task.stop()
        # 保存数据
        # await self.persistence.save_all_states(self.state_manager) # 视 state_manager 实现而定
        logger.info("HeartCore: System shutdown.")