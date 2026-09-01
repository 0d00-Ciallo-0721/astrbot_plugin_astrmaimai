# astrmai/memory/dream/dream_agent.py
"""
Dream maintenance agent.

The agent uses canonical Memory V2 records as maintenance seeds and falls back
to legacy MemoryEvent rows only when no canonical records are available.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from time import monotonic
from typing import Any, Dict, List, Optional

from astrbot.api import logger

from ...infrastructure.gateway.model_gateway import GlobalModelGateway
from ...infrastructure.persistence.database_service import DatabaseService
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from ...infrastructure.gateway.json_utils import parse_json_contract
from ..contracts.memory_query import MemoryQuery
from .fact_contract import format_dream_fact_log, normalize_dream_facts


class DreamAgent:
    MAX_ITERATIONS = 5
    TIMEOUT_SEC = 90.0
    SEED_QUERY_LIMIT = 10
    SEED_SAMPLE_SIZE = 5
    MIN_EVENTS_TO_DREAM = 5

    TOOLS = {
        "search_memory": "语义搜索记忆库，参数: {query: str, limit: int=5}",
        "get_memory_detail": "读取某条记忆的完整内容，参数: {event_id: str}",
        "merge_memories": "将多条冗余记忆合并为一条精华记忆，参数: {event_ids: [str], new_narrative: str}",
        "update_memory": "重写/精简某条记忆内容，参数: {event_id: str, new_narrative: str}",
        "delete_memory": "删除噪声或过时记忆，参数: {event_id: str, reason: str}",
        "search_jargon": "读取当前会话相关黑话词条，参数: {query: str='', limit: int=5}",
        "suggest_jargon_review": "提出黑话治理建议（只读，不直接修改词库），参数: {words: [str], reason: str}",
        "finish_dream": (
            "结束本次整理循环，参数: {summary: str, detected_facts: [{subject_id: str, entity: str, "
            "attribute: str, value: str, confidence_score: float, confidence_signal: str, evidence: dict}]}"
        ),
    }

    def __init__(
        self,
        gateway: GlobalModelGateway,
        db_service: DatabaseService,
        memory_engine=None,
        config=None,
        background_task_budget=None,
    ):
        self.gateway = gateway
        self.db = db_service
        self.memory_engine = memory_engine
        self.config = config if config else gateway.config
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()

    async def _call_background_llm(self, *, prompt: str, session_id: str):
        async def _call():
            return await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=LaneKey(
                    subsystem="bg",
                    task_family="dream",
                    scope_id=session_id or "global",
                    scope_kind="chat" if session_id and session_id != "global" else "global",
                ),
                base_origin="",
            )

        return await self.background_task_budget.run(
            _call,
            task_name="dream",
            scope_id=session_id or "global",
            defer_release_on_timeout=True,
        )

    async def run_dream_cycle(self, session_id: str = None) -> Optional[str]:
        if not session_id:
            session_id = await self._pick_random_session(preferred_session_id=getattr(self, "_last_session_id", None))
        if not session_id:
            logger.info("[DreamAgent] no eligible memory bucket, skipping this cycle")
            return None

        event_count = await self.count_session_events(session_id)
        if event_count < self.MIN_EVENTS_TO_DREAM:
            logger.debug(
                f"[DreamAgent] skip dream maintenance for session={session_id}: "
                f"events={event_count}, required={self.MIN_EVENTS_TO_DREAM}"
            )
            return None

        seed_events = await self._get_seed_events(session_id)
        if not seed_events:
            return None

        logger.info(f"[DreamAgent] start dream maintenance for session={session_id}")
        self._last_session_id = session_id

        dream_log: list[str] = []
        iteration = 0
        start_time = monotonic()

        events_desc = "\n".join(
            [f"- [{e.get('event_id', '?')}] {e.get('narrative', '')[:80]}..." for e in seed_events]
        )
        system_prompt = (
            "你是一个记忆整理者，正在处理下列记忆片段。\n"
            "你的目标是：合并重复内容、精简冗长叙事、删除无价值噪声，保留最精华的记忆。\n"
            "每次只执行一个工具操作，逐步完成整理。\n\n"
            f"当前待整理记忆：\n{events_desc}\n\n"
            "可用工具：\n" + "\n".join([f"- {k}: {v}" for k, v in self.TOOLS.items()])
        )

        messages = [{"role": "user", "content": system_prompt}]

        while iteration < self.MAX_ITERATIONS:
            if monotonic() - start_time > self.TIMEOUT_SEC:
                logger.warning(f"[DreamAgent] dream maintenance timeout after {self.TIMEOUT_SEC}s")
                break

            iteration += 1
            logger.debug(f"[DreamAgent] dream iteration={iteration}")

            try:
                response = await self._call_background_llm(
                    prompt=json.dumps(messages, ensure_ascii=False),
                    session_id=session_id,
                )
            except Exception as exc:
                logger.error(f"[DreamAgent] LLM call failed: {exc}")
                raise RuntimeError("dream_llm_call_failed") from exc

            action = self._parse_action(response)
            if not action:
                logger.debug("[DreamAgent] unable to parse action, retry required")
                raise ValueError("dream_action_parse_failed")

            tool_name = action.get("tool", "")
            params = action.get("params", {})
            thought = action.get("thought", "")
            dream_log.append(f"[思考] {thought}")

            observation = await self._execute_tool(tool_name, params, session_id)
            dream_log.append(f"[行动] {tool_name}({params}) -> {observation}")

            logger.debug(f"[DreamAgent] tool={tool_name} observation={observation[:80]}")

            if tool_name == "finish_dream":
                for fact in normalize_dream_facts(params.get("detected_facts", [])):
                    dream_log.append(format_dream_fact_log(fact))
                summary = params.get("summary", "整理完成")
                dream_log.append(f"[结束] {summary}")
                break

            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            messages.append(
                {
                    "role": "user",
                    "content": f"工具执行结果：{observation}\n请继续整理，或调用 finish_dream 结束。",
                }
            )

        log_text = "\n".join(dream_log)
        logger.info(f"[DreamAgent] dream maintenance completed in {iteration} rounds | session={session_id}")
        return log_text

    async def _execute_tool(self, tool_name: str, params: Dict, session_id: str) -> str:
        try:
            if tool_name == "search_memory":
                return await self._tool_search_memory(params, session_id)
            if tool_name == "get_memory_detail":
                return await self._tool_get_detail(params)
            if tool_name == "merge_memories":
                return await self._tool_merge(params, session_id)
            if tool_name == "update_memory":
                return await self._tool_update(params)
            if tool_name == "delete_memory":
                return await self._tool_delete(params)
            if tool_name == "search_jargon":
                return await self._tool_search_jargon(params, session_id)
            if tool_name == "suggest_jargon_review":
                return await self._tool_suggest_jargon_review(params)
            if tool_name == "finish_dream":
                return "整理循环结束"
            return f"未知工具: {tool_name}"
        except Exception as exc:
            return f"工具执行失败: {exc}"

    async def _tool_search_memory(self, params: Dict, session_id: str) -> str:
        query = params.get("query", "")
        limit = int(params.get("limit", 5))
        if not query:
            return "参数缺失: query"
        retrieval = getattr(self.memory_engine, "retrieval_service", None) if self.memory_engine else None
        if retrieval and hasattr(retrieval, "retrieve"):
            memory_query = MemoryQuery(query=str(query or ""), session_id=str(session_id or ""), top_k=limit)
            candidates = await retrieval.retrieve(memory_query)
            if hasattr(retrieval, "render_recall"):
                result = retrieval.render_recall(memory_query, candidates)
            else:
                result = "\n".join(
                    str(getattr(item, "summary", "") or getattr(item, "content", ""))
                    for item in candidates
                )
            return result or "未找到相关记忆"
        return "记忆检索服务不可用"

    async def _tool_get_detail(self, params: Dict) -> str:
        event_id = params.get("event_id", "")
        if not event_id:
            return "参数缺失: event_id"
        try:
            v2_store = getattr(self.memory_engine, "v2_store", None) if self.memory_engine else None
            getter = getattr(v2_store, "get_by_id", None)
            if self._is_canonical_memory_id(event_id) and callable(getter):
                memory = await getter(str(event_id), allow_stale=True)
                if memory:
                    metadata = dict(getattr(memory, "metadata", {}) or {})
                    return (
                        f"叙事: {getattr(memory, 'content', '')}\n"
                        f"情感: {metadata.get('emotion', '')}\n"
                        f"重要度: {getattr(memory, 'importance', 0)}"
                    )
                return "未找到该记忆"
            event = await asyncio.to_thread(self._get_event_by_id, event_id)
            if event:
                if event.get("error"):
                    return f"读取失败: {event.get('error', '')}"
                return (
                    f"叙事: {event.get('narrative', '')}\n"
                    f"情感: {event.get('emotion', '')}\n"
                    f"重要度: {event.get('importance', 0)}"
                )
            return "未找到该记忆"
        except Exception as exc:
            return f"读取失败: {exc}"

    async def _tool_merge(self, params: Dict, session_id: str) -> str:
        event_ids = params.get("event_ids", [])
        new_narrative = params.get("new_narrative", "")
        if not event_ids or not new_narrative:
            return "参数缺失: event_ids 或 new_narrative"
        try:
            new_memory_id = ""
            if self.memory_engine:
                new_memory_id = await self.memory_engine.add_memory(
                    content=new_narrative,
                    session_id=session_id,
                    importance=0.7,
                )
                if not str(new_memory_id or "").strip():
                    logger.warning("[DreamAgent] merge write was skipped; no canonical memory id returned")
                    return "合并失败: 新记忆写入被过滤或失败"
                canonical_ids = []
                for event_id in event_ids:
                    if self._is_canonical_memory_id(event_id):
                        canonical_ids.append(str(event_id))
                        continue
                    finder = getattr(getattr(self.memory_engine, "v2_store", None), "find_ids_by_source_ref", None)
                    if finder:
                        canonical_ids.extend(await finder(f"MemoryEvent:{event_id}"))
                if canonical_ids and getattr(self.memory_engine, "maintenance_service", None) and new_memory_id:
                    await self.memory_engine.maintenance_service.mark_merged(
                        canonical_ids,
                        superseded_by=new_memory_id,
                    )
            if event_ids:
                logger.info(
                    f"[DreamAgent] legacy merge skipped for {len(event_ids)} events; "
                    f"canonical merge completed via {new_memory_id}"
                )
            return f"已合并 {len(event_ids)} 条记忆为精华: {new_narrative[:50]}..."
        except Exception as exc:
            return f"合并失败: {exc}"

    async def _tool_update(self, params: Dict) -> str:
        event_id = params.get("event_id", "")
        new_narrative = params.get("new_narrative", "")
        if not event_id or not new_narrative:
            return "参数缺失"
        try:
            canonical_ids, _unresolved_legacy_ids = await self._resolve_canonical_ids([str(event_id or "")])
            if canonical_ids and self.memory_engine and getattr(self.memory_engine, "v2_store", None):
                updated = 0
                for memory_id in canonical_ids:
                    updated += await self.memory_engine.v2_store.update_content(
                        str(memory_id),
                        content=new_narrative,
                        summary=new_narrative[:240],
                    )
                    if updated and getattr(self.memory_engine, "index_projector", None):
                        await self.memory_engine.index_projector.project(str(memory_id))
                if updated:
                    if not self._is_canonical_memory_id(event_id):
                        logger.info(
                            f"[DreamAgent] legacy update skipped for {event_id}; "
                            f"canonical {canonical_ids[0]} is now the source of truth"
                        )
                    return f"Updated memory {canonical_ids[0]}"
                return f"Cannot update inactive memory {canonical_ids[0]}"
            if not self._is_canonical_memory_id(event_id):
                logger.info(
                    f"[DreamAgent] legacy-only update for {event_id} — "
                    f"no canonical mapping found, update deferred"
                )
            if self.memory_engine:
                finder = getattr(getattr(self.memory_engine, "v2_store", None), "find_ids_by_source_ref", None)
                if finder:
                    for memory_id in await finder(f"MemoryEvent:{event_id}"):
                        await self.memory_engine.v2_store.update_content(
                            memory_id,
                            content=new_narrative,
                            summary=new_narrative[:240],
                        )
                        if getattr(self.memory_engine, "index_projector", None):
                            await self.memory_engine.index_projector.project(memory_id)
            return f"Updated memory {event_id}"
        except Exception as exc:
            return f"更新失败: {exc}"

    async def _resolve_canonical_ids(self, event_ids: List[str]) -> tuple[list[str], list[str]]:
        canonical_ids: list[str] = []
        unresolved_legacy_ids: list[str] = []
        v2_store = getattr(self.memory_engine, "v2_store", None) if self.memory_engine else None
        finder = getattr(v2_store, "find_ids_by_source_ref", None)
        for event_id in event_ids:
            normalized = str(event_id or "").strip()
            if not normalized:
                continue
            if self._is_canonical_memory_id(normalized):
                canonical_ids.append(normalized)
                continue
            found = list(await finder(f"MemoryEvent:{normalized}")) if finder else []
            if found:
                canonical_ids.extend(str(item) for item in found if str(item or "").strip())
            else:
                unresolved_legacy_ids.append(normalized)
        return list(dict.fromkeys(canonical_ids)), list(dict.fromkeys(unresolved_legacy_ids))

    @staticmethod
    def _is_canonical_memory_id(event_id: str) -> bool:
        normalized = str(event_id or "").strip()
        return normalized.startswith(("mem_", "memory_", "canon_"))

    async def _tool_delete(self, params: Dict) -> str:
        event_id = params.get("event_id", "")
        reason = params.get("reason", "冗余/过时")
        if not event_id:
            return "参数缺失: event_id"
        try:
            if self.memory_engine:
                maintenance = getattr(self.memory_engine, "maintenance_service", None)
                if self._is_canonical_memory_id(event_id) and maintenance:
                    await maintenance.soft_delete(str(event_id), reason=reason)
                    return f"Deleted memory {event_id} (reason: {reason})"
                logger.info(
                    f"[DreamAgent] legacy delete skipped for {event_id}; "
                    f"soft-deleting canonical by source_ref only"
                )
                finder = getattr(getattr(self.memory_engine, "v2_store", None), "find_ids_by_source_ref", None)
                if finder and maintenance:
                    for memory_id in await finder(f"MemoryEvent:{event_id}"):
                        await maintenance.soft_delete(memory_id, reason=reason)
            return f"Deleted memory {event_id} (reason: {reason})"
        except Exception as exc:
            return f"删除失败: {exc}"

    async def _tool_search_jargon(self, params: Dict, session_id: str) -> str:
        query = str(params.get("query", "") or "").strip()
        limit = int(params.get("limit", 5) or 5)
        retrieval = getattr(self.memory_engine, "retrieval_service", None) if self.memory_engine else None
        if not retrieval:
            return "黑话检索服务不可用"
        try:
            items = await retrieval.retrieve(
                MemoryQuery(
                    query=query or session_id,
                    session_id=str(session_id or ""),
                    layers=["jargon"],
                    top_k=limit,
                    intent="jargon",
                    allow_stale=True,
                    metadata={"visibility_mode": "tool"},
                )
            )
            if not items:
                return "未找到相关黑话"
            lines = []
            for item in items[:limit]:
                metadata = dict(getattr(item, "metadata", {}) or {})
                text = str(getattr(item, "content", "") or "")
                meaning = str(getattr(item, "summary", "") or metadata.get("meaning", "") or "")
                count = int(metadata.get("count") or 0)
                canonical_id = str(getattr(item, "id", "") or "")
                status = str(getattr(item, "status", "") or "active")
                if text:
                    lines.append(
                        f"{text} [{canonical_id or '-'} | {status}]: {meaning or '含义待补全'} (频次:{count})"
                    )
            return "\n".join(lines) if lines else "未找到相关黑话"
        except Exception as exc:
            return f"黑话检索失败: {exc}"

    async def _tool_suggest_jargon_review(self, params: Dict) -> str:
        words = params.get("words", []) or []
        reason = str(params.get("reason", "") or "疑似低价值或含义漂移")
        if not isinstance(words, list):
            words = [str(words)]
        normalized = [str(word).strip() for word in words if str(word).strip()]
        if not normalized:
            return "未提供需要建议复核的黑话"
        return "建议复核黑话: " + "、".join(normalized[:6]) + f" | 原因: {reason}"

    def _get_event_by_id(self, event_id: str) -> Optional[Dict]:
        from ...infrastructure.persistence import MemoryEvent
        from sqlmodel import select

        try:
            with self.db.get_session() as session:
                stmt = select(MemoryEvent).where(MemoryEvent.event_id == event_id)
                event = session.exec(stmt).first()
                if event:
                    return {
                        "event_id": event.event_id,
                        "narrative": event.narrative,
                        "emotion": event.emotion,
                        "importance": event.importance,
                    }
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to read legacy memory event {event_id}: {exc}")
            return {"error": str(exc)}
        return None

    def _delete_event(self, event_id: str):
        from ...infrastructure.persistence import MemoryEvent
        from sqlmodel import select

        try:
            with self.db.get_session() as session:
                stmt = select(MemoryEvent).where(MemoryEvent.event_id == event_id)
                event = session.exec(stmt).first()
                if event:
                    session.delete(event)
                    session.commit()
                    logger.debug(f"[DreamAgent] deleted legacy memory event: {event_id}")
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to delete legacy event: {exc}")

    def _update_event_narrative(self, event_id: str, new_narrative: str):
        from ...infrastructure.persistence import MemoryEvent
        from sqlmodel import select

        try:
            with self.db.get_session() as session:
                stmt = select(MemoryEvent).where(MemoryEvent.event_id == event_id)
                event = session.exec(stmt).first()
                if event:
                    event.narrative = new_narrative
                    session.add(event)
                    session.commit()
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to update legacy event: {exc}")

    @staticmethod
    def _serialize_seed_event(event) -> Dict[str, Any]:
        return {
            "event_id": event.event_id,
            "narrative": event.narrative,
            "emotion": event.emotion,
            "importance": event.importance,
        }

    @staticmethod
    def _serialize_canonical_seed(memory) -> Dict[str, Any]:
        metadata = dict(getattr(memory, "metadata", {}) or {})
        narrative = str(getattr(memory, "content", "") or getattr(memory, "summary", "") or "")
        return {
            "event_id": str(getattr(memory, "id", "") or ""),
            "narrative": narrative,
            "emotion": str(metadata.get("emotion", "") or ""),
            "importance": float(getattr(memory, "importance", 0.5) or 0.5),
            "session_id": str(getattr(memory, "session_id", "") or ""),
            "source": "memory_v2",
        }

    async def _load_canonical_candidates(self, session_id: str = "", *, limit: int) -> list[Any]:
        store = getattr(self.memory_engine, "v2_store", None) if self.memory_engine else None
        loader = getattr(store, "list_candidates", None)
        if not callable(loader):
            return []
        candidates = list(
            await loader(
                session_id=str(session_id or ""),
                limit=max(1, int(limit or self.SEED_QUERY_LIMIT)),
                include_inactive=False,
            )
            or []
        )
        if not session_id:
            return candidates
        return [
            item
            for item in candidates
            if str(getattr(item, "session_id", "") or "") == str(session_id)
        ]

    async def _load_canonical_seed_events(self, session_id: str, *, limit: int) -> List[Dict[str, Any]]:
        candidates = await self._load_canonical_candidates(session_id, limit=limit)
        return [
            self._serialize_canonical_seed(item)
            for item in candidates
            if str(getattr(item, "content", "") or getattr(item, "summary", "") or "").strip()
        ]

    def _load_session_events(self, session, session_id: str):
        from ...infrastructure.persistence import MemoryEvent
        from sqlmodel import desc, select

        stmt = (
            select(MemoryEvent)
            .where(MemoryEvent.session_id == session_id)
            .order_by(desc(MemoryEvent.created_at))
            .limit(self.SEED_QUERY_LIMIT)
        )
        events = session.exec(stmt).all()
        if events:
            return events

        stmt = (
            select(MemoryEvent)
            .where(MemoryEvent.date == session_id)
            .order_by(desc(MemoryEvent.created_at))
            .limit(self.SEED_QUERY_LIMIT)
        )
        return session.exec(stmt).all()

    @staticmethod
    def _select_session_bucket(sessions: List[str], preferred_session_id: Optional[str] = None) -> Optional[str]:
        normalized = [str(item or "").strip() for item in sessions if str(item or "").strip()]
        if not normalized:
            return None
        preferred = str(preferred_session_id or "").strip()
        if preferred and preferred in normalized:
            return preferred
        return random.choice(normalized)

    async def _pick_random_session(self, preferred_session_id: Optional[str] = None) -> Optional[str]:
        from ...infrastructure.persistence import MemoryEvent
        from sqlmodel import select

        try:
            candidates = await self._load_canonical_candidates("", limit=500)
            buckets: dict[str, int] = {}
            for item in candidates:
                session_key = str(getattr(item, "session_id", "") or "")
                if session_key:
                    buckets[session_key] = buckets.get(session_key, 0) + 1
            selected = self._select_session_bucket(
                [key for key, count in buckets.items() if count >= self.MIN_EVENTS_TO_DREAM],
                preferred_session_id=preferred_session_id,
            )
            if selected:
                return selected
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to choose canonical session: {exc}")

        try:
            def _query():
                with self.db.get_session() as session:
                    stmt = select(MemoryEvent.session_id, MemoryEvent.date)
                    rows = session.exec(stmt).all()
                    buckets = {}
                    for session_key, date_key in rows:
                        bucket_key = session_key or date_key
                        if not bucket_key:
                            continue
                        buckets[bucket_key] = buckets.get(bucket_key, 0) + 1
                    return [key for key, count in buckets.items() if count >= self.MIN_EVENTS_TO_DREAM]

            sessions = await asyncio.to_thread(_query)
            selected = self._select_session_bucket(sessions, preferred_session_id=preferred_session_id)
            if selected:
                return selected
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to choose session: {exc}")
        return None

    async def count_session_events(self, session_id: str) -> int:
        from ...infrastructure.persistence import MemoryEvent
        from sqlalchemy import func
        from sqlmodel import select

        session_id = str(session_id or "").strip()
        if not session_id:
            return 0

        try:
            canonical = await self._load_canonical_candidates(
                session_id,
                limit=max(self.SEED_QUERY_LIMIT, self.MIN_EVENTS_TO_DREAM),
            )
            if canonical:
                return len(canonical)
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to count canonical seed events: {exc}")

        try:
            def _query() -> int:
                with self.db.get_session() as session:
                    count = session.exec(
                        select(func.count()).select_from(MemoryEvent).where(MemoryEvent.session_id == session_id)
                    ).one()
                    if int(count or 0) > 0:
                        return int(count)
                    legacy_count = session.exec(
                        select(func.count()).select_from(MemoryEvent).where(MemoryEvent.date == session_id)
                    ).one()
                    return int(legacy_count or 0)

            return await asyncio.to_thread(_query)
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to count seed events: {exc}")
            return 0

    async def _get_seed_events(self, session_id: str) -> List[Dict]:
        try:
            canonical = await self._load_canonical_seed_events(
                session_id,
                limit=self.SEED_QUERY_LIMIT,
            )
            if canonical:
                return random.sample(canonical, min(self.SEED_SAMPLE_SIZE, len(canonical)))
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to fetch canonical seed events: {exc}")

        try:
            def _query():
                with self.db.get_session() as session:
                    events = self._load_session_events(session, session_id)
                    return [self._serialize_seed_event(event) for event in events]

            events = await asyncio.to_thread(_query)
            return random.sample(events, min(self.SEED_SAMPLE_SIZE, len(events))) if events else []
        except Exception as exc:
            logger.warning(f"[DreamAgent] failed to fetch seed events: {exc}")
            return []

    @staticmethod
    def _parse_action(response) -> Optional[Dict]:
        if isinstance(response, dict):
            required = {"tool", "params"}
            if required.issubset(response) and isinstance(response.get("tool"), str) and isinstance(response.get("params"), dict):
                return response
        parsed = parse_json_contract(
            response,
            required_keys=("tool", "params"),
            optional_keys=("thought",),
            field_types={"tool": str, "params": dict, "thought": str},
            allow_extra_keys=False,
            allow_naked_members=True,
        )
        return dict(parsed.value) if parsed.schema_valid else None


__all__ = ["DreamAgent"]
