import asyncio
import re
import copy  # [新增] 用于深拷贝 (修复 Bug 1)
import time  # [新增] 用于时间戳节流 (修复 Bug 2)
import astrbot.api.message_components as Comp  # [新增] 提升至全局导入 (修复 Bug 3)
import contextvars # [新增] 用于导入上下文变量相关库
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import ProviderRequest, LLMResponse
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
        
        self._session_history_buffer = {}
        self._background_tasks = set() 
        
        # 🟢 [彻底修复 Bug 1] 放弃非法的 weakref，改用强引用字典，配合主动 GC 回收空闲锁，彻底杜绝并发锁的幽灵回收与内存穿透
        self._sys2_locks = {}    
        self._memory_locks = {}  
        
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
        self.prompt_refiner = PromptRefiner(self.memory_engine, self.config) 
        self.system2_planner = Planner(
            context, 
            self.gateway, 
            self.context_engine, 
            self.reply_engine,
            self.memory_engine, 
            self.evolution,
            state_engine=self.state_engine
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
        
    # [修改] 保留此唯一的统计更新方法，彻底删除原文件末尾多余的 _get_user_lock 和 _update_user_stats
    async def _update_user_stats(self, user_id: str):
        # [修复 Bug 1] 完全移除越权的锁逻辑，强制下推给专门管理状态的 StateEngine 执行原子操作，消灭脏写
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
        # 🟢 [彻底修复 Bug 5] 拉起数据库批量同步后台任务
        self._fire_and_forget(self._db_sync_task())

    async def _db_sync_task(self):
        """[修改] 数据库微批处理后台任务，增加 CancelledError 保护防死锁"""
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

    # [修改] 位置: main.py -> AstrMaiPlugin 类下
    async def _memory_gc_task(self):
        """[修改] 扩大 GC 范围，彻底消除 TOCTOU 竞态条件，增加防死锁保护与锁池回收"""
        while getattr(self, '_is_running', True):
            try:
                await asyncio.sleep(3600)  # 每小时执行一次
                now = time.time()
                stale_chats = []
                
                # 1. 识别并仅清理 Buffer 业务数据
                for chat_id, data in list(self._session_history_buffer.items()):
                    if now - data.get("last_update", 0) > 86400:
                        stale_chats.append(chat_id)
                        
                for chat_id in stale_chats:
                    lock = self._get_memory_lock(chat_id)
                    async with lock:
                        data = self._session_history_buffer.get(chat_id)
                        if data and now - data.get("last_update", 0) > 86400:
                            self._session_history_buffer.pop(chat_id, None)

                # 2. 识别并安全回收 Attention 层由于群活跃度下降遗留的僵尸池
                attention_stale_count = 0
                if hasattr(self, 'attention_gate') and hasattr(self.attention_gate, 'focus_pools'):
                    async with self.attention_gate._pool_lock:
                        for c_id, ctx in list(self.attention_gate.focus_pools.items()):
                            if now - ctx.last_active_time > 86400:
                                async with ctx.lock:
                                    if now - ctx.last_active_time > 86400:  # 二次校验
                                        self.attention_gate.focus_pools.pop(c_id, None)
                                        attention_stale_count += 1
                                        
                # 3. 🟢 [核心修复 Bug 1] 安全回收没有任何协程等待或持有的空闲系统锁，防止强引用导致的长期内存膨胀
                lock_cleaned = 0
                for lock_dict in [self._sys2_locks, self._memory_locks]:
                    for l_id, lck in list(lock_dict.items()):
                        if not lck.locked(): # 如果当前锁未被任何任务获取
                            lock_dict.pop(l_id, None)
                            lock_cleaned += 1
                    
                if stale_chats or attention_stale_count > 0 or lock_cleaned > 0:
                    logger.info(f"[AstrMai-GC] 🧹 成功回收 {len(stale_chats)} 个僵尸群缓冲池, {attention_stale_count} 个注意力残留内存, 以及 {lock_cleaned} 把空闲互斥锁。")
            except asyncio.CancelledError:
                logger.info("[AstrMai-GC] 🛑 内存 GC 任务收到终止信号，安全退出...")
                raise
            except Exception as e:
                logger.error(f"[AstrMai-GC] 🚨 内存 GC 任务发生异常: {e}")

    # [新增] 位置: main.py -> AstrMaiPlugin 类下
    def _get_sys2_lock(self, chat_id: str) -> asyncio.Lock:
        """安全获取 System 2 会话级防并发互斥锁"""
        lock = self._sys2_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._sys2_locks[chat_id] = lock
        return lock

    # [新增] 位置: main.py -> AstrMaiPlugin 类下
    def _get_memory_lock(self, chat_id: str) -> asyncio.Lock:
        """安全获取记忆缓冲区的原子操作锁"""
        lock = self._memory_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._memory_locks[chat_id] = lock
        return lock

    async def _system2_entry(self, main_event: AstrMessageEvent, events_to_process: list = None): 
        chat_id = main_event.unified_msg_origin
        lock = self._get_sys2_lock(chat_id)
        
        # 🟢 [核心修复 Bug 1] 删除了 lock.locked() 的丢弃判定，让所有通过了 Sys1 审核的有效请求排队处理。
        # 否则会导致花费 Token 判定为 REPLY 的长文上下文被并发的短消息鸠占鹊巢并无情抹杀！
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
        # [核心修改] 替换为细粒度模型的名称读取
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
    # 📡 核心事件钩子 (Event Hooks)
    # ==========================================

    def _is_framework_command(self, msg: str) -> bool:
        """
        [终极修复版] 实时探测并解析当前消息是否命中 AstrBot 底层注册的指令。
        采用底层内存态解析器 (无 DB I/O 开销)，完美兼容热重载与斜杠空格分离。
        """
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
                # 切除前缀后再次 strip，确保 "/ astrmai_debugger" 能正确变为 "astrmai_debugger"
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
        
        # 🟢 [核心修复] 调用底层同步的内存收集器 _collect_descriptors，避免协程崩溃和数据库 I/O
        try:
            from astrbot.core.star.command_management import _collect_descriptors
            # 实时从热加载的 Handler 注册表中抓取全部描述符
            descriptors = _collect_descriptors(include_sub_commands=True)
            
            for desc in descriptors:
                if desc.effective_command:
                    # 提取指令组首词
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

        # 5. 融合 config 中用户手动配置的额外指令兜底黑名单 (例如你的 "盒")
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
        """
        [修改] [入口] 接管所有平台消息，将数据泵入双系统架构与进化层。
        """
        # 🟢 [终极物理防抖屏障] 挂载到解释器根节点，结合线程锁，绝对免疫热重载分身与框架双发
        import sys
        import time
        import threading
        from astrbot.api import logger
        
        if not hasattr(sys, '_astrmai_debounce_lock'):
            sys._astrmai_debounce_lock = threading.Lock()
            sys._astrmai_global_debounce_cache = {}
            
        msg_str = event.message_str.strip() if event.message_str else ""
        if not msg_str:
            # 兜底纯图片等无文本消息，提取底层组件的长度特征作为哈希依据
            msg_str = f"obj_len_{len(str(getattr(event.message_obj, 'message', '')))}"
            
        sender_id = str(event.get_sender_id())
        chat_id = str(event.unified_msg_origin)
        
        # [彻底修复] 坚决抛弃可能带有内存地址差异的 message_id，直接用文本+发送人做强指纹！
        fingerprint = f"{chat_id}_{sender_id}_{msg_str}"
        now = time.time()
        
        with sys._astrmai_debounce_lock: # 互斥锁：分身必须排队进入
            # 原地清理过期缓存 (设置 1.5 秒绝对冷冻期)，不改变字典引用
            keys_to_delete = [k for k, v in sys._astrmai_global_debounce_cache.items() if now - v > 1.5]
            for k in keys_to_delete:
                sys._astrmai_global_debounce_cache.pop(k, None)
                
            if fingerprint in sys._astrmai_global_debounce_cache:
                # 打印醒目黄字警告，让你能亲眼看到分身被斩杀！
                logger.warning(f"[AstrMai-Sensor] 🛡️ 极速防抖生效！拦截 AstrBot 框架双发/分身消息: {msg_str[:15]}")
                return 
                
            sys._astrmai_global_debounce_cache[fingerprint] = now

        # ================= 以下为原有业务逻辑 =================
        message_chain = getattr(event.message_obj, 'message', []) if event.message_obj else []
        
        if any(isinstance(c, Comp.Poke) for c in message_chain):
            if hasattr(self, 'sensors') and hasattr(self, 'attention_gate'):
                await self.sensors.process_poke_event(event, self.context, self.attention_gate)
            return

        msg = event.message_str.strip() if event.message_str else ""
        
        # 无状态指令感知放行系统
        if msg and self._is_framework_command(msg):
            return

        group_id = event.get_group_id()
        enabled_groups = self.config.global_settings.enabled_groups
        if enabled_groups and group_id:
            if str(group_id) not in enabled_groups:
                return

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

    # =====================================================================
    # 👁️‍🗨️ [核心重构] 主拦截网关 & 全知视界探针 (整合 Sys1 隐身与 Sys2 注入)
    # =====================================================================
    @filter.on_llm_request()
    async def astrmai_master_interceptor(self, event: AstrMessageEvent, req: ProviderRequest):
        """【主拦截网关】整合内部标记清理、RAG 注入及终极日志快照打印"""
        if not event:
            return

        try:
            # --- 0. 兼容性提取 System Prompt ---
            sys_msg_text = ""
            if req.system_message and req.system_message[0].content:
                sys_msg_text = str(req.system_message[0].content)
            elif hasattr(req, 'system_prompt') and req.system_prompt: # 兼容极老版本或特殊模型适配器
                sys_msg_text = str(req.system_prompt)

            task_type = "UNKNOWN"
            is_internal = False

            # ---------------------------------------------------------
            # 1. 🥷 拦截底层网关注入的动态唯一隐身标记 (System 1)
            # ---------------------------------------------------------
            marker = getattr(self.gateway, 'internal_marker', '__ASTRMAI_INTERNAL_CALL__')
            
            # 清理系统提示词中的标记
            if marker in sys_msg_text:
                is_internal = True
                sys_msg_text = sys_msg_text.replace(marker, "").strip()
                # 写回修改
                if req.system_message:
                    req.system_message[0].content = sys_msg_text
                elif hasattr(req, 'system_prompt'):
                    req.system_prompt = sys_msg_text

            # 深度遍历 contexts (解决 v4.5.7+ 架构下 Marker 随 SystemMessageSegment 逃逸的问题)
            if hasattr(req, 'contexts') and isinstance(req.contexts, list):
                from astrbot.core.agent.message import SystemMessageSegment, TextPart
                for msg in req.contexts:
                    if isinstance(msg, SystemMessageSegment):
                        for part in getattr(msg, 'content', []):
                            if isinstance(part, TextPart) and marker in part.text:
                                is_internal = True
                                part.text = part.text.replace(marker, "").strip()

            if is_internal:
                event._system_internal_task = True
                task_type = "🥷 [System 1 / 后台推断]"

            # ---------------------------------------------------------
            # 2. 🧠 全局记忆注入 & 剧本模式核心 (System 2)
            # ---------------------------------------------------------
            # [核心修复] 彻底丢弃脆弱的 _is_final_reply_phase，改用特征文本锚定
            is_sys2 = "<CHAT_HISTORY>" in sys_msg_text or "{HISTORY_PLACEHOLDER}" in sys_msg_text
            
            if is_sys2:
                task_type = "🧠 [System 2 / 主脑决策]"
                
                # 将杂乱的替换逻辑全部移交至 PromptRefiner 专业模块处理
                await self.prompt_refiner.refine_prompt(event, req, self.context)
                
                # Refiner 注入完毕后，重新读取已被替换为完美剧本的 Prompt
                if req.system_message and req.system_message[0].content:
                    sys_msg_text = str(req.system_message[0].content)

            # ---------------------------------------------------------
            # 3. 👁️‍🗨️ 终极上下文核验探针 (全知视界日志打印)
            # ---------------------------------------------------------
            if getattr(self.config.global_settings, 'debug_mode', True):
                chat_id = getattr(event, 'unified_msg_origin', 'Unknown')
                
                history_msgs = []
                if req.messages:
                    for m in req.messages:
                        role = getattr(m, 'role', 'unknown')
                        content = getattr(m, 'content', '')
                        
                        # 为了排版美观，截断极长可能包含 Base64 的异常数据
                        if isinstance(content, str) and len(content) > 2000:
                            content = content[:2000] + "\n...[内容过长已截断]..."
                        history_msgs.append(f"<{role.upper()}>\n{content}")
                        
                full_history = "\n\n".join(history_msgs) if history_msgs else "无 Message 队列"

                logger.info(
                    f"\n{'='*70}\n"
                    f"👁️‍🗨️ 【全知视界】 拦截到即将发往大模型的 HTTP Payload 快照\n"
                    f"🎯 目标群聊: {chat_id}\n"
                    f"🔖 链路归属: {task_type}\n"
                    f"{'='*70}\n"
                    f"👇 【SYSTEM PROMPT (系统设定 & 剧本 & 记忆)】 👇\n"
                    f"{sys_msg_text}\n"
                    f"{'-'*70}\n"
                    f"👇 【MESSAGES 队列 (用户当前消息/工具链)】 👇\n"
                    f"{full_history}\n"
                    f"{'='*70}"
                )

        except Exception as e:
            logger.error(f"【全知视界探针】主拦截网关执行异常: {e}", exc_info=True)


    @filter.on_llm_response()
    async def handle_memory_reflection(self, event: AstrMessageEvent, resp: LLMResponse):
        """[修改] 阶段四：全局记忆反思与自动清理钩子 (增加生命周期强校验与中断回滚防线)"""
        if not event or not hasattr(self, 'memory_engine') or not self.memory_engine.summarizer: 
            return
        
        # 🟢 [核心修复 Bug 1] 严格过滤掉所有后台推断任务，绝不将其作为用户记忆写入
        is_internal = getattr(event, '_system_internal_task', False)
        if is_internal:
            return
            
        # 严格校验：确保只有属于正常对话回复阶段的响应才被抓取
        if not getattr(event, '_is_final_reply_phase', False):
            return
            
        ai_msg = resp.completion_text
        if not ai_msg: return
        
        # 防止漏网之鱼的后台 JSON 流入
        if ai_msg.strip().startswith('{') or ai_msg.strip().startswith('```json'):
            return
            
        chat_id = event.unified_msg_origin
        user_msg = event.message_str
        
        lock = self._get_memory_lock(chat_id)
        async with lock:
            if chat_id not in self._session_history_buffer:
                self._session_history_buffer[chat_id] = {"buffer": [], "last_update": time.time(), "cooldown_until": 0, "failures": 0}
                
            session_data = self._session_history_buffer[chat_id]
            buffer = session_data["buffer"]
            session_data["last_update"] = time.time()
            
            if user_msg and user_msg.strip(): buffer.append(f"用户：{user_msg}")
            if ai_msg and ai_msg.strip(): buffer.append(f"Bot：{ai_msg}")
            
            threshold = getattr(self.config.memory, 'summary_threshold', 30)
            
            if time.time() < session_data.get("cooldown_until", 0):
                return
            
            if len(buffer) >= threshold * 2:
                messages_to_process = buffer.copy()
                self._session_history_buffer[chat_id]["buffer"] = []
                
                history_text = "\n".join(messages_to_process)
                
                async def safe_summarize_task():
                    try:
                        await self.memory_engine.summarizer.summarize_session(
                            session_id=chat_id,
                            chat_history_text=history_text
                        )
                        async with self._get_memory_lock(chat_id):
                            if chat_id in self._session_history_buffer:
                                self._session_history_buffer[chat_id]["failures"] = 0
                    except asyncio.CancelledError:
                        # 🟢 [核心修复 Bug 3] 当协程被外力终止时，强制触发安全回滚，避免记忆蒸发
                        logger.info(f"[{chat_id}] ⚠️ 记忆摘要任务被强行中断，执行安全回滚...")
                        async with self._get_memory_lock(chat_id):
                            current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                            current_data["buffer"] = messages_to_process + current_data["buffer"]
                            self._session_history_buffer[chat_id] = current_data
                        raise
                    except Exception as e:
                        logger.error(f"[AstrMai-Memory] 🚨 记忆摘要生成失败，进入指数退避: {e}")
                        async with self._get_memory_lock(chat_id):
                            current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                            merged_buffer = messages_to_process + current_data["buffer"]
                            
                            max_capacity = threshold * 3
                            if len(merged_buffer) > max_capacity:
                                logger.warning(f"[AstrMai-Memory] ⚠️ 触及硬截断上限，丢弃 {len(merged_buffer) - max_capacity} 条极旧记忆防雪崩。")
                                merged_buffer = merged_buffer[-max_capacity:]
                                
                            current_data["buffer"] = merged_buffer
                            current_data["last_update"] = time.time()
                            
                            failures = current_data.get("failures", 0) + 1
                            current_data["failures"] = failures
                            backoff_time = min(3600, 300 * (2 ** (failures - 1)))
                            current_data["cooldown_until"] = time.time() + backoff_time
                            
                            self._session_history_buffer[chat_id] = current_data

                self._fire_and_forget(safe_summarize_task())

    async def terminate(self):
        """[修改] 优雅停机协调器 (Graceful Shutdown)"""
        logger.info("[AstrMai] 🛑 Terminating processes and unmounting...")
        self._is_running = False  # 发出全局停机广播
        
        if hasattr(self, 'memory_engine') and self.memory_engine.summarizer:
            await self.memory_engine.summarizer.stop()
        
        if hasattr(self, 'proactive_task'):
            await self.proactive_task.stop()

        tasks_to_wait = []
        if hasattr(self, '_background_tasks'):
            tasks_to_wait.extend(list(self._background_tasks))
            
        if hasattr(self, 'attention_gate') and hasattr(self.attention_gate, '_background_tasks'):
            tasks_to_wait.extend(list(self.attention_gate._background_tasks))
            
        # 🟢 [核心修复 Bug 3] 将进化模块与生命周期模块的异步任务纳入安全回收名单
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
            
            # 给予最大 3.0 秒缓冲期，让所有 try...finally 彻底释放文件锁和 SQLite DB 句柄
            done, pending = await asyncio.wait(tasks_to_wait, timeout=3.0)
            if pending:
                logger.warning(f"[AstrMai] ⚠️ 仍有 {len(pending)} 个任务未能优雅退出，已强行终止。")
            else:
                logger.info("[AstrMai] ✅ 所有后台任务已安全清理完毕，防死锁保护生效。")