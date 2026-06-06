from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(slots=True)
class MemoryQuery:
    """Query parameters for memory retrieval.

    ``layers`` is an **inclusion** list of ``kind`` values to retrieve.
    An empty list means "all kinds" (no filter).  Use ``exclude_kinds`` to
    subtract specific kinds from the result set after layer filtering.

    .. deprecated:: 2.x
        ``include_feedback`` and ``retrieve_keys`` are declared for API
        compatibility but are **not read** by any retrieval path.  Setting them
        has no effect.  They will be removed in a future major version.
    """
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
    exclude_kinds: List[str] = field(default_factory=list)
    # DEPRECATED: not read by any retrieval path; setting has no effect.
    include_feedback: bool = False
    include_persona_lore: bool = False
    exclude_ids: List[str] = field(default_factory=list)
    allow_stale: bool = False
    # DEPRECATED: not read by any retrieval path; setting has no effect.
    retrieve_keys: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.include_feedback:
            warnings.warn(
                "MemoryQuery.include_feedback is deprecated and has no effect. "
                "It will be removed in a future major version.",
                DeprecationWarning,
                stacklevel=2,
            )
        if self.retrieve_keys:
            warnings.warn(
                "MemoryQuery.retrieve_keys is deprecated and has no effect. "
                "It will be removed in a future major version.",
                DeprecationWarning,
                stacklevel=2,
            )


@dataclass(slots=True)
class MemoryWriteRequest:
    source: str
    kind: str
    session_id: str
    content: str
    sender_id: str = ""
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
    created_at: float = 0.0


@dataclass(slots=True)
class MemoryCandidate:
    id: str
    kind: str
    source: str
    summary: str
    content: str
    session_id: str = ""
    sender_id: str = ""
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
    superseded_by: str = ""
    access_count: int = 0
    decay_score: float = 1.0
    metadata_hydrated: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryClaim:
    subject_id: str
    entity: str
    attribute: str
    value: str
    polarity: str = "affirm"
    certainty: float = 0.5
    is_correction: bool = False
    fact_scope: str = "medium_term"
    source_text: str = ""
    evidence_turn_id: str = ""


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


@dataclass(slots=True)
class CommittedMemoryTurn:
    turn_id: str
    chat_id: str
    user_text: str
    assistant_text: str
    sender_id: str = ""
    source: str = ""
    is_proactive: bool = False
    think_level: int | None = None
    persona_id: str = ""
    committed_at: float = 0.0
    instant_gate_hit: bool = False
    instant_memory_id: str = ""


@dataclass(slots=True)
class InstantGateResult:
    hit: bool = False
    memory_id: str = ""
    category: str = ""
    skip_backfill: bool = False


__all__ = [
    "MemoryCandidate",
    "MemoryClaim",
    "MemoryInjectionBundle",
    "MemoryInjectionTrace",
    "MemoryQuery",
    "MemoryToolResult",
    "MemoryWriteRequest",
    "CommittedMemoryTurn",
    "InstantGateResult",
]
