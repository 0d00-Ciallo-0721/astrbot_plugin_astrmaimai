from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal


TriggerType = Literal["message", "external", "heartbeat"]
LoopAction = Literal[
    "INGRESS_MESSAGE",
    "INGRESS_EXTERNAL",
    "INTERRUPT_WAIT",
    "RESUME_WAIT",
    "PROACTIVE_WAKEUP",
    "HEARTFLOW_EVALUATE",
    "DREAM_MAINTENANCE",
    "MEMORY_MAINTENANCE",
    "COMPACTION_EVALUATE",
    "NOOP",
    "WAIT",
    "SKIP_BUSY",
]


@dataclass(slots=True)
class ChatLoopState:
    chat_id: str
    phase: str = "idle"
    last_trigger: str = ""
    last_decision: str = ""
    last_tick_at: float = 0.0
    last_message_at: float = 0.0
    last_heartbeat_at: float = 0.0
    next_tick_at: float = 0.0
    wait_mode: str = "none"
    wait_scope: str = ""
    wait_status: str = "idle"
    wait_target_ids: list[str] = field(default_factory=list)
    wait_target_name: str = ""
    wait_thread_signature: str = ""
    wait_started_at: float = 0.0
    wait_expires_at: float = 0.0
    wait_message_budget: int = 0
    wait_resume_reason: str = ""
    interrupt_reason: str = ""
    cooldowns: Dict[str, float] = field(default_factory=dict)
    pending_signals: Dict[str, Any] = field(default_factory=dict)
    last_background_action: str = ""
    last_interrupt_at: float = 0.0
    last_selected_at: float = 0.0
    consecutive_selected_count: int = 0
    last_maintenance_selected_at: float = 0.0
    retry_backoff_until: float = 0.0
    missed_due_passes: int = 0
    forced_promotion_count: int = 0
    last_forced_promotion_at: float = 0.0


@dataclass(slots=True)
class ChatLoopSnapshot:
    chat_id: str
    trigger_type: TriggerType
    has_new_message: bool = False
    latest_activity: Dict[str, Any] = field(default_factory=dict)
    executor_pending: int = 0
    wait_targets: list[str] = field(default_factory=list)
    message_signal: str = ""
    wait_signal: str = ""
    quiet_signal: str = ""
    attention_signal: str = ""
    proactive_signal: str = ""
    heartflow_signal: str = ""
    memory_signal: str = ""
    dream_signal: str = ""
    compaction_signal: str = ""
    group_wait_state: Dict[str, Any] = field(default_factory=dict)
    private_wait_state: Dict[str, Any] = field(default_factory=dict)
    proactive_summary: Dict[str, Any] = field(default_factory=dict)
    heartflow_summary: Dict[str, Any] = field(default_factory=dict)
    memory_summary: Dict[str, Any] = field(default_factory=dict)
    compaction_summary: Dict[str, Any] = field(default_factory=dict)
    dream_summary: Dict[str, Any] = field(default_factory=dict)
    quiet_summary: Dict[str, Any] = field(default_factory=dict)
    cooldown_state: Dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ChatLoopDecision:
    action: LoopAction
    reason: str
    should_dispatch: bool = False
    next_tick_delay: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChatLoopTickResult:
    state: ChatLoopState
    snapshot: ChatLoopSnapshot
    decision: ChatLoopDecision
    dispatch_result: Any = None
