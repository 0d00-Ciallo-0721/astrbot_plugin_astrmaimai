import re
import html
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger

class PromptRefiner:
    """
    第三阶段：剧场模式与潜意识注入中心 (Phase 3: Theater & Subconscious Injection)
    职责：
    1. 底层记忆回溯：使用锚点截取 AstrBot 原生历史，防止滑动窗口的重复污染。
    2. 工具链安全：保护原生 tool_calls 结构不被破坏。
    3. 剧场坍缩：将文本消息全部折叠为 System Prompt 里的自然语言剧本，打破 AI 思想钢印。
    """
    def __init__(self, memory_engine):
        self.memory_engine = memory_engine

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

        # 彻底清洗：利用正则清理上下文历史中所有先前注入的记忆块，防止上下文窗口膨胀
        for msg in req.system_message + req.messages:
            if isinstance(msg.content, str):
                msg.content = re.sub(r"<injected_memory>.*?</injected_memory>\n?", "", msg.content, flags=re.DOTALL)

        chat_id = event.unified_msg_origin

        # ==========================================
        # 2. 潜意识召回 (RAG)
        # ==========================================
        injection = ""
        current_query = event.message_str
        if self.memory_engine and current_query:
            memory_text = await self.memory_engine.recall(current_query, session_id=chat_id)
            if memory_text and "什么也没想起来" not in memory_text:
                safe_memory_text = html.escape(memory_text)
                injection = f"<injected_memory>\n记忆涌现：基于当前话题，你回忆起了以下事情：\n{safe_memory_text}\n</injected_memory>\n"

        # ==========================================
        # 3. 底层历史拉取与滑动窗口防污染截断
        # ==========================================
        anchor_event = event.get_extra("astrmai_anchor_event")
        anchor_text = anchor_event.message_str.strip() if anchor_event else None
        
        conv_mgr = context.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)
        conversation = await conv_mgr.get_conversation(chat_id, curr_cid)
        
        history_lines = []
        if conversation and hasattr(conversation, "history") and conversation.history:
            raw_history = conversation.history
            cutoff_idx = len(raw_history)
            
            # 倒序查找锚点，截断包含滑动窗口的重复数据
            if anchor_text:
                for i in range(len(raw_history) - 1, -1, -1):
                    msg_data = raw_history[i]
                    content = ""
                    # 兼容 AstrBot 底层字典和对象两种序列化形态
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
                        
            # 获取无污染的真实历史
            valid_history = raw_history[:cutoff_idx]
            
            # 格式化为剧本台词
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
                    match = re.match(r"^(.*?):\s*(.*)$", content, re.DOTALL)
                    if match:
                        sender, text = match.groups()
                        history_lines.append(f"[{sender}] 说: {text}")
                    else:
                        history_lines.append(f"[群友/用户] 说: {content}")
                elif role == "assistant":
                    history_lines.append(f"[我] 说: {content}")

        history_script = "\n".join(history_lines) if history_lines else "无近期历史记录。"

        # ==========================================
        # 4. 当前视界提取与标签替换 (修复占位符错位)
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
                last_msg_content = str(req.messages[current_user_msg_idx].content)
                for line in last_msg_content.split("\n"):
                    match = re.match(r"^(.*?):\s*(.*)$", line, re.DOTALL)
                    if match:
                        current_msg_text += f"[{match.group(1)}] 说: {match.group(2)}\n"
                    else:
                        current_msg_text += f"[群友/用户] 说: {line}\n"

        # 执行正则替换，兼容新旧版本占位符
        if req.system_message and req.system_message[0].content:
            final_prompt = req.system_message[0].content
            final_prompt = re.sub(r'<CHAT_HISTORY>|\{HISTORY_PLACEHOLDER\}', f"历史对话记忆：\n{history_script}", final_prompt)
            final_prompt = re.sub(r'<CURRENT_MESSAGES>|\{CURRENT_MSG_PLACEHOLDER\}', current_msg_text.strip(), final_prompt)
            final_prompt = re.sub(r'<RAG_MEMORY>|\{MEMORY_PLACEHOLDER\}', injection, final_prompt)
            req.system_message[0].content = final_prompt

        # ==========================================
        # 5. 混合剧场坍缩与工具保护
        # ==========================================
        if not req.messages: return

        preserved_messages = []
        for i, msg in enumerate(req.messages):
            if i < current_user_msg_idx:
                # 保护历史工具链结构
                if getattr(msg, "tool_calls", None) or getattr(msg, "tool_call_id", None) or msg.role not in ["user", "assistant"]:
                    preserved_messages.append(msg)
            elif i == current_user_msg_idx:
                # 剧本化当前指令包装
                original_content = msg.content
                msg.content = f"(导演旁白：请仔细阅读系统设定的世界观和前面的历史记忆，这是当前你看到的最新消息：\n{original_content}\n\n请直接沉浸在角色中给出回应。不要使用诸如'Bot:'或'[我]:'之类的角色名前缀！)"
                preserved_messages.append(msg)
            else:
                # 保护当前可能正在进行的流式/迭代工具调用
                preserved_messages.append(msg)

        # 覆写底层消息矩阵
        req.messages = preserved_messages
        logger.info(f"[{chat_id}] 🎬 剧本坍缩完成。真实历史截取锚点命中: {anchor_text[:10] if anchor_text else '无锚点'}")