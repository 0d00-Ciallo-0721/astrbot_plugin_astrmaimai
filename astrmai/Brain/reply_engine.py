import re
import asyncio
import random
from typing import List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

# 引入依赖模块
from ..infra.datamodels import ChatState
from ..Heart.state_engine import StateEngine
from ..Heart.mood_manager import MoodManager
from ..meme_engine.meme_config import MEMES_DIR
from ..meme_engine.meme_sender import send_meme

class ReplyEngine:
    """
    回复引擎 (Expression Layer)
    职责: 清洗 LLM 输出、拟人化分段、情绪后处理与表情包发送
    """
    def __init__(self, state_engine: StateEngine, mood_manager: MoodManager):
        self.state_engine = state_engine
        self.mood_manager = mood_manager
        
        # 配置项 (可后续通过 config 传入，此处使用 HeartFlow 默认值)
        self.segmentation_threshold = 15 # 分段阈值
        self.no_segment_limit = 120      # 长文不分段阈值
        self.meme_probability = 60       # 表情包概率

    def _clean_reply_content(self, text: str) -> str:
        """
        清洗 LLM 输出的幻觉前缀
        """
        if not text: return ""
        # 去除 [HH:MM:SS] 时间戳
        text = re.sub(r'^\[.*?\]\s*', '', text)
        # 去除 BotName: 前缀 (简单正则，匹配常见的 名字: 格式)
        text = re.sub(r'(?i)^[a-zA-Z0-9_\u4e00-\u9fa5]+[：:]\s*', '', text)
        return text.strip()

    def _segment_reply_content(self, text: str) -> List[str]:
        """
        拟人化分段算法
        """
        if len(text) > self.no_segment_limit:
            return [text]

        # 保护颜文字 (简单版)
        kaomoji_pattern = r'(\(.*?\)|（.*?）)'
        kaomojis = []
        def replace_kaomoji(match):
            kaomojis.append(match.group(0))
            return f"__KAOMOJI_{len(kaomojis)-1}__"
        
        protected_text = re.sub(kaomoji_pattern, replace_kaomoji, text)
        
        # 标点切分
        split_pattern = r'([。！？；!?;~]+)'
        parts = re.split(split_pattern, protected_text)
        
        segments = []
        current = ""
        for part in parts:
            if not part: continue
            if re.match(split_pattern, part):
                if len(current) >= self.segmentation_threshold:
                    segments.append(current.strip())
                    current = ""
                else:
                    current += part
            else:
                current += part
        
        if current.strip():
            segments.append(current.strip())
            
        # 还原
        final_segments = []
        for seg in segments:
            for i, k in enumerate(kaomojis):
                seg = seg.replace(f"__KAOMOJI_{i}__", k)
            final_segments.append(seg)
            
        return final_segments

    async def handle_reply(self, event: AstrMessageEvent, raw_text: str, chat_id: str):
        """
        执行回复全流程
        """
        if not raw_text: return

        # 1. 清洗
        clean_text = self._clean_reply_content(raw_text)
        if not clean_text: return

        # 2. 情绪后处理 (Post-Processing Mood)
        # LLM 的回复本身蕴含了它的情绪，我们需要解析它来更新 Bot 的心情状态
        try:
            # 获取当前状态
            state = await self.state_engine.get_state(chat_id)
            
            # 分析回复文本的情绪
            (tag, new_mood) = await self.mood_manager.analyze_text_mood(clean_text, state.mood)
            
            # 更新状态 (StateEngine 会处理持久化)
            state.mood = new_mood
            await self.state_engine.db.save_chat_state(state)
            
            logger.debug(f"[Reply] 😃 情绪更新: {tag} ({new_mood:.2f})")
        except Exception as e:
            logger.warning(f"[Reply] 情绪分析失败: {e}")
            tag = "neutral"

        # 3. 分段发送
        segments = self._segment_reply_content(clean_text)
        for i, seg in enumerate(segments):
            await event.send(event.plain_result(seg))
            # 拟人化打字延迟
            if i < len(segments) - 1:
                delay = min(2.0, max(0.5, len(seg) * 0.1))
                await asyncio.sleep(delay)

        # 4. 发送表情包 (基于刚才分析出的 tag)
        if tag and tag != "neutral":
            await send_meme(event, tag, self.meme_probability, MEMES_DIR)