from __future__ import annotations

import asyncio
import uuid
from typing import Any

from astrbot.api import logger

from ...conversation.contracts.turn_context import get_turn_context
from ..contracts.memory_query import MemoryQuery, MemoryToolResult
from .memory_retrieval_service import MemoryRetrievalService


class MemoryToolService:
    def __init__(self, retrieval_service: MemoryRetrievalService, db_service: Any = None, config: Any = None):
        self.retrieval_service = retrieval_service
        self.db_service = db_service
        self.config = config

    @staticmethod
    def _already_injected_ids(event) -> list[str]:
        trace = event.get_extra("astrmai_memory_injection_trace", None) if hasattr(event, "get_extra") else None
        selected = list(getattr(trace, "selected_ids", []) or [])
        jargon_trace = event.get_extra("astrmai_jargon_injection_trace", None) if hasattr(event, "get_extra") else None
        selected.extend(list(getattr(jargon_trace, "selected_ids", []) or []))
        turn_context = get_turn_context(event)
        if turn_context is not None:
            selected.extend(list(getattr(turn_context.memory, "selected_ids", []) or []))
        return list(dict.fromkeys(str(item) for item in selected if str(item).strip()))

    @staticmethod
    def render_result(result: MemoryToolResult) -> str:
        sections: list[str] = []
        if result.items:
            lines = []
            for item in result.items:
                status_note = " (possibly stale)" if item.status == "stale" else ""
                if item.kind == "jargon":
                    meaning = str((item.metadata or {}).get("meaning") or item.summary or "").strip()
                    scene = str((item.metadata or {}).get("scene") or "").strip()
                    line = f"- [jargon]{status_note} {item.content}"
                    if meaning:
                        line += f" -> {meaning}"
                    if scene:
                        line += f" (scene: {scene})"
                else:
                    line = f"- [{item.kind or 'memory'}]{status_note} {item.summary or item.content}"
                lines.append(line)
            sections.append("[Memory]\n" + "\n".join(lines))
        if result.guidance:
            sections.append("[Guidance]\n" + result.guidance)
        if result.warnings:
            sections.append("[Warnings]\n" + "\n".join(f"- {item}" for item in result.warnings))
        if not sections:
            return "System note: no usable internal memory was found."
        return "\n\n".join(sections)

    async def search_memory(
        self,
        *,
        query: str,
        session_id: str = "",
        persona_id: str = "",
        layers: list[str] | None = None,
        top_k: int = 5,
        event=None,
        allow_stale: bool = False,
    ) -> MemoryToolResult:
        exclude_ids = self._already_injected_ids(event) if event is not None else []
        memory_query = MemoryQuery(
            query=query,
            session_id=session_id,
            persona_id=persona_id,
            layers=layers or [],
            top_k=top_k,
            exclude_ids=exclude_ids,
            allow_stale=allow_stale,
            policy="tool",
            metadata={"visibility_mode": "tool"},
        )
        items = await self.retrieval_service.retrieve(memory_query)
        warnings = ["Some returned memories may be stale."] if any(item.status == "stale" for item in items) else []
        return MemoryToolResult(
            query=query,
            items=items,
            guidance="Use tool memories only if they resolve missing context; do not mention the tool call.",
            trace_id=f"tooltrace_{uuid.uuid4().hex[:12]}",
            already_injected_ids=exclude_ids,
            warnings=warnings,
        )

    async def self_lore_query(
        self,
        *,
        query: str,
        persona_id: str = "",
        event=None,
        top_k: int = 3,
        allow_stale: bool = False,
    ) -> MemoryToolResult:
        return await self.search_memory(
            query=query,
            session_id="__self_lore__",
            persona_id=persona_id,
            layers=["persona_lore"],
            top_k=top_k,
            event=event,
            allow_stale=allow_stale,
        )

    async def omni_query(
        self,
        *,
        query: str = "",
        target_name: str = "",
        recall_date: str = "",
        chat_id: str = "",
        current_sender_id: str = "",
        current_sender_name: str = "",
        event=None,
    ) -> str:
        search_query = f"{target_name} {query}".strip() if target_name else query

        async def _memory():
            if not search_query:
                return None
            try:
                result = await self.search_memory(
                    query=search_query,
                    session_id=chat_id,
                    top_k=5,
                    event=event,
                    allow_stale=False,
                )
                result.items = [item for item in result.items if item.kind != "jargon"]
                return self.render_result(result) if result.items else None
            except Exception as exc:
                logger.debug(f"[MemoryToolService] memory lookup failed: {exc}")
                return None

        async def _profile():
            if not self.db_service:
                return None
            entity = target_name or query
            if not entity:
                return None
            try:
                profile = None
                if entity == current_sender_name or entity in {"我", "自己", "当前用户"}:
                    getter = getattr(self.db_service, "get_user_profile", None)
                    if getter and current_sender_id:
                        profile = await getter(current_sender_id) if asyncio.iscoroutinefunction(getter) else getter(current_sender_id)
                else:
                    getter = getattr(self.db_service, "get_profile_by_name", None)
                    if getter:
                        profile = await getter(entity) if asyncio.iscoroutinefunction(getter) else getter(entity)
                if not profile:
                    return None
                social_score = float(getattr(profile, "social_score", 0.0) or 0.0)
                persona = getattr(profile, "persona_analysis", "") or "No stable profile analysis."
                name = getattr(profile, "name", entity)
                return f"Target: {name}\nSocial score: {social_score:.1f}\nProfile: {persona}"
            except Exception as exc:
                logger.debug(f"[MemoryToolService] profile lookup failed: {exc}")
                return None

        async def _nodes():
            if not self.db_service or not hasattr(self.db_service, "search_nodes_async"):
                return None
            term = target_name or query
            if not term:
                return None
            try:
                nodes = await self.db_service.search_nodes_async(term, limit=3, include_description=True)
                if not nodes:
                    return None
                return "\n".join(
                    f"- {getattr(node, 'name', '')} ({getattr(node, 'type', '')}): {getattr(node, 'description', '')}"
                    for node in nodes
                )
            except Exception as exc:
                logger.debug(f"[MemoryToolService] node lookup failed: {exc}")
                return None

        async def _reflection():
            if not self.db_service or not recall_date or not hasattr(self.db_service, "get_reflection_async"):
                return None
            try:
                reflection = await self.db_service.get_reflection_async(recall_date)
                if not reflection:
                    return None
                return f"[{getattr(reflection, 'date', recall_date)}]\n{getattr(reflection, 'reflection', '')}"
            except Exception as exc:
                logger.debug(f"[MemoryToolService] reflection lookup failed: {exc}")
                return None

        async def _jargon():
            if not search_query:
                return None
            try:
                result = await self.search_memory(
                    query=search_query,
                    session_id=chat_id,
                    layers=["jargon"],
                    top_k=3,
                    event=event,
                    allow_stale=False,
                )
                if result.items:
                    return self.render_result(result)
            except Exception as exc:
                logger.debug(f"[MemoryToolService] jargon lookup failed: {exc}")
            return None

        memory, profile, nodes, reflection, jargon = await asyncio.gather(
            _memory(),
            _profile(),
            _nodes(),
            _reflection(),
            _jargon(),
        )
        sections: list[str] = []
        if memory:
            sections.append(memory)
        if jargon:
            sections.append(jargon)
        if profile:
            sections.append(f"[Profile]\n{profile}")
        if nodes:
            sections.append(f"[Nodes]\n{nodes}")
        if reflection:
            sections.append(f"[Reflection]\n{reflection}")
        if not sections:
            return "System note: no usable internal data was found."
        return "\n\n".join(sections)


__all__ = ["MemoryToolService"]
