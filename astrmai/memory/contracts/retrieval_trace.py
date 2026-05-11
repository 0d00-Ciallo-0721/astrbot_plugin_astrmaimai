from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from ...infrastructure.persistence.orm_models import MemoryRetrievalTrace


@dataclass(slots=True)
class RetrievalTrace:
    trace_id: str
    chat_id: str
    sender_name: str
    query: str
    planner_question: str
    tool_calls: str
    selected_memory_ids: str
    final_answer: str
    source_layers: str
    confidence: float = 0.0

    def to_orm_model(self) -> MemoryRetrievalTrace:
        return MemoryRetrievalTrace(
            trace_id=self.trace_id,
            chat_id=self.chat_id,
            sender_name=self.sender_name,
            query=self.query,
            planner_question=self.planner_question,
            tool_calls=self.tool_calls,
            selected_memory_ids=self.selected_memory_ids,
            final_answer=self.final_answer,
            source_layers=self.source_layers,
            confidence=self.confidence,
        )


__all__ = ["RetrievalTrace", "MemoryRetrievalTrace"]
