from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...conversation.contracts.prompt_envelope import PromptEnvelope
from ..contracts.memory_query import MemoryQuery


def _clean_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip()


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return default


def read_memory_feature_flag(config: Any, event: AstrMessageEvent | None, name: str, default: bool) -> bool:
    """Read an internal memory feature flag from event/debug/config channels."""
    event_key = f"astrmai_{name}"
    if event is not None and hasattr(event, "get_extra"):
        try:
            value = event.get_extra(event_key, None)
            if value is not None:
                return _as_bool(value, default)
        except Exception:
            pass
    for holder_name in ("memory", "global_settings"):
        holder = (
            config.get(holder_name)
            if isinstance(config, dict)
            else getattr(config, holder_name, None) if config is not None else None
        )
        if isinstance(holder, dict) and name in holder:
            return _as_bool(holder.get(name), default)
        if holder is not None and hasattr(holder, name):
            return _as_bool(getattr(holder, name), default)
    debug_flags = config.get("debug_flags") if isinstance(config, dict) else getattr(config, "debug_flags", None) if config is not None else None
    if isinstance(debug_flags, dict) and name in debug_flags:
        return _as_bool(debug_flags.get(name), default)
    return default


@dataclass(slots=True)
class MemoryRetrievalFlags:
    query_builder: bool = True
    intent_rerank: bool = False
    adaptive_top_k: bool = False
    rrf_fusion: bool = False
    mmr: bool = False
    debug_trace: bool = False

    @classmethod
    def from_sources(cls, config: Any, event: AstrMessageEvent | None = None) -> "MemoryRetrievalFlags":
        return cls(
            query_builder=read_memory_feature_flag(config, event, "memory_query_builder_enabled", True),
            intent_rerank=read_memory_feature_flag(config, event, "intent_rerank_enabled", False),
            adaptive_top_k=read_memory_feature_flag(config, event, "adaptive_top_k_enabled", False),
            rrf_fusion=read_memory_feature_flag(config, event, "memory_rrf_fusion_enabled", False),
            mmr=read_memory_feature_flag(config, event, "memory_mmr_enabled", False),
            debug_trace=read_memory_feature_flag(config, event, "memory_retrieval_debug_trace_enabled", False),
        )


@dataclass(slots=True)
class QueryBuildResult:
    raw_query: str
    normalized_query: str
    retrieval_query: str
    primary_intent: str
    intents: list[str] = field(default_factory=list)
    intent_confidence: float = 0.0
    expansion_terms: list[str] = field(default_factory=list)
    context_terms: list[str] = field(default_factory=list)
    is_low_information: bool = False
    injection_top_k: int = 5
    candidate_limit: int | None = None


class QueryNormalizer:
    @staticmethod
    def normalize(text: str) -> str:
        return _clean_text(text)


class QueryIntentClassifier:
    FOOD_TERMS = ("吃", "爱吃", "口味", "食物", "火锅", "芒果", "饭", "菜", "忌口", "喝")
    DISLIKE_TERMS = ("不喜欢", "讨厌", "厌恶", "别", "不要", "dislike", "hate")
    RECENT_TERMS = ("上次", "之前", "刚才", "还记得", "记得", "那个", "前面", "last time", "earlier", "before")
    IDENTITY_TERMS = ("我叫", "名字", "年龄", "生日", "身份", "name", "age")
    LOCATION_TERMS = ("在哪", "哪里", "公司", "学校", "城市", "地址", "location", "company")
    PREFERENCE_TERMS = ("喜欢", "偏好", "爱好", "想要", "prefer", "like")

    def classify(self, query: str) -> tuple[str, list[str], float]:
        lowered = query.lower()
        intents: list[str] = []
        if any(term in query or term in lowered for term in self.RECENT_TERMS):
            intents.append("recent_reference")
        if any(term in query or term in lowered for term in self.DISLIKE_TERMS):
            intents.append("dislike")
        if any(term in query or term in lowered for term in self.FOOD_TERMS):
            intents.append("food_preference")
        if any(term in query or term in lowered for term in self.IDENTITY_TERMS):
            intents.append("identity")
        if any(term in query or term in lowered for term in self.LOCATION_TERMS):
            intents.append("location")
        if any(term in query or term in lowered for term in self.PREFERENCE_TERMS):
            if "food_preference" not in intents:
                intents.append("preference_general")
            elif query in {"我喜欢什么", "喜欢什么"}:
                intents.append("preference_general")
        if not intents:
            intents.append("general")
        priority = ("food_preference", "identity", "location", "preference_general", "dislike", "recent_reference")
        primary = next((item for item in priority if item in intents), intents[0])
        confidence = 0.85 if primary != "general" else 0.45
        return primary, intents, confidence


class QueryExpansionProvider:
    EXPANSIONS = {
        "food_preference": ["吃", "爱吃", "口味", "食物", "忌口"],
        "preference_general": ["喜欢", "偏好", "爱好"],
        "identity": ["名字", "身份", "年龄"],
        "location": ["公司", "地点", "哪里"],
        "dislike": ["讨厌", "不喜欢", "避开"],
    }

    def terms_for(self, intents: list[str]) -> list[str]:
        terms: list[str] = []
        for intent in intents:
            for term in self.EXPANSIONS.get(intent, []):
                if term not in terms:
                    terms.append(term)
        return terms


class ContextTermExtractor:
    WEAK_TERMS = {"什么", "那个", "这个", "记得", "还记得", "上次", "之前", "吗", "呢"}
    TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}")
    ANCHOR_TERMS = (
        "火锅", "芒果", "名字", "年龄", "生日", "公司", "学校", "城市",
        "喜欢", "讨厌", "口味", "忌口", "工作", "住址", "甜食", "点心",
        "颜色", "蓝色", "音乐", "游戏", "跑步", "地点", "食物",
    )

    def has_anchor(self, query: str) -> bool:
        text = _clean_text(query)
        if any(term in text for term in self.ANCHOR_TERMS):
            return True
        residual = text
        for term in sorted(self.WEAK_TERMS, key=len, reverse=True):
            residual = residual.replace(term, "")
        residual = re.sub(r"[\W_]+", "", residual, flags=re.UNICODE)
        return len(residual) >= 2

    def is_low_information(self, query: str, intents: list[str]) -> bool:
        text = _clean_text(query)
        if not text:
            return True
        if self.has_anchor(text) and len(text) >= 3:
            return False
        if "recent_reference" in intents:
            return True
        return len(text) <= 3

    def extract_terms(self, text: str, *, limit: int = 6) -> list[str]:
        terms: list[str] = []
        source = str(text or "")
        for anchor in self.ANCHOR_TERMS:
            if anchor in source and anchor not in terms:
                terms.append(anchor)
            if len(terms) >= limit:
                return terms
        for match in self.TOKEN_RE.findall(source):
            term = match.strip()
            for weak_term in sorted(self.WEAK_TERMS, key=len, reverse=True):
                term = term.replace(weak_term, "")
            if not term or len(term) < 2 or len(term) > 8:
                continue
            if term not in terms:
                terms.append(term)
            if len(terms) >= limit:
                break
        return terms

    def context_terms(self, prompt_envelope: PromptEnvelope | None, current_query: str, *, limit: int = 4) -> list[str]:
        if not isinstance(prompt_envelope, PromptEnvelope):
            return []
        sources = [
            prompt_envelope.focus_message_text,
            prompt_envelope.direct_context_text,
            prompt_envelope.warm_topics_preview,
            prompt_envelope.warm_zone_summary,
            prompt_envelope.related_context_text,
        ]
        terms: list[str] = []
        current = _clean_text(current_query)
        for source in sources:
            text = _clean_text(source)
            if not text or text == current:
                continue
            for term in self.extract_terms(text, limit=limit):
                if term not in terms and term not in current:
                    terms.append(term)
                if len(terms) >= limit:
                    return terms
        return terms


class AdaptiveTopKDecider:
    INTENT_TOP_K = {
        "identity": 3,
        "food_preference": 6,
        "preference_general": 8,
        "recent_reference": 8,
        "location": 5,
        "general": 5,
    }

    def decide(self, primary_intent: str, intents: list[str], base_top_k: int) -> tuple[int, int]:
        intent_top_k = max(self.INTENT_TOP_K.get(primary_intent, base_top_k), self.INTENT_TOP_K.get("recent_reference", 0) if "recent_reference" in intents else 0)
        injection_top_k = max(intent_top_k, 1)
        candidate_limit = max(injection_top_k * 3, 8)
        return injection_top_k, candidate_limit


class QueryTraceBuilder:
    @staticmethod
    def summary(result: QueryBuildResult, flags: MemoryRetrievalFlags) -> dict[str, Any]:
        return {
            "query_builder_enabled": bool(flags.query_builder),
            "intent_rerank_enabled": bool(flags.intent_rerank),
            "adaptive_top_k_enabled": bool(flags.adaptive_top_k),
            "rrf_fusion_enabled": bool(flags.rrf_fusion),
            "mmr_enabled": bool(flags.mmr),
            "debug_trace_enabled": bool(flags.debug_trace),
            "primary_intent": result.primary_intent,
            "intents": list(result.intents),
            "intent_confidence": round(float(result.intent_confidence or 0.0), 4),
            "is_low_information": bool(result.is_low_information),
            "adaptive_top_k": {
                "injection_top_k": int(result.injection_top_k),
                "candidate_limit": result.candidate_limit,
            },
        }

    @staticmethod
    def debug(result: QueryBuildResult) -> dict[str, Any]:
        return {
            "raw_query": result.raw_query,
            "normalized_query": result.normalized_query,
            "retrieval_query": result.retrieval_query,
            "context_terms": list(result.context_terms),
            "expansion_terms": list(result.expansion_terms),
        }


class MemoryQueryBuilder:
    def __init__(self, config: Any = None):
        self.config = config
        self.normalizer = QueryNormalizer()
        self.intent_classifier = QueryIntentClassifier()
        self.expansion_provider = QueryExpansionProvider()
        self.context_extractor = ContextTermExtractor()
        self.top_k_decider = AdaptiveTopKDecider()
        self.trace_builder = QueryTraceBuilder()

    def build(
        self,
        *,
        event: AstrMessageEvent,
        raw_query: str,
        prompt_envelope: PromptEnvelope | None,
        session_id: str,
        persona_id: str,
        sender_id: str,
        top_k: int,
        policy: str,
        think_level: int | None,
        retrieve_keys: list[str],
        allow_stale: bool,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryQuery:
        flags = MemoryRetrievalFlags.from_sources(self.config, event)

        def legacy_query() -> MemoryQuery:
            return MemoryQuery(
                query=raw_query,
                session_id=session_id,
                persona_id=persona_id,
                sender_id=sender_id,
                top_k=top_k,
                policy=policy,
                think_level=think_level,
                retrieve_keys=list(retrieve_keys),
                allow_stale=allow_stale,
                metadata=dict(metadata or {}),
            )

        if not flags.query_builder:
            return legacy_query()

        try:
            normalized_query = self.normalizer.normalize(raw_query)
            primary_intent, intents, intent_confidence = self.intent_classifier.classify(normalized_query)
            context_terms: list[str] = []
            is_low_information = self.context_extractor.is_low_information(normalized_query, intents)
            retrieval_query = normalized_query
            if is_low_information:
                context_terms = self.context_extractor.context_terms(prompt_envelope, normalized_query)
                if context_terms:
                    retrieval_query = " ".join([normalized_query, *context_terms]).strip()
            expansion_terms = self.expansion_provider.terms_for(intents)
            injection_top_k = max(int(top_k or 5), 1)
            candidate_limit = None
            if flags.adaptive_top_k:
                injection_top_k, candidate_limit = self.top_k_decider.decide(primary_intent, intents, injection_top_k)

            result = QueryBuildResult(
                raw_query=raw_query,
                normalized_query=normalized_query,
                retrieval_query=retrieval_query,
                primary_intent=primary_intent,
                intents=intents,
                intent_confidence=intent_confidence,
                expansion_terms=expansion_terms,
                context_terms=context_terms,
                is_low_information=is_low_information,
                injection_top_k=injection_top_k,
                candidate_limit=candidate_limit,
            )
            query_metadata = dict(metadata or {})
            query_metadata.update(
                {
                    "raw_query": raw_query,
                    "normalized_query": normalized_query,
                    "retrieval_query": retrieval_query,
                    "primary_intent": primary_intent,
                    "intents": list(intents),
                    "intent_confidence": intent_confidence,
                    "is_low_information": is_low_information,
                    "intent_rerank_enabled": flags.intent_rerank,
                    "adaptive_top_k_enabled": flags.adaptive_top_k,
                    "memory_rrf_fusion_enabled": flags.rrf_fusion,
                    "memory_mmr_enabled": flags.mmr,
                    "memory_retrieval_debug_trace_enabled": flags.debug_trace,
                }
            )
            if context_terms:
                query_metadata["context_terms"] = list(context_terms)
            if expansion_terms:
                query_metadata["expansion_terms"] = list(expansion_terms)
            if candidate_limit is not None:
                query_metadata["candidate_limit"] = int(candidate_limit)
                query_metadata["retrieval_candidate_k"] = int(candidate_limit)
            trace = query_metadata.setdefault("_trace", {})
            trace["query_builder"] = self.trace_builder.summary(result, flags)
            if flags.debug_trace:
                trace["query_builder_debug"] = self.trace_builder.debug(result)
        except Exception as exc:
            logger.warning(f"[MemoryQueryBuilder] query build degraded to legacy retrieval: {exc}")
            return legacy_query()

        return MemoryQuery(
            query=retrieval_query,
            session_id=session_id,
            persona_id=persona_id,
            sender_id=sender_id,
            top_k=injection_top_k,
            policy=policy,
            think_level=think_level,
            retrieve_keys=list(retrieve_keys),
            allow_stale=allow_stale,
            intent=primary_intent,
            metadata=query_metadata,
        )


__all__ = [
    "MemoryQueryBuilder",
    "MemoryRetrievalFlags",
    "read_memory_feature_flag",
]
