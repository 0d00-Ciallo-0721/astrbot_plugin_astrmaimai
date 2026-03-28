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
        """[修改] 融合视觉皮层成功逻辑的 VLM 同步降维转述模式，并加入强力的底层 API 崩溃嗅探以激活模型池"""
        chat_id = event.unified_msg_origin
        bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "SELF_BOT"
        
        import asyncio
        async with self._global_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = asyncio.Lock()
                self._chat_pending_count[chat_id] = 0
            
            if self._chat_pending_count[chat_id] >= 2:
                from astrbot.api import logger
                logger.warning(f"[{chat_id}] 🛑 并发熔断：当前群组排队思考任务过多 ({self._chat_pending_count[chat_id]})，已主动丢弃。")
                return
                
            self._chat_pending_count[chat_id] += 1
            
        chat_lock = self._chat_locks[chat_id]
        
        try:
            async with chat_lock:
                models = self.gateway.get_agent_models()
                if not models:
                    from astrbot.api import logger
                    logger.error(f"[{chat_id}] Agent 模型未配置且无备用池，无法执行动作。")
                    return

                is_fast_mode = event.get_extra("is_fast_mode", False)
                config_max_steps = getattr(self.config.agent, 'max_steps', 5)
                max_steps = max(5, config_max_steps) 
                timeout = 15 if is_fast_mode else self.config.agent.timeout
                
                if getattr(self.config.global_settings, 'debug_mode', True):
                    from astrbot.api import logger
                    logger.info(f"\n{'='*70}\n👁️‍🗨️ 【全知视界】 Payload 快照 | 目标: {chat_id}\n{'='*70}")
                
                try:
                    event._is_final_reply_phase = True 
                    
                    # ==========================================
                    # 🟢 复刻 VisualCortex 成功逻辑的同步降维转述
                    # ==========================================
                    import aiohttp
                    import tempfile
                    import os
                    import io
                    from PIL import Image
                    from astrbot.api import logger
                    
                    contexts = []
                    api_prompt = prompt
                    vision_descriptions = []

                    if direct_vision_urls and len(direct_vision_urls) > 0:
                        logger.info(f"[{chat_id}] 👁️ 触发主脑视觉直通车 (执行物理落盘与 VLM 转述)...")
                        for url_or_path in direct_vision_urls:
                            temp_file_path = None
                            is_temp = False
                            try:
                                image_bytes = None
                                if str(url_or_path).startswith("http"):
                                    async with aiohttp.ClientSession() as session:
                                        async with session.get(url_or_path, timeout=15) as resp:
                                            if resp.status == 200:
                                                image_bytes = await resp.read()
                                elif str(url_or_path).startswith("data:image"):
                                    import base64
                                    _, encoded = str(url_or_path).split(",", 1)
                                    image_bytes = base64.b64decode(encoded)
                                elif os.path.exists(url_or_path):
                                    temp_file_path = url_or_path
                                
                                if image_bytes:
                                    try:
                                        img_format = Image.open(io.BytesIO(image_bytes)).format.lower()
                                    except Exception:
                                        img_format = "jpeg"
                                        
                                    fd, temp_file_path = tempfile.mkstemp(suffix=f".{img_format}")
                                    with os.fdopen(fd, 'wb') as f:
                                        f.write(image_bytes)
                                    is_temp = True

                                if temp_file_path and os.path.exists(temp_file_path):
                                    logger.debug(f"[{chat_id}] 🚀 正在调用 Gateway 同步解析: {temp_file_path}")
                                    system_prompt_vision = (
                                        "你是一个群聊视觉分析助手。请分析图片内容，并严格使用且仅使用以下 JSON 格式输出结果，不要输出任何 Markdown 标记：\n"
                                        '{ "type": "image" 或 "emoji", "description": "详细的画面内容描述", "emotion_tags": ["情绪1", "情绪2"] }'
                                    )
                                    
                                    result_dict = await self.gateway.call_vision_task(
                                        image_data=temp_file_path,
                                        prompt="请详细分析这幅图片或表情包的内容。",
                                        system_prompt=system_prompt_vision
                                    )
                                    
                                    if result_dict:
                                        desc = result_dict.get("description", "无法识别内容的图片")
                                        tags = result_dict.get("emotion_tags", [])
                                        tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                                        vision_descriptions.append(f"【视觉画面】{desc} | 【蕴含情绪】[{tags_str}]")
                                        logger.info(f"[{chat_id}] ✅ 同步转述成功: {desc[:20]}...")

                            except Exception as e:
                                logger.error(f"[{chat_id}] ⚠️ 视觉旁路转述失败: {e}")
                            finally:
                                if is_temp and temp_file_path and os.path.exists(temp_file_path):
                                    try:
                                        os.remove(temp_file_path)
                                    except Exception:
                                        pass
                        
                        if vision_descriptions:
                            vision_inject = "\n\n(系统旁白：用户刚刚发送了图片，以下是你的视觉神经元解析出的画面剧本：\n" + "\n".join(vision_descriptions) + ")"
                            api_prompt += vision_inject
                            prompt += vision_inject 

                    # 统一的异常拦截嗅探字典
                    error_keywords = ['请求失败', '错误类型', '错误信息', '调用失败', '处理失败', '获取模型列表失败', 'api error', 'all chat models fail', 'connection error', 'notfounderror', 'exception:']

                    # ==========================================
                    # 非 Agent 模式：纯文本 / 纯 VQA 模式
                    # ==========================================
                    if tools is None or len(tools) == 0:
                        logger.debug(f"[{chat_id}] ⚡ 纯文本模式运行...")
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
                                    raise ValueError(f"模型 {provider_id} 生成为空")
                                
                                # 🟢 异常拦截屏障：如果大模型透传了 API 报错，主动引爆以触发备用模型池
                                if any(kw in reply_text.lower() for kw in error_keywords):
                                    raise RuntimeError(f"底层模型穿透异常: {reply_text}")

                                await self.reply_engine.handle_reply(event, reply_text, chat_id)
                                
                                if hasattr(self.evolution_manager, 'process_bot_reply'):
                                    await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                                return 
                            except Exception as e:
                                last_error = str(e)
                                logger.warning(f"[{chat_id}] ⚠️ 模型 {provider_id} 异常，正在切换备用模型: {e}")
                                continue
                                
                        logger.error(f"[{chat_id}] ❌ 模型池耗尽: {last_error}")
                        await self._handle_fatal_fallback(event, chat_id, f"模型全部耗尽:\n{last_error}")

                    # ==========================================
                    # Agent 工具循环模式
                    # ==========================================
                    else:
                        logger.debug(f"[{chat_id}] 👁️ 触发 Agent 工具循环运行...")
                        tool_set = ToolSet(tools)
                        last_error = "" 
                        for provider_id in models:
                            try:
                                llm_resp = await self.context.tool_loop_agent(
                                    event=event,
                                    chat_provider_id=provider_id,
                                    prompt=api_prompt,
                                    system_prompt=system_prompt,
                                    contexts=contexts, 
                                    tools=tool_set,
                                    max_steps=max_steps, 
                                    tool_call_timeout=timeout
                                )
                                reply_text = getattr(llm_resp, 'completion_text', "")
                                if not reply_text:
                                    raise ValueError("回复为空")
                                
                                # 🟢 异常拦截屏障：如果大模型透传了 API 报错，主动引爆以触发备用模型池
                                if any(kw in reply_text.lower() for kw in error_keywords):
                                    raise RuntimeError(f"底层模型穿透异常: {reply_text}")

                                if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                                    return

                                if "[TERMINAL_YIELD]:" in reply_text:
                                    idx = reply_text.find("[TERMINAL_YIELD]:")
                                    terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:"):].strip()
                                    await self.reply_engine.handle_reply(event, terminal_content, chat_id)
                                    if hasattr(self.evolution_manager, 'process_bot_reply'):
                                        await self.evolution_manager.process_bot_reply(chat_id, bot_id, terminal_content)
                                    return

                                await self.reply_engine.handle_reply(event, reply_text, chat_id)
                                
                                if hasattr(self.evolution_manager, 'process_bot_reply'):
                                    await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                                return 
                            except Exception as e:
                                last_error = str(e)
                                logger.warning(f"[{chat_id}] ⚠️ Agent {provider_id} 异常，正在切换备用模型: {e}")
                                continue
                        
                        logger.error(f"[{chat_id}] ❌ 所有 Agent 模型均已耗尽。")
                        await self._handle_fatal_fallback(event, chat_id, last_error if last_error else "Agent 模型池耗尽。")

                except Exception as global_e:
                    from astrbot.api import logger
                    logger.error(f"[{chat_id}] 💥 Executor 核心崩溃: {global_e}")
                    await self._handle_fatal_fallback(event, chat_id, f"核心循环异常:\n{str(global_e)}")
                    
                finally:
                    if hasattr(event, '_is_final_reply_phase'):
                        delattr(event, '_is_final_reply_phase')

        finally:
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
