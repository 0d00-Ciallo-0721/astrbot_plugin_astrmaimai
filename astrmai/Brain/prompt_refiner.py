import re
import html
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger

class PromptRefiner:
    """
    第三阶段：剧场模式与潜意识注入中心 (Phase 3: Theater & Subconscious Injection)
    职责：
    1. 拦截底层机械化的 OpenAI 格式，将其折叠为自然语言的剧场剧本，打破 AI 思想钢印。
    2. 处理 RAG 潜意识记忆的安全无感注入。
    """
    def __init__(self, memory_engine):
        self.memory_engine = memory_engine

    async def refine_prompt(self, event: AstrMessageEvent, req: ProviderRequest, context) -> None:
        # ==========================================
        # 1. 状态校验与清理
        # ==========================================
        disable_rag = False
        if hasattr(context, "get"):
            disable_rag = context.get("disable_rag_injection")
        elif hasattr(context, "shared_dict"):
            disable_rag = context.shared_dict.get("disable_rag_injection", False)
            
        if disable_rag:
            return

        # 彻底清洗：利用正则清理上下文历史中所有先前注入的记忆块，防止上下文窗口被旧记忆污染
        for msg in req.system_message + req.messages:
            if isinstance(msg.content, str):
                msg.content = re.sub(r"<injected_memory>.*?</injected_memory>\n?", "", msg.content, flags=re.DOTALL)

        # ==========================================
        # 2. 潜意识召回 (RAG)
        # ==========================================
        chat_id = event.unified_msg_origin
        current_query = event.message_str
        injection = ""
        
        if self.memory_engine and current_query:
            memory_text = await self.memory_engine.recall(current_query, session_id=chat_id)
            if memory_text and "什么也没想起来" not in memory_text:
                # [核心安全补丁] 对提取的文本进行实体转义，防止污染指令边界
                safe_memory_text = html.escape(memory_text)
                # 使用新的剧本模式拼接格式
                injection = f"<injected_memory>\n记忆涌现：基于当前话题，你回忆起了以下事情：\n{safe_memory_text}\n</injected_memory>\n"

        # ==========================================
        # 3. 混合剧场坍缩与工具保护层 (Theater Collapse & Tool Calling Protection)
        # ==========================================
        if not req.messages:
            return

        # 找到当前回合的起始点（当前触发的最终 user 消息）
        # 从后往前找，找到第一个 role == 'user' 且没有 tool_call_id 的消息
        current_user_msg_idx = -1
        for i in range(len(req.messages) - 1, -1, -1):
            msg = req.messages[i]
            # 兼容性判断：普通文本 user 消息，不含 tool_call_id
            if msg.role == "user" and getattr(msg, "tool_call_id", None) is None:
                if isinstance(msg.content, str):
                    current_user_msg_idx = i
                    break
        
        if current_user_msg_idx == -1:
            current_user_msg_idx = len(req.messages) - 1 # 兜底

        history_lines = []
        preserved_messages = []
        
        for i, msg in enumerate(req.messages):
            if i < current_user_msg_idx:
                # 历史消息提取
                if getattr(msg, "tool_calls", None) or getattr(msg, "tool_call_id", None) or msg.role not in ["user", "assistant"]:
                    # 保护：保留历史中的工具调用链格式
                    preserved_messages.append(msg)
                else:
                    # 纯文本历史，彻底扁平化转为剧本历史线
                    if isinstance(msg.content, str):
                        speaker = "[我]" if msg.role == "assistant" else "[群友/用户]"
                        history_lines.append(f"{speaker} 说: {msg.content}")
            elif i == current_user_msg_idx:
                # 当前触发视界的终极指令封装
                original_content = msg.content
                msg.content = f"当前你看到的消息：\n{original_content}\n\n(导演旁白请仔细阅读上述群聊历史和当前消息，顺着场景，完全沉浸在你的角色中直接给出你的回应。绝不要输出诸如'Bot:'或'[我]:'之类的角色名前缀，像真人一样直接接话。)"
                preserved_messages.append(msg)
            else:
                # current_user_msg_idx 之后的工具调用链，原样保护放行
                preserved_messages.append(msg)

        # ==========================================
        # 4. 终极 Payload 组装
        # ==========================================
        history_script = ""
        if history_lines:
            history_script = "\n群聊历史消息：\n" + "\n".join(history_lines) + "\n"
            
        if req.system_message and isinstance(req.system_message[0].content, str):
            # 将生成的扁平化历史与潜意识记忆均注入到系统前传世界观中
            if injection:
                req.system_message[0].content += f"\n{injection}"
            if history_script:
                req.system_message[0].content += f"\n{history_script}"
                
        # 用提取并保护了工具链的 messages 替换掉原有的底层数组
        req.messages = preserved_messages