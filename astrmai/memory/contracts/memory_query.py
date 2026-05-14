from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(slots=True)
class MemoryQuery:
    query: str
    session_id: str = ""
    persona_id: str = ""
    sender_id: str = ""
    layers: List[str] = field(default_factory=list)
    top_k: int = 5
    policy: str = "light"
    think_level: int | None = None
    intent: str = ""
    time_window: float | None = None
    include_feedback: bool = False
    include_persona_lore: bool = False
    exclude_ids: List[str] = field(default_factory=list)
    allow_stale: bool = False
    retrieve_keys: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryWriteRequest:
    source: str
    kind: str
    session_id: str
    content: str
    persona_id: str = ""
    summary: str = ""
    tags: List[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.8
    metadata: Dict[str, Any] = field(default_factory=dict)
    dedup_key: str = ""
    source_ref: str = ""
    visibility: str = "auto_and_tool"
    status: str = "active"


@dataclass(slots=True)
class MemoryCandidate:
    id: str
    kind: str
    source: str
    summary: str
    content: str
    session_id: str = ""
    persona_id: str = ""
    tags: List[str] = field(default_factory=list)
    importance: float = 0.5
    confidence: float = 0.8
    relevance_score: float = 0.0
    recency_score: float = 0.0
    status: str = "active"
    visibility: str = "auto_and_tool"
    created_at: float = 0.0
    updated_at: float = 0.0
    last_access_time: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryInjectionTrace:
    trace_id: str
    policy: str = "light"
    source: str = ""
    layers: List[str] = field(default_factory=list)
    injected: bool = False
    skip_reason: str = ""
    candidate_count: int = 0
    selected_count: int = 0
    selected_ids: List[str] = field(default_factory=list)
    summary_preview: str = ""


@dataclass(slots=True)
class MemoryInjectionBundle:
    rendered_prompt_block: str = ""
    items: List[MemoryCandidate] = field(default_factory=list)
    guidance: str = ""
    trace: MemoryInjectionTrace | None = None
    skip_reason: str = ""


@dataclass(slots=True)
class MemoryToolResult:
    query: str
    items: List[MemoryCandidate] = field(default_factory=list)
    guidance: str = ""
    trace_id: str = ""
    already_injected_ids: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


__all__ = [
    "MemoryCandidate",
    "MemoryInjectionBundle",
    "MemoryInjectionTrace",
    "MemoryQuery",
    "MemoryToolResult",
    "MemoryWriteRequest",
]
