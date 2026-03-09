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
                injection = f"<injected_memory>\n[潜意识涌现：基于当前话题，你回忆起了以下事情：]\n{safe_memory_text}\n</injected_memory>\n"

        # ==========================================
        # 3. 剧场模式坍缩 (Theater Mode Collapse)
        # ==========================================
        script_lines = []
        sender_name = event.get_sender_name() or "用户"

        # 将原有的多轮 JSON 数组，展平为单维度的文字剧本
        for i, msg in enumerate(req.messages):
            if not isinstance(msg.content, str):
                continue
                
            # 角色转换映射
            if msg.role == "assistant":
                speaker = "[你]"
            elif msg.role == "user":
                # 若为最后一条，明确是当前触发消息的用户
                speaker = f"[{sender_name}]" if i == len(req.messages) - 1 else "[群友/用户]"
            else:
                speaker = f"[{msg.role.capitalize()}]"
            
            # 剧本组装
            if i == len(req.messages) - 1:
                script_lines.append(f"【当前场景】\n{speaker} 说: {msg.content}")
            else:
                script_lines.append(f"{speaker} 说: {msg.content}")

        # 构建最终的剧本指令
        theater_script = (
            "【历史剧本记录】\n" 
            + "\n".join(script_lines) 
            + "\n\n(系统提示：请顺着上述场景，完全沉浸在你的角色中，直接给出你的回应。绝不要输出诸如'Bot:'或'[你]:'之类的角色名前缀，像真人一样直接说话。)"
        )

        # ==========================================
        # 4. 终极 Payload 组装
        # ==========================================
        # A. 将潜意识记忆注入 System Prompt
        if req.system_message and isinstance(req.system_message[0].content, str):
            if injection:
                req.system_message[0].content += f"\n{injection}"
                
        # B. 降维打击：用剧本替换掉整个历史消息数组
        if req.messages:
            last_msg = req.messages[-1]
            last_msg.content = theater_script
            # 大模型现在只会看到“一条”包含完整历史剧本的用户消息
            req.messages = [last_msg]