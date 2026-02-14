### 📄 services/evolution/pattern_learner.py
import re
from typing import List, Dict, Any
from astrbot.api import logger

class PatternLearner:
    """
    表达模式学习器 (Ported from SelfLearning)
    职责：分析用户的聊天记录，提取其表达特征（长度、标点、表情习惯）。
    """
    
    def __init__(self):
        # 标点符号特征
        self.punctuations = {
            '~': 'tilde_user', 
            '！': 'exclamation_user', 
            '!': 'exclamation_user',
            '？': 'question_user',
            '?': 'question_user',
            '。。。': 'ellipsis_user',
            '...': 'ellipsis_user'
        }

    def analyze_patterns(self, messages: List[str]) -> Dict[str, Any]:
        """
        分析最近 N 条消息，返回风格特征向量
        """
        if not messages:
            return {}

        total_len = 0
        punc_counts = {k: 0 for k in self.punctuations.values()}
        emoji_count = 0
        
        for msg in messages:
            total_len += len(msg)
            # 统计标点
            for char, key in self.punctuations.items():
                if char in msg:
                    punc_counts[key] += 1
            # 简单统计表情 (方括号格式 [表情])
            emoji_count += len(re.findall(r'\[.*?\]', msg))

        avg_len = total_len / len(messages)
        
        # 生成风格描述 Prompt
        style_prompt = []
        if avg_len < 5:
            style_prompt.append("对方说话非常简短，类似微信短句。")
        elif avg_len > 20:
            style_prompt.append("对方喜欢发长段文字。")
            
        if punc_counts['tilde_user'] > 0:
            style_prompt.append("对方喜欢使用波浪号~，语气比较荡漾。")
        
        if emoji_count > len(messages) / 2:
            style_prompt.append("对方非常喜欢使用表情包。")

        return {
            "avg_length": avg_len,
            "style_prompt": "\n".join(style_prompt)
        }