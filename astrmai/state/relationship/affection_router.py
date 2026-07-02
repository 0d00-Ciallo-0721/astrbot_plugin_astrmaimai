from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger

try:
    from astrbot.api.event import AstrMessageEvent
except ImportError:  # pragma: no cover
    AstrMessageEvent = object  # type: ignore[misc,assignment]


class AffectionRouter:
    """Route affection settlement signals to the main relationship engine."""

    def __init__(self, relationship_engine=None, event_bus=None):
        self.relationship_engine = relationship_engine
        self.event_bus = event_bus

    async def publish_change(
        self,
        user_id: str,
        old_score: float,
        new_score: float,
        mood_tag: str,
        event_type: str,
    ) -> None:
        if abs(new_score - old_score) > 0.1:
            logger.info(
                f"[StateEngine] affection updated user={user_id} {old_score:.1f} -> {new_score:.1f}"
            )
            if hasattr(self.event_bus, "trigger_affection_change"):
                await self.event_bus.trigger_affection_change()
            return

        if not self.relationship_engine:
            return
        vec = self.relationship_engine.get_or_create(user_id)
        logger.debug(
            f"[StateEngine] affection settled user={user_id} mood={mood_tag} event={event_type} "
            f"trust={vec.trust:.1f} fam={vec.familiarity:.1f} emo={vec.emotion_bond:.1f} "
            f"resp={vec.respect:.1f} score={new_score:.1f}"
        )

    @staticmethod
    def _extract_info(event: Any, fallback_uid: str = "") -> Tuple[str, str]:
        if not event:
            return "", ""

        sender_id = ""
        text = ""
        if isinstance(event, AstrMessageEvent):
            raw_id = event.get_sender_id()
            sender_id = str(raw_id).strip() if raw_id is not None else ""
            text = event.message_str
        elif isinstance(event, dict):
            raw_id = event.get("sender_id")
            if raw_id is None:
                raw_id = event.get("user_id")
            sender_id = str(raw_id).strip() if raw_id is not None else ""
            if sender_id == "None":
                sender_id = ""
            role = event.get("role", "")
            if not sender_id and (role == "user" or fallback_uid):
                if role not in ["assistant", "system", "bot"]:
                    sender_id = fallback_uid
            content = event.get("content", event.get("message", ""))
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(
                    [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and (c.get("type") == "text" or "text" in c)
                    ]
                )
        else:
            raw_id = getattr(event, "sender_id", getattr(event, "user_id", None))
            sender_id = str(raw_id).strip() if raw_id is not None else ""
            if sender_id == "None":
                sender_id = ""
            role = getattr(event, "role", "")
            if not sender_id and (role == "user" or fallback_uid):
                if role not in ["assistant", "system", "bot"]:
                    sender_id = fallback_uid
            content = getattr(event, "content", getattr(event, "message", getattr(event, "message_str", "")))
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join([getattr(c, "text", "") for c in content if hasattr(c, "text")])

        return str(sender_id).strip(), str(text).strip()

    @staticmethod
    def _calculate_mqs(text: str) -> float:
        length = len(text)
        if length == 0:
            return 0.0
        if length <= 5:
            return 0.5
        if length <= 30:
            return 1.0
        if length <= 150:
            return 1.5
        return 0.1

    @staticmethod
    def _get_decay_factor(index: int, total_len: int) -> float:
        if total_len <= 1:
            return 1.0
        return 0.2 + 0.8 * (index / (total_len - 1))

    @classmethod
    def _calculate_normalized_scores(cls, events: List[Any], max_weight: float, fallback_uid: str = "") -> Dict[str, float]:
        raw_scores: Dict[str, float] = {}
        total_raw = 0.0
        total_len = len(events)
        for i, event in enumerate(events):
            sender_id, text = cls._extract_info(event, fallback_uid=fallback_uid)
            if not sender_id or not text:
                continue
            mqs = cls._calculate_mqs(text)
            df = cls._get_decay_factor(i, total_len)
            score = mqs * df
            raw_scores[sender_id] = raw_scores.get(sender_id, 0.0) + score
            total_raw += score
        if total_raw <= 0:
            return {}
        return {uid: (raw / total_raw) * max_weight for uid, raw in raw_scores.items()}

    @classmethod
    def route(
        cls,
        history_events: List[Any],
        window_events: List[Any],
        trigger_event: Any,
        mood_tag: str,
        config: Any,
        fallback_uid: str = "",
    ) -> Optional[str]:
        attention_cfg = getattr(config, "attention", None)
        weights = getattr(attention_cfg, "affection_weights", {"trigger": 20.0, "window": 50.0, "history": 30.0})
        w_weight = weights.get("window", 50.0)
        h_weight = weights.get("history", 30.0)
        t_weight = weights.get("trigger", 20.0)
        threshold = getattr(attention_cfg, "adjudication_threshold", 50.0)
        sensitive_words = getattr(
            attention_cfg,
            "sensitive_words",
            ["傻逼", "弱智", "滚", "死", "妈", "废物", "神经", "有病"],
        )

        w_scores = cls._calculate_normalized_scores(window_events, w_weight, fallback_uid=fallback_uid)
        h_scores = cls._calculate_normalized_scores(history_events, h_weight, fallback_uid=fallback_uid)

        t_sender_id, t_text = cls._extract_info(trigger_event, fallback_uid=fallback_uid)
        t_score = 0.0
        if t_sender_id:
            t_score = t_weight
            if mood_tag in ["angry", "sad"] and any(word in t_text for word in sensitive_words):
                # affection boost for hostile messages (NOT a safety filter — does not block messages)
                logger.warning(f"[AffectionRouter] trigger hostile-message affection boost by user {t_sender_id}")
                t_score = 80.0

        total_scores: Dict[str, float] = {}
        all_users = set(list(w_scores.keys()) + list(h_scores.keys()) + ([t_sender_id] if t_sender_id else []))
        if len(all_users) == 1:
            threshold = 0.0

        for uid in all_users:
            score = w_scores.get(uid, 0.0) + h_scores.get(uid, 0.0)
            if uid == t_sender_id:
                score += t_score
            total_scores[uid] = score

        if not total_scores:
            logger.debug("[AffectionRouter] empty adjudication table")
            return None

        winner_id, max_score = max(total_scores.items(), key=lambda item: item[1])
        max_possible_score = w_weight + h_weight + (t_score if t_score > t_weight else t_weight)
        logger.debug(
            f"[AffectionRouter] leaderboard={total_scores} winner={winner_id}({max_score:.1f}) threshold={threshold}"
        )
        if max_score > threshold:
            logger.info(
                f"[AffectionRouter] lock emotional guide user={winner_id} score={max_score:.1f}/{max_possible_score:.1f}"
            )
            return winner_id
        logger.info(f"[AffectionRouter] draw highest={max_score:.1f} threshold={threshold}")
        return None


__all__ = ["AffectionRouter"]
