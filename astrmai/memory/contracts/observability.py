from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class MemoryObservationEvent:
    event_id: str
    chat_id: str
    component: str
    stage: str
    level: str = "info"
    turn_id: str = ""
    memory_id: str = ""
    reason: str = ""
    summary: str = ""
    timestamp: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryRuntimeStatusSnapshot:
    instant_gate_ready: bool = False
    memory_pipeline_ready: bool = False
    session_summarizer_ready: bool = False
    pipeline_running: bool = False
    sweep_task_running: bool = False
    buffered_chats: int = 0
    tracked_chats: int = 0
    active_worker_count: int = 0
    active_worker_chats: list[str] = field(default_factory=list)
    recent_error_count: int = 0
    recent_warning_count: int = 0
    last_gate_hit_at: float = 0.0
    last_backfill_success_at: float = 0.0
    last_summarize_success_at: float = 0.0
    last_summarize_failure_at: float = 0.0


@dataclass(slots=True)
class ChatMemoryStatusSnapshot:
    chat_id: str
    pending_messages: int = 0
    cooldown_until: float = 0.0
    failures: int = 0
    last_update: float = 0.0
    last_memory_run_at: float = 0.0
    worker_active: bool = False
    last_gate_stage: str = ""
    last_backfill_stage: str = ""
    last_summarize_stage: str = ""
    recent_events: list[dict[str, Any]] = field(default_factory=list)


__all__ = [
    "ChatMemoryStatusSnapshot",
    "MemoryObservationEvent",
    "MemoryRuntimeStatusSnapshot",
]
