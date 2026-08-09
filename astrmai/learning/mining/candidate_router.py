from __future__ import annotations

import re
from dataclasses import dataclass

from ..dedup import normalize_jargon_term


@dataclass(frozen=True, slots=True)
class CandidateRoute:
    target: str
    reason: str
    quality_tier: str


class LearningCandidateRouter:
    """Deterministically separates speaking habits from semantic jargon."""

    EXPRESSION_MARKERS = {
        "的说",
        "hiyohiyo",
        "唉嘿嘿",
        "欸嘿嘿",
        "诶嘿嘿",
        "啊呜",
        "呜哇",
    }
    INTERACTION_WORDS = {"摸摸", "抱抱", "亲亲", "贴贴", "妈妈", "哥哥", "妹妹", "欧尼酱"}
    COMMON_WORDS = {
        "这个",
        "那个",
        "就是",
        "不是",
        "没有",
        "什么",
        "怎么",
        "方法",
        "群聊",
        "图片",
        "照片",
    }

    @classmethod
    def classify(cls, value: str) -> CandidateRoute:
        term = normalize_jargon_term(value)
        if not term:
            return CandidateRoute("reject", "empty_candidate", "invalid")
        if term in cls.EXPRESSION_MARKERS or term.endswith("的说"):
            return CandidateRoute("expression", "speaking_habit_marker", "high")
        if term in cls.INTERACTION_WORDS:
            return CandidateRoute("reject", "ordinary_interaction_word", "low")
        if term in cls.COMMON_WORDS:
            return CandidateRoute("reject", "ordinary_vocabulary", "low")
        if re.fullmatch(r"(?:啦|呀|呢|哦|嘛|哒|捏|呐|喵|诶)", term):
            return CandidateRoute("expression", "sentence_ending", "high")
        if re.search(r"[~～♡☆♪]|(.)\1{2,}", term):
            return CandidateRoute("expression", "symbol_or_rhythm_habit", "medium")
        return CandidateRoute("jargon", "semantic_candidate", "medium")


__all__ = ["CandidateRoute", "LearningCandidateRouter"]
