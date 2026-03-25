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
        
        
    # [修改] 在执行成功的两个分支内，调用 evolution_manager.process_bot_reply 闭环反馈
    async def execute(self, event: AstrMessageEvent, prompt: str, system_prompt: str, tools: List[Any] = None, direct_vision_urls: List[str] = None):
        """[修改] 显式安插 👁️‍🗨️【全知视界】探针，手动闭环记忆处理，并拦截 [TERMINAL_YIELD] 硬中断指令"""
        chat_id = event.unified_msg_origin
        bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else "SELF_BOT"
        
        models = self.gateway.get_agent_models()
        if not models:
            logger.error(f"[{chat_id}] Agent 模型未配置且无备用池，无法执行动作。")
            return

        is_fast_mode = event.get_extra("is_fast_mode", False)
        max_steps = 1 if is_fast_mode else self.config.agent.max_steps
        timeout = 15 if is_fast_mode else self.config.agent.timeout
        
        # 🟢 显式打印全知视界探针
        if getattr(self.config.global_settings, 'debug_mode', True):
            task_type = "🧠 [System 2 / 主脑决策]"
            logger.info(
                f"\n{'='*70}\n"
                f"👁️‍🗨️ 【全知视界】 准备发往大模型的 Payload 快照\n"
                f"🎯 目标: {chat_id}\n"
                f"🔖 链路归属: {task_type}\n"
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
            # [新增] 阶段四：多模态对象组装 (主脑视觉直通车)
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
                    # 1. 处理临时文件落盘
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

                    # 2. 组装多模态 Contexts
                    contexts = [SystemMessageSegment(content=[TextPart(text=system_prompt)])]
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
                    
                    # 3. 发起调用
                    for provider_id in models:
                        try:
                            # 绕过 tool_loop_agent，直接发起 llm_generate
                            llm_resp = await self.context.llm_generate(
                                chat_provider_id=provider_id,
                                prompt=None, # Prompt 已整体打包入 contexts 中
                                contexts=contexts
                            )
                            reply_text = getattr(llm_resp, 'completion_text', "")
                            if not reply_text:
                                raise ValueError(f"多模态模型 {provider_id} 生成的回复为空")
                                
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
                    return # 视觉直通车执行完毕后直接返回，不要再走纯文本的 fallback
                    
                finally:
                    # 4. 安全销毁临时文件
                    for temp_path in temp_files_to_clean:
                        if os.path.exists(temp_path):
                            try:
                                os.remove(temp_path)
                            except Exception as e:
                                logger.error(f"[{chat_id}] 无法删除临时视觉文件 {temp_path}: {e}")

            # ==========================================
            # 以下为原有逻辑，仅调整了缩进与 if/elif 关系
            # ==========================================
            elif tools is None or len(tools) == 0:
                logger.debug(f"[{chat_id}] ⚡ 纯文本模式：降级为纯文本生成器，剥离 Agent 环境...")
                from astrbot.core.agent.message import SystemMessageSegment, TextPart
                contexts = [SystemMessageSegment(content=[TextPart(text=system_prompt)])]
                last_error = ""
                
                for provider_id in models:
                    try:
                        llm_resp = await self.context.llm_generate(
                            chat_provider_id=provider_id,
                            prompt=prompt,
                            contexts=contexts
                        )
                        reply_text = getattr(llm_resp, 'completion_text', "")
                        if not reply_text:
                            raise ValueError(f"模型 {provider_id} 生成的回复文本为空")
                            
                        await self.reply_engine.handle_reply(event, reply_text, chat_id)
                        
                        if hasattr(self.evolution_manager, 'process_bot_reply'):
                            await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
                        
                        # 🟢 手动闭环记忆处理
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
            else:
                tool_set = ToolSet(tools)
                for provider_id in models:
                    try:
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

                        if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                            logger.info(f"[{chat_id}] 💤 Brain 决定挂起并倾听后续消息 (Wait/Listening)。")
                            return

                        # 🟢 拦截 [TERMINAL_YIELD] 硬中断
                        if "[TERMINAL_YIELD]:" in reply_text:
                            idx = reply_text.find("[TERMINAL_YIELD]:")
                            terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:"):].strip()
                            logger.info(f"[{chat_id}] 🛑 触发硬中断 (TERMINAL_YIELD)，大模型被接管，纯文本下发: {terminal_content}")
                            
                            # 将终端内容通过 reply_engine 发送，维持拟人化延迟和可能的情绪流转
                            await self.reply_engine.handle_reply(event, terminal_content, chat_id)
                            
                            if hasattr(self.evolution_manager, 'process_bot_reply'):
                                await self.evolution_manager.process_bot_reply(chat_id, bot_id, terminal_content)
                            return

                        await self.reply_engine.handle_reply(event, reply_text, chat_id)
                        
                        if hasattr(self.evolution_manager, 'process_bot_reply'):
                            await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)

                        # 🟢 手动闭环记忆处理
                        try:
                            plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
                            if plugin and hasattr(plugin, 'memory_engine') and plugin.memory_engine.summarizer:
                                await plugin.memory_engine.summarizer.pump_memory_reflection(chat_id, prompt, reply_text)
                        except Exception as mem_e:
                            logger.debug(f"[{chat_id}] 手动闭环记忆处理失败: {mem_e}")
                            
                        return 
                    except Exception as e:
                        logger.warning(f"[{chat_id}] ⚠️ Agent 模型 {provider_id} 调用异常，尝试切换备用: {e}")
                        continue
        finally:
            if hasattr(event, '_is_final_reply_phase'):
                delattr(event, '_is_final_reply_phase')