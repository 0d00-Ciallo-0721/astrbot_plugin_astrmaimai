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
            digest = hashlib.sha256(
                f"{request.kind}|{request.session_id}|{request.persona_id}|{request.content}".encode("utf-8")
            ).hexdigest()[:24]
            dedup_key = f"{request.kind}:{request.session_id}:{request.persona_id}:{digest}"
        normalized = MemoryWriteRequest(
            source=str(request.source or "unknown").strip() or "unknown",
            kind=str(request.kind or "memory").strip() or "memory",
            session_id=str(request.session_id or ""),
            sender_id=str(request.sender_id or ""),
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
            status=str(request.status or "active").strip() or "active",
            created_at=float(request.created_at or 0.0),
        )
        upsert_result = await self.store.upsert(normalized)
        memory_id = getattr(upsert_result, "memory_id", "") or str(upsert_result.get("memory_id") or "")
        superseded_old_ids = getattr(upsert_result, "superseded_old_ids", []) or list(upsert_result.get("superseded_old_ids") or [])
        new_record_is_superseded = bool(
            getattr(upsert_result, "new_record_is_superseded", False) or upsert_result.get("new_record_is_superseded", False)
        )
        if self.index_projector and superseded_old_ids:
            await self.index_projector.cleanup_deleted(superseded_old_ids)
        if self.index_projector and memory_id and not new_record_is_superseded:
            await self.index_projector.project(memory_id=memory_id, request=normalized)
        return memory_id


__all__ = ["MemoryWriteService"]
