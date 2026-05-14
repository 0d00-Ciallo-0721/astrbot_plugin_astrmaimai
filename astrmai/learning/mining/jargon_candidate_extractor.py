from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


class JargonCandidateExtractor:
    NOISE_TOKENS = {
        "哈哈",
        "哈哈哈",
        "卧槽",
        "牛逼",
        "好的",
        "收到",
        "可以",
        "然后",
        "这个",
        "那个",
    }

    def __init__(self, *, min_count: int = 2):
        self.min_count = max(int(min_count or 2), 1)

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _normalize_token(token: str) -> str:
        return str(token or "").strip().lower()

    @classmethod
    def _tokens(cls, text: str) -> list[str]:
        cleaned = cls._clean_text(text)
        if not cleaned:
            return []
        matches = re.findall(r"[A-Za-z0-9_]{2,24}|[\u4e00-\u9fff]{2,8}", cleaned)
        return [cls._normalize_token(token) for token in matches if cls._normalize_token(token)]

    @classmethod
    def _looks_noise(cls, token: str) -> bool:
        if not token or token in cls.NOISE_TOKENS:
            return True
        if token.isdigit():
            return True
        if len(token) <= 1:
            return True
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 6:
            return True
        return False

    @staticmethod
    def _near_duplicate(token: str, existing: set[str]) -> bool:
        for item in existing:
            if token == item:
                return True
            if token in item or item in token:
                if abs(len(token) - len(item)) <= 2:
                    return True
        return False

    async def extract(
        self,
        group_id: str,
        messages: list[Any],
        *,
        existing_terms: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        contexts: dict[str, list[str]] = defaultdict(list)
        existing = {self._normalize_token(item) for item in (existing_terms or set()) if self._normalize_token(item)}
        for message in messages or []:
            content = self._clean_text(getattr(message, "content", ""))
            if not content:
                continue
            for token in self._tokens(content):
                if self._looks_noise(token) or self._near_duplicate(token, existing):
                    continue
                counts[token] += 1
                if len(contexts[token]) < 4:
                    contexts[token].append(content[:160])
        candidates: list[dict[str, Any]] = []
        for token, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            if count < self.min_count:
                continue
            examples = contexts.get(token, [])
            activation = min(1.0, 0.45 + count * 0.15)
            candidates.append(
                {
                    "content": token,
                    "raw_content": examples[0] if examples else token,
                    "count": count,
                    "activation_score": activation,
                    "examples": list(examples),
                    "group_id": group_id,
                }
            )
            existing.add(token)
        return candidates[:12]


__all__ = ["JargonCandidateExtractor"]
