import asyncio
import time
from typing import List, Optional

from sqlmodel import desc, or_, select

from .orm_models import DailyReflection, MemoryEvent, MemoryNode, MemoryRetrievalTrace


class MemoryPersistenceMixin:
    def update_nodes(self, nodes: List[MemoryNode]):
        with self.get_session() as session:
            for node in nodes:
                statement = select(MemoryNode).where(MemoryNode.name == node.name)
                existing = session.exec(statement).first()
                if existing:
                    existing.type = node.type
                    existing.description = node.description
                    existing.last_updated = time.time()
                    session.add(existing)
                else:
                    session.add(node)
            session.commit()

    def search_nodes(self, query: str, limit: int = 3, include_description: bool = True) -> List[MemoryNode]:
        with self.get_session() as session:
            lower_query = f"%{query.lower()}%"
            conditions = [MemoryNode.name.like(lower_query)]
            if include_description:
                conditions.append(MemoryNode.description.like(lower_query))
            statement = (
                select(MemoryNode)
                .where(or_(*conditions))
                .order_by(MemoryNode.last_updated.desc())
                .limit(limit)
            )
            results = session.exec(statement).all()
            return [MemoryNode.model_validate(item.model_dump()) for item in results]

    def save_reflection(self, date: str, reflection: str):
        with self.get_session() as session:
            statement = select(DailyReflection).where(DailyReflection.date == date)
            existing = session.exec(statement).first()
            if existing:
                existing.reflection = reflection
                session.add(existing)
            else:
                session.add(DailyReflection(date=date, reflection=reflection))
            session.commit()

    def get_reflection(self, date: str) -> Optional[DailyReflection]:
        with self.get_session() as session:
            statement = select(DailyReflection).where(DailyReflection.date == date)
            result = session.exec(statement).first()
            return DailyReflection.model_validate(result.model_dump()) if result else None

    def save_event(self, event: MemoryEvent):
        with self.get_session() as session:
            statement = select(MemoryEvent).where(MemoryEvent.event_id == event.event_id)
            existing = session.exec(statement).first()
            if existing:
                existing.session_id = getattr(event, "session_id", "") or existing.session_id
                existing.narrative = event.narrative
                existing.emotion = event.emotion
                existing.importance = event.importance
                existing.emotional_intensity = event.emotional_intensity
                existing.reflection = event.reflection
                existing.tags = event.tags
                session.add(existing)
            else:
                session.add(event)
            session.commit()

    def save_retrieval_trace(self, trace: MemoryRetrievalTrace):
        with self.get_session() as session:
            statement = select(MemoryRetrievalTrace).where(MemoryRetrievalTrace.trace_id == trace.trace_id)
            existing = session.exec(statement).first()
            if existing:
                existing.chat_id = trace.chat_id
                existing.sender_name = trace.sender_name
                existing.query = trace.query
                existing.planner_question = trace.planner_question
                existing.tool_calls = trace.tool_calls
                existing.selected_memory_ids = trace.selected_memory_ids
                existing.final_answer = trace.final_answer
                existing.source_layers = trace.source_layers
                existing.confidence = trace.confidence
                session.add(existing)
            else:
                session.add(trace)
            session.commit()

    def get_recent_retrieval_traces(self, chat_id: str, limit: int = 10) -> List[MemoryRetrievalTrace]:
        with self.get_session() as session:
            statement = (
                select(MemoryRetrievalTrace)
                .where(MemoryRetrievalTrace.chat_id == chat_id)
                .order_by(desc(MemoryRetrievalTrace.created_at))
                .limit(limit)
            )
            results = session.exec(statement).all()
            return [MemoryRetrievalTrace.model_validate(item.model_dump()) for item in results]

    async def update_nodes_async(self, nodes):
        async with self._db_lock:
            return await asyncio.to_thread(self.update_nodes, nodes)

    async def search_nodes_async(self, query: str, limit: int = 3, include_description: bool = True):
        return await asyncio.to_thread(self.search_nodes, query, limit, include_description)

    async def save_reflection_async(self, date: str, reflection: str):
        async with self._db_lock:
            return await asyncio.to_thread(self.save_reflection, date, reflection)

    async def get_reflection_async(self, date: str):
        return await asyncio.to_thread(self.get_reflection, date)

    async def save_event_async(self, event):
        async with self._db_lock:
            return await asyncio.to_thread(self.save_event, event)

    async def save_retrieval_trace_async(self, trace: MemoryRetrievalTrace):
        async with self._db_lock:
            return await asyncio.to_thread(self.save_retrieval_trace, trace)

    async def get_recent_retrieval_traces_async(self, chat_id: str, limit: int = 10) -> List[MemoryRetrievalTrace]:
        return await asyncio.to_thread(self.get_recent_retrieval_traces, chat_id, limit)