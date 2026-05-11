# astrmai/Heart/relationship_engine.py
"""
多维度关系引擎 (Multi-Dimensional Relationship Engine) — Phase 5
参考: MaiBot/personality/relationship.py + HeartFlow/social_dynamics.py

核心设计 (完全不依赖 LLM，零 token 消耗):

## 四维好感度模型
1. trust (信任度):      [-100, 100] — 基于持续互动频率与一致性
2. familiarity (熟悉度): [-100, 100] — 基于交互总量与时间跨度
3. emotion_bond (情感纽带): [-100, 100] — 基于情绪共振与正负面事件
4. respect (尊重度):    [-100, 100] — 基于内容质量与行为模式

## 算法特性
- 所有维度变更均由事件驱动 (Event-Driven)，非 LLM 推断
- 每个维度独立的衰减曲线和饱和函数
- social_score = weighted_sum(trust, familiarity, emotion_bond, respect) 作为向后兼容字段
- 支持"高度变更的深度多维度算法"：
  - 对数饱和函数 (高好感时增量递减)
  - 指数衰减 (长期不互动信任/情感纽带下降)
  - 共振放大器 (连续正面互动累积加速效应)
  - 惩罚倍率 (负面事件的伤害随信任度提高而放大)
"""
import math
import time
from typing import Dict, Optional
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class RelationshipVector:
    """四维关系向量"""
    trust: float = 0.0           # 信任度 [-100, 100]
    familiarity: float = 0.0     # 熟悉度 [-100, 100]
    emotion_bond: float = 0.0    # 情感纽带 [-100, 100]
    respect: float = 0.0         # 尊重度 [-100, 100]

    # 元数据
    total_interactions: int = 0
    positive_streak: int = 0     # 连续正面互动次数 (共振用)
    negative_streak: int = 0     # 连续负面互动次数
    first_seen: float = field(default_factory=time.time)
    last_interaction: float = field(default_factory=time.time)
    last_decay_time: float = field(default_factory=time.time)

    @property
    def social_score(self) -> float:
        """向后兼容的单维度好感度 (加权融合)"""
        return self._weighted_score()

    def _weighted_score(self) -> float:
        """加权融合四维为单维 social_score"""
        score = (
            self.trust * 0.30 +
            self.familiarity * 0.20 +
            self.emotion_bond * 0.30 +
            self.respect * 0.20
        )
        return max(-100.0, min(100.0, round(score, 2)))

    def to_dict(self) -> Dict:
        return {
            "trust": round(self.trust, 2),
            "familiarity": round(self.familiarity, 2),
            "emotion_bond": round(self.emotion_bond, 2),
            "respect": round(self.respect, 2),
            "social_score": self.social_score,
            "total_interactions": self.total_interactions,
            "positive_streak": self.positive_streak,
            "negative_streak": self.negative_streak,
            "first_seen": self.first_seen,
            "last_interaction": self.last_interaction,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> 'RelationshipVector':
        return cls(
            trust=data.get("trust", 0.0),
            familiarity=data.get("familiarity", 0.0),
            emotion_bond=data.get("emotion_bond", 0.0),
            respect=data.get("respect", 0.0),
            total_interactions=data.get("total_interactions", 0),
            positive_streak=data.get("positive_streak", 0),
            negative_streak=data.get("negative_streak", 0),
            first_seen=data.get("first_seen", time.time()),
            last_interaction=data.get("last_interaction", time.time()),
            last_decay_time=data.get("last_decay_time", time.time()),
        )

    def get_context_description(self) -> str:
        """供 Prompt 注入的关系描述 (无 LLM 消耗)"""
        level = self._get_relationship_level()
        trust_desc = self._dim_desc("信任", self.trust)
        famil_desc = self._dim_desc("熟悉", self.familiarity)
        emoti_desc = self._dim_desc("情感", self.emotion_bond)
        respe_desc = self._dim_desc("尊重", self.respect)
        
        days_known = max(1, (time.time() - self.first_seen) / 86400)
        
        return (
            f"关系等级: {level} (综合好感: {self.social_score:.0f})\n"
            f"  {trust_desc} | {famil_desc} | {emoti_desc} | {respe_desc}\n"
            f"  认识天数: {days_known:.0f}天 | 累计互动: {self.total_interactions}次"
        )

    def _get_relationship_level(self) -> str:
        s = self.social_score
        if s >= 80: return "💖 至亲挚友"
        if s >= 50: return "💕 亲密好友"
        if s >= 25: return "😊 友好熟人"
        if s >= 5:  return "🙂 普通认识"
        if s >= -10: return "😐 陌生人"
        if s >= -30: return "😒 有些反感"
        if s >= -60: return "😠 厌恶"
        return "🚫 敌对"

    @staticmethod
    def _dim_desc(name: str, value: float) -> str:
        if value >= 50: return f"{name}:极高"
        if value >= 20: return f"{name}:高"
        if value >= 0:  return f"{name}:中"
        if value >= -30: return f"{name}:低"
        return f"{name}:极低"


# ==========================================
# 事件类型定义 (驱动关系变更的事件)
# ==========================================

class RelationshipEvent:
    """关系变更事件的预定义类型"""
    # 正面
    GREETING = "greeting"                # 打招呼
    NORMAL_CHAT = "normal_chat"          # 普通对话
    HELPFUL_REPLY = "helpful_reply"      # 有帮助的回复
    EMOTIONAL_SUPPORT = "emotional_support"  # 情感支持
    COMPLIMENT = "compliment"            # 夸奖/称赞
    DEEP_CONVERSATION = "deep_conversation"  # 深度对话 (触发了记忆检索)
    SHARED_INTEREST = "shared_interest"  # 共同兴趣
    GIFT = "gift"                        # 送礼物 (虚拟)

    # 负面
    INSULT = "insult"                    # 侮辱
    IGNORE = "ignore"                    # 被无视
    ARGUMENT = "argument"               # 争吵
    RUDENESS = "rudeness"               # 粗鲁行为
    SPAM = "spam"                       # 刷屏

    # 中性
    SILENCE_DECAY = "silence_decay"      # 长时间未互动


# ==========================================
# 核心引擎
# ==========================================

class RelationshipEngine:
    """
    多维关系引擎 — 纯算法驱动，零 LLM 消耗。
    
    核心算法:
    1. 对数饱和: delta = raw_delta * log_saturation(current_value)
       高好感时正面增量递减，低好感时负面惩罚放大
    2. 共振放大: 连续正面互动累积，streak_bonus = log2(streak + 1) * 0.3
    3. 信任惩罚: 在负面事件中，信任度每降一次，emotion_bond 受到额外 20% 伤害
    4. 指数衰减: 长时间不互动，各维度向 0 指数衰减
    """

    # 事件影响矩阵 (每种事件对四维的 raw delta)
    EVENT_MATRIX: Dict[str, Dict[str, float]] = {
        # event_type: {trust, familiarity, emotion_bond, respect}
        RelationshipEvent.GREETING:          {"trust": 0.3, "familiarity": 0.8, "emotion_bond": 0.2, "respect": 0.1},
        RelationshipEvent.NORMAL_CHAT:       {"trust": 0.5, "familiarity": 1.0, "emotion_bond": 0.3, "respect": 0.2},
        RelationshipEvent.HELPFUL_REPLY:     {"trust": 1.5, "familiarity": 0.5, "emotion_bond": 0.8, "respect": 1.2},
        RelationshipEvent.EMOTIONAL_SUPPORT: {"trust": 2.0, "familiarity": 0.8, "emotion_bond": 2.5, "respect": 0.5},
        RelationshipEvent.COMPLIMENT:        {"trust": 0.5, "familiarity": 0.3, "emotion_bond": 1.5, "respect": 0.8},
        RelationshipEvent.DEEP_CONVERSATION: {"trust": 1.8, "familiarity": 1.5, "emotion_bond": 1.2, "respect": 1.5},
        RelationshipEvent.SHARED_INTEREST:   {"trust": 1.0, "familiarity": 1.2, "emotion_bond": 1.8, "respect": 0.5},
        RelationshipEvent.GIFT:              {"trust": 0.8, "familiarity": 0.5, "emotion_bond": 2.0, "respect": 0.3},

        RelationshipEvent.INSULT:            {"trust": -3.0, "familiarity": 0.2, "emotion_bond": -4.0, "respect": -3.5},
        RelationshipEvent.IGNORE:            {"trust": -0.5, "familiarity": -0.3, "emotion_bond": -1.0, "respect": -0.5},
        RelationshipEvent.ARGUMENT:          {"trust": -2.0, "familiarity": 0.5, "emotion_bond": -2.5, "respect": -1.5},
        RelationshipEvent.RUDENESS:          {"trust": -1.5, "familiarity": 0.1, "emotion_bond": -2.0, "respect": -2.5},
        RelationshipEvent.SPAM:              {"trust": 0.0, "familiarity": 0.1, "emotion_bond": -0.5, "respect": -1.5},
    }

    # 情绪标签到事件类型的映射 (零 LLM 消耗)
    MOOD_TO_EVENT: Dict[str, str] = {
        "happy": RelationshipEvent.COMPLIMENT,
        "surprise": RelationshipEvent.SHARED_INTEREST,
        "curious": RelationshipEvent.NORMAL_CHAT,
        "neutral": RelationshipEvent.NORMAL_CHAT,
        "sad": RelationshipEvent.EMOTIONAL_SUPPORT,
        "angry": RelationshipEvent.ARGUMENT,
    }

    # 衰减参数
    DECAY_INTERVAL_HOURS = 24     # 每 24 小时衰减一次
    TRUST_DECAY_RATE = 0.02       # 信任衰减率 (每周期)
    FAMILIARITY_DECAY_RATE = 0.01 # 熟悉度衰减率
    EMOTION_DECAY_RATE = 0.03     # 情感纽带衰减率
    RESPECT_DECAY_RATE = 0.005    # 尊重度衰减率 (最持久)

    def __init__(self, config=None):
        self.config = config
        self._vectors: Dict[str, RelationshipVector] = {}  # user_id -> vector

    def get_or_create(self, user_id: str) -> RelationshipVector:
        """获取或创建用户的关系向量"""
        if user_id not in self._vectors:
            self._vectors[user_id] = RelationshipVector()
        return self._vectors[user_id]

    def load_from_profile(self, user_id: str, profile_data: Dict):
        """从 UserProfile 的 group_footprints 中恢复关系向量"""
        rel_data = profile_data.get("relationship_vector", {})
        if rel_data:
            self._vectors[user_id] = RelationshipVector.from_dict(rel_data)
        elif "social_score" in profile_data:
            # 向后兼容: 从旧的 social_score 推断初始四维
            old_score = float(profile_data.get("social_score", 0))
            self._vectors[user_id] = RelationshipVector(
                trust=old_score * 0.3,
                familiarity=old_score * 0.25,
                emotion_bond=old_score * 0.3,
                respect=old_score * 0.15,
                first_seen=profile_data.get("first_seen", time.time()),
                last_interaction=profile_data.get("last_seen", time.time()),
            )

    def process_event(
        self, user_id: str, event_type: str, 
        intensity: float = 1.0, mood_tag: str = ""
    ) -> float:
        """
        核心入口: 处理一个关系变更事件，返回新的 social_score。
        
        所有计算均为纯数学运算，零 LLM 消耗。
        
        Args:
            user_id: 用户 ID
            event_type: 事件类型 (RelationshipEvent.*)
            intensity: 强度乘数 (0.0 ~ 3.0)
            mood_tag: 当前情绪标签 (可选，用于事件类型推断)
            
        Returns:
            新的 social_score
        """
        vec = self.get_or_create(user_id)
        now = time.time()

        # 0. 自动衰减检查
        self._apply_decay(vec, now)

        # 1. 如果传入了 mood_tag 但没有明确 event_type，自动映射
        if mood_tag and event_type == RelationshipEvent.NORMAL_CHAT:
            event_type = self.MOOD_TO_EVENT.get(mood_tag, event_type)

        # 2. 查找事件影响矩阵
        deltas = self.EVENT_MATRIX.get(event_type)
        if not deltas:
            deltas = self.EVENT_MATRIX[RelationshipEvent.NORMAL_CHAT]

        # 3. 对每个维度应用深度算法
        old_score = vec.social_score
        is_positive = sum(deltas.values()) > 0

        for dim_name, raw_delta in deltas.items():
            current_val = getattr(vec, dim_name, 0.0)
            
            # 3.1 对数饱和函数
            saturated_delta = self._log_saturation(raw_delta, current_val)

            # 3.2 强度乘数
            saturated_delta *= max(0.1, min(3.0, intensity))

            # 3.3 共振放大 (连续正面互动)
            if is_positive and vec.positive_streak > 1:
                streak_bonus = math.log2(vec.positive_streak + 1) * 0.3
                saturated_delta *= (1.0 + min(streak_bonus, 1.5))

            # 3.4 信任惩罚放大 (负面事件时，高信任的背叛更痛)
            if raw_delta < 0 and dim_name == "emotion_bond":
                trust_betrayal = max(0, vec.trust / 100.0) * 0.2
                saturated_delta *= (1.0 + trust_betrayal)

            # 3.5 应用增量并夹紧
            new_val = max(-100.0, min(100.0, current_val + saturated_delta))
            setattr(vec, dim_name, new_val)

        # 4. 更新元数据
        vec.total_interactions += 1
        vec.last_interaction = now

        if is_positive:
            vec.positive_streak += 1
            vec.negative_streak = 0
        else:
            vec.negative_streak += 1
            vec.positive_streak = 0

        new_score = vec.social_score
        delta = new_score - old_score

        if abs(delta) > 0.1:
            logger.info(
                f"[Relationship] 💕 用户 {user_id} | 事件: {event_type} | "
                f"好感: {old_score:.1f} → {new_score:.1f} (Δ{delta:+.2f}) | "
                f"streak: +{vec.positive_streak}/-{vec.negative_streak}"
            )

        return new_score

    def process_mood_event(self, user_id: str, mood_tag: str, intensity: float = 1.0) -> float:
        """
        便捷入口: 根据情绪标签更新关系。
        由 StateEngine.calculate_and_update_affection 调用，替代旧的简单加减法。
        """
        event_type = self.MOOD_TO_EVENT.get(mood_tag, RelationshipEvent.NORMAL_CHAT)
        return self.process_event(user_id, event_type, intensity, mood_tag)

    def get_social_score(self, user_id: str) -> float:
        """获取用户的综合好感度 (向后兼容)"""
        vec = self.get_or_create(user_id)
        return vec.social_score

    def get_context(self, user_id: str) -> str:
        """获取关系描述文本 (供 Prompt 注入)"""
        vec = self.get_or_create(user_id)
        return vec.get_context_description()

    def apply_global_decay(self):
        """全局衰减: 在心跳循环中周期性调用"""
        now = time.time()
        decayed_count = 0
        for uid, vec in self._vectors.items():
            if self._apply_decay(vec, now):
                decayed_count += 1
        if decayed_count > 0:
            logger.debug(f"[Relationship] 🌙 全局关系衰减: {decayed_count} 名用户")

    # ==========================================
    # 核心算法
    # ==========================================

    @staticmethod
    def _log_saturation(raw_delta: float, current_value: float) -> float:
        """
        对数饱和函数 — 好感度变更的核心非线性变换。
        
        设计思想:
        - 当 current_value 接近极值 (±100) 时，同方向的增量被压缩
        - 当 current_value 在中间区域时，增量几乎不受影响
        - 逆方向的增量 (如高好感时负面事件) 不做压缩，保留破坏力
        
        公式: effective_delta = raw_delta * (1 - |current / cap|^1.5)
        """
        cap = 100.0

        # 同方向增量: 应用饱和压缩
        if (raw_delta > 0 and current_value > 0) or (raw_delta < 0 and current_value < 0):
            saturation = 1.0 - (abs(current_value) / cap) ** 1.5
            return raw_delta * max(0.05, saturation)  # 最低保留 5% 增量

        # 逆方向增量 (如: 高好感时的负面事件): 不压缩
        return raw_delta

    def _apply_decay(self, vec: RelationshipVector, now: float) -> bool:
        """
        指数衰减函数 — 长时间不互动导致关系自然冷却。
        
        设计:
        - 每个维度独立的衰减率
        - 衰减方向: 向 0 靠近 (正值下降，负值上升)
        - 衰减量: dim *= (1 - decay_rate) ^ periods
        - 高信任度不容易衰减（衰减率乘以 0.5）
        """
        hours_since_decay = (now - vec.last_decay_time) / 3600
        if hours_since_decay < self.DECAY_INTERVAL_HOURS:
            return False

        periods = hours_since_decay / self.DECAY_INTERVAL_HOURS
        vec.last_decay_time = now

        # 信任度衰减 (高信任时衰减更慢)
        trust_rate = self.TRUST_DECAY_RATE
        if vec.trust > 50:
            trust_rate *= 0.5  # 高信任的惰性
        vec.trust *= (1.0 - trust_rate) ** periods

        # 熟悉度衰减 (最慢)
        vec.familiarity *= (1.0 - self.FAMILIARITY_DECAY_RATE) ** periods

        # 情感纽带衰减 (最快，情感容易淡化)
        vec.emotion_bond *= (1.0 - self.EMOTION_DECAY_RATE) ** periods

        # 尊重度衰减 (非常缓慢，尊重是持久的)
        vec.respect *= (1.0 - self.RESPECT_DECAY_RATE) ** periods

        # 极小值归零
        for dim in ("trust", "familiarity", "emotion_bond", "respect"):
            val = getattr(vec, dim)
            if abs(val) < 0.01:
                setattr(vec, dim, 0.0)

        return True

    # ==========================================
    # 高级分析 (纯算法)
    # ==========================================

    def classify_interaction_type(self, text: str) -> str:
        """
        纯算法交互类型分类 — 基于关键词匹配，零 LLM 消耗。
        返回 RelationshipEvent 常量。
        """
        if not text:
            return RelationshipEvent.NORMAL_CHAT

        lower = text.lower()

        # 侮辱检测
        insult_words = {"sb", "傻逼", "滚", "去死", "白痴", "废物", "脑残", "cnm", "nmsl"}
        if any(w in lower for w in insult_words):
            return RelationshipEvent.INSULT

        # 粗鲁检测
        rude_words = {"闭嘴", "别说了", "无语", "恶心", "讨厌你"}
        if any(w in lower for w in rude_words):
            return RelationshipEvent.RUDENESS

        # 夸奖检测
        compliment_words = {"好棒", "太强了", "厉害", "牛逼", "赞", "佩服", "漂亮", "可爱"}
        if any(w in lower for w in compliment_words):
            return RelationshipEvent.COMPLIMENT

        # 情感支持检测
        support_words = {"没事的", "别担心", "加油", "抱抱", "心疼", "辛苦了", "贴贴"}
        if any(w in lower for w in support_words):
            return RelationshipEvent.EMOTIONAL_SUPPORT

        # 问好检测
        greeting_words = {"早上好", "晚上好", "你好", "嗨", "hello", "hi"}
        if any(w in lower for w in greeting_words):
            return RelationshipEvent.GREETING

        # 深度对话检测 (长消息 > 50 字或包含问句)
        if len(text) > 50 and ("?" in text or "？" in text):
            return RelationshipEvent.DEEP_CONVERSATION

        # 刷屏检测 (极短 + 无意义)
        if len(text) <= 2 and text in {".", "。", "1", "?", "？", ".."}:
            return RelationshipEvent.SPAM

        return RelationshipEvent.NORMAL_CHAT

    def get_all_vectors(self) -> Dict[str, Dict]:
        """获取所有关系向量的字典快照 (用于持久化)"""
        return {uid: vec.to_dict() for uid, vec in self._vectors.items()}
