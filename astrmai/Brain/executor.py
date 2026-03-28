# astrmai/Brain/executor.py
from typing import Any, List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.agent.tool import ToolSet
from ..infra.gateway import GlobalModelGateway
from .reply_engine import ReplyEngine 

class ConcurrentExecutor:
    """
    智能体执行器 (System 2)
    使用 AstrBot 原生 tool_loop_agent 替代原有手写 Action Loop。
    """
    def __init__(self, context, gateway: GlobalModelGateway, reply_engine: ReplyEngine, evolution_manager, config=None):
        self.context = context
        self.gateway = gateway
        self.reply_engine = reply_engine
        self.evolution_manager = evolution_manager  # 挂载进化管理器
        self.config = config if config else gateway.config
        
        # ==========================================
        # 🟢 [新增] 阶段 3：增强型并发状态检查所需变量
        # ==========================================
        import asyncio
        self._chat_locks = {}
        self._chat_pending_count = {}
        self._global_lock = asyncio.Lock()
        
        
    async def execute(self, event: AstrMessageEvent, prompt: str, system_prompt: str, tools: List[Any] = None, direct_vision_urls: List[str] = None):
        """[修改] 显式安插 👁️‍🗨️【全知视界】探针，手动闭环记忆处理，并拦截 [TERMINAL_YIELD] 硬中断指令，加入并发熔断防线"""
        chat_id = event.unified_msg_origin
        bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "SELF_BOT"
        
        # ==========================================
        # 🟢 [核心修复] 阶段 3：增强型并发状态检查
        # ==========================================
        import asyncio
        async with self._global_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = asyncio.Lock()
                self._chat_pending_count[chat_id] = 0
            
            # 如果当前该群组积压的待处理或正在处理的思考任务数 >= 2，直接丢弃新来的请求防雪崩
            if self._chat_pending_count[chat_id] >= 2:
                from astrbot.api import logger
                logger.warning(f"[{chat_id}] 🛑 并发熔断：当前群组排队思考任务过多 ({self._chat_pending_count[chat_id]})，已主动丢弃本次请求！")
                return
                
            self._chat_pending_count[chat_id] += 1
            
        chat_lock = self._chat_locks[chat_id]
        
        try:
            # 获取该 Chat ID 专属锁，实现严格的会话级串行执行
            async with chat_lock:
                models = self.gateway.get_agent_models()
                if not models:
                    from astrbot.api import logger
                    logger.error(f"[{chat_id}] Agent 模型未配置且无备用池，无法执行动作。")
                    return

                # ==========================================
                # 🟢 [核心修复] Agent Max Steps Fix
                # ==========================================
                is_fast_mode = event.get_extra("is_fast_mode", False)
                
                # [解除限制] 抹除 `1 if is_fast_mode else` 的硬编码，让快速模式享有同等调用工具的次数
                config_max_steps = getattr(self.config.agent, 'max_steps', 5)
                max_steps = max(5, config_max_steps) 
                
                timeout = 15 if is_fast_mode else self.config.agent.timeout
                
                # 🟢 显式打印全知视界探针
                if getattr(self.config.global_settings, 'debug_mode', True):
                    from astrbot.api import logger
                    task_type = "🧠 [System 2 / 极速主脑决策]" if is_fast_mode else "🧠 [System 2 / 主脑决策]"
                    logger.info(
                        f"\n{'='*70}\n"
                        f"👁️‍🗨️ 【全知视界】 准备发往大模型的 Payload 快照\n"
                        f"🎯 目标: {chat_id}\n"
                        f"🔖 链路归属: {task_type}\n"
                        f"⚙️ 当前分配最大思考步数: {max_steps}\n"
                        f"{'='*70}\n"
                        f"👇 【SYSTEM PROMPT (系统设定 & 剧本 & 记忆)】 👇\n"
                        f"{system_prompt}\n"
                        f"{'-'*70}\n"
                        f"👇 【USER PROMPT (当前消息/旁白)】 👇\n"
                        f"{prompt}\n"
                        f"{'='*70}"
                    )
                
                try:
                    event._is_final_reply_phase = True 

                    # ==========================================
                    # 🟢 [修复] 统一的上下文封装机制：区分大模型 Payload 与下发组件
                    # ==========================================
                    from astrbot.core.agent.message import SystemMessageSegment, UserMessageSegment, TextPart
                    try:
                        from astrbot.core.agent.message import ImagePart
                    except ImportError:
                        ImagePart = None
                    
                    contexts = []
                    api_prompt = prompt

                    # 检测是否捕获到了主脑直通车视觉特征 (直连图片 URLs)
                    if direct_vision_urls and len(direct_vision_urls) > 0:
                        user_content = []
                        
                        # 1. 组装提示词文本
                        if prompt:
                            user_content.append(TextPart(text=prompt))
                            # 当携带多模态内容时，将 prompt 置入 contexts 而非 api_prompt，以规避某些模型的强制 API 校验
                            api_prompt = None 
                            
                        # 2. 组装多模态视觉对象 (强制使用 LLM 专用的 ContentPart 对象)
                        for url in direct_vision_urls:
                            from astrbot.api import logger
                            logger.debug(f"[{chat_id}] 👁️ 正在为主脑注入大模型视觉神经元 (ImagePart): {url[:40]}...")
                            if ImagePart:
                                user_content.append(ImagePart(url=url))
                            else:
                                logger.warning(f"[{chat_id}] 当前 AstrBot 版本不支持 ImagePart，降级忽略视觉注入。")
                            
                        # 3. 将组装好的 ContentPart 列表打包装入 User 消息片
                        if user_content:
                            contexts.append(UserMessageSegment(content=user_content))
                            
                    # ==========================================
                    # 非 Agent 模式：纯文本 / 纯 VQA 模式
                    # ==========================================
                    if tools is None or len(tools) == 0:
                        if direct_vision_urls and len(direct_vision_urls) > 0:
                            from astrbot.api import logger
                            logger.info(f"[{chat_id}] 👁️ 触发主脑视觉直通车！开启多模态 VQA 模式...")
                        else:
                            from astrbot.api import logger
                            logger.debug(f"[{chat_id}] ⚡ 纯文本模式：降级为纯文本生成器，剥离 Agent 环境...")
                            
                        last_error = ""
                        
                        for provider_id in models:
                            try:
                                llm_resp = await self.context.llm_generate(
                                    chat_provider_id=provider_id,
                                    prompt=api_prompt,
                                    system_prompt=system_prompt, 
                                    contexts=contexts
                                )
                                reply_text = getattr(llm_resp, 'completion_text', "")
                                if not reply_text:
                                    raise ValueError(f"模型 {provider_id} 生成的回复文本为空")
                                
                                # [新增] 拦截透传底层错误
                                if "Exception:" in reply_text or "All chat models fail" in reply_text or "请求失败" in reply_text:
                                    raise RuntimeError(f"底层模型抛出异常穿透文本: {reply_text}")

                                await self.reply_engine.handle_reply(event, reply_text, chat_id)
                                
                                if hasattr(self.evolution_manager, 'process_bot_reply'):
                                    await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                                
                                try:
                                    plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
                                    if plugin and hasattr(plugin, 'memory_engine') and plugin.memory_engine.summarizer:
                                        await plugin.memory_engine.summarizer.pump_memory_reflection(chat_id, prompt, reply_text)
                                except Exception as mem_e:
                                    from astrbot.api import logger
                                    logger.debug(f"[{chat_id}] 手动闭环记忆处理失败: {mem_e}")
                                    
                                return 
                            except Exception as e:
                                last_error = str(e)
                                from astrbot.api import logger
                                logger.warning(f"[{chat_id}] ⚠️ 模型 {provider_id} 调用异常，尝试切换备用: {e}")
                                continue
                                
                        from astrbot.api import logger
                        logger.error(f"[{chat_id}] ❌ 模型池耗尽: {last_error}")
                        await self._handle_fatal_fallback(event, chat_id, f"模型全部耗尽:\n{last_error}")

                    # ==========================================
                    # Agent 工具循环模式
                    # ==========================================
                    else:
                        if direct_vision_urls and len(direct_vision_urls) > 0:
                            from astrbot.api import logger
                            logger.info(f"[{chat_id}] 👁️ 触发 Agent 多模态循环！图片载荷已通过 Data URI 注入上下文。")
                            
                        tool_set = ToolSet(tools)
                        last_error = "" 
                        for provider_id in models:
                            try:
                                # 🟢 [阶段 3 核心修复] 传入 contexts=[UserMessageSegment(...)]，支持多模态 Agent
                                llm_resp = await self.context.tool_loop_agent(
                                    event=event,
                                    chat_provider_id=provider_id,
                                    prompt=api_prompt,
                                    system_prompt=system_prompt,
                                    contexts=contexts, # 注入组装好的图片与文本
                                    tools=tool_set,
                                    max_steps=max_steps, 
                                    tool_call_timeout=timeout
                                )
                                reply_text = getattr(llm_resp, 'completion_text', "")
                                if not reply_text:
                                    raise ValueError(f"模型 {provider_id} 生成的回复为空")
                                
                                # [新增] 拦截透传底层错误
                                if "Exception:" in reply_text or "All chat models fail" in reply_text or "请求失败" in reply_text:
                                    raise RuntimeError(f"Agent底层由于额度耗尽抛出了异常穿透文本: {reply_text}")

                                if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                                    from astrbot.api import logger
                                    logger.info(f"[{chat_id}] 💤 Brain 决定挂起并倾听后续消息 (Wait/Listening)。")
                                    return

                                if "[TERMINAL_YIELD]:" in reply_text:
                                    idx = reply_text.find("[TERMINAL_YIELD]:")
                                    terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:"):].strip()
                                    from astrbot.api import logger
                                    logger.info(f"[{chat_id}] 🛑 触发硬中断 (TERMINAL_YIELD)，大模型被接管，纯文本下发: {terminal_content}")
                                    await self.reply_engine.handle_reply(event, terminal_content, chat_id)
                                    if hasattr(self.evolution_manager, 'process_bot_reply'):
                                        await self.evolution_manager.process_bot_reply(chat_id, bot_id, terminal_content)
                                    return

                                await self.reply_engine.handle_reply(event, reply_text, chat_id)
                                
                                if hasattr(self.evolution_manager, 'process_bot_reply'):
                                    await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)

                                try:
                                    plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
                                    if plugin and hasattr(plugin, 'memory_engine') and plugin.memory_engine.summarizer:
                                        await plugin.memory_engine.summarizer.pump_memory_reflection(chat_id, prompt, reply_text)
                                except Exception as mem_e:
                                    from astrbot.api import logger
                                    logger.debug(f"[{chat_id}] 手动闭环记忆处理失败: {mem_e}")
                                    
                                return 
                            except Exception as e:
                                last_error = str(e)
                                from astrbot.api import logger
                                logger.warning(f"[{chat_id}] ⚠️ Agent 模型 {provider_id} 调用异常，尝试切换备用: {e}")
                                continue
                        
                        from astrbot.api import logger
                        logger.error(f"[{chat_id}] ❌ 所有 Agent 模型均已耗尽，触发系统兜底回复。")
                        await self._handle_fatal_fallback(event, chat_id, last_error if last_error else "Agent模型池全部耗尽。")

                # [新增] 增加全局级的异常捕获，确保绝对的人设隔离
                except Exception as global_e:
                    from astrbot.api import logger
                    logger.error(f"[{chat_id}] 💥 Executor 遭遇未捕获的全局级崩溃: {global_e}")
                    await self._handle_fatal_fallback(event, chat_id, f"Executor 核心循环异常:\n{str(global_e)}")
                    
                finally:
                    if hasattr(event, '_is_final_reply_phase'):
                        delattr(event, '_is_final_reply_phase')

        finally:
            # ==========================================
            # 🟢 [核心修复] 执行结束，释放队列状态，保障后续通信顺畅
            # ==========================================
            async with self._global_lock:
                self._chat_pending_count[chat_id] -= 1
                if self._chat_pending_count[chat_id] == 0:
                    self._chat_locks.pop(chat_id, None)
                    self._chat_pending_count.pop(chat_id, None)


    async def _handle_fatal_fallback(self, event: AstrMessageEvent, chat_id: str, error_detail: str):
        """[新增] 处理致命崩溃，执行兜底回复与管理员私聊推送"""
        logger.error(f"[{chat_id}] ❌ 触发系统致命异常拦截，正在下发兜底回复。")
        fallback_msg = getattr(self.config.reply, 'fallback_text', "（陷入了短暂的沉默...）")
        await self.reply_engine.handle_reply(event, fallback_msg, chat_id)
        
        # 遍历管理员列表进行静默推送
        config_global = getattr(self.config, 'global_settings', None)
        if config_global and getattr(config_global, 'enable_error_interception', True):
            admin_ids = getattr(config_global, 'admin_ids', [])
            if not admin_ids:
                return
            
            for admin_id in admin_ids:
                try:
                    # 获取当前平台标识，组装跨界私聊 UMO
                    platform_id = event.unified_msg_origin.split(":")[0] 
                    admin_umo = f"{platform_id}:FriendMessage:{admin_id}"
                    
                    from astrbot.api.event import MessageChain
                    import astrbot.api.message_components as Comp
                    
                    error_report = f"🚨 [AstrMai 异常拦截报告]\n📍 目标: {event.unified_msg_origin}\n⚠️ 错误详情:\n{error_detail}"
                    chain = MessageChain().message(error_report)
                    await self.context.send_message(admin_umo, chain)
                    logger.debug(f"[Executor] 成功向管理员 {admin_id} 推送报错日志。")
                except Exception as e:
                    logger.error(f"[Executor] 尝试向管理员 {admin_id} 推送报错时失败: {e}")
