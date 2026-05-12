from __future__ import annotations

import aiosqlite
from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest


class SelfLoreService:
    def __init__(self, memory_engine):
        self.memory_engine = memory_engine

    async def clear_persona_lore(self, persona_id: str | None = None) -> int:
        if hasattr(self.memory_engine, "clear_persona_lore"):
            return await self.memory_engine.clear_persona_lore(persona_id)
        if not await self.memory_engine._ensure_faiss_initialized():
            return 0
        try:
            async with aiosqlite.connect(self.memory_engine.db_path) as db:
                query = "DELETE FROM documents WHERE json_extract(metadata, '$.session_id') = '__self_lore__'"
                params = []
                if persona_id:
                    query += " AND json_extract(metadata, '$.persona_id') = ?"
                    params.append(persona_id)
                cursor = await db.execute(query, params)
                await db.commit()
                return cursor.rowcount
        except Exception as exc:
            logger.error(f"[MemoryEngine] clear persona lore failed: {exc}")
            return 0

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
        if hasattr(self.memory_engine, "recall_persona_lore"):
            return await self.memory_engine.recall_persona_lore(query=query, persona_id=persona_id, top_k=top_k)
        if not await self.memory_engine._ensure_faiss_initialized():
            return "（设定原典离线）"
        results = await self.memory_engine.retriever.search(
            query,
            k=top_k,
            session_id="__self_lore__",
            persona_id=persona_id,
        )
        valid_results = [result for result in results if getattr(result, "score", 1.0) >= 0.05]
        if not valid_results:
            return "（潜意识原典库中未发现相关事实）"
        return "\n".join(f"[绝对事实]: {result.content}" for result in valid_results)
