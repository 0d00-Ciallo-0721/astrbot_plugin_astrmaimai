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
        "at_type",
        "at_tinyid",
        "astrbot",
        "groupmessage",
        "friendmessage",
        "message_id",
        "sender_id",
        "user_id",
        "target_id",
        "base64",
        "image",
        "file",
        "json",
        "http",
        "https",
        "jpeg",
        "webp",
        "png",
    }

    def __init__(self, *, min_count: int = 2):
        self.min_count = max(int(min_count or 2), 1)
        self.last_report: dict[str, Any] = {}

    @staticmethod
    def _clean_text(value: Any) -> str:
        text = str(value or "").strip()
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\[(?:At|CQ|Image|图片)[^\]]*\]", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"https?://\S+", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"(^|\s)/[\w\u4e00-\u9fff-]+", " ", text)
        return " ".join(text.split())

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
    def _looks_noise(cls, token: str, sender_tokens: set[str] | None = None) -> bool:
        return bool(cls.noise_reason(token, sender_tokens))

    @classmethod
    def noise_reason(cls, token: str, sender_tokens: set[str] | None = None) -> str:
        if not token or token in cls.NOISE_TOKENS:
            return "内置普通词或协议噪声"
        if token in (sender_tokens or set()):
            return "发送者昵称"
        if token.isdigit():
            return "纯数字"
        if "_" in token or token.startswith(("cq", "http", "www")):
            return "协议或字段标识"
        if re.fullmatch(r"\d+(?:px|kb|mb|gb)", token):
            return "尺寸或容量标识"
        if len(token) <= 1:
            return "长度不足"
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 6:
            return "过长普通中文片段"
        return ""

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
        sender_tokens: set[str] = set()
        for message in messages or []:
            sender_name = self._normalize_token(getattr(message, "sender_name", ""))
            if sender_name:
                sender_tokens.add(sender_name)
                sender_tokens.update(self._tokens(sender_name))
        accepted_tokens = 0
        skipped_noise = 0
        for message in messages or []:
            content = self._clean_text(getattr(message, "content", ""))
            if not content:
                continue
            for token in self._tokens(content):
                if self._looks_noise(token, sender_tokens) or self._near_duplicate(token, existing):
                    skipped_noise += 1
                    continue
                accepted_tokens += 1
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
        self.last_report = {
            "input_messages": len(messages or []),
            "accepted_tokens": accepted_tokens,
            "skipped_noise": skipped_noise,
            "candidate_count": len(candidates),
            "reason": "candidates_ready" if candidates else "no_repeated_jargon",
        }
        return candidates[:12]


__all__ = ["JargonCandidateExtractor"]
