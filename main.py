import asyncio
import re
import copy  # 鐢ㄤ簬娣辨嫹璐?(淇 Bug 1)
import time  # 鐢ㄤ簬鏃堕棿鎴宠妭娴?(淇 Bug 2)
import astrbot.api.message_components as Comp  # 鎻愬崌鑷冲叏灞€瀵煎叆 (淇 Bug 3)
import contextvars # 鐢ㄤ簬瀵煎叆涓婁笅鏂囧彉閲忕浉鍏冲簱
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
from .astrmai.infra.lane_manager import LaneKey, LaneManager
from .astrmai.infra.event_bus import EventBus 
from .astrmai.infra.chat_runtime_coordinator import ChatRuntimeCoordinator
from .astrmai.infra.host_bridge import HostBridge

# --- Phase 4: Memory ---
from .astrmai.memory.engine import MemoryEngine
from .astrmai.memory.react_retriever import ReActRetriever  # Phase 2

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
from .astrmai.evolution.reflector import ExpressionReflector  # Phase 4
from .astrmai.evolution.expression_auto_check_task import ExpressionAutoCheckTask
from .astrmai.evolution.reflect_tracker import ReflectTracker
from .astrmai.evolution.review_service import ExpressionReviewService

# --- Phase 2: System 1 (Heart) ---
from .astrmai.Heart.state_engine import StateEngine
from .astrmai.Heart.judge import Judge
from .astrmai.Heart.sensors import PreFilters
from .astrmai.Heart.attention import AttentionGate
from .astrmai.Heart.visual_cortex import VisualCortex 
from .astrmai.Heart.group_reply_wait_manager import GroupReplyWaitManager

# --- Phase 7: System 3 (Task) ---
from .astrmai.work.router import Sys3Router
from .astrmai.work.cron_guard.heartbeat import CronHeartbeatGuard

# --- Phase 6.3: Frequency Controller ---
from .astrmai.Heart.frequency_controller import FrequencyController
# --- Phase 8.3: Private Chat Manager ---
from .astrmai.Heart.private_chat_manager import PrivateChatManager

@register("astrmai", "Gemini Antigravity", "AstrMai: Dual-Process Architecture Plugin", "1.0.0", "https://github.com/0d00-Ciallo-0721/astrbot_plugin_astrmaimai")
class AstrMaiPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        import weakref
        super().__init__(context)
        self.raw_config = config 
        
        self.config = AstrMaiConfig(**(config or {}))
        
        self._background_tasks = set() 
        self.runtime_coordinator = ChatRuntimeCoordinator()
        self.host_bridge = HostBridge()
        
        # 馃煝 [鏍稿績淇] 閫傞厤鏂扮殑妯″瀷姹犲垪琛?(List[str]) 鏇挎崲鏃у崟浣撳瓧绗︿覆
        task_models = getattr(self.config.provider, 'task_models', []) or ['Unconfigured']
        agent_models = getattr(self.config.provider, 'agent_models', []) or ['Unconfigured']
        embedding_models = getattr(self.config.provider, 'embedding_models', [])
        
        # 鎵撳嵃鍒楄〃涓殑棣栭€夋ā鍨嬩互渚涘惎鍔ㄦ棩蹇楃‘璁?
        logger.info(f"[AstrMai] 馃殌 Booting... Task(Judge): {task_models[0]} | Agent: {agent_models[0]}")

        self.persistence = PersistenceManager()                 
        self.db_service = DatabaseService(self.persistence)     
        self.gateway = GlobalModelGateway(context, self.config) 
        self.lane_manager = LaneManager(context.conversation_manager, config=self.config)
        self.gateway.set_lane_manager(self.lane_manager)
        self.event_bus = EventBus()   
        
        # 馃煝 [鏍稿績淇] 浼犲弬鏀逛负 embedding_models
        self.memory_engine = MemoryEngine(context, self.gateway, embedding_models=embedding_models)

        self.state_engine = StateEngine(self.persistence, self.gateway, event_bus=self.event_bus)
        self.judge = Judge(self.gateway, self.state_engine)
        self.sensors = PreFilters(self.config) 

        self.visual_cortex = VisualCortex(self.gateway, self.db_service) 

        self.reply_engine = ReplyEngine(
            self.state_engine,
            self.state_engine.mood_manager,
            runtime_coordinator=self.runtime_coordinator,
        )
        self.evolution = EvolutionManager(self.db_service, self.gateway)

        self.persona_summarizer = PersonaSummarizer(self.persistence, self.gateway, memory_engine=self.memory_engine)
        self.context_engine = ContextEngine(self.db_service, self.persona_summarizer)
        
        # Phase 2: ReAct Agent 璁板繂妫€绱㈠櫒
        self.react_retriever = ReActRetriever(
            memory_engine=self.memory_engine,
            db_service=self.db_service,
            gateway=self.gateway,
            config=self.config
        )
        
        # 馃煝 鏄惧紡浼犲叆 db_service 缁?PromptRefiner锛岃В鍐冲浘鐗囧け蹇嗙棁
        self.prompt_refiner = PromptRefiner(
            self.memory_engine, 
            self.db_service, 
            self.config,
            react_retriever=self.react_retriever  # Phase 2 娉ㄥ叆
        ) 
        
        # 馃煝 [Sys3閰嶇疆鎷︽埅] 鏍规嵁閰嶇疆鍐冲畾鏄惁鍒濆鍖?Sys3 璺敱涓庨檷绾х増蹇収瀹堟姢
        if getattr(self.config, 'sys3', None) and getattr(self.config.sys3, 'enable_work_mode', False):
            self.sys3_router = Sys3Router(self.config, context, self.db_service)
            self.cron_guard = CronHeartbeatGuard(self.db_service, context)
            logger.info("[AstrMai] Sys3 (Work) enabled by config.")
        else:
            self.sys3_router = None
            self.cron_guard = None
            logger.info("[AstrMai] Sys3 (Work) disabled; running in chat-only mode.")

        self.system2_planner = Planner(
            context, 
            self.gateway, 
            self.context_engine, 
            self.reply_engine,
            self.memory_engine, 
            self.evolution,
            state_engine=self.state_engine,
            prompt_refiner=self.prompt_refiner,
            sys3_router=self.sys3_router,
            runtime_coordinator=self.runtime_coordinator,
        )

        # Phase 6.3: 鍙戣█棰戠巼鎺у埗鍣?(蹇呴』鍦?AttentionGate 涔嬪墠鍒涘缓)
        self.frequency_controller = FrequencyController(config=self.config)
        # Phase 8.3: 绉佽亰涓撶敤浼氳瘽绠＄悊鍣?
        self.private_chat_manager = PrivateChatManager(config=self.config)
        self.group_reply_wait_manager = GroupReplyWaitManager()

        self.attention_gate = AttentionGate(
            state_engine=self.state_engine,
            judge=self.judge,
            sensors=self.sensors,
            system2_callback=self._system2_entry,
            config=self.config,                          
            persona_summarizer=self.persona_summarizer,  
            visual_cortex=self.visual_cortex,
            frequency_controller=self.frequency_controller,  # Phase 6.3 娉ㄥ叆
            private_chat_manager=self.private_chat_manager,
            runtime_coordinator=self.runtime_coordinator,
        )
        
        # Phase 4: 琛ㄨ揪鍙嶆€濆櫒
        self.reflector = ExpressionReflector(
            db_service=self.db_service,
            gateway=self.gateway,
            config=self.config
        )
        self.reflect_tracker = ReflectTracker(
            db_service=self.db_service,
            gateway=self.gateway,
            config=self.config,
        )
        self.review_service = ExpressionReviewService(self.db_service)
        self.auto_check_task = ExpressionAutoCheckTask(
            db_service=self.db_service,
            gateway=self.gateway,
            tracker=self.reflect_tracker,
            config=self.config,
        )
        
        self.proactive_task = ProactiveTask(
            context=context,
            state_engine=self.state_engine,
            gateway=self.gateway,
            persistence=self.persistence,
            memory_engine=self.memory_engine,
            reflector=self.reflector,  # Phase 4 娉ㄥ叆
            config=self.config,
        )
        self.proactive_task.auto_check_task = self.auto_check_task
        self.proactive_task.reflect_tracker = self.reflect_tracker
        # Phase 7: 娉ㄥ叆 db_service锛堝欢杩熷埌 start() 鍓嶏級
        self.proactive_task.set_db_service(self.db_service)

        logger.info("[AstrMai] ✅ 双进程主架构已就绪（Phase 1-7 已挂载）。")

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50):
        return await self.review_service.list_pending_reviews(group_id=group_id or None, limit=limit)

    async def get_expression_review_detail(self, pattern_id: int):
        return await self.review_service.get_review_detail(pattern_id)

    async def submit_expression_review(
        self,
        pattern_id: int,
        decision: str,
        reviewer_id: str,
        replacement_expression: str = "",
        style: str = "",
        reason: str = "",
        weight_delta: float = 0.0,
    ):
        return await self.review_service.submit_review(
            pattern_id=pattern_id,
            decision=decision,
            reviewer_id=reviewer_id,
            replacement_expression=replacement_expression,
            style=style or None,
            reason=reason,
            weight_delta=weight_delta,
        )

    async def _update_user_stats(self, user_id: str):
        await self.state_engine.increment_user_message_count(user_id)
        
    def _fire_and_forget(self, coro):
        """Wrap background tasks so they are retained and errors are surfaced."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

    def _handle_task_result(self, task: asyncio.Task):
        """Handle background task completion and log failures."""
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[AstrMai-Background] 鍚庡彴浠诲姟寮傚父: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass       
    
    async def _init_memory(self):
        await self.memory_engine.initialize()
        await self.memory_engine.start_background_tasks()

    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        logger.info("[AstrMai] 馃弫 AstrBot Loaded. Starting System Initialization...")
        logger.info("[AstrMai] 馃 Initializing Memory Engine...")
        await self._init_memory()
        init_meme_storage()        
        await self.sensors._load_foreign_commands()
        await self.proactive_task.start()
        self.visual_cortex.start()
        # 鎷夎捣鍐呭瓨鍚庡彴浠ｈ阿浠诲姟
        self._fire_and_forget(self._memory_gc_task())
        # 鎷夎捣鏁版嵁搴撴壒閲忓悓姝ュ悗鍙颁换鍔?
        self._fire_and_forget(self._db_sync_task())
        
        # 馃煝 [Sys3閰嶇疆鎷︽埅] 浠呭綋 Sys3 鍚敤涓斿瓨鍦ㄦ椂锛屾墠鍚姩瀹堟姢杩涚▼
        if getattr(self, 'cron_guard', None):
            await self.cron_guard.reload_all_lost_jobs()
            self._fire_and_forget(self.cron_guard.run_heartbeat())
            logger.info("[AstrMai] Sys3 CronHeartbeatGuard started.")

    async def _db_sync_task(self):
        """Flush database-related background state on a fixed interval."""
        while getattr(self, '_is_running', True):
            try:
                await asyncio.sleep(15)  # 姣?15 绉掑悓姝ヤ竴娆?
                if hasattr(self.state_engine, 'flush_message_counters'):
                    await self.state_engine.flush_message_counters()
            except asyncio.CancelledError:
                logger.info("[AstrMai-DB-Sync] 馃洃 鏀跺埌缁堟淇″彿锛屾墽琛屾渶鍚庝竴娆′簨鍔℃彁浜ら噴鏀鹃攣...")
                if hasattr(self.state_engine, 'flush_message_counters'):
                    await self.state_engine.flush_message_counters()
                raise
            except Exception as e:
                logger.error(f"[AstrMai-DB-Sync] 🚨 数据库批量同步任务异常: {e}")

    async def _memory_gc_task(self):
        """扩展 GC 范围，清理失活焦点池和空闲锁，避免残留缓存长期堆积。"""
        while getattr(self, '_is_running', True):
            try:
                await asyncio.sleep(3600)  # 姣忓皬鏃舵墽琛屼竴娆?
                now = time.time()
                
                # 1. 璇嗗埆骞跺畨鍏ㄥ洖鏀?Attention 灞傜敱浜庣兢娲昏穬搴︿笅闄嶉仐鐣欑殑鍍靛案姹?
                attention_stale_count = 0
                if hasattr(self, 'attention_gate') and hasattr(self.attention_gate, 'focus_pools'):
                    async with self.attention_gate._pool_lock:
                        for c_id, ctx in list(self.attention_gate.focus_pools.items()):
                            if now - ctx.last_active_time > 86400:
                                async with ctx.lock:
                                    if now - ctx.last_active_time > 86400:  # 浜屾鏍￠獙
                                        self.attention_gate.focus_pools.pop(c_id, None)
                                        attention_stale_count += 1
                                        
                if attention_stale_count > 0:
                    logger.info(f"[AstrMai-GC] cleaned {attention_stale_count} stale focus pools.")
            except asyncio.CancelledError:
                logger.info("[AstrMai-GC] 📴 内存 GC 任务收到终止信号，正在安全退出...")
                raise
            except Exception as e:
                logger.error(f"[AstrMai-GC] 馃毃 鍐呭瓨 GC 浠诲姟鍙戠敓寮傚父: {e}")

    async def _get_sys2_lock(self, chat_id: str) -> asyncio.Lock:
        """Get the per-chat System 2 lock safely."""
        return await self.runtime_coordinator.get_sys2_lock(chat_id)

    async def _system2_entry(self, main_event: AstrMessageEvent, events_to_process: list = None): 
        chat_id = main_event.unified_msg_origin
        lock = await self._get_sys2_lock(chat_id)
        
        logger.debug(f"[{chat_id}] 🧠 System 2 请求已登记，正在排队等待进入主执行队列...")
            
        async with lock:
            try:
                if isinstance(events_to_process, list) and len(events_to_process) > 0:
                    queue_events = events_to_process.copy()
                else:
                    queue_events = [main_event]
                
                main_event.set_extra("astrmai_reply_sent", False)
                main_event.set_extra("astrmai_wait_targets", [])
                main_event.set_extra("astrmai_wait_target_name", "")

                await self.state_engine.consume_energy(chat_id)
                await self.lane_manager.ensure_lane(
                    lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
                    base_origin=chat_id,
                )
                await self.system2_planner.plan_and_execute(main_event, queue_events)
                reply_sent = bool(main_event.get_extra("astrmai_reply_sent", False))
                if getattr(self, "runtime_coordinator", None):
                    await self.runtime_coordinator.update_wait_targets(
                        chat_id,
                        list(main_event.get_extra("astrmai_wait_targets", []) or []),
                        str(main_event.get_extra("astrmai_wait_target_name", "") or ""),
                    )
                
                # Phase 8.3: 绉佽亰鍥炶瘽绛夊緟閫昏緫
                is_private = main_event.get_extra("is_private_chat", False)
                if reply_sent and is_private and self.private_chat_manager:
                    sender_id = str(main_event.get_sender_id())
                    # 杩涘叆绛夊緟鐘舵€?(閲婃斁閿佸墠闃诲锛屾柊娑堟伅浠?AttentionGate 浜х敓鎵撴柇)
                    has_reply = await self.private_chat_manager.wait_for_new_message(sender_id)
                    if not has_reply:
                        logger.info(f"[{chat_id}] 💤 私聊用户长时间未回复，会话已自然休眠，后续可触发主动破冰。")
                        # TODO: 鑻ュ厑璁革紝杩欓噷鍙互杩藉姞 Proactive Poke 鐨勯€昏緫
                elif reply_sent and main_event.get_group_id() and self.group_reply_wait_manager:
                    self.group_reply_wait_manager.register_from_reply_event(main_event)
            finally:
                logger.debug(f"[AstrMai] System2 execution finished safely for {chat_id}.")

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        """Show AstrMai status and help information."""
        
        # 馃煝 [鏍稿績淇] 灏嗗崟浣撴ā鍨嬫樉绀烘洿鏂颁负妯″瀷姹犻暱搴?棣栭€夋ā鍨嬫樉绀?
        task_models = getattr(self.config.provider, 'task_models', [])
        agent_models = getattr(self.config.provider, 'agent_models', [])
        embedding_models = getattr(self.config.provider, 'embedding_models', [])
        fallback_models = getattr(self.config.provider, 'fallback_models', [])
        
        task_str = f"{task_models[0]} (+{len(task_models)-1})" if task_models else "Unconfigured"
        agent_str = f"{agent_models[0]} (+{len(agent_models)-1})" if agent_models else "Unconfigured"
        emb_str = f"{embedding_models[0]} (+{len(embedding_models)-1})" if embedding_models else "Unconfigured"
        fallback_str = f"({len(fallback_models)} models standby)" if fallback_models else "(No fallback)"
        
        help_text = (
            "✨ **AstrMai (v1.0.0)**\n"
            "-----------------------\n"
            "🧠 架构状态：Phase 6 (Lifecycle Active)\n"
            f"🔲 Task Pool: {task_str}\n"
            f"🔲 Agent Pool: {agent_str}\n"
            f"🔲 Emb Pool: {emb_str}\n"
            f"🛟 Fallback: {fallback_str}\n"
            "💾 SQLite & Faiss RAG: Connected\n"
            "🧬 Subconscious Miner: Running\n"
            "🌱 Proactive Life: Running"
        )
        yield event.plain_result(help_text)

    # ==========================================
    # 馃摗 鏍稿績浜嬩欢娴佸鐞?(Event Routing)
    # ==========================================

    def _is_framework_command(self, msg: str) -> bool:
        """Detect whether the incoming text is an AstrBot framework command."""
        if not msg:
            return False
            
        # 1. 娓呮礂闆跺瀛楃
        clean_text = msg.replace('\u200b', '').strip()
        if not clean_text:
            return False
            
        # 2. 鍓ョ鍙兘鐨勫墠缂€ (鏀寔鑷畾涔夊墠缂€涓庨粯璁ゆ枩鏉狅紝涓斿厤鐤?"/ 鎸囦护" 鐨勭┖鏍煎共鎵?
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
            
        # 3. 鑾峰彇鐪熸鐨勯璇?
        clean_cmd = clean_text.split()[0].lower()
        
        # 4. 鏋勫缓瀹炴椂鎸囦护姹?
        registered_cmds = {"help", "plugin", "restart", "reload", "stop", "start", "list", "provider"}
        
        try:
            from astrbot.core.star.command_management import _collect_descriptors
            # 瀹炴椂浠庣儹鍔犺浇鐨?Handler 娉ㄥ唽琛ㄤ腑鎶撳彇鍏ㄩ儴鎻忚堪绗?
            descriptors = _collect_descriptors(include_sub_commands=True)
            
            for desc in descriptors:
                if desc.effective_command:
                    registered_cmds.add(str(desc.effective_command).split()[0].lower())
                
                if getattr(desc, 'aliases', None):
                    for alias in desc.aliases:
                        registered_cmds.add(str(alias).split()[0].lower())
                        
        except Exception as e:
            from astrbot.api import logger
            logger.debug(f"[AstrMai-Filter] 鍐呭瓨鎬佺┛閫忓け璐ワ紝灏濊瘯闄嶇骇: {e}")
            try:
                cmd_mgr = getattr(self.context, 'command_manager', None)
                if cmd_mgr and hasattr(cmd_mgr, 'commands'):
                    registered_cmds.update([str(k).lower() for k in cmd_mgr.commands.keys()])
            except Exception:
                pass

        # 5. 铻嶅悎 config 涓敤鎴锋墜鍔ㄩ厤缃殑棰濆鎸囦护鍏滃簳榛戝悕鍗?
        try:
            extra_cmds = getattr(self.config.system1, 'extra_command_list', [])
            if extra_cmds:
                registered_cmds.update([str(c).lower() for c in extra_cmds])
        except Exception:
            pass
            
        # 6. 鍒ゅ喅
        return clean_cmd in registered_cmds

    @filter.on_decorating_result()
    async def sniff_external_plugin_results(self, event: AstrMessageEvent):
        """
        旁路嗅探器：截获其他插件即将下发的消息，并将其注入 Sys1 注意力窗口和 Evolution 数据流。
        """
        import time
        import astrbot.api.message_components as Comp

        # 鍓嶇疆闃插尽锛氭帓闄よ嚜韬彂閫佺殑娑堟伅
        if event.get_extra("astrmai_is_self_reply", False):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        # 鎻愬彇鍑哄叾浠栨彃浠跺噯澶囧彂閫佺殑绾枃鏈垨鍥剧墖鏍囪瘑
        reply_text = ""
        for comp in result.chain:
            if isinstance(comp, Comp.Plain):
                reply_text += comp.text
            elif isinstance(comp, Comp.Image):
                reply_text += "[图片]"

        if not reply_text:
            return

        chat_id = event.unified_msg_origin
        
        # 瀹夊叏鑾峰彇 bot_id
        bot_id = ""
        if hasattr(event, 'get_self_id'):
            try:
                bot_id = str(event.get_self_id())
            except:
                pass
        if not bot_id:
            bot_id = getattr(event.message_obj, 'self_id', 'SELF_BOT') if hasattr(event, 'message_obj') and event.message_obj else 'SELF_BOT'

        # 鏋勯€犳敞鍏ュ璞?
        bot_reply_event = {
            "is_external_bot_reply": True,
            "content": reply_text,
            "timestamp": time.time()
        }

        # 寮鸿濉炲叆婊戝姩绐楀彛
        if hasattr(self, 'attention_gate') and hasattr(self.attention_gate, 'inject_external_event'):
            await self.attention_gate.inject_external_event(chat_id, bot_reply_event)

        # 鍐欏叆杩涘寲灞?
        if hasattr(self, 'evolution'):
            await self.evolution.process_bot_reply(chat_id, bot_id, f"(鍐呯疆鎻掍欢鎵ц缁撴灉): {reply_text}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_global_message(self, event: AstrMessageEvent):
        """Main event entry for all inbound platform messages."""
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
                logger.warning(f"[AstrMai-Sensor] ⚠️ 极速防抖生效，已拦截 AstrBot 框架双发/分身消息: {msg_str[:15]}")
                return 
                
            sys._astrmai_global_debounce_cache[fingerprint] = now

        message_chain = getattr(event.message_obj, 'message', []) if event.message_obj else []
        
        if any(isinstance(c, Comp.Poke) for c in message_chain):
            if hasattr(self, 'sensors') and hasattr(self, 'attention_gate'):
                await self.sensors.process_poke_event(event, self.context, self.attention_gate)
            return

        msg = event.message_str.strip() if event.message_str else ""
        
        # 鏃犵姸鎬佹寚浠ゆ劅鐭ユ斁琛岀郴缁?
        if msg and self._is_framework_command(msg):
            return

        # ==========================================
        # [淇敼] 缁熶竴 ID 瑙ｆ瀽鍣ㄤ笌涓夌骇鏉冮檺璺敱
        # ==========================================
        umo = str(event.unified_msg_origin)
        parts = umo.split(":")
        platform_type = parts[1] if len(parts) >= 3 else ("GroupMessage" if event.get_group_id() else "FriendMessage")
        entity_id = parts[2] if len(parts) >= 3 else str(event.get_group_id() or event.get_sender_id())

        whitelist_ids = getattr(self.config.global_settings, 'whitelist_ids', [])
        admin_ids = getattr(self.config.global_settings, 'admin_ids', [])
        enable_private_chat = getattr(self.config.global_settings, 'enable_private_chat', False)
        
        is_admin = entity_id in admin_ids or sender_id in admin_ids
        
        # 1. 缁濆鐧藉悕鍗曟斁琛?(鏈€楂樹紭鍏堢骇)锛氱鐞嗗憳鑷甫缁濆鐧藉悕鍗?
        is_whitelisted = (umo in whitelist_ids) or (entity_id in whitelist_ids) or is_admin

        if not is_whitelisted:
            # 2. 娆￠珮浼樺厛绾э細缇よ亰甯歌鍒ゆ柇
            if platform_type == "GroupMessage":
                if whitelist_ids:
                    return # 鐧藉悕鍗曚笉涓虹┖涓旀湭鍛戒腑锛屾嫤鎴兢鑱?
            # 3. 绗笁浼樺厛绾э細绉佽亰鍏ㄥ眬寮€鍏?
            elif platform_type == "FriendMessage":
                if not enable_private_chat and not is_admin:
                    return # 鏈懡涓櫧鍚嶅崟涓旂鑱婃€诲紑鍏冲叧闂紝涓斾笉鏄鐞嗗憳锛屾嫤鎴鑱?

        self_id = None
        if hasattr(event.message_obj, 'self_id'):
            self_id = str(event.message_obj.self_id)
        if not self_id and hasattr(event, 'bot') and hasattr(event.bot, 'self_id'):
            self_id = str(event.bot.self_id)
        if not self_id:
            self_id = "unknown"
            
        if str(event.get_sender_id()) == self_id:
            return

        group_wait_result = "NONE"
        if event.get_group_id() and self.group_reply_wait_manager:
            group_wait_result = self.group_reply_wait_manager.handle_incoming_message(event)

        sender_name = event.get_sender_name()
        
        if self.config.global_settings.debug_mode:
            logger.info(f"[AstrMai-Sensor] 📨 收到消息 | 发送者: {sender_name} | 内容: {msg_str[:20]}...")
        
        user_id = event.get_sender_id()
        if user_id:
            self._fire_and_forget(self._update_user_stats(user_id))

        if hasattr(self, "reflect_tracker") and self.reflect_tracker:
            review_feedback = await self.reflect_tracker.try_consume_feedback(event)
            if review_feedback:
                yield event.plain_result(review_feedback)
                return
            
        await self.evolution.record_user_message(event)
        
        # 鎵ц闂ㄦ帶閫昏緫
        status = await self.attention_gate.process_event(event)
        
        # ==========================================
        # 馃煝 [鏋舵瀯绾т慨澶峕 绮惧噯浜嬩欢闃绘柇閫昏緫 (瑙ｅ喅鍙岄噸鍥炲涓斾笉楗挎鍏朵粬鎻掍欢)
        # ==========================================
        is_direct_call = False
        
        # 1. 鍒ゅ畾鏄惁涓虹鑱?(绉佽亰蹇呭畾鏄洿鎺ュ懠鍙?
        if not event.get_group_id():
            is_direct_call = True
        else:
            # 2. 鍒ゅ畾缇よ亰涓槸鍚︽槑纭?@ 浜嗘満鍣ㄤ汉
            bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else ""
            if event.message_obj and event.message_obj.message:
                for c in event.message_obj.message:
                    if isinstance(c, Comp.At) and str(c.qq) == bot_id:
                        is_direct_call = True
                        break

        if (
            event.get_group_id()
            and self.group_reply_wait_manager
            and group_wait_result != "RESUME"
            and status in {"ENGAGED", "BUFFERED"}
        ):
            self.group_reply_wait_manager.cancel_wait(
                event.unified_msg_origin,
                reason=f"interrupted_by_{status.lower()}",
            )

        # 閫昏緫鍒ゅ喅锛?
        # - 鍓ョ鏆村姏鎴柇 event.stop_event()锛屼繚鎶や簨浠剁洃鍚摼涓嶈鍒囨柇銆?
        # - 濡傛灉 status == "ENGAGED" (琚垽瀹氫负鏋侀€熷搷搴?锛屽繀鐒堕樆鏂師鐢?LLM銆?
        # - 濡傛灉 is_direct_call == True (绉佽亰鎴栨槑纭瓳)锛屾棤璁?AstrMai 鏄湪寮€绐楀彛缂撳啿杩樻槸鍐冲畾蹇界暐锛?
        #   閮藉凡缁忕敱 AstrMai 鍏ㄦ潈鎺ョ浜嗗璇濇剰蹇楋紝蹇呴』鎶涘嚭骞界伒鍗犱綅绗︽楠楀簳灞傞粯璁?LLM锛岃鍏朵紤鐪狅紒
        if status == "ENGAGED" or is_direct_call:
            
            # 馃専 [鏍稿績淇] 鎶曢€?call_llm 璇遍サ锛岃涔夌骇娆洪獥搴曞眰 ProcessStage 鐨勫厹搴曞垽瀹?
            # 姝ゆ搷浣滄棤鎹熸斁琛屼笅娓告寚浠?鍔熻兘鎻掍欢锛屼絾浼氱洿鎺ラ樆鏂簳灞?AstrMainAgent 鐨勫弻閲嶅洖澶?
            ghost_message = self.host_bridge.suppress_default_llm(event)
            yield event.plain_result(ghost_message)

    @filter.on_decorating_result(priority=90)
    async def intercept_and_notify_errors(self, event: AstrMessageEvent):
        """
        全局拦截器：1. 静默销毁幽灵占位符 2. 拦截 API 错误并私发给管理员。
        """
        result = event.get_result()
        if not result:
            return
            
        message_str = result.get_plain_text()
        if not message_str:
            try:
                reply_text = ""
                chain = getattr(result, 'chain', None)
                if chain:
                    if isinstance(chain, str):
                        reply_text = chain
                    elif hasattr(chain, '__iter__'):
                        for comp in chain:
                            if hasattr(comp, 'text'):
                                reply_text += str(comp.text)
                            elif isinstance(comp, str):
                                reply_text += comp
                message_str = reply_text
            except Exception as e:
                from astrbot.api import logger
                logger.warning(f"解析回复链失败: {e}")
                return
                
        if not message_str:
            return

        # ==========================================
        # 馃煝 [鏋舵瀯绾т慨澶峕 闈欓粯閿€姣佸菇鐏靛崰浣嶇 (浼樺厛鎷︽埅)
        # ==========================================
        if self.host_bridge.is_ghost_sentinel(message_str):
            from astrbot.api import logger
            logger.debug("[AstrMai-Phantom] ghost placeholder intercepted and dropped silently.")
            event.set_result(None)  # 娓呯┖鍐呭锛岀‘淇濅笉鍙戦€佺粰鐢ㄦ埛
            return  # 绔嬪嵆鏀捐缁撴潫锛岄槻姝㈣Е鍙戜笅闈㈢殑鎶ラ敊鍛婅

        # ==========================================
        # 妫€鏌ユ槸鍚﹀紑鍚簡閿欒鎷︽埅 (鍘熼€昏緫)
        # ==========================================
        if not getattr(self.config.global_settings, 'enable_error_interception', True):
            return
            
        # 瀹氫箟閿欒鐗瑰緛搴?
        if self.host_bridge.should_intercept_error(
            message_str,
            enabled=getattr(self.config.global_settings, 'enable_error_interception', True),
        ):
            from astrbot.api import logger
            logger.warning(f"[AstrMai-ErrorGuard] 拦截到系统报错，已阻止下发: {message_str[:50]}...")
            
            # 1. 褰诲簳鎷︽埅娑堟伅
            event.set_result(None)
            event.stop_event()
            
            # 2. 缁勮鍛婅淇℃伅
            alert_msg = self.host_bridge.build_admin_alert(event, message_str)
            
            # 3. 闈跺悜鎶曢€掔粰绠＄悊鍛?
            admin_ids = getattr(self.config.global_settings, 'admin_ids', [])
            client = getattr(event, 'bot', None)
            
            if client and hasattr(client, 'api'):
                for admin_id in self.host_bridge.admin_targets(admin_ids):
                    if str(admin_id).isdigit():
                        try:
                            await client.api.call_action('send_private_msg', user_id=int(admin_id), message=alert_msg)
                        except Exception as e:
                            logger.error(f"[AstrMai-ErrorGuard] 无法向管理员 {admin_id} 推送告警: {e}")
    
    @filter.command("work")
    async def enter_sys3_direct(self, event: AstrMessageEvent):
        """Enter Sys3 direct task mode and execute with the full toolset."""
        
        # 馃煝 [Sys3閰嶇疆鎷︽埅] 鑻ユ湭寮€鍚换鍔℃ā寮忥紝鎷︽埅鎸囦护骞惰繘琛屾彁绀?
        if not getattr(self.config, 'sys3', None) or not getattr(self.config.sys3, 'enable_work_mode', False):
            yield event.plain_result("Sys3 work mode is disabled. Please enable it in WebUI first.")
            return
            
        task_query = event.message_str.replace("/work", "").strip()
        if not task_query:
            yield event.plain_result(
                "❌ 请告诉我需要执行什么任务。\n"
                "示例：/work 帮我定一个明天早上8点的开会提醒"
            )
            return
        
        umo = event.unified_msg_origin
        chat_id = umo
        
        # 鑾峰彇 Provider ID
        models = self.gateway.get_agent_models()
        if not models or models[0] == 'Unconfigured':
            yield event.plain_result("Agent model is not configured, so the task cannot run.")
            return
        full_tools = await self.sys3_router.get_full_tools_for_direct_entry()
        
        # 鍏嶇柅鏍囪涓庡簳灞傛鏋跺厹搴曞菇鐏甸攣
        event.set_extra("astrmai_is_self_reply", True)  
        event.call_llm = True  
        
        from astrbot.api import logger
        logger.info(f"[{chat_id}] 🔧 [/work 直连] 进入 Sys3 纯任务模式：{task_query[:50]}...")
        
        try:
            result = await self.gateway.tool_chat_in_lane_result(
                lane_key=LaneKey(subsystem="sys3", task_family="direct", scope_id=chat_id),
                base_origin=chat_id,
                event=event,
                prompt=task_query,
                system_prompt=(
                    "You are a task execution specialist with strong tool-using ability.\n"
                    "When a task arrives, call the most suitable tools directly and avoid unnecessary narration.\n"
                    "After the task is complete, report the result clearly and concisely."
                ),
                tools=full_tools,
                models=models,
                max_steps=30,
                timeout=120,
                persona_id=getattr(self.config.persona, "persona_id", "") or "astrmai",
            )
            reply = result.text
            await self.reply_engine.handle_reply(event, reply, chat_id)
            
        except Exception as e:
            logger.error(f"[{chat_id}] /work 直连 Sys3 异常: {e}")
            await self.reply_engine.handle_reply(
                event, f"任务执行中发生错误：{str(e)[:100]}", chat_id
            )

    async def terminate(self):
        """优雅停机协调器 (Graceful Shutdown)"""
        logger.info("[AstrMai] 📴 正在终止进程并卸载资源...")
        self._is_running = False 
        
        if hasattr(self, 'memory_engine') and self.memory_engine.summarizer:
            await self.memory_engine.summarizer.stop()
        
        if hasattr(self, 'proactive_task'):
            await self.proactive_task.stop()

        # 馃煝 [Sys3閰嶇疆鎷︽埅] 浠呭綋 Sys3 鍚敤涓斿瓨鍦ㄦ椂锛屾墠鍋滄瀹堟姢杩涚▼
        if getattr(self, 'cron_guard', None):
            self.cron_guard.stop()

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
            # 骞挎挱鍙栨秷淇″彿锛屾縺娲?CancelledError 鎹曡幏蹇収
            for task in tasks_to_wait:
                if not task.done():
                    task.cancel()
            
            done, pending = await asyncio.wait(tasks_to_wait, timeout=3.0)
            if pending:
                logger.warning(f"[AstrMai] {len(pending)} background tasks did not exit gracefully before timeout.")
            else:
                logger.info("[AstrMai] all background tasks were cleaned up safely.")
