### 📄 core/reply_engine.py
import re
import asyncio
from typing import Any
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain, Image

# 导入工具
from ..utils.prompt_builder import PromptBuilder
from ..utils.text_cleaner import TextCleaner
from ..meme_engine.meme_config import MEMES_DIR
from ..meme_engine.meme_sender import send_meme

class ReplyEngine:
    """
    (v2.0) 表达引擎 (纯执行器)
    职责：
    1. 接收 Instruction，生成回复
    2. 执行非语言动作 (Actions)
    3. 发送表情包 (Meme)
    注意：严禁在此处修改 ChatState 或写入数据库！
    """
    
    def __init__(self, 
                 context, 
                 config, 
                 prompt_builder: PromptBuilder, 
                 state_manager, # 仅保留用于读取，不修改
                 persistence,   # 仅保留用于读取
                 mood_manager):
        self.context = context
        self.config = config
        self.prompt_builder = prompt_builder
        self.state_manager = state_manager
        self.mood_manager = mood_manager
        # self.persistence = persistence # 2.0 中不再需要在此处调用 persistence 写库

    async def handle_reply(self, event: AstrMessageEvent, 
                         decision: Any): # ImpulseDecision
        """
        统一回复入口
        """
        # 1. 解析指令 (从 ImpulseDecision 中获取 thought)
        thought = getattr(decision, "thought", "")
        if not thought and hasattr(decision, "reason"): 
            thought = decision.reason # 兼容旧版
            
        logger.info(f"🗣️ [ReplyEngine] Executing reply. Instruction: {thought[:50]}...")

        # 2. 生成回复 (LLM)
        reply_text = await self._generate_reply(event, thought)
        if not reply_text: return None

        # 3. 提取并执行动作 (Physical Actions)
        # 例如: (poke), (sigh)
        actions = TextCleaner.extract_actions(reply_text)
        
        # 4. 清洗文本 (移除动作标记和幻觉前缀)
        clean_text = TextCleaner.clean_reply(reply_text)
        clean_text = TextCleaner.remove_actions(clean_text)

        # 5. 发送文本 (分段)
        if clean_text.strip():
            await self._send_segmented(event, clean_text)

        # 6. 异步执行动作
        if actions:
            asyncio.create_task(self._execute_actions(event, actions))
            
        # [已删除] _update_state_after_reply (副作用代码)
        # [已删除] persistence.save_message (副作用代码)
        
        return clean_text # 返回给调度器，用于后续存储

    async def _generate_reply(self, event: AstrMessageEvent, instruction: str) -> str:
        """调用 LLM 生成"""
        # 调用 PromptBuilder (v2.0 接口)
        prompt = await self.prompt_builder.get_reply_prompt(event, instruction)
        
        resp = await self.context.llm_chat(prompt)
        if resp and resp.completion_text:
            return resp.completion_text
        return ""

    async def _send_segmented(self, event: AstrMessageEvent, text: str):
        """拟人化分段发送"""
        if len(text) > 50 and self.config.enable_segmentation:
            # 简单按标点分段
            segments = re.split(r'([。！？\n])', text)
            buffer = ""
            for seg in segments:
                buffer += seg
                if len(buffer) > 20 or seg in "。！？\n":
                    await event.send(Plain(buffer))
                    await asyncio.sleep(len(buffer) * 0.05 + 0.5) # 模拟打字延迟
                    buffer = ""
            if buffer: await event.send(Plain(buffer))
        else:
            await event.send(Plain(text))

    async def _execute_actions(self, event: AstrMessageEvent, actions: list):
        """执行物理动作"""
        for act in actions:
            act = act.lower()
            if "poke" in act or "戳" in act:
                # 尝试调用平台戳一戳 (需适配器支持)
                # platform = self.context.get_platform(...)
                pass 
            elif "sigh" in act or "叹气" in act:
                # 发送叹气表情
                await self.mood_manager.check_and_send_emotion(event, force_tag="sigh")
            elif "wink" in act or "眨眼" in act:
                await self.mood_manager.check_and_send_emotion(event, force_tag="wink")