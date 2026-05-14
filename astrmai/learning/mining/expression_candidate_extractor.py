from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


class ExpressionCandidateExtractor:
    NOISE_MESSAGES = {
        "收到",
        "好的",
        "ok",
        "okk",
        "嗯",
        "哈",
        "哈哈",
        "哈哈哈",
        "6",
    }

    def __init__(self, *, min_count: int = 2):
        self.min_count = max(int(min_count or 2), 1)

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().lower())

    @classmethod
    def _looks_noise(cls, text: str) -> bool:
        cleaned = cls._clean_text(text)
        if not cleaned:
            return True
        lowered = cleaned.lower()
        if lowered in cls.NOISE_MESSAGES:
            return True
        if cleaned.startswith("[") or len(cleaned) > 60:
            return True
        if re.fullmatch(r"[0-9\s\W_]+", cleaned):
            return True
        return False

    @classmethod
    def _infer_situation(cls, text: str) -> str:
        cleaned = cls._clean_text(text)
        if "?" in cleaned or "？" in cleaned:
            return "追问确认"
        if any(token in cleaned for token in ("哈哈", "笑死", "乐", "233")):
            return "轻松玩笑"
        if any(token in cleaned for token in ("行", "好", "收到", "ok", "安排")):
            return "简短接话"
        if any(token in cleaned for token in ("我觉得", "我感觉", "我看", "我还挺")):
            return "表达态度"
        if any(token in cleaned for token in ("别", "不要", "先", "等下")):
            return "提醒收束"
        return "日常回应"

    @classmethod
    def _infer_style(cls, text: str) -> str:
        cleaned = cls._clean_text(text)
        if any(token in cleaned for token in ("哈哈", "笑死", "乐", "233")):
            return "轻松"
        if cleaned.endswith(("。", "！", "!")):
            return "直接"
        if "?" in cleaned or "？" in cleaned:
            return "追问"
        if len(cleaned) <= 8:
            return "短句"
        return "自然"

    @staticmethod
    def _near_duplicate(text: str, existing: set[str]) -> bool:
        normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
        if not normalized:
            return True
        for item in existing:
            if normalized == item:
                return True
            if normalized in item or item in normalized:
                if abs(len(normalized) - len(item)) <= 3:
                    return True
        return False

    async def extract(
        self,
        group_id: str,
        messages: list[Any],
        *,
        existing_patterns: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        samples: dict[str, list[str]] = defaultdict(list)
        situations: dict[str, str] = {}
        styles: dict[str, str] = {}
        existing = {self._normalize_text(item) for item in (existing_patterns or set()) if self._normalize_text(item)}

        for message in messages or []:
            content = self._clean_text(getattr(message, "content", ""))
            if self._looks_noise(content):
                continue
            normalized = self._normalize_text(content)
            if self._near_duplicate(content, existing):
                continue
            counts[normalized] += 1
            if len(samples[normalized]) < 4:
                samples[normalized].append(content[:160])
            situations.setdefault(normalized, self._infer_situation(content))
            styles.setdefault(normalized, self._infer_style(content))

        candidates: list[dict[str, Any]] = []
        for normalized, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            if count < self.min_count:
                continue
            content_samples = list(samples.get(normalized, []))
            expression = content_samples[0] if content_samples else normalized
            activation_score = min(1.0, 0.4 + count * 0.18)
            candidates.append(
                {
                    "group_id": group_id,
                    "expression": expression,
                    "normalized_expression": normalized,
                    "situation": situations.get(normalized, "日常回应"),
                    "style": styles.get(normalized, "自然"),
                    "content_samples": content_samples,
                    "count": count,
                    "activation_score": activation_score,
                    "think_level": 1 if len(expression) >= 10 else 0,
                }
            )
            existing.add(normalized)
        return candidates[:12]


__all__ = ["ExpressionCandidateExtractor"]
