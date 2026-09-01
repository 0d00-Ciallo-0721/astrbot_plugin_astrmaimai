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
    def _candidate_spans(cls, text: str) -> list[dict[str, Any]]:
        cleaned = cls._clean_text(text)
        if not cleaned:
            return []
        spans: list[dict[str, Any]] = []
        seen: set[tuple[str, int, int]] = set()

        def add(term: str, start: int, end: int, *, meaning_hint: str = "", explicit: bool = False) -> None:
            normalized = cls._normalize_token(term.strip(" \t\r\n，。！？；：:,.!?;‘’“”\"'「」『』（）()[]【】"))
            if not normalized or len(normalized) > 60:
                return
            key = (normalized, max(start, 0), max(end, start))
            if key in seen:
                return
            seen.add(key)
            spans.append(
                {
                    "term": normalized,
                    "start": max(start, 0),
                    "end": max(end, start),
                    "text": term.strip()[:60],
                    "meaning_hint": meaning_hint.strip()[:160],
                    "explicit_definition": explicit,
                }
            )

        definition_pattern = re.compile(
            r"(?:^|[，。！？；;\s])(?P<term>[A-Za-z0-9_\u4e00-\u9fff·~～]{2,60}?)"
            r"(?:这个词)?(?:意思是|指的是|就是|＝|=)(?P<meaning>[^，。！？；;\n]{2,120})",
            re.IGNORECASE,
        )
        for match in definition_pattern.finditer(cleaned):
            add(
                match.group("term"),
                match.start("term"),
                match.end("term"),
                meaning_hint=match.group("meaning"),
                explicit=True,
            )
        for match in re.finditer(r"[「『“\"]([^「」『』“”\"\n]{2,60})[」』”\"]", cleaned):
            add(match.group(1), match.start(1), match.end(1))
        for match in re.finditer(r"[A-Za-z0-9_]{2,24}", cleaned):
            add(match.group(0), match.start(), match.end())
        for match in re.finditer(r"[^，。！？；;\s]{2,24}", cleaned):
            clause = match.group(0)
            if re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_·~～]+", clause):
                add(clause, match.start(), match.end())
        return spans

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
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 24:
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
        existing_terms: set[str] | dict[str, str] | None = None,
        blocked_terms: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        contexts: dict[str, list[str]] = defaultdict(list)
        context_keys: dict[str, set[str]] = defaultdict(set)
        sender_keys: dict[str, set[str]] = defaultdict(set)
        evidence_indexes: dict[str, list[int]] = defaultdict(list)
        source_spans: dict[str, list[dict[str, Any]]] = defaultdict(list)
        definition_hints: dict[str, list[str]] = defaultdict(list)
        explicit_definitions: dict[str, bool] = defaultdict(bool)
        canonical_terms: dict[str, str] = {}
        if isinstance(existing_terms, dict):
            canonical_terms = {
                self._normalize_token(key): str(value or key).strip()
                for key, value in existing_terms.items()
                if self._normalize_token(key)
            }
        else:
            canonical_terms = {
                self._normalize_token(item): str(item).strip()
                for item in (existing_terms or set())
                if self._normalize_token(item)
            }
        blocked = {self._normalize_token(item) for item in (blocked_terms or set()) if self._normalize_token(item)}
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
            for span in self._candidate_spans(content):
                token = str(span["term"])
                if self._looks_noise(token, sender_tokens) or self._near_duplicate(token, blocked):
                    skipped_noise += 1
                    continue
                # Existing canonical terms and aliases remain evidence only; do
                # not send them through enrichment again on checkpoint overlap.
                if self._normalize_token(token) in canonical_terms:
                    quality_filtered += 1
                    route_reasons["existing_term"] += 1
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
                source_spans[token].append(
                    {
                        "message_id": message_key,
                        "start": span["start"],
                        "end": span["end"],
                        "text": span["text"],
                    }
                )
                if span.get("meaning_hint"):
                    definition_hints[token].append(str(span["meaning_hint"]))
                explicit_definitions[token] = explicit_definitions[token] or bool(span.get("explicit_definition"))
                if sender_key:
                    sender_keys[token].add(sender_key)
                if len(contexts[token]) < 4:
                    contexts[token].append(content[:160])
        candidates: list[dict[str, Any]] = []
        for token, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            if count < self.min_count and not explicit_definitions[token]:
                continue
            if len(context_keys[token]) < self.min_count and not explicit_definitions[token]:
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
                    "canonical_form": canonical_terms.get(token, token),
                    "existing_identity": token in canonical_terms,
                    "explicit_definition": explicit_definitions[token],
                    "definition_hints": list(dict.fromkeys(definition_hints[token]))[:4],
                }
            payload.update(
                build_evidence_bundle(
                    group_id=group_id,
                    messages=list(messages or []),
                    matched_indexes=evidence_indexes.get(token, []),
                    source_examples=examples,
                    source_spans=source_spans.get(token, []),
                )
            )
            candidates.append(payload)
            blocked.add(token)
        self.last_report = {
            "input_messages": len(messages or []),
            "accepted_tokens": accepted_tokens,
            "skipped_noise": skipped_noise,
            "routed_to_expression": routed_to_expression,
            "quality_filtered": quality_filtered,
            "route_reasons": dict(sorted(route_reasons.items())),
            "candidate_count": len(candidates),
            "explicit_definition_candidates": sum(bool(item.get("explicit_definition")) for item in candidates),
            "existing_identity_candidates": sum(bool(item.get("existing_identity")) for item in candidates),
            "reason": "candidates_ready" if candidates else "no_repeated_jargon",
        }
        return candidates[:12]


__all__ = ["JargonCandidateExtractor"]
