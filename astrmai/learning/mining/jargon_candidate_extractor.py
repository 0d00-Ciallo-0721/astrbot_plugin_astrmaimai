from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ..dedup import normalize_jargon_term
from .candidate_router import LearningCandidateRouter
from .learning_evidence import build_evidence_bundle


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
        "什么",
        "不是",
        "没有",
        "怎么",
        "方法",
        "不需要",
        "本人照片",
        "点评",
        "财运",
        "事业运",
        "桃花运",
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
        return normalize_jargon_term(token)

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
        if re.fullmatch(r"(?:[0-9a-f]{2}){1,4}(?:version)?", token):
            return "URL 编码或十六进制碎片"
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
        context_keys: dict[str, set[str]] = defaultdict(set)
        sender_keys: dict[str, set[str]] = defaultdict(set)
        evidence_indexes: dict[str, list[int]] = defaultdict(list)
        existing = {self._normalize_token(item) for item in (existing_terms or set()) if self._normalize_token(item)}
        sender_tokens: set[str] = set()
        for message in messages or []:
            sender_name = self._normalize_token(getattr(message, "sender_name", ""))
            if sender_name:
                sender_tokens.add(sender_name)
                sender_tokens.update(self._tokens(sender_name))
        accepted_tokens = 0
        skipped_noise = 0
        routed_to_expression = 0
        quality_filtered = 0
        route_reasons: dict[str, int] = defaultdict(int)
        for message_index, message in enumerate(messages or []):
            content = self._clean_text(getattr(message, "content", ""))
            if not content:
                continue
            message_key = str(getattr(message, "id", "") or f"row:{message_index}")
            sender_key = str(getattr(message, "sender_id", "") or getattr(message, "sender_name", "") or "")
            for token in set(self._tokens(content)):
                if self._looks_noise(token, sender_tokens) or self._near_duplicate(token, existing):
                    skipped_noise += 1
                    continue
                route = LearningCandidateRouter.classify(token)
                route_reasons[route.reason] += 1
                if route.target == "expression":
                    routed_to_expression += 1
                    continue
                if route.target != "jargon":
                    quality_filtered += 1
                    continue
                accepted_tokens += 1
                counts[token] += 1
                context_keys[token].add(message_key)
                evidence_indexes[token].append(message_index)
                if sender_key:
                    sender_keys[token].add(sender_key)
                if len(contexts[token]) < 4:
                    contexts[token].append(content[:160])
        candidates: list[dict[str, Any]] = []
        for token, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            if count < self.min_count:
                continue
            if len(context_keys[token]) < self.min_count:
                continue
            examples = contexts.get(token, [])
            activation = min(1.0, 0.45 + count * 0.15)
            payload = {
                    "content": token,
                    "raw_content": examples[0] if examples else token,
                    "count": count,
                    "context_count": len(context_keys[token]),
                    "speaker_count": len(sender_keys[token]),
                    "activation_score": activation,
                    "examples": list(examples),
                    "group_id": group_id,
                    "candidate_origin": "human_group_text",
                    "classification": "jargon",
                    "classification_reason": "semantic_candidate",
                    "quality_tier": "high" if count >= 3 and len(sender_keys[token]) >= 2 else "medium",
                }
            payload.update(
                build_evidence_bundle(
                    group_id=group_id,
                    messages=list(messages or []),
                    matched_indexes=evidence_indexes.get(token, []),
                    source_examples=examples,
                )
            )
            candidates.append(payload)
            existing.add(token)
        self.last_report = {
            "input_messages": len(messages or []),
            "accepted_tokens": accepted_tokens,
            "skipped_noise": skipped_noise,
            "routed_to_expression": routed_to_expression,
            "quality_filtered": quality_filtered,
            "route_reasons": dict(sorted(route_reasons.items())),
            "candidate_count": len(candidates),
            "reason": "candidates_ready" if candidates else "no_repeated_jargon",
        }
        return candidates[:12]


__all__ = ["JargonCandidateExtractor"]
