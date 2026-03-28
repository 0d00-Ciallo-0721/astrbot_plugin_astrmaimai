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
        
        
# [修改] 在执行成功的两个分支内，调用 evolution_manager.process_bot_reply 闭环反馈
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
                logger.warning(f"[{chat_id}] 🛑 并发熔断：当前群组排队思考任务过多 ({self._chat_pending_count[chat_id]})，已主动丢弃本次请求！")
                return
                
            self._chat_pending_count[chat_id] += 1
            
        chat_lock = self._chat_locks[chat_id]
        
        try:
            # 获取该 Chat ID 专属锁，实现严格的会话级串行执行
            async with chat_lock:
                models = self.gateway.get_agent_models()
                if not models:
                    logger.error(f"[{chat_id}] Agent 模型未配置且无备用池，无法执行动作。")
                    return

                # ==========================================
                # 🟢 [核心修复 Bug 3] Agent Max Steps Fix
                # ==========================================
                is_fast_mode = event.get_extra("is_fast_mode", False)
                # 极速模式因为必须直接回答，强制 1 步；普通模式强制读取 config 中的 max_steps (至少为 5)
                config_max_steps = getattr(self.config.agent, 'max_steps', 5)
                max_steps = 1 if is_fast_mode else max(5, config_max_steps) 
                
                timeout = 15 if is_fast_mode else self.config.agent.timeout
                
                # 🟢 显式打印全知视界探针
                if getattr(self.config.global_settings, 'debug_mode', True):
                    task_type = "🧠 [System 2 / 主脑决策]"
                    logger.info(
                        f"\n{'='*70}\n"
                        f"👁️‍🗨️ 【全知视界】 准备发往大模型的 Payload 快照\n"
                        f"🎯 目标: {chat_id}\n"
                        f"🔖 链路归属: {task_type}\n"
                        f"⚙️ 当前分配最大思考步数: {max_steps}\n" # 加入打印方便排查
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
                    # 阶段四：多模态对象组装 (主脑视觉直通车)
                    # ==========================================
                    if direct_vision_urls and len(direct_vision_urls) > 0:
                        logger.info(f"[{chat_id}] 👁️ 触发主脑视觉直通车！绕过 ToolLoop 工具链，开启多模态 VQA 模式...")
                        import os, tempfile, base64
                        from astrbot.core.agent.message import SystemMessageSegment, UserMessageSegment, TextPart
                        try:
                            from astrbot.core.agent.message import ImagePart
                        except ImportError:
                            ImagePart = None

                        temp_files_to_clean = []
                        processed_image_urls = []
                        last_error = ""

                        try:
                            for url in direct_vision_urls:
                                if url.startswith("data:image"):
                                    try:
                                        header, encoded = url.split(",", 1)
                                        ext = header.split(";")[0].split("/")[1]
                                        if ext == "jpeg": ext = "jpg"
                                        img_bytes = base64.b64decode(encoded)

                                        fd, temp_path = tempfile.mkstemp(suffix=f".{ext}")
                                        with os.fdopen(fd, 'wb') as f:
                                            f.write(img_bytes)

                                        processed_image_urls.append(temp_path)
                                        temp_files_to_clean.append(temp_path)
                                    except Exception as e:
                                        logger.error(f"[{chat_id}] 视觉直通车提取 Base64 失败: {e}")
                                        processed_image_urls.append(url)
                                else:
                                    processed_image_urls.append(url)

                            # [修复 Bug 2]：移除 SystemMessageSegment 的字典包装
                            contexts = []
                            user_content = []
                            
                            if prompt:
                                user_content.append(TextPart(text=prompt))
                                
                            if ImagePart:
                                for path_or_url in processed_image_urls:
                                    if os.path.exists(path_or_url):
                                        user_content.append(ImagePart(file=path_or_url))
                                    else:
                                        user_content.append(ImagePart(url=path_or_url))
                            else:
                                logger.warning(f"[{chat_id}] 当前 AstrBot 版本不支持 ImagePart，视觉直通车可能失效。")
                                
                            contexts.append(UserMessageSegment(content=user_content))
                            
                            for provider_id in models:
                                try:
                                    llm_resp = await self.context.llm_generate(
                                        chat_provider_id=provider_id,
                                        prompt=None,
                                        system_prompt=system_prompt, # [修复 Bug 2]：作为原生形参直传
                                        contexts=contexts
                                    )
                                    reply_text = getattr(llm_resp, 'completion_text', "")
                                    if not reply_text:
                                        raise ValueError(f"多模态模型 {provider_id} 生成的回复为空")
                                    
                                    # [新增] 拦截透传底层错误
                                    if "Exception:" in reply_text or "All chat models fail" in reply_text or "请求失败" in reply_text:
                                        raise RuntimeError(f"底层视觉模型抛出异常穿透文本: {reply_text}")

                                    await self.reply_engine.handle_reply(event, reply_text, chat_id)
                                    
                                    if hasattr(self.evolution_manager, 'process_bot_reply'):
                                        await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                                        
                                    try:
                                        plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
                                        if plugin and hasattr(plugin, 'memory_engine') and plugin.memory_engine.summarizer:
                                            await plugin.memory_engine.summarizer.pump_memory_reflection(chat_id, prompt, reply_text)
                                    except Exception as mem_e:
                                        logger.debug(f"[{chat_id}] 手动闭环记忆处理失败: {mem_e}")
                                        
                                    return
                                except Exception as e:
                                    last_error = str(e)
                                    logger.warning(f"[{chat_id}] ⚠️ 多模态模型 {provider_id} 调用异常，尝试切换备用: {e}")
                                    continue
                                    
                            logger.error(f"[{chat_id}] ❌ 多模态模型池耗尽: {last_error}")
                            await self._handle_fatal_fallback(event, chat_id, f"多模态模型崩溃:\n{last_error}")
                            return 
                            
                        finally:
                            for temp_path in temp_files_to_clean:
                                if os.path.exists(temp_path):
                                    try:
                                        os.remove(temp_path)
                                    except Exception as e:
                                        logger.error(f"[{chat_id}] 无法删除临时视觉文件 {temp_path}: {e}")

                    # ==========================================
                    # 纯文本模式 / 降级生成
                    # ==========================================
                    elif tools is None or len(tools) == 0:
                        logger.debug(f"[{chat_id}] ⚡ 纯文本模式：降级为纯文本生成器，剥离 Agent 环境...")
                        from astrbot.core.agent.message import SystemMessageSegment, TextPart
                        
                        # [修复 Bug 2]：移除 SystemMessageSegment 的字典包装
                        contexts = []
                        last_error = ""
                        
                        for provider_id in models:
                            try:
                                llm_resp = await self.context.llm_generate(
                                    chat_provider_id=provider_id,
                                    prompt=prompt,
                                    system_prompt=system_prompt, # [修复 Bug 2]：作为原生形参直传
                                    contexts=contexts
                                )
                                reply_text = getattr(llm_resp, 'completion_text', "")
                                if not reply_text:
                                    raise ValueError(f"模型 {provider_id} 生成的回复文本为空")
                                
                                # [新增] 拦截透传底层错误
                                if "Exception:" in reply_text or "All chat models fail" in reply_text or "请求失败" in reply_text:
                                    raise RuntimeError(f"纯文本大模型抛出异常穿透文本: {reply_text}")

                                await self.reply_engine.handle_reply(event, reply_text, chat_id)
                                
                                if hasattr(self.evolution_manager, 'process_bot_reply'):
                                    await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                                
                                try:
                                    plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
                                    if plugin and hasattr(plugin, 'memory_engine') and plugin.memory_engine.summarizer:
                                        await plugin.memory_engine.summarizer.pump_memory_reflection(chat_id, prompt, reply_text)
                                except Exception as mem_e:
                                    logger.debug(f"[{chat_id}] 手动闭环记忆处理失败: {mem_e}")
                                    
                                return 
                            except Exception as e:
                                last_error = str(e)
                                logger.warning(f"[{chat_id}] ⚠️ 纯文本模型 {provider_id} 调用异常，尝试切换备用: {e}")
                                continue
                                
                        logger.error(f"[{chat_id}] ❌ 模型池耗尽: {last_error}")
                        await self._handle_fatal_fallback(event, chat_id, f"纯文本模型耗尽:\n{last_error}")

                    # ==========================================
                    # Agent 工具循环模式
                    # ==========================================
                    else:
                        tool_set = ToolSet(tools)
                        last_error = "" 
                        for provider_id in models:
                            try:
                                # 🟢 [核心修复 Bug 3] Agent Max Steps Fix
                                # 在传递给底层 tool_loop_agent 之前，明确传递解除了限制的 max_steps，打破只能调用一次就被强制终止的死锁
                                llm_resp = await self.context.tool_loop_agent(
                                    event=event,
                                    chat_provider_id=provider_id,
                                    prompt=prompt,
                                    system_prompt=system_prompt,
                                    tools=tool_set,
                                    max_steps=max_steps, 
                                    tool_call_timeout=timeout
                                )
                                reply_text = getattr(llm_resp, 'completion_text', "")
                                if not reply_text:
                                    raise ValueError(f"模型 {provider_id} 生成的回复为空")
                                
                                # [新增] 拦截透传底层错误 (这是导致之前日志里死信污染记忆的核心源头)
                                if "Exception:" in reply_text or "All chat models fail" in reply_text or "请求失败" in reply_text:
                                    raise RuntimeError(f"Agent底层由于额度耗尽抛出了异常穿透文本: {reply_text}")

                                if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                                    logger.info(f"[{chat_id}] 💤 Brain 决定挂起并倾听后续消息 (Wait/Listening)。")
                                    return

                                if "[TERMINAL_YIELD]:" in reply_text:
                                    idx = reply_text.find("[TERMINAL_YIELD]:")
                                    terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:"):].strip()
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
                                    logger.debug(f"[{chat_id}] 手动闭环记忆处理失败: {mem_e}")
                                    
                                return 
                            except Exception as e:
                                last_error = str(e)
                                logger.warning(f"[{chat_id}] ⚠️ Agent 模型 {provider_id} 调用异常，尝试切换备用: {e}")
                                continue
                        
                        logger.error(f"[{chat_id}] ❌ 所有 Agent 模型均已耗尽，触发系统兜底回复。")
                        await self._handle_fatal_fallback(event, chat_id, last_error if last_error else "Agent模型池全部耗尽。")

                # [新增] 增加全局级的异常捕获，确保绝对的人设隔离
                except Exception as global_e:
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
