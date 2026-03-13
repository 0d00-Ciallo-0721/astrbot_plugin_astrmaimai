import re
from typing import List, Dict, Optional, Any, Tuple
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

class AffectionRouter:
    """
    情绪归因路由器 (System 1 - Subconscious Attribution)
    职责: 纯算力驱动的启发式统计算法。判定多用户群聊中，真正导致 AI 情绪起伏的“情绪引导人”。
    特点: 无状态 (Stateless)、零 IO、纳秒级防抖。
    """

    @staticmethod
    def _extract_info(event: Any) -> Tuple[str, str]:
        """
        多态提取器：兼容 AstrMessageEvent 与原生历史记录 Dict/Object
        """
        if not event:
            return "", ""
            
        sender_id = ""
        text = ""
        
        # 1. 如果是 AstrMessageEvent (如 W 窗口期事件)
        if isinstance(event, AstrMessageEvent):
            sender_id = event.get_sender_id()
            text = event.message_str
        # 2. 如果是字典格式 (如 H 历史记录的某些平台底层实现)
        elif isinstance(event, dict):
            sender_id = str(event.get("sender_id", event.get("user_id", "")))
            
            # 兼容富文本数组与纯文本
            content = event.get("content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join([c.get("text", "") for c in content if c.get("type") == "text"])
        # 3. 如果是普通对象 (带属性)
        else:
            sender_id = str(getattr(event, "sender_id", getattr(event, "user_id", "")))
            
            content = getattr(event, "content", "")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join([getattr(c, "text", "") for c in content if getattr(c, "type", "") == "text"])
                
        return sender_id.strip(), text.strip()

    @staticmethod
    def _calculate_mqs(text: str) -> float:
        """
        步骤 1: 基础防抖计算 —— 消息质量得分 (MQS)
        """
        length = len(text)
        if length == 0:
            return 0.0
        if length <= 5:
            return 0.5   # 短碎词
        if length <= 30:
            return 1.0   # 正常交流
        if length <= 150:
            return 1.5   # 高密度输出
        return 0.1       # 疑似刷屏断崖式下跌

    @staticmethod
    def _get_decay_factor(index: int, total_len: int) -> float:
        """
        步骤 2: 时间衰减因子 (Decay Factor)
        越靠近末尾 (最新)，权重越高。
        """
        if total_len <= 1:
            return 1.0
        return 0.2 + 0.8 * (index / (total_len - 1))

    @classmethod
    def _calculate_normalized_scores(cls, events: List[Any], max_weight: float) -> Dict[str, float]:
        """
        步骤 3: 动态归一化分配 (Normalization)
        """
        raw_scores = {}
        total_raw = 0.0
        total_len = len(events)

        for i, event in enumerate(events):
            sender_id, text = cls._extract_info(event)
            if not sender_id or not text:
                continue

            mqs = cls._calculate_mqs(text)
            df = cls._get_decay_factor(i, total_len)
            score = mqs * df

            raw_scores[sender_id] = raw_scores.get(sender_id, 0.0) + score
            total_raw += score

        # 防御除零错误 (如果全是空消息)
        if total_raw <= 0:
            return {}

        return {uid: (raw / total_raw) * max_weight for uid, raw in raw_scores.items()}

    @classmethod
    def route(cls, 
              history_events: List[Any], 
              window_events: List[Any], 
              trigger_event: Any, 
              mood_tag: str, 
              config: Any) -> Optional[str]:
        """
        主干路由逻辑: 融合 H, W, T 并执行裁决
        """
        # --- 提取配置参数 (具备默认兜底) ---
        attention_cfg = getattr(config, 'attention', None)
        
        weights = getattr(attention_cfg, 'affection_weights', {"trigger": 20.0, "window": 50.0, "history": 30.0})
        w_weight = weights.get("window", 50.0)
        h_weight = weights.get("history", 30.0)
        t_weight = weights.get("trigger", 20.0)
        
        threshold = getattr(attention_cfg, 'adjudication_threshold', 50.0)
        sensitive_words = getattr(attention_cfg, 'sensitive_words', ["傻逼", "弱智", "滚", "死", "妈", "废物", "神经", "有病"])

        # --- 步骤 3: 计算 W 与 H 的归一化分布 ---
        w_scores = cls._calculate_normalized_scores(window_events, w_weight)
        h_scores = cls._calculate_normalized_scores(history_events, h_weight)

        # --- 步骤 4: 破窗者特权与沉默刺客防线 ---
        t_sender_id, t_text = cls._extract_info(trigger_event)
        t_score = 0.0
        
        if t_sender_id:
            t_score = t_weight
            # 极值判定：只有生气或破防，且命中了敏感词汇，瞬间拉满仇恨
            if mood_tag in ['angry', 'sad']:
                if any(word in t_text for word in sensitive_words):
                    logger.warning(f"[AffectionRouter] 🗡️ 触发沉默刺客防线！用户 {t_sender_id} 的言论被判定为恶性破窗。")
                    t_score = 80.0

        # --- 步骤 5: 最终聚合与裁决 ---
        total_scores = {}
        all_users = set(list(w_scores.keys()) + list(h_scores.keys()) + ([t_sender_id] if t_sender_id else []))

        for uid in all_users:
            score = w_scores.get(uid, 0.0) + h_scores.get(uid, 0.0)
            if uid == t_sender_id:
                score += t_score
            total_scores[uid] = score

        if not total_scores:
            logger.debug("[AffectionRouter] 空白对局，无法计算有效权重。")
            return None

        # 找出分数最高的用户
        winner_id, max_score = max(total_scores.items(), key=lambda x: x[1])
        
        logger.debug(f"[AffectionRouter] 结算榜单: {total_scores} | 最高分: {winner_id}({max_score:.1f}) | 阈值: {threshold}")

        # 及格线判定
        if max_score > threshold:
            logger.info(f"[AffectionRouter] 🎯 锁定情绪引导人: {winner_id}，得分 {max_score:.1f}/{w_weight+h_weight+max(t_weight, 80.0)}")
            return winner_id
        else:
            logger.info(f"[AffectionRouter] 🤷‍♂️ 流局 (Draw): 最高分 {max_score:.1f} 未达及格线 {threshold}。群内过于混乱，放弃好感度结算。")
            return None