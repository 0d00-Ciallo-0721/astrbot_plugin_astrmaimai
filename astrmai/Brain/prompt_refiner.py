import re
import html
import json  # [新增] 用于处理被序列化为字符串的历史记录
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger

class PromptRefiner:
    """
    第三阶段：剧场模式与潜意识注入中心 (Phase 3: Theater & Subconscious Injection)
    职责：
    1. 底层记忆回溯：使用锚点截取 AstrBot 原生历史，彻底扁平化为纯文本剧本。
    2. 工具链安全：保护原生 tool_calls 结构不被破坏，同时清空历史数组污染。
    3. 剧场坍缩：将上下文折叠为 System Prompt 里的自然语言，打破 AI 思想钢印。
    """
# 文件位置: astrmai/Brain/prompt_refiner.py
# 标注: 修改 __init__ 和 refine_prompt

    def __init__(self, memory_engine, db_service=None, config=None, react_retriever=None):
        self.memory_engine = memory_engine
        self.db_service = db_service
        self.config = config
        self.react_retriever = react_retriever  # Phase 2: ReAct Agent 记忆检索器

    async def refine_prompt(self, event: AstrMessageEvent, system_prompt: str, prompt: str, context) -> tuple[str, str]:
        # 1. 状态校验与污染清理
        disable_rag = False
        if hasattr(context, "get"):
            disable_rag = context.get("disable_rag_injection")
        elif hasattr(context, "shared_dict"):
            disable_rag = context.shared_dict.get("disable_rag_injection", False)

        chat_id = event.unified_msg_origin
        
        # 读取极速模式信标
        retrieve_keys = event.get_extra("retrieve_keys", [])
        is_fast_mode = "CORE_ONLY" in retrieve_keys

        # 2. 潜意识召回 (RAG)
        injection = ""
        current_query = event.message_str
        import html
        if not disable_rag and current_query and not is_fast_mode:
            # Phase 2: 优先使用 ReAct Agent 检索（如可用且没有被配置关闭），否则回退到单次 recall
            react_result = ""
            enable_react = True
            if self.config and hasattr(self.config, 'memory'):
                enable_react = self.config.memory.enable_react_agent
                
            if self.react_retriever and enable_react:
                try:
                    react_result = await self.react_retriever.retrieve(
                        query=current_query,
                        chat_id=chat_id,
                        chat_context=prompt,
                        sender_name=event.get_sender_name() or ""
                    )
                except Exception as e:
                    logger.debug(f"[PromptRefiner] ReAct 检索异常，回退到单次 recall: {e}")

            if react_result:
                safe_text = html.escape(react_result)
                injection = f"<injected_memory>\n{safe_text}\n</injected_memory>\n"
            elif self.memory_engine:
                memory_text = await self.memory_engine.recall(current_query, session_id=chat_id)
                if memory_text and "什么也没想起来" not in memory_text:
                    safe_memory_text = html.escape(memory_text)
                    injection = f"<injected_memory>\n记忆涌现（记忆模块）：基于当前话题，你回忆起了以下事情：\n{safe_memory_text}\n</injected_memory>\n"

        # 3. 底层历史拉取与剧本化格式转换
        history_script = "无近期历史记录。"
        anchor_event = event.get_extra("astrmai_anchor_event")
        anchor_text = anchor_event.message_str.strip() if anchor_event else None
        
        conv_mgr = context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)
        conversation = await conv_mgr.get_conversation(chat_id, curr_cid)
        
        history_lines = []
        if conversation and hasattr(conversation, "history") and conversation.history:
            import json
            raw_history_data = conversation.history
            if isinstance(raw_history_data, str):
                try:
                    raw_history_data = json.loads(raw_history_data)
                except json.JSONDecodeError:
                    from astrbot.api import logger
                    logger.error("[PromptRefiner] 🚨 底层历史记录 JSON 反序列化失败！已清空防污染。")
                    raw_history_data = []
            
            if not isinstance(raw_history_data, list):
                raw_history_data = list(raw_history_data)
                
            raw_history = raw_history_data
            cutoff_idx = len(raw_history)
            
            import re
            def _parse_msg_data(raw_data):
                role, text = "", ""
                if isinstance(raw_data, str):
                    try:
                        raw_data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        return "", raw_data
                
                if isinstance(raw_data, dict):
                    role = raw_data.get("role", "")
                    c = raw_data.get("content", "")
                    if isinstance(c, str):
                        text = c
                    elif isinstance(c, list):
                        text = "".join([p.get("text", "") for p in c if isinstance(p, dict) and "text" in p])
                elif hasattr(raw_data, 'content'): 
                    role = getattr(raw_data, "role", "")
                    c = getattr(raw_data, "content", "")
                    if isinstance(c, str):
                        text = c
                    elif isinstance(c, list):
                        text = "".join([getattr(p, "text", "") for p in c if hasattr(p, "text")])
                return role, text

            if anchor_text:
                clean_anchor = re.sub(r'\s+', '', anchor_text)
                for i in range(len(raw_history) - 1, -1, -1):
                    _, content = _parse_msg_data(raw_history[i])
                    clean_content = re.sub(r'\s+', '', content) if content else ""
                    if clean_anchor and clean_anchor in clean_content:
                        cutoff_idx = i
                        break
                        
            fetch_count = getattr(self.config.attention, 'bg_pool_size', 20) if self.config else 20
            start_idx = max(0, cutoff_idx - fetch_count)
            valid_history = raw_history[start_idx:cutoff_idx]
            
            for msg_data in valid_history:
                role, content = _parse_msg_data(msg_data)
                if not content: 
                    continue
                    
                if role == "user":
                    match = re.match(r"^(.*?):\s*(.*)$", content, re.DOTALL)
                    if match:
                        sender, text = match.groups()
                        history_lines.append(f"[{sender}] 说: {text}")
                    else:
                        history_lines.append(f"[群友/用户] 说: {content}")
                elif role == "assistant":
                    history_lines.append(f"[我] 说: {content}")
                else:
                    history_lines.append(content)

        if history_lines:
            history_script = "\n".join(history_lines)
            if is_fast_mode:
                history_script = f"（极速唤醒：已同步最近 {len(history_lines)} 条群聊剧本）\n" + history_script

        async def _resolve_visual_memory(text: str) -> str:
            if not isinstance(text, str): return text
            import re
            picids = set(re.findall(r'\[picid:([a-fA-F0-9]{32})\]', text))
            if not picids: return text
            
            # 🟢 [核心修复 Bug 2] 直接使用类实例挂载的 db_service
            for picid in picids:
                resolved_text = "[一张尚未看清的图片]"
                if self.db_service:
                    for _ in range(15): 
                        with self.db_service.get_session() as session:
                            from ..infra.datamodels import VisualMemory
                            import json
                            mem = session.get(VisualMemory, picid)
                            if mem:
                                try:
                                    tags = json.loads(mem.emotion_tags)
                                    tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                                except Exception:
                                    tags_str = ""
                                if mem.type == "emoji":
                                    resolved_text = f"[发了一个表情包，画面是：{mem.description}，传达了：{tags_str}]" if tags_str else f"[发了一张图片，画面是：{mem.description}]"
                                break
                        import asyncio
                        await asyncio.sleep(1.0)
                else:
                    from astrbot.api import logger
                    logger.warning(f"[PromptRefiner] ⚠️ db_service 未挂载，无法解析图片 ID: {picid}")
                    
                text = text.replace(f"[picid:{picid}]", resolved_text)
            return text

        import re
        final_system_prompt = system_prompt
        final_system_prompt = re.sub(r'<CHAT_HISTORY>|\{HISTORY_PLACEHOLDER\}', f"群聊历史消息：\n{history_script}", final_system_prompt)
        
        # 🟢 [核心瘦身] 连同 "当前你看到的消息：" 这个标题一起，把 System Prompt 里的冗余消息占位符彻底抹除
        final_system_prompt = re.sub(r'当前你看到的消息：\s*(<CURRENT_MESSAGES>|\{CURRENT_MSG_PLACEHOLDER\})', '', final_system_prompt)
        final_system_prompt = re.sub(r'<CURRENT_MESSAGES>|\{CURRENT_MSG_PLACEHOLDER\}', '', final_system_prompt)
        
        final_system_prompt = re.sub(r'<RAG_MEMORY>|\{MEMORY_PLACEHOLDER\}', injection, final_system_prompt)
        
        final_system_prompt = await _resolve_visual_memory(final_system_prompt)
        
        original_content = prompt
        original_content = await _resolve_visual_memory(original_content)
            
        if is_fast_mode:
            director_voice = "【极速唤醒】虽然参考了前面的剧本，但请保持回复极其简短、直接，不要带任何角色名前缀！"
        else:
            director_voice = "【动作提示】请先判断是否需要调用工具。如果不需要，请直接沉浸在角色中说出你的台词，不要带任何角色名前缀！"
            
        # 🟢 [精简 User Prompt] 保持 User Prompt 为唯一携带最新消息的地方
        final_prompt = f"(导演旁白：请仔细阅读设定和前面的剧本。这是当前你看到的最新消息：\n{original_content}\n\n>> {director_voice})"

        from astrbot.api import logger
        logger.info(f"[{chat_id}] 🎬 剧本坍缩完成（模式: {'极速' if is_fast_mode else '标准'}）。已成功解析 {len(history_lines)} 条底层历史数据。")
        
        return final_system_prompt, final_prompt