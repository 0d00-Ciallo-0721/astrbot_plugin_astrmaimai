import re
import html
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
    def __init__(self, memory_engine, config=None):
        self.memory_engine = memory_engine
        self.config = config

    async def refine_prompt(self, event: AstrMessageEvent, req: ProviderRequest, context) -> None:
        # ==========================================
        # 1. 状态校验与污染清理
        # ==========================================
        disable_rag = False
        if hasattr(context, "get"):
            disable_rag = context.get("disable_rag_injection")
        elif hasattr(context, "shared_dict"):
            disable_rag = context.shared_dict.get("disable_rag_injection", False)
            
        if disable_rag:
            return

        for msg in req.system_message + req.messages:
            if isinstance(msg.content, str):
                msg.content = re.sub(r"<injected_memory>.*?</injected_memory>\n?", "", msg.content, flags=re.DOTALL)

        chat_id = event.unified_msg_origin
        
        # 读取极速模式信标
        retrieve_keys = event.get_extra("retrieve_keys", [])
        is_fast_mode = "CORE_ONLY" in retrieve_keys

        # ==========================================
        # 2. 潜意识召回 (RAG)
        # ==========================================
        injection = ""
        current_query = event.message_str
        # [修改] 极速穿透模式下强制短路 RAG 向量检索（保持原本的性能优化点，不查外部知识库）
        if self.memory_engine and current_query and not is_fast_mode:
            memory_text = await self.memory_engine.recall(current_query, session_id=chat_id)
            if memory_text and "什么也没想起来" not in memory_text:
                safe_memory_text = html.escape(memory_text)
                injection = f"<injected_memory>\n记忆涌现（记忆模块）：基于当前话题，你回忆起了以下事情：\n{safe_memory_text}\n</injected_memory>\n"

        # ==========================================
        # 3. 底层历史拉取与剧本化格式转换
        # ==========================================
        history_script = "无近期历史记录。"
        
        # [核心改造] 解除极速模式的历史拦截。
        # 即使是极速唤醒模式，也允许拉取完整历史，以防止回复牛头不对马嘴。
        anchor_event = event.get_extra("astrmai_anchor_event")
        anchor_text = anchor_event.message_str.strip() if anchor_event else None
        
        conv_mgr = context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)
        conversation = await conv_mgr.get_conversation(chat_id, curr_cid)
        
        history_lines = []
        if conversation and hasattr(conversation, "history") and conversation.history:
            raw_history = conversation.history
            cutoff_idx = len(raw_history)
            
            # 定位滑动窗口的断点
            if anchor_text:
                for i in range(len(raw_history) - 1, -1, -1):
                    msg_data = raw_history[i]
                    content = ""
                    if isinstance(msg_data, dict):
                        if 'content' in msg_data:
                            if isinstance(msg_data['content'], str):
                                content = msg_data['content']
                            elif isinstance(msg_data['content'], list):
                                content = "".join([c.get("text", "") for c in msg_data['content'] if c.get("type") == "text"])
                    elif hasattr(msg_data, 'content'):
                        if isinstance(msg_data.content, str):
                            content = msg_data.content
                        elif isinstance(msg_data.content, list):
                            content = "".join([getattr(c, "text", "") for c in msg_data.content if getattr(c, "type", "") == "text"])
                            
                    if anchor_text in content:
                        cutoff_idx = i
                        break
                        
            # 向上拉取指定的历史条数并进行彻底的纯文本转换
            fetch_count = getattr(self.config.attention, 'history_pull_count', 20) if self.config else 20
            start_idx = max(0, cutoff_idx - fetch_count)
            valid_history = raw_history[start_idx:cutoff_idx]
            
            for msg_data in valid_history:
                role = msg_data.get("role", "") if isinstance(msg_data, dict) else getattr(msg_data, "role", "")
                content = ""
                if isinstance(msg_data, dict):
                    if 'content' in msg_data:
                        if isinstance(msg_data['content'], str):
                            content = msg_data['content']
                        elif isinstance(msg_data['content'], list):
                            content = "".join([c.get("text", "") for c in msg_data['content'] if c.get("type") == "text"])
                elif hasattr(msg_data, 'content'):
                    if isinstance(msg_data.content, str):
                        content = msg_data.content
                    elif isinstance(msg_data.content, list):
                        content = "".join([getattr(c, "text", "") for c in msg_data.content if getattr(c, "type", "") == "text"])
                        
                if not content: continue
                    
                if role == "user":
                    # 尝试切分出用户姓名
                    match = re.match(r"^(.*?):\s*(.*)$", content, re.DOTALL)
                    if match:
                        sender, text = match.groups()
                        history_lines.append(f"[{sender}] 说: {text}")
                    else:
                        history_lines.append(f"[群友/用户] 说: {content}")
                elif role == "assistant":
                    history_lines.append(f"[我] 说: {content}")

        if history_lines:
            history_script = "\n".join(history_lines)
            if is_fast_mode:
                history_script = f"（极速唤醒：已同步最近 {len(history_lines)} 条群聊剧本）\n" + history_script

        # ==========================================
        # 4. 当前视界提取与标签替换
        # ==========================================
        current_msg_text = ""
        current_user_msg_idx = -1
        
        if req.messages:
            # 逆向寻找触发本次推理的最后一条用户指令
            for i in range(len(req.messages) - 1, -1, -1):
                msg = req.messages[i]
                if msg.role == "user" and getattr(msg, "tool_call_id", None) is None:
                    current_user_msg_idx = i
                    break
                    
            if current_user_msg_idx != -1:
                # 这个 content 实际上就是 planner.py 传过来的扁平化 prompt_content
                current_msg_text = str(req.messages[current_user_msg_idx].content)

        # 替换占位符
        if req.system_message and req.system_message[0].content:
            final_prompt = req.system_message[0].content
            final_prompt = re.sub(r'<CHAT_HISTORY>|\{HISTORY_PLACEHOLDER\}', f"群聊历史消息：\n{history_script}", final_prompt)
            final_prompt = re.sub(r'<CURRENT_MESSAGES>|\{CURRENT_MSG_PLACEHOLDER\}', current_msg_text.strip(), final_prompt)
            final_prompt = re.sub(r'<RAG_MEMORY>|\{MEMORY_PLACEHOLDER\}', injection, final_prompt)
            req.system_message[0].content = final_prompt

        # ==========================================
        # 5. 混合剧场坍缩与工具保护 (心智隔离核心)
        # ==========================================
        if not req.messages: return

        preserved_messages = []
        for i, msg in enumerate(req.messages):
            if i < current_user_msg_idx:
                # 【防污染隔离】丢弃所有常规的纯文本 user/assistant 历史
                # 【工具保护】绝对保留所有含有 tool_calls, tool_call_id 的记录
                if getattr(msg, "tool_calls", None) or getattr(msg, "tool_call_id", None) or msg.role not in ["user", "assistant"]:
                    preserved_messages.append(msg)
            elif i == current_user_msg_idx:
                # 强化角色沉浸
                original_content = msg.content
                
                if is_fast_mode:
                    director_voice = "【极速唤醒】虽然参考了前面的剧本，但请保持回复极其简短、直接，不要带任何角色名前缀！"
                else:
                    director_voice = "【动作提示】请先判断是否需要调用工具。如果不需要，请直接沉浸在角色中说出你的台词，不要带任何角色名前缀！"
                    
                msg.content = f"(导演旁白：请仔细阅读设定和前面的剧本。这是当前你看到的最新消息：\n{original_content}\n\n>> {director_voice})"
                preserved_messages.append(msg)
            else:
                preserved_messages.append(msg)

        req.messages = preserved_messages
        logger.info(f"[{chat_id}] 🎬 剧本坍缩完成（模式: {'极速' if is_fast_mode else '标准'}）。历史已提取，工具上下文受保护。")