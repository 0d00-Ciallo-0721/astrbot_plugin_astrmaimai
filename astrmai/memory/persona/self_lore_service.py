from __future__ import annotations

from ..contracts.memory_query import MemoryQuery, MemoryWriteRequest


class SelfLoreService:
    def __init__(self, memory_engine):
        self.memory_engine = memory_engine

    async def clear_persona_lore(self, persona_id: str | None = None) -> int:
        if hasattr(self.memory_engine, "clear_persona_lore"):
            return await self.memory_engine.clear_persona_lore(persona_id)
        maintenance = getattr(self.memory_engine, "maintenance_service", None)
        if not maintenance or not hasattr(maintenance, "soft_delete_by_filter"):
            return 0
        return await maintenance.soft_delete_by_filter(
            kind="persona_lore",
            session_id="__self_lore__",
            persona_id=str(persona_id or ""),
            reason="persona_lore_rebuild",
        )

    async def add_persona_lore(self, content: str, persona_id: str | None = None):
        if hasattr(self.memory_engine, "add_persona_lore"):
            return await self.memory_engine.add_persona_lore(content, persona_id)
        writer = getattr(self.memory_engine, "write_service", None)
        if writer and hasattr(writer, "write"):
            return await writer.write(
                MemoryWriteRequest(
                    source="persona_lore",
                    kind="persona_lore",
                    session_id="__self_lore__",
                    persona_id=str(persona_id or ""),
                    content=str(content or ""),
                    summary=str(content or "")[:240],
                    importance=1.0,
                    confidence=0.9,
                    source_ref=f"persona_lore:{persona_id or ''}",
                )
            )

    async def recall_persona_lore(self, query: str, persona_id: str | None = None, top_k: int = 3) -> str:
        retrieval = getattr(self.memory_engine, "retrieval_service", None)
        if not retrieval or not hasattr(retrieval, "retrieve"):
            return "锛堣瀹氬師鍏哥绾匡級"
        results = await retrieval.retrieve(
            MemoryQuery(
                query=str(query or ""),
                session_id="__self_lore__",
                persona_id=str(persona_id or ""),
                layers=["persona_lore"],
                top_k=int(top_k or 3),
                include_persona_lore=True,
                allow_stale=True,
            )
        )
        if not results:
            return "锛堟綔鎰忚瘑鍘熷吀搴撲腑鏈彂鐜扮浉鍏充簨瀹烇級"
        return "\n".join(f"[缁濆浜嬪疄]: {result.summary or result.content}" for result in results)
