from __future__ import annotations

import re
from typing import Any

from ..contracts.memory_query import MemoryCandidate
from .v2_store import MemoryV2Store


class ExpressionPatternRetrievalPolicy:
    def __init__(self, store: MemoryV2Store):
        self.store = store

    @staticmethod
    def _tokens(text: str) -> list[str]:
        cleaned = " ".join(str(text or "").strip().split()).lower()
        if not cleaned:
            return []
        matches = re.findall(r"[A-Za-z0-9_]{2,24}|[\u4e00-\u9fff]{1,8}", cleaned)
        return [str(item).strip() for item in matches if str(item).strip()]

    @staticmethod
    def _match_score(candidate: MemoryCandidate, tokens: list[str]) -> float:
        metadata = dict(candidate.metadata or {})
        haystack = "\n".join(
            [
                str(candidate.content or ""),
                str(candidate.summary or ""),
                str(metadata.get("situation") or ""),
                str(metadata.get("style") or ""),
                " ".join(str(item) for item in metadata.get("content_samples", []) or []),
            ]
        ).lower()
        if not tokens:
            return 0.1
        overlap = sum(1 for token in tokens if token in haystack)
        return min(1.0, overlap / max(len(tokens), 1))

    async def search(
        self,
        *,
        query: str,
        session_id: str,
        top_k: int = 5,
        shared_scope: str = "",
        think_level: int | None = None,
        exclude_ids: list[str] | None = None,
        allow_stale: bool = False,
        visibility_mode: str = "",
    ) -> list[MemoryCandidate]:
        statuses = ["active"]
        if allow_stale:
            statuses.append("stale")
        rows = await self.store.list_candidates(
            session_id=session_id,
            kinds=["expression_pattern"],
            statuses=statuses,
            limit=max(int(top_k or 5) * 8, 40),
            visibility_mode=visibility_mode,
        )
        excluded = {str(item) for item in exclude_ids or [] if str(item).strip()}
        tokens = self._tokens(query)
        candidates: list[MemoryCandidate] = []
        for item in rows:
            if item.id in excluded:
                continue
            metadata = dict(item.metadata or {})
            review_status = str(metadata.get("review_status") or "").strip().lower()
            pattern_scope = str(metadata.get("shared_scope") or "").strip()
            pattern_think_level = int(metadata.get("think_level") or 0)
            if review_status not in {"approved", "active"}:
                continue
            if shared_scope:
                # Prefer the current speaker's scope, while allowing legacy records
                # written at chat scope. Never admit another speaker's scope.
                allowed_scopes = {str(shared_scope).strip(), str(session_id or "").strip()}
                if pattern_scope not in allowed_scopes:
                    continue
            if think_level is not None and pattern_think_level > int(think_level or 0):
                continue
            score = self._match_score(item, tokens)
            if tokens and score <= 0:
                continue
            item.relevance_score = score
            item.metadata = metadata
            candidates.append(item)
        candidates.sort(
            key=lambda item: (
                item.relevance_score * 0.45
                + float(dict(item.metadata or {}).get("weight") or 1.0) * 0.25
                + min(float(dict(item.metadata or {}).get("count") or 1), 8.0) / 8.0 * 0.15
                + item.recency_score * 0.1
                - (0.25 if item.status == "stale" else 0.0)
            ),
            reverse=True,
        )
        selected = candidates[: max(int(top_k or 5), 1)]
        return selected


__all__ = ["ExpressionPatternRetrievalPolicy"]
