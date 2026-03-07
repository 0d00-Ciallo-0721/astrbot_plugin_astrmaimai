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
    def __init__(self, state_engine: StateEngine, mood_manager: MoodManager, config=None):
        self.state_engine = state_engine
        self.mood_manager = mood_manager
        self.config = config if config else state_engine.config
        
        # 接入 Config (不再硬编码)
        self.segmentation_threshold = self.config.reply.segment_min_len # 分段阈值
        self.no_segment_limit = self.config.reply.no_segment_max_len      # 长文不分段阈值
        self.meme_probability = self.config.reply.meme_probability       # 表情包概率


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
        [修改] 拟人化分段算法 (增强版，解决空段和颜文字重组导致的段数溢出)
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
            if not part.strip(): continue # 强化过滤：忽略纯空白片段
            
            if re.match(split_pattern, part):
                current += part
                if len(current) >= self.segmentation_threshold:
                    segments.append(current.strip())
                    current = ""
            else:
                current += part
        
        if current.strip():
            segments.append(current.strip())
            
        # 还原颜文字并执行二次净化
        final_segments = []
        for seg in segments:
            for i, k in enumerate(kaomojis):
                seg = seg.replace(f"__KAOMOJI_{i}__", k)
            if seg.strip(): # 最后一道防线，确保绝不把空字符串当做独立段落
                final_segments.append(seg.strip())
            
        return final_segments

    async def handle_reply(self, event: AstrMessageEvent, raw_text: str, chat_id: str):
        """
        执行回复全流程
        """
        if not raw_text: return

        # 1. 清洗
        clean_text = self._clean_reply_content(raw_text)
        if not clean_text: return

        tag = "neutral"
        # 2. 情绪后处理 (Post-Processing Mood)
        # LLM 的回复本身蕴含了它的情绪，我们需要解析它来更新 Bot 的心情状态
        try:
            # 获取当前状态与触发本次交互的用户画像
            state = await self.state_engine.get_state(chat_id)
            user_id = event.get_sender_id()
            
            # 安全获取画像，容错处理
            if hasattr(self.state_engine, 'get_user_profile'):
                profile = await self.state_engine.get_user_profile(user_id)
                user_affection = getattr(profile, 'social_score', 0.0) if profile else 0.0
            else:
                user_affection = 0.0
            
            # 适配 MoodManager 的方法调用
            if hasattr(self.mood_manager, 'analyze_mood'):
                (tag, new_mood) = await self.mood_manager.analyze_mood(
                    text=clean_text, 
                    current_mood=state.mood,
                    user_affection=user_affection
                )
            elif hasattr(self.mood_manager, 'analyze_text_mood'):
                (tag, new_mood) = await self.mood_manager.analyze_text_mood(clean_text, state.mood)
            else:
                new_mood = state.mood
                
            # 更新状态 (修复: 补充传入缺失的 chat_id 位置参数)
            state.mood = new_mood
            if hasattr(self.state_engine, 'persistence'):
                await self.state_engine.persistence.save_chat_state(chat_id, state)
            elif hasattr(self.state_engine, 'db'):
                await self.state_engine.db.save_chat_state(chat_id, state)
            
            logger.debug(f"[Reply] 😃 情绪更新: {tag} ({new_mood:.2f})")
        except AttributeError as e:
            logger.warning(f"[Reply] 情绪模块 API 漂移/失效: {e}")
            tag = "neutral"
        except Exception as e:
            logger.warning(f"[Reply] 情绪分析失败: {e}")
            tag = "neutral"

        # 3. 分段发送
        segments = self._segment_reply_content(clean_text)
        for i, seg in enumerate(segments):
            await event.send(event.plain_result(seg))
            # 拟人化打字延迟 (接入 Config)
            if i < len(segments) - 1:
                delay = min(2.0, max(0.5, len(seg) * self.config.reply.typing_speed_factor))
                await asyncio.sleep(delay)

        # 4. 发送表情包 (基于刚才分析出的 tag)
        if tag and tag != "neutral":
            await send_meme(event, tag, self.meme_probability, MEMES_DIR)