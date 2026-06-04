from __future__ import annotations

import re

from ..contracts.memory_query import MemoryCandidate


class JargonRetrievalPolicy:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join(str(text or "").strip().lower().split())

    @classmethod
    def _query_terms(cls, text: str) -> list[str]:
        normalized = cls._normalize(text)
        if not normalized:
            return []
        terms: list[str] = []
        for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", normalized):
            if len(token) >= 2 and token not in terms:
                terms.append(token)
            if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 4:
                for size in range(2, min(6, len(token)) + 1):
                    for index in range(0, len(token) - size + 1):
                        piece = token[index:index + size]
                        if piece not in terms:
                            terms.append(piece)
        if normalized not in terms and len(normalized) <= 24:
            terms.append(normalized)
        return terms[:48]

    @staticmethod
    def _candidate_text(candidate: MemoryCandidate) -> str:
        metadata = dict(candidate.metadata or {})
        aliases = metadata.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        examples = metadata.get("examples", [])
        if not isinstance(examples, list):
            examples = []
        parts = [
            candidate.content,
            candidate.summary,
            str(metadata.get("meaning", "") or ""),
            str(metadata.get("scene", "") or ""),
            " ".join(str(item) for item in aliases if str(item).strip()),
            " ".join(str(item) for item in examples if str(item).strip()),
        ]
        return "\n".join(part for part in parts if str(part or "").strip()).lower()

    @classmethod
    def _score(cls, candidate: MemoryCandidate, query: str, terms: list[str]) -> float:
        haystack = cls._candidate_text(candidate)
        content = cls._normalize(candidate.content)
        summary = cls._normalize(candidate.summary)
        raw_query = cls._normalize(query)
        score = 0.0
        if raw_query and (raw_query == content or raw_query == summary):
            score += 2.0
        if raw_query and raw_query in haystack:
            score += 1.2
        if content and content in cls._normalize(query):
            score += 1.5
        overlap = 0
        for term in terms:
            if term in haystack:
                overlap += 1
        if terms:
            score += min(1.0, overlap / max(len(terms), 1)) * 1.2
        score += float(candidate.importance or 0.0) * 0.2
        score += float(candidate.confidence or 0.0) * 0.2
        if candidate.status == "stale":
            score -= 0.4
        return score

    async def search(
        self,
        *,
        query: str,
        session_id: str = "",
        persona_id: str = "",
        top_k: int = 3,
        exclude_ids: list[str] | None = None,
        allow_stale: bool = False,
        visibility_mode: str = "",
        trace: dict | None = None,
    ) -> list[MemoryCandidate]:
        query_text = str(query or "").strip()
        if not query_text:
            return []
        statuses = ["active"]
        if allow_stale:
            statuses.append("stale")
        candidates = await self.store.list_candidates(
            session_id=session_id,
            persona_id=persona_id,
            kinds=["jargon"],
            statuses=statuses,
            limit=max(int(top_k or 3) * 8, 32),
            visibility_mode=visibility_mode,
        )
        excluded = {str(item) for item in exclude_ids or [] if str(item).strip()}
        terms = self._query_terms(query_text)
        ranked: list[tuple[float, MemoryCandidate]] = []
        for candidate in candidates:
            if candidate.id in excluded:
                continue
            score = self._score(candidate, query_text, terms)
            if score <= 0:
                continue
            candidate.relevance_score = max(candidate.relevance_score, min(score / 3.0, 1.0))
            ranked.append((score, candidate))
        ranked.sort(key=lambda item: item[0], reverse=True)
        selected = [candidate for _, candidate in ranked[: max(int(top_k or 3), 1)]]
        if selected:
            await self.store.mark_accessed([item.id for item in selected])
        if trace is not None:
            trace.setdefault("matched_terms", list(terms))
            trace.setdefault("top_k_scores", [
                {"id": str(getattr(c, "id", "") or ""), "score": round(s, 4)}
                for s, c in ranked[:max(int(top_k or 3), 1)]
            ])
        return selected


__all__ = ["JargonRetrievalPolicy"]
