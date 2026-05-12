from __future__ import annotations

import hashlib

from ..contracts.memory_query import MemoryWriteRequest
from .v2_store import MemoryV2Store


class MemoryWriteService:
    def __init__(self, store: MemoryV2Store, index_projector=None):
        self.store = store
        self.index_projector = index_projector

    @staticmethod
    def should_skip_content(content: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return True
        if text.startswith("{") or text.startswith("```json"):
            return True
        lowered = text.lower()
        noisy_tokens = ("traceback", "exception", "all chat models fail", "apitimesouterror")
        return any(token in lowered for token in noisy_tokens)

    async def write(self, request: MemoryWriteRequest) -> str:
        if self.should_skip_content(request.content):
            return ""
        dedup_key = str(request.dedup_key or "").strip()
        if not dedup_key:
            digest = hashlib.sha1(
                f"{request.kind}|{request.session_id}|{request.persona_id}|{request.content}".encode("utf-8")
            ).hexdigest()[:24]
            dedup_key = f"{request.kind}:{request.session_id}:{request.persona_id}:{digest}"
        normalized = MemoryWriteRequest(
            source=str(request.source or "unknown").strip() or "unknown",
            kind=str(request.kind or "memory").strip() or "memory",
            session_id=str(request.session_id or ""),
            persona_id=str(request.persona_id or ""),
            content=str(request.content or ""),
            summary=str(request.summary or ""),
            tags=list(request.tags or []),
            importance=float(request.importance or 0.5),
            confidence=float(request.confidence or 0.8),
            metadata=dict(request.metadata or {}),
            dedup_key=dedup_key,
            source_ref=str(request.source_ref or ""),
            visibility=request.visibility if request.visibility in {"auto_and_tool", "tool_only", "maintenance_only"} else "auto_and_tool",
        )
        memory_id = await self.store.upsert(normalized)
        if self.index_projector and memory_id:
            await self.index_projector.project(memory_id=memory_id, request=normalized)
        return memory_id


__all__ = ["MemoryWriteService"]
