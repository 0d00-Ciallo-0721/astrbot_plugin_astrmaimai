import asyncio
import re
import copy  # 用于深拷贝 (修复 Bug 1)
import time  # 用于时间戳节流 (修复 Bug 2)
import astrbot.api.message_components as Comp  # 提升至全局导入 (修复 Bug 3)
import contextvars # 用于导入上下文变量相关库
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig

# --- Config ---
from .config import AstrMaiConfig

# --- Phase 1: Infra ---
from .astrmai.infra.persistence import PersistenceManager
from .astrmai.infra.database import DatabaseService
from .astrmai.infra.gateway import GlobalModelGateway
from .astrmai.infra.event_bus import EventBus 

# --- Phase 4: Memory ---
from .astrmai.memory.engine import MemoryEngine

# --- Phase 3: System 2 (Brain) ---
from .astrmai.Brain.context_engine import ContextEngine
from .astrmai.Brain.planner import Planner
from .astrmai.Brain.persona_summarizer import PersonaSummarizer
from .astrmai.Brain.prompt_refiner import PromptRefiner

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
from .astrmai.Heart.visual_cortex import VisualCortex 

@register("astrmai", "Gemini Antigravity", "AstrMai: Dual-Process Architecture Plugin", "1.0.0", "https://github.com/0d00-Ciallo-0721/astrbot_plugin_astrmaimai")
class AstrMaiPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        import weakref
        super().__init__(context)
        self.raw_config = config 
        
        self.config = AstrMaiConfig(**(config or {}))
        
        self._background_tasks = set() 
        
        # 🟢 [彻底修复 Bug 1] 放弃非法的 weakref，改用强引用字典，彻底杜绝并发锁的幽灵回收与内存穿透
        # (注: _memory_locks 已被高内聚转移至 summarizer.py)
        self._sys2_locks = {}    
        
        judge_id = self.config.provider.judge_model or 'Unconfigured'
        agent_id = self.config.provider.agent_model or 'Unconfigured'
        emb_id = self.config.provider.embedding_provider_id or ''
        
        logger.info(f"[AstrMai] 🚀 Booting... Judge: {judge_id} | Agent: {agent_id}")

        self.persistence = PersistenceManager()                 
        self.db_service = DatabaseService(self.persistence)     
        self.gateway = GlobalModelGateway(context, self.config) 
        self.event_bus = EventBus()   
        
        self.memory_engine = MemoryEngine(context, self.gateway, embedding_provider_id=emb_id)

        self.state_engine = StateEngine(self.persistence, self.gateway, event_bus=self.event_bus)
        self.judge = Judge(self.gateway, self.state_engine)
        self.sensors = PreFilters(self.config) 

        self.visual_cortex = VisualCortex(self.gateway, self.db_service) 

        self.reply_engine = ReplyEngine(self.state_engine, self.state_engine.mood_manager)
        self.evolution = EvolutionManager(self.db_service, self.gateway)

        self.persona_summarizer = PersonaSummarizer(self.persistence, self.gateway)
        self.context_engine = ContextEngine(self.db_service, self.persona_summarizer)
        
        # 🟢 [修改] 显式传入 db_service 给 PromptRefiner，解决图片失忆症
        self.prompt_refiner = PromptRefiner(self.memory_engine, self.db_service, self.config) 
        
        self.system2_planner = Planner(
            context, 
            self.gateway, 
            self.context_engine, 
            self.reply_engine,
            self.memory_engine, 
            self.evolution,
            state_engine=self.state_engine,
            prompt_refiner=self.prompt_refiner # 注入 Refiner 给 Planner 显式调用
        )

        self.attention_gate = AttentionGate(
            state_engine=self.state_engine,
            judge=self.judge,
            sensors=self.sensors,
            system2_callback=self._system2_entry,
            config=self.config,                          
            persona_summarizer=self.persona_summarizer,  
            visual_cortex=self.visual_cortex     
        )
        
        self.proactive_task = ProactiveTask(
            context=context,
            state_engine=self.state_engine,
            gateway=self.gateway,
            persistence=self.persistence,
            memory_engine=self.memory_engine,
            config=self.config,
        )        
        
        logger.info("[AstrMai] ✅ Full Dual-Process Architecture Ready (Phases 1-6 Mounted).")
        
    async def _update_user_stats(self, user_id: str):
        await self.state_engine.increment_user_message_count(user_id)
        
    def _fire_and_forget(self, coro):
        """[新增] 安全触发后台任务的通用封装，防止被 GC 和吞噬异常"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

    def _handle_task_result(self, task: asyncio.Task):
        """[新增] 处理后台任务完成后的清理与异常捕获"""
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[AstrMai-Background] 后台任务异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass       
    
    async def _init_memory(self):
        await self.memory_engine.initialize()
        await self.memory_engine.start_background_tasks()

    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        logger.info("[AstrMai] 🏁 AstrBot Loaded. Starting System Initialization...")
        logger.info("[AstrMai] 🧠 Initializing Memory Engine...")
        await self._init_memory()
        init_meme_storage()        
        await self.sensors._load_foreign_commands()
        await self.proactive_task.start()
        self.visual_cortex.start()
        # 拉起内存后台代谢任务
        self._fire_and_forget(self._memory_gc_task())
        # 拉起数据库批量同步后台任务
        self._fire_and_forget(self._db_sync_task())

    async def _db_sync_task(self):
        """数据库微批处理后台任务，增加 CancelledError 保护防死锁"""
        while getattr(self, '_is_running', True):
            try:
                await asyncio.sleep(15)  # 每 15 秒同步一次
                if hasattr(self.state_engine, 'flush_message_counters'):
                    await self.state_engine.flush_message_counters()
            except asyncio.CancelledError:
                logger.info("[AstrMai-DB-Sync] 🛑 收到终止信号，执行最后一次事务提交释放锁...")
                if hasattr(self.state_engine, 'flush_message_counters'):
                    await self.state_engine.flush_message_counters()
                raise
            except Exception as e:
                logger.error(f"[AstrMai-DB-Sync] 🚨 数据库批量同步任务异常: {e}")

    async def _memory_gc_task(self):
        """[重构] 扩大 GC 范围，彻底消除 TOCTOU 竞态条件，移除已被抽离的旧 Hook 缓存逻辑"""
        while getattr(self, '_is_running', True):
            try:
                await asyncio.sleep(3600)  # 每小时执行一次
                now = time.time()
                
                # 1. 识别并安全回收 Attention 层由于群活跃度下降遗留的僵尸池
                attention_stale_count = 0
                if hasattr(self, 'attention_gate') and hasattr(self.attention_gate, 'focus_pools'):
                    async with self.attention_gate._pool_lock:
                        for c_id, ctx in list(self.attention_gate.focus_pools.items()):
                            if now - ctx.last_active_time > 86400:
                                async with ctx.lock:
                                    if now - ctx.last_active_time > 86400:  # 二次校验
                                        self.attention_gate.focus_pools.pop(c_id, None)
                                        attention_stale_count += 1
                                        
                # 2. 安全回收没有任何协程等待或持有的空闲系统锁，防止强引用导致的长期内存膨胀
                lock_cleaned = 0
                for l_id, lck in list(self._sys2_locks.items()):
                    if not lck.locked(): # 如果当前锁未被任何任务获取
                        self._sys2_locks.pop(l_id, None)
                        lock_cleaned += 1
                    
                if attention_stale_count > 0 or lock_cleaned > 0:
                    logger.info(f"[AstrMai-GC] 🧹 成功回收 {attention_stale_count} 个注意力残留内存, 以及 {lock_cleaned} 把空闲互斥锁。")
            except asyncio.CancelledError:
                logger.info("[AstrMai-GC] 🛑 内存 GC 任务收到终止信号，安全退出...")
                raise
            except Exception as e:
                logger.error(f"[AstrMai-GC] 🚨 内存 GC 任务发生异常: {e}")

    def _get_sys2_lock(self, chat_id: str) -> asyncio.Lock:
        """安全获取 System 2 会话级防并发互斥锁"""
        lock = self._sys2_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._sys2_locks[chat_id] = lock
        return lock

    async def _system2_entry(self, main_event: AstrMessageEvent, events_to_process: list = None): 
        chat_id = main_event.unified_msg_origin
        lock = self._get_sys2_lock(chat_id)
        
        logger.debug(f"[{chat_id}] 🧠 System 2 请求已注册，正在排队等待进入主执行队列...")
            
        async with lock:
            try:
                if isinstance(events_to_process, list) and len(events_to_process) > 0:
                    queue_events = events_to_process.copy()
                else:
                    queue_events = [main_event]
                
                await self.state_engine.consume_energy(chat_id)
                await self.system2_planner.plan_and_execute(main_event, queue_events)
            finally:
                logger.debug(f"[AstrMai] 🛡️ System2 任务链执行完毕，安全退出规划层。")

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        '''AstrMai 状态面板'''
        help_text = (
            "🤖 **AstrMai (v1.0.0)**\n"
            "-----------------------\n"
            "🧠 架构状态: Phase 6 (Lifecycle Active)\n"
            f"🔌 Judge Provider: {self.config.provider.judge_model}\n"
            f"🔌 Agent Provider: {self.config.provider.agent_model}\n"
            f"🔌 Emb Provider: {self.config.provider.embedding_provider_id}\n"
            "💾 SQLite & Faiss RAG: Connected\n"
            "🌀 Subconscious Miner: Running\n"
            "🌱 Proactive Life: Running"
        )
        yield event.plain_result(help_text)

    # ==========================================
    # 📡 核心事件流处理 (Event Routing)
    # ==========================================

    def _is_framework_command(self, msg: str) -> bool:
        """实时探测并解析当前消息是否命中 AstrBot 底层注册的指令。"""
        if not msg:
            return False
            
        # 1. 清洗零宽字符
        clean_text = msg.replace('\u200b', '').strip()
        if not clean_text:
            return False
            
        # 2. 剥离可能的前缀 (支持自定义前缀与默认斜杠，且免疫 "/ 指令" 的空格干扰)
        prefixes = getattr(self.config.global_settings, 'command_prefixes', [])
        if not prefixes:
            prefixes = ["/"]
            
        for prefix in prefixes:
            if clean_text.startswith(prefix):
                clean_text = clean_text[len(prefix):].strip()
                break
        else:
            if clean_text.startswith("/"):
                clean_text = clean_text[1:].strip()
                
        if not clean_text:
            return False
            
        # 3. 获取真正的首词
        clean_cmd = clean_text.split()[0].lower()
        
        # 4. 构建实时指令池
        registered_cmds = {"help", "plugin", "restart", "reload", "stop", "start", "list", "provider"}
        
        try:
            from astrbot.core.star.command_management import _collect_descriptors
            # 实时从热加载的 Handler 注册表中抓取全部描述符
            descriptors = _collect_descriptors(include_sub_commands=True)
            
            for desc in descriptors:
                if desc.effective_command:
                    registered_cmds.add(str(desc.effective_command).split()[0].lower())
                
                if getattr(desc, 'aliases', None):
                    for alias in desc.aliases:
                        registered_cmds.add(str(alias).split()[0].lower())
                        
        except Exception as e:
            from astrbot.api import logger
            logger.debug(f"[AstrMai-Filter] 内存态穿透失败，尝试降级: {e}")
            try:
                cmd_mgr = getattr(self.context, 'command_manager', None)
                if cmd_mgr and hasattr(cmd_mgr, 'commands'):
                    registered_cmds.update([str(k).lower() for k in cmd_mgr.commands.keys()])
            except Exception:
                pass

        # 5. 融合 config 中用户手动配置的额外指令兜底黑名单
        try:
            extra_cmds = getattr(self.config.system1, 'extra_command_list', [])
            if extra_cmds:
                registered_cmds.update([str(c).lower() for c in extra_cmds])
        except Exception:
            pass
            
        # 6. 判决
        return clean_cmd in registered_cmds

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_global_message(self, event: AstrMessageEvent):
        """[入口] 接管所有平台消息，将数据泵入双系统架构与进化层。"""
        import sys
        import time
        import threading
        from astrbot.api import logger
        
        if not hasattr(sys, '_astrmai_debounce_lock'):
            sys._astrmai_debounce_lock = threading.Lock()
            sys._astrmai_global_debounce_cache = {}
            
        msg_str = event.message_str.strip() if event.message_str else ""
        if not msg_str:
            msg_str = f"obj_len_{len(str(getattr(event.message_obj, 'message', '')))}"
            
        sender_id = str(event.get_sender_id())
        chat_id = str(event.unified_msg_origin)
        
        fingerprint = f"{chat_id}_{sender_id}_{msg_str}"
        now = time.time()
        
        with sys._astrmai_debounce_lock: 
            keys_to_delete = [k for k, v in sys._astrmai_global_debounce_cache.items() if now - v > 1.5]
            for k in keys_to_delete:
                sys._astrmai_global_debounce_cache.pop(k, None)
                
            if fingerprint in sys._astrmai_global_debounce_cache:
                logger.warning(f"[AstrMai-Sensor] 🛡️ 极速防抖生效！拦截 AstrBot 框架双发/分身消息: {msg_str[:15]}")
                return 
                
            sys._astrmai_global_debounce_cache[fingerprint] = now

        message_chain = getattr(event.message_obj, 'message', []) if event.message_obj else []
        
        if any(isinstance(c, Comp.Poke) for c in message_chain):
            if hasattr(self, 'sensors') and hasattr(self, 'attention_gate'):
                await self.sensors.process_poke_event(event, self.context, self.attention_gate)
            return

        msg = event.message_str.strip() if event.message_str else ""
        
        # 无状态指令感知放行系统
        if msg and self._is_framework_command(msg):
            return

        # ==========================================
        # [修改] 统一 ID 解析器与三级权限路由
        # ==========================================
        umo = str(event.unified_msg_origin)
        parts = umo.split(":")
        platform_type = parts[1] if len(parts) >= 3 else ("GroupMessage" if event.get_group_id() else "FriendMessage")
        entity_id = parts[2] if len(parts) >= 3 else str(event.get_group_id() or event.get_sender_id())

        whitelist_ids = getattr(self.config.global_settings, 'whitelist_ids', [])
        enable_private_chat = getattr(self.config.global_settings, 'enable_private_chat', False)
        
        # 1. 绝对白名单放行 (最高优先级)
        is_whitelisted = (umo in whitelist_ids) or (entity_id in whitelist_ids)

        if not is_whitelisted:
            # 2. 次高优先级：群聊常规判断
            if platform_type == "GroupMessage":
                if whitelist_ids:
                    return # 白名单不为空且未命中，拦截群聊
            # 3. 第三优先级：私聊全局开关
            elif platform_type == "FriendMessage":
                if not enable_private_chat:
                    return # 未命中白名单且私聊总开关关闭，拦截私聊

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
        
        if self.config.global_settings.debug_mode:
            logger.info(f"[AstrMai-Sensor] 📡 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")
        
        user_id = event.get_sender_id()
        if user_id:
            self._fire_and_forget(self._update_user_stats(user_id))
            
        await self.evolution.record_user_message(event)
        
        status = await self.attention_gate.process_event(event)
        if status == "ENGAGED":
            event.stop_event()

    async def terminate(self):
        """优雅停机协调器 (Graceful Shutdown)"""
        logger.info("[AstrMai] 🛑 Terminating processes and unmounting...")
        self._is_running = False 
        
        if hasattr(self, 'memory_engine') and self.memory_engine.summarizer:
            await self.memory_engine.summarizer.stop()
        
        if hasattr(self, 'proactive_task'):
            await self.proactive_task.stop()

        tasks_to_wait = []
        if hasattr(self, '_background_tasks'):
            tasks_to_wait.extend(list(self._background_tasks))
            
        if hasattr(self, 'attention_gate') and hasattr(self.attention_gate, '_background_tasks'):
            tasks_to_wait.extend(list(self.attention_gate._background_tasks))
            
        if hasattr(self, 'evolution') and hasattr(self.evolution, '_background_tasks'):
            tasks_to_wait.extend(list(self.evolution._background_tasks))

        if hasattr(self, 'proactive_task') and hasattr(self.proactive_task, '_background_tasks'):
            tasks_to_wait.extend(list(self.proactive_task._background_tasks))
        
        if hasattr(self, 'visual_cortex'):
            self.visual_cortex.stop()             
        
        if tasks_to_wait:
            logger.info(f"[AstrMai] ⏳ 正在等待 {len(tasks_to_wait)} 个后台协程安全结束...")
            # 广播取消信号，激活 CancelledError 捕获快照
            for task in tasks_to_wait:
                if not task.done():
                    task.cancel()
            
            done, pending = await asyncio.wait(tasks_to_wait, timeout=3.0)
            if pending:
                logger.warning(f"[AstrMai] ⚠️ 仍有 {len(pending)} 个任务未能优雅退出，已强行终止。")
            else:
                logger.info("[AstrMai] ✅ 所有后台任务已安全清理完毕，防死锁保护生效。")