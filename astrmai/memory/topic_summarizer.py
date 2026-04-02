# astrmai/memory/topic_summarizer.py
"""
话题图谱概括器 (Topic Graph Summarizer) — Phase 3
参考: MaiBot/memory_system/chat_history_summarizer.py

核心设计:
- 将对话按话题分类，而非按时间线平铺
- 每个话题维护独立的摘要、关键事实、情感倾向
- 话题间通过"共现关系"建立图谱
- 纯算法驱动话题检测（关键词聚类 + 时间窗口分割），仅在最终摘要阶段调用一次 LLM

工作流:
1. 接收原始消息流 → 基于时间间隔 + 语义断裂检测 → 切割为话题段落
2. 对每个话题段落 → 提取关键词、参与者、情感倾向
3. 一次性 LLM 调用 → 生成各话题段落的摘要
4. 写入记忆引擎 (附带话题标签)
"""
import time
import re
import math
from typing import Dict, List, Optional, Tuple
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class TopicSegment:
    """单个话题段落"""
    messages: List[Dict] = field(default_factory=list)   # [{sender, content, timestamp}]
    keywords: List[str] = field(default_factory=list)
    participants: List[str] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    sentiment_score: float = 0.0  # -1.0 ~ 1.0
    message_count: int = 0

    @property
    def duration_minutes(self) -> float:
        return (self.end_time - self.start_time) / 60 if self.end_time > self.start_time else 0

    @property
    def text_block(self) -> str:
        """拼接为纯文本块供 LLM 摘要"""
        return "\n".join(
            f"[{m.get('sender', '?')}]: {m.get('content', '')}"
            for m in self.messages
        )


class TopicSummarizer:
    """话题图谱概括器"""

    # 话题分割参数
    SILENCE_THRESHOLD_SEC = 300   # 静默 5 分钟视为话题断裂
    MIN_SEGMENT_MESSAGES = 3     # 最少 3 条消息才构成有效话题
    MAX_TOPICS_PER_BATCH = 8     # 单次最多处理 8 个话题段

    # 情感关键词表 (纯算法，零 LLM)
    POSITIVE_WORDS = frozenset([
        "哈哈", "嘿嘿", "太棒了", "谢谢", "感谢", "喜欢", "开心", "好玩",
        "有趣", "牛", "厉害", "赞", "可以", "好的", "666", "nice",
        "笑死", "绝了", "爱了", "贴贴", "宝", "乖", "棒"
    ])
    NEGATIVE_WORDS = frozenset([
        "烦", "气死", "讨厌", "滚", "闭嘴", "无聊", "傻", "笨",
        "难过", "呜呜", "唉", "惨", "垃圾", "差评", "恶心",
        "生气", "怒", "sb", "cnm", "怎么回事"
    ])

    def __init__(self, gateway=None, config=None):
        self.gateway = gateway
        self.config = config

    async def process_history(
        self, messages: List[Dict], session_id: str = ""
    ) -> List[Dict]:
        """
        主入口: 处理消息流 → 返回结构化话题摘要列表。
        
        Args:
            messages: [{"sender": str, "content": str, "timestamp": float}]
            session_id: 会话标识
            
        Returns:
            [{"topic_keywords": [...], "summary": str, "sentiment": str, 
              "participants": [...], "importance": float, "message_count": int}]
        """
        if not messages or len(messages) < self.MIN_SEGMENT_MESSAGES:
            return []

        # 阶段一: 纯算法 — 话题分割
        segments = self._segment_by_silence(messages)
        logger.info(
            f"[TopicSummarizer] 📊 话题分割完成: "
            f"{len(messages)} 条消息 → {len(segments)} 个话题段"
        )

        # 阶段二: 纯算法 — 关键词提取 + 情感分析
        for seg in segments:
            seg.keywords = self._extract_keywords(seg)
            seg.participants = list(set(
                m.get("sender", "") for m in seg.messages if m.get("sender")
            ))
            seg.sentiment_score = self._calculate_sentiment(seg)
            seg.message_count = len(seg.messages)

        # 过滤无效段 + 限制数量
        valid_segments = [
            s for s in segments if s.message_count >= self.MIN_SEGMENT_MESSAGES
        ][:self.MAX_TOPICS_PER_BATCH]

        if not valid_segments:
            return []

        # 阶段三: 单次 LLM 批量摘要 (将所有话题段合为一个 prompt)
        summaries = await self._batch_summarize(valid_segments)

        # 阶段四: 组装结果
        results = []
        for i, seg in enumerate(valid_segments):
            summary_text = summaries[i] if i < len(summaries) else "话题内容较短"
            sentiment_label = self._sentiment_to_label(seg.sentiment_score)
            importance = self._calculate_importance(seg)

            results.append({
                "topic_keywords": seg.keywords[:5],
                "summary": summary_text,
                "sentiment": sentiment_label,
                "participants": seg.participants,
                "importance": importance,
                "message_count": seg.message_count,
                "duration_minutes": seg.duration_minutes,
            })

        logger.info(
            f"[TopicSummarizer] ✅ 话题概括完成: 提取 {len(results)} 个有效话题"
        )
        return results

    # ==========================================
    # 阶段一: 纯算法话题分割
    # ==========================================

    def _segment_by_silence(self, messages: List[Dict]) -> List[TopicSegment]:
        """基于时间间隔 + 语义断裂点分割话题"""
        if not messages:
            return []

        # 按时间排序
        sorted_msgs = sorted(messages, key=lambda m: m.get("timestamp", 0))

        segments: List[TopicSegment] = []
        current = TopicSegment()
        current.start_time = sorted_msgs[0].get("timestamp", 0)

        for i, msg in enumerate(sorted_msgs):
            ts = msg.get("timestamp", 0)

            # 检查是否需要断裂
            should_split = False
            if i > 0:
                prev_ts = sorted_msgs[i - 1].get("timestamp", 0)
                gap = ts - prev_ts

                # 条件1: 超过静默阈值
                if gap > self.SILENCE_THRESHOLD_SEC:
                    should_split = True

                # 条件2: 关键词突变检测 (每 10 条检测一次)
                if not should_split and len(current.messages) >= 10 and len(current.messages) % 10 == 0:
                    if self._detect_topic_shift(current.messages, msg):
                        should_split = True

            if should_split and len(current.messages) >= self.MIN_SEGMENT_MESSAGES:
                current.end_time = sorted_msgs[i - 1].get("timestamp", 0)
                segments.append(current)
                current = TopicSegment()
                current.start_time = ts

            current.messages.append(msg)

        # 最后一个段
        if current.messages:
            current.end_time = sorted_msgs[-1].get("timestamp", 0)
            segments.append(current)

        return segments

    def _detect_topic_shift(self, recent_msgs: List[Dict], new_msg: Dict) -> bool:
        """
        简易语义断裂检测:
        比较最近 5 条消息的高频词与新消息的重叠率。
        重叠率 < 0.15 视为话题切换。
        """
        if len(recent_msgs) < 5:
            return False

        recent_text = " ".join(
            m.get("content", "") for m in recent_msgs[-5:]
        )
        new_text = new_msg.get("content", "")

        recent_chars = set(self._tokenize(recent_text))
        new_chars = set(self._tokenize(new_text))

        if not recent_chars or not new_chars:
            return False

        overlap = len(recent_chars & new_chars)
        total = len(recent_chars | new_chars)
        similarity = overlap / total if total > 0 else 0

        return similarity < 0.15

    # ==========================================
    # 阶段二: 纯算法关键词提取 + 情感分析
    # ==========================================

    def _extract_keywords(self, segment: TopicSegment) -> List[str]:
        """基于词频的关键词提取 (无需 jieba 依赖)"""
        all_text = " ".join(m.get("content", "") for m in segment.messages)
        tokens = self._tokenize(all_text)

        # 停用词过滤
        stopwords = {
            "的", "了", "是", "在", "我", "你", "他", "她", "它", "不",
            "就", "也", "都", "有", "这", "那", "吗", "呢", "啊", "吧",
            "吃", "去", "来", "说", "看", "做", "个", "人", "什么",
            "会", "到", "把", "和", "要", "能", "好", "很", "可以",
            "一", "二", "三", "上", "下", "大", "小", "对", "多",
        }

        filtered = [t for t in tokens if t not in stopwords and len(t) >= 2]
        counter = Counter(filtered)

        # 按频率排序取 Top 5
        return [word for word, _ in counter.most_common(8)]

    def _calculate_sentiment(self, segment: TopicSegment) -> float:
        """纯算法情感分析: 基于正负面词汇出现比例"""
        pos_count = 0
        neg_count = 0
        total_len = 0

        for msg in segment.messages:
            text = msg.get("content", "").lower()
            total_len += len(text)
            for w in self.POSITIVE_WORDS:
                if w in text:
                    pos_count += 1
            for w in self.NEGATIVE_WORDS:
                if w in text:
                    neg_count += 1

        if pos_count + neg_count == 0:
            return 0.0

        # 归一化到 -1.0 ~ 1.0
        raw_score = (pos_count - neg_count) / (pos_count + neg_count)
        return max(-1.0, min(1.0, raw_score))

    def _calculate_importance(self, segment: TopicSegment) -> float:
        """纯算法重要度计算: 综合参与人数、消息密度、持续时长"""
        # 因子1: 参与人数 (2人以上越多越重要)
        participant_factor = min(1.0, len(segment.participants) / 5.0)

        # 因子2: 消息密度 (每分钟消息数)
        density = segment.message_count / max(1, segment.duration_minutes)
        density_factor = min(1.0, density / 3.0)  # 每分钟 3 条为满分

        # 因子3: 持续时长 (5-30分钟为合理范围)
        duration = segment.duration_minutes
        duration_factor = min(1.0, duration / 15.0)

        # 因子4: 情感强度 (绝对值越高越重要)
        emotion_factor = abs(segment.sentiment_score)

        # 加权融合
        importance = (
            participant_factor * 0.25 +
            density_factor * 0.30 +
            duration_factor * 0.25 +
            emotion_factor * 0.20
        )

        return round(min(1.0, importance), 3)

    @staticmethod
    def _sentiment_to_label(score: float) -> str:
        if score > 0.3:
            return "positive"
        elif score < -0.3:
            return "negative"
        return "neutral"

    # ==========================================
    # 阶段三: 单次 LLM 批量摘要
    # ==========================================

    async def _batch_summarize(self, segments: List[TopicSegment]) -> List[str]:
        """用一次 LLM 调用为所有话题段生成摘要"""
        if not self.gateway:
            # 无 gateway 时纯算法降级: 用关键词拼接
            return [
                f"讨论了{'、'.join(s.keywords[:3]) or '日常话题'}" for s in segments
            ]

        blocks = []
        for i, seg in enumerate(segments):
            text = seg.text_block
            if len(text) > 1500:
                text = text[:1500] + "..."
            blocks.append(f"[话题{i + 1}] (共{seg.message_count}条消息)\n{text}")

        combined = "\n\n---\n\n".join(blocks)

        prompt = f"""以下是从群聊中按话题分割出的 {len(segments)} 个对话段。
请为每个话题段生成一句简洁的摘要（不超过30字）。

{combined}

严格返回 JSON 数组，顺序对应各话题段:
["话题1摘要", "话题2摘要", ...]"""

        try:
            result = await self.gateway.call_data_process_task(prompt, is_json=True)
            summaries = self._parse_summaries(result, len(segments))
            return summaries
        except Exception as e:
            logger.warning(f"[TopicSummarizer] LLM 摘要失败，降级为关键词拼接: {e}")
            return [
                f"讨论了{'、'.join(s.keywords[:3]) or '日常话题'}" for s in segments
            ]

    @staticmethod
    def _parse_summaries(raw, expected_count: int) -> List[str]:
        """安全解析摘要数组"""
        import json
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str):
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                try:
                    items = json.loads(match.group(0))
                except Exception:
                    pass

        # 确保数量对齐
        result = [str(s)[:50] for s in items if isinstance(s, str)]
        while len(result) < expected_count:
            result.append("话题内容较短")
        return result[:expected_count]

    # ==========================================
    # 工具函数
    # ==========================================

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """极简分词: 按标点/空格切分 + 2-gram"""
        # 清洗
        clean = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', ' ', text)
        words = clean.split()

        # 对中文生成 2-gram
        result = []
        for w in words:
            if len(w) <= 4 and len(w) >= 2:
                result.append(w)
            elif len(w) > 4:
                # 滑动窗口 2-gram
                for i in range(len(w) - 1):
                    result.append(w[i:i + 2])
            # 单字跳过

        return result
