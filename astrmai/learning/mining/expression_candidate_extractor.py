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
    PHRASE_NOISE = {
        "什么情况",
        "怎么回事",
        "这个东西",
        "那个东西",
        "你觉得呢",
        "我不知道",
    }

    def __init__(self, *, min_count: int = 2):
        self.min_count = max(int(min_count or 2), 1)
        self.last_report: dict[str, Any] = {}

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
    def _phrase_fragments(cls, text: str) -> set[str]:
        fragments: set[str] = set()
        cleaned = cls._clean_text(text)
        for run in re.findall(r"[\u4e00-\u9fff]{3,24}", cleaned):
            max_size = min(12, len(run))
            for size in range(max_size, 2, -1):
                for start in range(0, len(run) - size + 1):
                    phrase = run[start : start + size]
                    if phrase not in cls.PHRASE_NOISE:
                        fragments.add(phrase)
        words = re.findall(r"[A-Za-z][A-Za-z0-9']{1,23}", cleaned.lower())
        for size in range(min(5, len(words)), 1, -1):
            for start in range(0, len(words) - size + 1):
                fragments.add(" ".join(words[start : start + size]))
        return fragments

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
        phrase_counts: dict[str, int] = defaultdict(int)
        phrase_samples: dict[str, list[str]] = defaultdict(list)
        accepted_messages = 0
        skipped_noise = 0
        skipped_existing = 0

        for message in messages or []:
            content = self._clean_text(getattr(message, "content", ""))
            if self._looks_noise(content):
                skipped_noise += 1
                continue
            normalized = self._normalize_text(content)
            if self._near_duplicate(content, existing):
                skipped_existing += 1
                continue
            accepted_messages += 1
            counts[normalized] += 1
            if len(samples[normalized]) < 4:
                samples[normalized].append(content[:160])
            situations.setdefault(normalized, self._infer_situation(content))
            styles.setdefault(normalized, self._infer_style(content))
            for phrase in self._phrase_fragments(content):
                normalized_phrase = self._normalize_text(phrase)
                if self._near_duplicate(normalized_phrase, existing):
                    continue
                phrase_counts[normalized_phrase] += 1
                if len(phrase_samples[normalized_phrase]) < 4:
                    phrase_samples[normalized_phrase].append(content[:160])

        candidates: list[dict[str, Any]] = []
        qualifying_exact: list[tuple[str, int]] = []
        for normalized, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
            if count < self.min_count:
                continue
            qualifying_exact.append((normalized, count))
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
                    "candidate_type": "exact",
                }
            )
            existing.add(normalized)

        selected_phrases: list[tuple[str, int]] = []
        for phrase, count in sorted(phrase_counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0])):
            if count < self.min_count:
                continue
            if any(phrase in selected and selected_count == count for selected, selected_count in selected_phrases):
                continue
            selected_phrases.append((phrase, count))
            if phrase in existing:
                continue
            if any(phrase in exact and exact_count == count for exact, exact_count in qualifying_exact):
                continue
            content_samples = list(phrase_samples.get(phrase, []))
            expression = phrase
            candidates.append(
                {
                    "group_id": group_id,
                    "expression": expression,
                    "normalized_expression": phrase,
                    "situation": self._infer_situation(content_samples[0] if content_samples else phrase),
                    "style": self._infer_style(content_samples[0] if content_samples else phrase),
                    "content_samples": content_samples,
                    "count": count,
                    "activation_score": min(1.0, 0.35 + count * 0.16),
                    "think_level": 0,
                    "candidate_type": "phrase",
                }
            )
            existing.add(phrase)

        candidates.sort(key=lambda item: (-int(item.get("count", 0)), item.get("candidate_type") != "exact", -len(str(item.get("expression", "")))))
        self.last_report = {
            "input_messages": len(messages or []),
            "accepted_messages": accepted_messages,
            "skipped_noise": skipped_noise,
            "skipped_existing": skipped_existing,
            "exact_candidates": len(qualifying_exact),
            "phrase_candidates": sum(1 for item in candidates if item.get("candidate_type") == "phrase"),
            "candidate_count": len(candidates),
            "reason": "candidates_ready" if candidates else "no_repeated_expression",
        }
        return candidates[:12]


__all__ = ["ExpressionCandidateExtractor"]
