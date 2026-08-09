from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime
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
    EXPRESSION_MARKERS = (
        "唉嘿嘿",
        "hiyohiyo",
        "哈哈",
        "嘿嘿",
        "哼哼",
        "呜呜",
        "啊呜",
        "诶",
        "欸",
        "哎呀",
        "救命",
        "好家伙",
        "难绷",
        "绷不住",
        "麻了",
        "寄了",
        "绝了",
        "离谱",
        "牛哇",
        "卧槽",
        "的说",
    )
    ENDING_PARTICLES = ("啦", "呀", "呢", "哦", "嘛", "哒", "捏", "呐", "喵", "诶")

    def __init__(self, *, min_count: int = 2):
        self.min_count = max(int(min_count or 2), 1)
        self.last_report: dict[str, Any] = {}

    @staticmethod
    def _clean_text(value: Any) -> str:
        return " ".join(str(value or "").strip().split())

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().lower())

    @staticmethod
    def _message_evidence_id(message: Any, *, fallback_index: int) -> str:
        raw_id = getattr(message, "id", None)
        if raw_id is not None and str(raw_id).strip():
            return str(raw_id)
        payload = "|".join(
            (
                str(getattr(message, "group_id", "") or ""),
                str(getattr(message, "sender_id", "") or ""),
                str(getattr(message, "timestamp", "") or ""),
                str(getattr(message, "content", "") or ""),
                str(fallback_index),
            )
        )
        return f"synthetic:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"

    @staticmethod
    def _candidate_id(group_id: str, candidate_type: str, normalized_expression: str, scope_id: str = "") -> str:
        payload = f"{group_id}|{scope_id}|{candidate_type}|{normalized_expression}"
        return f"expr:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"

    @staticmethod
    def _speaker_info(message: Any) -> tuple[str, str, str]:
        speaker_id = str(getattr(message, "sender_id", "") or "").strip()
        speaker_name = str(getattr(message, "sender_name", "") or "").strip()
        if speaker_id and speaker_id.upper() in {"SELF", "BOT", "SELF_BOT"}:
            speaker_id = ""
        identity = speaker_id or speaker_name or "unknown"
        return identity, speaker_id, speaker_name

    @staticmethod
    def _message_day(message: Any, *, fallback_index: int) -> str:
        raw_timestamp = getattr(message, "timestamp", None)
        if raw_timestamp is not None and str(raw_timestamp).strip():
            try:
                return datetime.fromtimestamp(float(raw_timestamp)).date().isoformat()
            except (TypeError, ValueError, OverflowError, OSError):
                text = str(raw_timestamp).strip()
                if len(text) >= 10:
                    return text[:10]
        return f"message:{fallback_index}"

    @staticmethod
    def _message_timestamp(message: Any) -> float | None:
        raw_timestamp = getattr(message, "timestamp", None)
        try:
            return float(raw_timestamp)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _is_expression_candidate(cls, text: str, *, exact: bool = False) -> bool:
        cleaned = cls._clean_text(text)
        if not cleaned or cls._looks_noise(cleaned):
            return False
        lowered = cleaned.lower()
        if any(marker in lowered for marker in cls.EXPRESSION_MARKERS):
            return True
        if re.search(r"[~～♡☆♪ヾヽﾉ╥；;（）()<>《》]", cleaned):
            return True
        if re.search(r"(?:啦|呀|呢|哦|嘛|哒|捏|呐|喵|诶)[!?！？。.]*$", cleaned):
            return True
        if re.search(r"(.)\1{1,}", cleaned):
            return True
        if exact and re.fullmatch(r"[A-Za-z][A-Za-z' -]{2,39}", cleaned):
            # Repeated multi-word utterances may be an English catchphrase;
            # a single English token is a domain term candidate for JargonMiner.
            return len(re.findall(r"[A-Za-z']+", cleaned)) >= 2
        return False

    @classmethod
    def _candidate_quality_rejection_reason(cls, text: str, *, exact: bool = False) -> str:
        cleaned = cls._clean_text(text)
        lowered = cleaned.lower()
        if any(token in lowered for token in ("http://", "https://", "[图片", "[pic", "cq:")):
            return "transport_or_media_placeholder"
        if cleaned.startswith(("/", "!")):
            return "command_text"
        if not exact:
            return ""

        compact = cls._normalize_text(cleaned)
        has_marker = any(marker in lowered for marker in cls.EXPRESSION_MARKERS)
        has_symbol = bool(re.search(r"[~～♡☆♪ヾヽﾉ╥；;（）()<>《》]", cleaned))
        has_repetition = bool(re.search(r"(.)\1{1,}", compact))
        has_emphatic_punctuation = bool(re.search(r"[!?！？]{2,}", cleaned))
        english_words = re.findall(r"[A-Za-z']+", cleaned)
        has_english_catchphrase = len(english_words) >= 2
        terminal_particle_only = bool(
            re.search(r"(?:啦|呀|呢|哦|嘛|哒|捏|呐|喵|诶)[!?！？。.]*$", cleaned)
        ) and not any(
            (has_marker, has_symbol, has_repetition, has_emphatic_punctuation, has_english_catchphrase)
        )
        if terminal_particle_only and len(compact) > 12:
            return "plain_sentence_with_terminal_particle"
        return ""

    @classmethod
    def _infer_habit_type(cls, text: str, *, candidate_type: str = "exact") -> str:
        cleaned = cls._clean_text(text)
        compact = cls._normalize_text(cleaned)
        if candidate_type == "rhythm":
            return "rhythm"
        if re.search(r"(?:[~～♡☆♪]{1,}|\([^)]{1,16}\)|（[^）]{1,16}）|[ヾヽﾉ]{2,})", cleaned):
            return "symbol"
        if compact in cls.ENDING_PARTICLES:
            return "ending"
        if any(compact == cls._normalize_text(marker) for marker in cls.EXPRESSION_MARKERS):
            return "catchphrase"
        if re.search(r"(.)\1{1,}", compact) or re.search(r"[!?！？]{2,}", cleaned):
            return "rhythm"
        if len(compact) <= 3:
            return "particle"
        return "sentence_pattern"

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
        for run in re.findall(r"[\u4e00-\u9fff]{2,24}", cleaned):
            for marker in cls.EXPRESSION_MARKERS:
                marker_index = run.lower().find(marker.lower())
                if marker_index >= 0:
                    fragments.add(run[marker_index : marker_index + len(marker)])
            if re.search(r"(.)\1{1,}", run):
                repeated = re.search(r"(.)\1{1,}", run)
                if repeated:
                    fragments.add(repeated.group(0))
            if run[-1] in cls.ENDING_PARTICLES:
                fragments.add(run[-1])
        for marker in cls.EXPRESSION_MARKERS:
            if marker.lower() in cleaned.lower():
                fragments.add(marker)
        # Keep only a short, expressive fragment; ordinary topic phrases are left to JargonMiner.
        fragments = {
            phrase
            for phrase in fragments
            if phrase not in cls.PHRASE_NOISE
            and len(phrase) <= 12
            and (len(phrase) >= 2 or phrase in cls.ENDING_PARTICLES)
        }
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
        existing_patterns: set[Any] | None = None,
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = defaultdict(int)
        samples: dict[str, list[str]] = defaultdict(list)
        situations: dict[str, str] = {}
        styles: dict[str, str] = {}
        turn_keys: dict[str, set[str]] = defaultdict(set)
        day_keys: dict[str, set[str]] = defaultdict(set)
        contributor_keys: dict[str, set[str]] = defaultdict(set)
        existing: set[str] = set()
        for raw_item in existing_patterns or set():
            if isinstance(raw_item, tuple) and len(raw_item) == 2:
                raw_item = raw_item[1]
            normalized_existing = self._normalize_text(str(raw_item or ""))
            if normalized_existing:
                existing.add(normalized_existing)
        phrase_counts: dict[str, int] = defaultdict(int)
        phrase_samples: dict[str, list[str]] = defaultdict(list)
        phrase_turn_keys: dict[str, set[str]] = defaultdict(set)
        phrase_day_keys: dict[str, set[str]] = defaultdict(set)
        phrase_contributors: dict[str, set[str]] = defaultdict(set)
        group_messages: list[tuple[int, str, str, float | None]] = []
        group_days: set[str] = set()
        group_contributors: set[str] = set()
        accepted_messages = 0
        skipped_noise = 0
        skipped_existing = 0
        quality_filtered = 0
        quality_filter_reasons: dict[str, int] = defaultdict(int)

        for message_index, message in enumerate(messages or []):
            content = self._clean_text(getattr(message, "content", ""))
            if self._looks_noise(content):
                skipped_noise += 1
                continue
            if bool(getattr(message, "is_bot", False)) or str(getattr(message, "role", "") or "").lower() in {"assistant", "bot", "self"}:
                skipped_noise += 1
                continue
            if self._near_duplicate(content, existing):
                skipped_existing += 1
                continue
            normalized = self._normalize_text(content)
            speaker_identity, _, _ = self._speaker_info(message)
            message_day = self._message_day(message, fallback_index=message_index)
            accepted_messages += 1
            evidence_id = self._message_evidence_id(message, fallback_index=message_index)
            group_messages.append((message_index, content, evidence_id, self._message_timestamp(message)))
            group_days.add(message_day)
            group_contributors.add(speaker_identity)
            if self._is_expression_candidate(content, exact=True):
                quality_reason = self._candidate_quality_rejection_reason(content, exact=True)
                if quality_reason:
                    quality_filtered += 1
                    quality_filter_reasons[quality_reason] += 1
                else:
                    counts[normalized] += 1
                    turn_keys[normalized].add(evidence_id)
                    day_keys[normalized].add(message_day)
                    contributor_keys[normalized].add(speaker_identity)
                    if len(samples[normalized]) < 4:
                        samples[normalized].append(content[:160])
                    situations.setdefault(normalized, self._infer_situation(content))
                    styles.setdefault(normalized, self._infer_style(content))
            for phrase in self._phrase_fragments(content):
                normalized_phrase = self._normalize_text(phrase)
                if self._near_duplicate(normalized_phrase, existing):
                    continue
                phrase_counts[normalized_phrase] += 1
                phrase_turn_keys[normalized_phrase].add(evidence_id)
                phrase_day_keys[normalized_phrase].add(message_day)
                phrase_contributors[normalized_phrase].add(speaker_identity)
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
                    "shared_scope": group_id,
                    "scope_kind": "group",
                    "expression": expression,
                    "normalized_expression": normalized,
                    "habit_type": self._infer_habit_type(expression, candidate_type="exact"),
                    "content_kind": "expression",
                    "situation": situations.get(normalized, "日常回应"),
                    "style": styles.get(normalized, "自然"),
                    "content_samples": content_samples,
                    "count": count,
                    "distinct_turn_count": len(turn_keys.get(normalized, set())),
                    "distinct_day_count": len(day_keys.get(normalized, set())),
                    "distinct_contributor_count": len(contributor_keys.get(normalized, set())),
                    "activation_score": activation_score,
                    "think_level": 1 if len(expression) >= 10 else 0,
                    "candidate_type": "exact",
                    "candidate_origin": "human_group_text",
                    "classification": "expression",
                    "classification_reason": "deterministic_style_signal",
                    "quality_tier": "high" if count >= 3 else "medium",
                    "candidate_id": self._candidate_id(group_id, "exact", normalized),
                }
            )
            existing.add(normalized)

        selected_phrases: list[tuple[str, int]] = []
        for phrase, count in sorted(phrase_counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0])):
            if count < self.min_count:
                continue
            if any(phrase == selected and selected_count == count for selected, selected_count in selected_phrases):
                continue
            selected_phrases.append((phrase, count))
            if phrase in existing:
                continue
            if any(phrase == exact and exact_count == count for exact, exact_count in qualifying_exact):
                continue
            content_samples = list(phrase_samples.get(phrase, []))
            expression = phrase
            candidates.append(
                {
                    "group_id": group_id,
                    "shared_scope": group_id,
                    "scope_kind": "group",
                    "expression": expression,
                    "normalized_expression": phrase,
                    "habit_type": self._infer_habit_type(expression, candidate_type="phrase"),
                    "content_kind": "expression",
                    "situation": self._infer_situation(content_samples[0] if content_samples else phrase),
                    "style": self._infer_style(content_samples[0] if content_samples else phrase),
                    "content_samples": content_samples,
                    "count": count,
                    "distinct_turn_count": len(phrase_turn_keys.get(phrase, set())),
                    "distinct_day_count": len(phrase_day_keys.get(phrase, set())),
                    "distinct_contributor_count": len(phrase_contributors.get(phrase, set())),
                    "activation_score": min(1.0, 0.35 + count * 0.16),
                    "think_level": 0,
                    "candidate_type": "phrase",
                    "candidate_origin": "human_group_text",
                    "classification": "expression",
                    "classification_reason": "repeated_style_fragment",
                    "quality_tier": "high" if count >= 3 else "medium",
                    "candidate_id": self._candidate_id(group_id, "phrase", phrase),
                }
            )
            existing.add(phrase)

        rhythm_candidates = 0
        for entries in ([group_messages] if group_messages else []):
            evidence = list(dict.fromkeys(item[2] for item in entries if item[2]))
            if len(evidence) < max(self.min_count, 3):
                continue
            lengths = [len(self._normalize_text(item[1])) for item in entries if item[1]]
            if not lengths:
                continue
            short_ratio = sum(1 for length in lengths if length <= 12) / len(lengths)
            burst_pairs = 0
            for previous, current in zip(entries, entries[1:]):
                previous_time, current_time = previous[3], current[3]
                if previous_time is None or current_time is None:
                    continue
                gap = current_time - previous_time
                if 0 <= gap <= 8:
                    burst_pairs += 1
            if burst_pairs >= 2 and short_ratio >= 0.6:
                expression = "偏好短句连发，常在数秒内连续补充"
                normalized_rhythm = "rapid_short_bursts"
                style = "短句连发"
            elif len(evidence) >= 5 and short_ratio >= 0.8:
                expression = "偏好简短单句回复"
                normalized_rhythm = "brief_single_replies"
                style = "简短利落"
            else:
                continue
            candidates.append(
                {
                    "group_id": group_id,
                    "shared_scope": group_id,
                    "scope_kind": "group",
                    "expression": expression,
                    "normalized_expression": normalized_rhythm,
                    "habit_type": "rhythm",
                    "content_kind": "expression",
                    "candidate_origin": "derived_message_timing",
                    "situation": "日常回应",
                    "style": style,
                    "content_samples": [item[1][:160] for item in entries[:4]],
                    "count": len(evidence),
                    "distinct_turn_count": len(evidence),
                    "distinct_day_count": len(group_days),
                    "distinct_contributor_count": len(group_contributors),
                    "activation_score": min(1.0, 0.35 + len(evidence) * 0.08),
                    "think_level": 0,
                    "candidate_type": "rhythm",
                    "classification": "expression",
                    "classification_reason": "derived_group_reply_rhythm",
                    "quality_tier": "medium",
                    "candidate_id": self._candidate_id(group_id, "rhythm", normalized_rhythm),
                }
            )
            rhythm_candidates += 1

        candidates.sort(key=lambda item: (-int(item.get("count", 0)), item.get("candidate_type") != "exact", -len(str(item.get("expression", "")))))
        self.last_report = {
            "input_messages": len(messages or []),
            "accepted_messages": accepted_messages,
            "skipped_noise": skipped_noise,
            "skipped_existing": skipped_existing,
            "quality_filtered": quality_filtered,
            "quality_filter_reasons": dict(sorted(quality_filter_reasons.items())),
            "exact_candidates": len(qualifying_exact),
            "phrase_candidates": sum(1 for item in candidates if item.get("candidate_type") == "phrase"),
            "rhythm_candidates": rhythm_candidates,
            "contributor_count": len(group_contributors),
            "candidate_count": len(candidates),
            "reason": "candidates_ready" if candidates else "no_repeated_expression",
        }
        return candidates[:12]


__all__ = ["ExpressionCandidateExtractor"]
