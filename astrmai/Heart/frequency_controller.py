# astrmai/Heart/frequency_controller.py
"""
发言频率控制器 (Frequency Controller) — Phase 6.3 / Gap 1
参考: MaiBot/heart_flow/heartFC_chat.py frequency_control

核心功能: 模拟人类"有时健谈、有时沉默"的自然节奏，
         避免 Bot 对每条消息都回复的"话匣子"行为。

算法（全部纯数学，零 LLM 消耗）:
1. 基础概率 base_prob (config 可调，默认 0.7)
2. @mention 豁免: 被 @bot 时概率强制 = 1.0
3. 精力惩罚: energy < 0.4 时 prob *= energy_factor
4. 密集发言惩罚: 最近 N 分钟已回复 reply_count 次则降频
5. 冷场激励: 超过 silence_threshold 分钟无互动，Bot 主动开口概率略升
6. 情绪加成: mood > 0.5 (兴奋) 略升频；mood < -0.5 (低落) 降频
"""
import time
import random
from typing import Dict, List
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class ChatReplyRecord:
    """每个会话的回复记录（用于频率统计）"""
    reply_timestamps: List[float] = field(default_factory=list)  # 最近的回复时间戳列表
    last_message_time: float = field(default_factory=time.time)  # 最后一条消息的时间


class FrequencyController:
    """
    发言频率控制器
    
    在消息进入 Planner 前调用 should_reply()，返回 False 则跳过本次决策。
    """

    # 默认参数（可由 config 覆盖）
    DEFAULT_BASE_FREQ = 0.7          # 基础回复概率
    DENSE_WINDOW_SEC = 300           # 密集发言检测窗口（5分钟）
    DENSE_REPLY_THRESHOLD = 3        # 5分钟内回复超过此次数触发降频
    DENSE_PENALTY = 0.5              # 密集发言时的概率乘数
    SILENCE_THRESHOLD_MIN = 10.0     # 冷场阈值（分钟）
    SILENCE_BOOST = 0.2              # 冷场时的概率加成
    LOW_ENERGY_THRESHOLD = 0.3       # 精力低于此值开始降频
    HIGH_MOOD_THRESHOLD = 0.5        # 情绪高于此值略升频
    LOW_MOOD_THRESHOLD = -0.5        # 情绪低于此值略降频

    def __init__(self, config=None):
        self.config = config
        self._records: Dict[str, ChatReplyRecord] = {}

        # 从配置加载参数（如果配置提供）
        if config and hasattr(config, 'reply'):
            self.BASE_FREQ = config.reply.base_frequency
        else:
            self.BASE_FREQ = self.DEFAULT_BASE_FREQ

    def should_reply(
        self,
        chat_id: str,
        is_mentioned: bool = False,
        energy: float = 1.0,
        mood: float = 0.0,
        message_text: str = "",
    ) -> bool:
        """
        主入口: 判断本次消息是否应该尝试回复。

        Args:
            chat_id:       会话 ID
            is_mentioned:  是否被 @bot 提及
            energy:        当前精力 (0.0 ~ 1.0)
            mood:          当前情绪 (-1.0 ~ 1.0)
            message_text:  消息文本（暂未使用，预留关键词分析）

        Returns:
            True  = 进入正常决策流程
            False = 跳过本次回复（沉默）
        """
        # @mention 豁免：被提及时强制进入决策
        if is_mentioned:
            self._record_message(chat_id)
            return True

        # 获取/初始化记录
        record = self._get_record(chat_id)
        self._record_message_raw(record)

        # 计算综合概率
        prob = self.BASE_FREQ

        # 1. 精力惩罚
        if energy < self.LOW_ENERGY_THRESHOLD:
            energy_factor = max(0.2, energy / self.LOW_ENERGY_THRESHOLD)
            prob *= energy_factor

        # 2. 密集发言惩罚（频繁回复后自动沉默）
        recent_replies = self._count_recent_replies(record, self.DENSE_WINDOW_SEC)
        if recent_replies >= self.DENSE_REPLY_THRESHOLD:
            prob *= self.DENSE_PENALTY
            logger.debug(
                f"[FrequencyController] 📉 {chat_id} 密集发言惩罚 "
                f"(5min内已回复{recent_replies}次), prob={prob:.2f}"
            )

        # 3. 冷场激励（长时间无互动时主动开口）
        silence_min = self._silence_minutes(record)
        if silence_min >= self.SILENCE_THRESHOLD_MIN:
            prob = min(1.0, prob + self.SILENCE_BOOST)
            logger.debug(
                f"[FrequencyController] 💬 {chat_id} 冷场激励 "
                f"(沉默{silence_min:.1f}min), prob={prob:.2f}"
            )

        # 4. 情绪调节
        if mood > self.HIGH_MOOD_THRESHOLD:
            prob = min(1.0, prob + 0.1)  # 兴奋时更活跃
        elif mood < self.LOW_MOOD_THRESHOLD:
            prob = max(0.1, prob - 0.15)  # 低落时更沉默

        # 5. 概率采样
        result = random.random() < prob
        if not result:
            logger.info(
                f"[FrequencyController] 🔇 {chat_id} 沉默本次回复 "
                f"(prob={prob:.2f}, energy={energy:.2f}, mood={mood:.2f})"
            )
        else:
            # 记录本次有效回复
            self._record_reply(record)

        return result

    def on_message_received(self, chat_id: str):
        """外部调用：收到新消息时更新最后消息时间"""
        record = self._get_record(chat_id)
        record.last_message_time = time.time()

    # ==========================================
    # 内部工具
    # ==========================================

    def _get_record(self, chat_id: str) -> ChatReplyRecord:
        if chat_id not in self._records:
            self._records[chat_id] = ChatReplyRecord()
        return self._records[chat_id]

    def _record_message(self, chat_id: str):
        record = self._get_record(chat_id)
        record.last_message_time = time.time()

    def _record_message_raw(self, record: ChatReplyRecord):
        record.last_message_time = time.time()

    def _record_reply(self, record: ChatReplyRecord):
        """记录一次有效回复时间戳"""
        now = time.time()
        record.reply_timestamps.append(now)
        # 只保留最近 1 小时的记录，防止无限增长
        cutoff = now - 3600
        record.reply_timestamps = [t for t in record.reply_timestamps if t > cutoff]

    def _count_recent_replies(self, record: ChatReplyRecord, window_sec: float) -> int:
        """统计时间窗口内的回复次数"""
        cutoff = time.time() - window_sec
        return sum(1 for t in record.reply_timestamps if t > cutoff)

    def _silence_minutes(self, record: ChatReplyRecord) -> float:
        """计算距离上次消息的沉默时间（分钟）"""
        return (time.time() - record.last_message_time) / 60.0

    def cleanup_inactive(self, max_age_hours: float = 24.0):
        """清理长时间不活跃的会话记录"""
        cutoff = time.time() - max_age_hours * 3600
        stale = [
            cid for cid, rec in self._records.items()
            if rec.last_message_time < cutoff
        ]
        for cid in stale:
            del self._records[cid]
        if stale:
            logger.debug(f"[FrequencyController] 🧹 清理 {len(stale)} 个过期会话记录")
