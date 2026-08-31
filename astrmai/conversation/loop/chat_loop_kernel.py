from __future__ import annotations

import inspect
import time
from time import monotonic
from copy import deepcopy
from typing import Any, Awaitable, Callable

from astrbot.api import logger

from ...infrastructure.runtime.observability import RuntimeObservabilityHub
from ...infrastructure.runtime.trace_runtime import debug_trace, ensure_external_result_id
from ...proactive.rhythm import evaluate_proactive_rhythm
from ...shared.helpers.plugin_helpers import safe_create_task
from .models import ChatLoopDecision, ChatLoopSnapshot, ChatLoopState, ChatLoopTickResult
from .state_store import ChatLoopStateStore


MessageHandler = Callable[[Any], Awaitable[Any]]
HeartbeatHandler = Callable[[str, ChatLoopSnapshot, ChatLoopDecision], Awaitable[Any]]
DispatchBridge = Callable[[str, ChatLoopSnapshot, ChatLoopDecision], Awaitable[Any]]


class ChatLoopKernel:
    BACKGROUND_ACTIONS = {
        "PROACTIVE_WAKEUP",
        "HEARTFLOW_EVALUATE",
        "DREAM_MAINTENANCE",
        "MEMORY_MAINTENANCE",
        "COMPACTION_EVALUATE",
    }
    COOLDOWN_ACTIONS = {"wakeup", "heartflow", "compaction", "followup"}
    DEFAULT_FOLLOWUP_COOLDOWN_SEC = 8.0
    HEARTBEAT_DUE_HORIZON_SECONDS = 2.0
    HEARTBEAT_MAX_BATCH = 32
    FAST_RECHECK_SECONDS = 5.0
    WAIT_RECHECK_SECONDS = 20.0
    POST_DIALOGUE_RECHECK_SECONDS = 15.0
    MAINTENANCE_RECHECK_SECONDS = 120.0
    DREAM_RECHECK_SECONDS = 180.0
    MAINTENANCE_CANDIDATE_RECHECK_SECONDS = 30.0
    IDLE_BACKOFF_SECONDS = 300.0
    COOLDOWN_MIN_SECONDS = 15.0
    COOLDOWN_MAX_SECONDS = 300.0
    NORMAL_POLL_SECONDS = 10.0
    IDLE_POLL_SECONDS = 15.0
    FORCED_PROMOTION_MAX_SLOTS = 4
    PERSISTENT_DUE_RESERVED_SLOTS = 2
    DIALOGUE_BATCH_SLOTS = 24
    MAINTENANCE_BATCH_SLOTS = 4
    MAINTENANCE_HEAVY_DIALOGUE_SLOTS = 12
    MAINTENANCE_HEAVY_BATCH_SLOTS = 12
    IDLE_DIALOGUE_BATCH_SLOTS = 8
    BUSY_BACKPRESSURE_RATIO = 0.5
    BUSY_BACKPRESSURE_DIALOGUE_CAP = 16
    MAINTENANCE_QUOTA_PRESSURE_ESCALATION = 4
    MAINTENANCE_BUDGET_BLOCKED_IDLE_THRESHOLD = 3
    CONSECUTIVE_SELECTION_SOFT_LIMIT = 3
    CONSECUTIVE_SELECTION_BIAS_MULTIPLIER = 8.0
    PHASE_PRIORITY = {
        "ACTIVE": 0,
        "WAITING": 1,
        "BUSY": 2,
        "COOLDOWN": 3,
        "MAINTENANCE": 4,
        "IDLE": 5,
    }
    PHASE_SCORES = {
        "WAITING": 80.0,
        "BUSY": 70.0,
        "ACTIVE": 60.0,
        "COOLDOWN": 35.0,
        "MAINTENANCE": 45.0,
        "IDLE": 20.0,
    }
    STARVATION_DIVISOR_SECONDS = 30.0
    FAIRNESS_PENALTY_MULTIPLIER = 12.0
    MAINTENANCE_BOOST_DIVISOR_SECONDS = 45.0
    RETRY_PRESSURE_BONUS = 25.0
    STARVATION_PASS_THRESHOLDS = {
        "WAITING": 2,
        "BUSY": 2,
        "ACTIVE": 3,
        "COOLDOWN": 4,
        "MAINTENANCE": 4,
        "IDLE": 5,
    }
    DEFAULT_SCHEDULER_PROFILE_NAME = "balanced"
    _SCHEDULER_BASE = {
        "fairness_penalty_multiplier": 12.0,
        "selection_cooldown_soft_limit": 3,
        "selection_cooldown_bias_multiplier": 8.0,
        "maintenance_boost_divisor_seconds": 45.0,
        "starvation_divisor_seconds": 30.0,
        "forced_promotion_max_slots": 4,
        "dialogue_batch_slots": 24,
        "maintenance_batch_slots": 4,
        "maintenance_heavy_dialogue_slots": 12,
        "maintenance_heavy_batch_slots": 12,
        "busy_backpressure_ratio": 0.5,
        "busy_backpressure_dialogue_cap": 16,
        "forced_promotion_pass_thresholds": {
            "WAITING": 2,
            "BUSY": 2,
            "ACTIVE": 3,
            "COOLDOWN": 4,
            "MAINTENANCE": 4,
            "IDLE": 5,
        },
    }
    SCHEDULER_POLICY_PROFILES = {
        "balanced": _SCHEDULER_BASE,
        "dialogue_first": {
            **_SCHEDULER_BASE,
            "forced_promotion_pass_thresholds": {
                "WAITING": 2,
                "BUSY": 2,
                "ACTIVE": 3,
                "COOLDOWN": 5,
                "MAINTENANCE": 5,
                "IDLE": 6,
            },
            "fairness_penalty_multiplier": 14.0,
            "selection_cooldown_bias_multiplier": 10.0,
            "maintenance_boost_divisor_seconds": 60.0,
            "starvation_divisor_seconds": 28.0,
            "maintenance_heavy_batch_slots": 10,
            "busy_backpressure_ratio": 0.45,
        },
        "maintenance_friendly": {
            **_SCHEDULER_BASE,
            "forced_promotion_pass_thresholds": {
                "WAITING": 2,
                "BUSY": 2,
                "ACTIVE": 3,
                "COOLDOWN": 4,
                "MAINTENANCE": 3,
                "IDLE": 4,
            },
            "fairness_penalty_multiplier": 10.0,
            "selection_cooldown_soft_limit": 4,
            "selection_cooldown_bias_multiplier": 6.0,
            "maintenance_boost_divisor_seconds": 30.0,
            "starvation_divisor_seconds": 24.0,
            "dialogue_batch_slots": 20,
            "maintenance_batch_slots": 6,
            "maintenance_heavy_dialogue_slots": 10,
            "maintenance_heavy_batch_slots": 14,
            "busy_backpressure_ratio": 0.55,
            "busy_backpressure_dialogue_cap": 14,
        },
    }

    def __init__(
        self,
        *,
        runtime_coordinator: Any = None,
        message_handler: MessageHandler | None = None,
        heartbeat_handler: HeartbeatHandler | None = None,
        state_store: ChatLoopStateStore | None = None,
        observability_hub: RuntimeObservabilityHub | None = None,
    ) -> None:
        self.runtime_coordinator = runtime_coordinator
        self._message_handler = message_handler
        self._heartbeat_handler = heartbeat_handler
        self._state_store = state_store or ChatLoopStateStore()
        self.observability_hub = observability_hub
        self._dispatch_bridges: dict[str, DispatchBridge] = {}
        self.group_reply_wait_manager = None
        self.private_chat_manager = None
        self.wakeup_service = None
        self.heartflow_manager = None
        self.memory_service = None
        self.dream_scheduler = None
        self.context_compaction = None
        self._heartbeat_pass_context: dict[str, Any] = {}
        self._last_due_selection_report: dict[str, Any] = {}
        self._maintenance_idle_rounds = 0
        self._maintenance_quota_pressure_rounds = 0
        self._scheduler_policy_profile_name = self.DEFAULT_SCHEDULER_PROFILE_NAME

    def bind_message_handler(self, handler: MessageHandler | None) -> None:
        self._message_handler = handler

    def bind_heartbeat_handler(self, handler: HeartbeatHandler | None) -> None:
        self._heartbeat_handler = handler

    def bind_dispatch_bridge(self, action: str, handler: DispatchBridge | None) -> None:
        action_name = str(action or "").strip().upper()
        if not action_name:
            return
        if handler is None:
            self._dispatch_bridges.pop(action_name, None)
            return
        self._dispatch_bridges[action_name] = handler

    def bind_signal_sources(
        self,
        *,
        group_reply_wait_manager: Any = None,
        private_chat_manager: Any = None,
        wakeup_service: Any = None,
        heartflow_manager: Any = None,
        memory_service: Any = None,
        dream_scheduler: Any = None,
        context_compaction: Any = None,
    ) -> None:
        if group_reply_wait_manager is not None:
            self.group_reply_wait_manager = group_reply_wait_manager
        if private_chat_manager is not None:
            self.private_chat_manager = private_chat_manager
        if wakeup_service is not None:
            self.wakeup_service = wakeup_service
        if heartflow_manager is not None:
            self.heartflow_manager = heartflow_manager
        if memory_service is not None:
            self.memory_service = memory_service
        if dream_scheduler is not None:
            self.dream_scheduler = dream_scheduler
        if context_compaction is not None:
            self.context_compaction = context_compaction

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _event_sender_id(event: Any) -> str:
        if event is None or not hasattr(event, "get_sender_id"):
            return ""
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _event_source_name(event: Any) -> str:
        if event is None or not hasattr(event, "get_extra"):
            return ""
        try:
            return str(event.get_extra("astrmai_loop_source", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _dedupe_ids(items: list[Any]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in items or []:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            ordered.append(text)
        return ordered

    @staticmethod
    def _has_active_wait(state: ChatLoopState) -> bool:
        return str(state.wait_mode or "none") != "none" and str(state.wait_status or "idle") == "armed"

    @staticmethod
    def _wait_state_payload(state: ChatLoopState) -> dict[str, Any]:
        if not ChatLoopKernel._has_active_wait(state):
            return {}
        return {
            "mode": state.wait_mode,
            "scope": state.wait_scope,
            "status": state.wait_status,
            "target_ids": list(state.wait_target_ids or []),
            "target_name": state.wait_target_name,
            "thread_signature": state.wait_thread_signature,
            "remaining_seconds": max(0.0, float(state.wait_expires_at or 0.0) - monotonic()) if state.wait_expires_at > 0 else 0.0,
            "remaining_messages": int(state.wait_message_budget or 0),
        }

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        value = str(scope or "").strip().lower()
        if value in {"group", "private", "runtime"}:
            return value
        return ""

    @classmethod
    def _default_scheduler_policy_values(cls) -> dict[str, Any]:
        return {
            "forced_promotion_pass_thresholds": dict(cls.STARVATION_PASS_THRESHOLDS),
            "fairness_penalty_multiplier": cls.FAIRNESS_PENALTY_MULTIPLIER,
            "selection_cooldown_soft_limit": cls.CONSECUTIVE_SELECTION_SOFT_LIMIT,
            "selection_cooldown_bias_multiplier": cls.CONSECUTIVE_SELECTION_BIAS_MULTIPLIER,
            "maintenance_boost_divisor_seconds": cls.MAINTENANCE_BOOST_DIVISOR_SECONDS,
            "starvation_divisor_seconds": cls.STARVATION_DIVISOR_SECONDS,
            "forced_promotion_max_slots": cls.FORCED_PROMOTION_MAX_SLOTS,
            "dialogue_batch_slots": cls.DIALOGUE_BATCH_SLOTS,
            "maintenance_batch_slots": cls.MAINTENANCE_BATCH_SLOTS,
            "maintenance_heavy_dialogue_slots": cls.MAINTENANCE_HEAVY_DIALOGUE_SLOTS,
            "maintenance_heavy_batch_slots": cls.MAINTENANCE_HEAVY_BATCH_SLOTS,
            "idle_dialogue_batch_slots": cls.IDLE_DIALOGUE_BATCH_SLOTS,
            "busy_backpressure_ratio": cls.BUSY_BACKPRESSURE_RATIO,
            "busy_backpressure_dialogue_cap": cls.BUSY_BACKPRESSURE_DIALOGUE_CAP,
        }

    def _scheduler_policy(self) -> dict[str, Any]:
        cached = getattr(self, "_cached_scheduler_policy", None)
        if cached is not None:
            return cached
        default_policy = self._default_scheduler_policy_values()
        active_profile = str(self._scheduler_policy_profile_name or self.DEFAULT_SCHEDULER_PROFILE_NAME)
        profile_values = dict(self.SCHEDULER_POLICY_PROFILES.get(active_profile, {}) or {})
        merged = deepcopy(default_policy)
        for key, value in profile_values.items():
            merged[key] = deepcopy(value)
        self._cached_scheduler_policy = merged
        return merged

    def _invalidate_scheduler_policy_cache(self):
        self._cached_scheduler_policy = None

    def _scheduler_policy_value(self, key: str, default: Any = None) -> Any:
        policy = self._scheduler_policy()
        return policy.get(key, default)

    @classmethod
    def scheduler_policy_profiles_sync(cls) -> dict[str, dict[str, Any]]:
        return {name: deepcopy(policy) for name, policy in cls.SCHEDULER_POLICY_PROFILES.items()}

    def set_scheduler_profile_for_testing(self, name: str) -> None:
        profile_name = str(name or "").strip()
        if profile_name not in self.SCHEDULER_POLICY_PROFILES:
            raise ValueError(f"Unknown scheduler profile: {profile_name}")
        self._scheduler_policy_profile_name = profile_name
        self._invalidate_scheduler_policy_cache()

    def scheduler_policy_sync(self) -> dict[str, Any]:
        return {
            "active_profile": str(self._scheduler_policy_profile_name or self.DEFAULT_SCHEDULER_PROFILE_NAME),
            "available_profiles": list(self.SCHEDULER_POLICY_PROFILES.keys()),
            "current": deepcopy(self._scheduler_policy()),
            "profiles": self.scheduler_policy_profiles_sync(),
        }

    def describe_last_due_selection_sync(self) -> dict[str, Any]:
        return deepcopy(self._last_due_selection_report or {})

    def describe_status_sync(self) -> dict[str, Any]:
        active_turn_task_count = 0
        coordinator = self.runtime_coordinator
        if coordinator is not None and hasattr(coordinator, "active_turn_task_count_sync"):
            try:
                active_turn_task_count = int(coordinator.active_turn_task_count_sync() or 0)
            except Exception:
                active_turn_task_count = 0
        return {
            "enabled": True,
            "tracked_chats": self._state_store.count_sync(),
            "active_turn_task_count": active_turn_task_count,
            "message_handler_bound": self._message_handler is not None,
            "heartbeat_handler_bound": self._heartbeat_handler is not None,
            "decision_mode": "single_primary_action",
            "heartbeat_dispatch_mode": "kernel_mediated",
            "private_wait_visible_in_heartbeat": True,
            "heartflow_preview_readonly": True,
            "dream_scope": "global_throttle",
            "dispatch_bridges": {
                "PROACTIVE_WAKEUP": "PROACTIVE_WAKEUP" in self._dispatch_bridges,
                "HEARTFLOW_EVALUATE": "HEARTFLOW_EVALUATE" in self._dispatch_bridges,
                "DREAM_MAINTENANCE": "DREAM_MAINTENANCE" in self._dispatch_bridges,
                "MEMORY_MAINTENANCE": "MEMORY_MAINTENANCE" in self._dispatch_bridges,
                "COMPACTION_EVALUATE": "COMPACTION_EVALUATE" in self._dispatch_bridges,
            },
            "scheduler_policy": self.scheduler_policy_sync(),
            "last_due_selection_summary": {
                "selected_count": len(list(self._last_due_selection_report.get("selected", []) or [])),
                "skipped_not_due_count": len(list(self._last_due_selection_report.get("skipped_not_due", []) or [])),
                "skipped_by_batch_count": len(list(self._last_due_selection_report.get("skipped_by_batch", []) or [])),
                "poll_mode": str(self._last_due_selection_report.get("poll_mode", "") or ""),
                "batch_fill_rate": float(self._last_due_selection_report.get("batch_fill_rate", 0.0) or 0.0),
                "batch_plan": deepcopy(self._last_due_selection_report.get("batch_plan", {}) or {}),
                "quota_skip_counts": deepcopy(self._last_due_selection_report.get("quota_skip_counts", {}) or {}),
                "forced_promotion_count": len(list(self._last_due_selection_report.get("forced_promotions_selected", []) or [])),
            },
            "last_due_selection_report": self.describe_last_due_selection_sync(),
        }

    async def describe_status(self) -> dict[str, Any]:
        status = self.describe_status_sync()
        status["tracked_chats"] = await self._state_store.count()
        return status

    def set_heartbeat_scheduler_context(self, context: dict[str, Any] | None) -> None:
        self._heartbeat_pass_context = dict(context or {})

    def clear_heartbeat_scheduler_context(self) -> None:
        self._heartbeat_pass_context = {}

    def get_heartbeat_scheduler_context(self) -> dict[str, Any]:
        return dict(self._heartbeat_pass_context or {})

    async def get_loop_state(self, chat_id: str) -> ChatLoopState:
        return await self._state_store.get_or_create(str(chat_id or ""))

    async def peek_loop_state(self, chat_id: str) -> ChatLoopState | None:
        return await self._state_store.get(str(chat_id or ""))

    async def clear_chat_state(self, chat_id: str) -> bool:
        chat_key = str(chat_id or "")
        self._heartbeat_pass_context.pop(chat_key, None)
        return await self._state_store.clear(chat_key)

    async def arm_group_wait(self, chat_id: str, payload: dict[str, Any]) -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        now = monotonic()
        remaining_seconds = float(payload.get("remaining_seconds", 0.0) or 0.0)
        expires_at = now + remaining_seconds if remaining_seconds > 0 else 0.0
        self._apply_wait_arm(
            state,
            wait_mode="group_reply",
            wait_scope="group",
            target_ids=[payload.get("target_user_id", "")],
            target_name=str(payload.get("target_name", "") or ""),
            thread_signature=str(payload.get("thread_signature", "") or ""),
            started_at=now,
            expires_at=expires_at,
            message_budget=int(payload.get("remaining_messages", 0) or 0),
            reason=str(payload.get("reason", "group_wait_armed") or "group_wait_armed"),
        )
        await self._state_store.save(state)
        await self._clear_runtime_wait_targets_if_needed(chat_id)
        return state

    async def arm_private_wait(self, chat_id: str, payload: dict[str, Any]) -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        now = monotonic()
        wait_timeout = float(payload.get("timeout", 0.0) or 0.0)
        expires_at = now + wait_timeout if wait_timeout > 0 else 0.0
        self._apply_wait_arm(
            state,
            wait_mode="private_reply",
            wait_scope="private",
            target_ids=payload.get("target_ids") or [payload.get("user_id", "")],
            target_name=str(payload.get("target_name", "") or ""),
            thread_signature=str(payload.get("thread_signature", "") or ""),
            started_at=now,
            expires_at=expires_at,
            message_budget=int(payload.get("message_budget", 1) or 1),
            reason=str(payload.get("reason", "private_wait_armed") or "private_wait_armed"),
        )
        await self._state_store.save(state)
        await self._clear_runtime_wait_targets_if_needed(chat_id)
        return state

    async def sync_runtime_wait_targets(self, chat_id: str, targets: list[str], target_name: str = "") -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        target_ids = self._dedupe_ids(list(targets or []))
        if not target_ids:
            if state.wait_mode == "runtime_targets":
                self._clear_wait_state(state, status="expired", reason="runtime_wait_targets_cleared")
            await self._state_store.save(state)
            return state
        self._apply_wait_arm(
            state,
            wait_mode="runtime_targets",
            wait_scope="runtime",
            target_ids=target_ids,
            target_name=str(target_name or ""),
            thread_signature="",
            started_at=time.time(),
            expires_at=0.0,
            message_budget=0,
            reason="runtime_wait_targets_synced",
        )
        await self._state_store.save(state)
        await self._mirror_runtime_wait_targets(chat_id, target_ids, str(target_name or ""))
        return state

    async def resume_wait(
        self,
        chat_id: str,
        reason: str,
        *,
        resume_target_id: str = "",
        resume_source: str = "",
    ) -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        state.wait_resume_reason = str(reason or "")
        state.pending_signals["resume_source"] = str(resume_source or "")
        state.pending_signals["resume_target_id"] = str(resume_target_id or "")
        self._clear_wait_state(state, status="resumed", reason=reason)
        await self._state_store.save(state)
        await self._clear_runtime_wait_targets_if_needed(chat_id)
        return state

    async def interrupt_wait(self, chat_id: str, reason: str, *, source: str = "") -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        state.pending_signals["interrupt_source"] = str(source or "")
        self._clear_wait_state(state, status="interrupted", reason=reason)
        state.last_interrupt_at = time.time()
        await self._state_store.save(state)
        await self._clear_runtime_wait_targets_if_needed(chat_id)
        return state

    async def expire_wait(self, chat_id: str, reason: str) -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        self._clear_wait_state(state, status="expired", reason=reason)
        await self._state_store.save(state)
        await self._clear_runtime_wait_targets_if_needed(chat_id)
        return state

    async def set_cooldown(self, chat_id: str, action: str, until_ts: float, reason: str = "") -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        action_key = str(action or "").strip().lower()
        if action_key and action_key != "dream":
            if float(until_ts or 0.0) > time.time():
                state.cooldowns[action_key] = float(until_ts or 0.0)
                if reason:
                    state.pending_signals[f"{action_key}_cooldown_reason"] = str(reason or "")
                state.pending_signals[f"{action_key}_cooldown_set_at"] = time.time()
            else:
                state.cooldowns.pop(action_key, None)
        await self._state_store.save(state)
        return state

    async def clear_cooldown(self, chat_id: str, action: str) -> ChatLoopState:
        state = await self._state_store.get_or_create(str(chat_id or ""))
        action_key = str(action or "").strip().lower()
        if action_key:
            state.cooldowns.pop(action_key, None)
            state.pending_signals.pop(f"{action_key}_cooldown_reason", None)
            state.pending_signals.pop(f"{action_key}_cooldown_set_at", None)
        await self._state_store.save(state)
        return state

    async def tick(self, *, chat_id: str, trigger: str, event: Any = None) -> ChatLoopTickResult:
        chat_id = str(chat_id or "")
        trigger = str(trigger or "").strip().lower() or "heartbeat"
        external_result_id = ""
        tick_started = monotonic()
        if trigger == "external" and event is not None:
            external_result_id = ensure_external_result_id(event)
            debug_trace(
                event,
                "external_result.kernel_tick_enter",
                external_result_id=external_result_id,
                chat_id=chat_id,
            )
        state = await self._state_store.get_or_create(chat_id)
        pre_state_summary = self._summarize_state(state)
        snapshot = await self._build_snapshot(state, chat_id, trigger, event)
        decision = self._decide(state, snapshot, event)
        self._plan_next_tick(state, snapshot, decision, None)
        self._update_state(state, snapshot, decision)
        await self._state_store.save(state)
        dispatch_result = None
        try:
            dispatch_result = await self._dispatch(chat_id, snapshot, decision, event)
        except Exception as exc:
            self._apply_dispatch_failure_state(state, snapshot, decision, exc)
            await self._state_store.save(state)
            dispatch_result = {
                "dispatch_failed": True,
                "dispatch_error_type": exc.__class__.__name__,
                "dispatch_error_reason": str(exc or ""),
            }
            self._trace_tick(state, snapshot, decision, dispatch_result, pre_state_summary)
            if external_result_id:
                debug_trace(
                    event,
                    "external_result.kernel_tick_failed",
                    external_result_id=external_result_id,
                    elapsed_ms=round((monotonic() - tick_started) * 1000.0, 1),
                    error_type=type(exc).__name__,
                )
            raise
        if external_result_id:
            debug_trace(
                event,
                "external_result.kernel_tick_after_dispatch",
                external_result_id=external_result_id,
                action=decision.action,
                elapsed_ms=round((monotonic() - tick_started) * 1000.0, 1),
            )
        self._apply_post_dispatch_state(state, decision, dispatch_result)
        if (
            trigger == "message"
            and str(state.wait_mode or "none") == "none"
            and bool(getattr(self.group_reply_wait_manager, "threaded_enabled", False))
        ):
            await self._sync_wait_from_adapters(state, chat_id, event)
        await self._state_store.save(state)
        self._trace_tick(state, snapshot, decision, dispatch_result, pre_state_summary)
        if external_result_id:
            debug_trace(
                event,
                "external_result.kernel_tick_return",
                external_result_id=external_result_id,
                action=decision.action,
                elapsed_ms=round((monotonic() - tick_started) * 1000.0, 1),
                next_tick_delay=float(decision.next_tick_delay or 0.0),
            )
        return ChatLoopTickResult(state=state, snapshot=snapshot, decision=decision, dispatch_result=dispatch_result)

    async def select_due_chats(
        self,
        chat_ids: list[str],
        *,
        now: float | None = None,
        horizon_seconds: float | None = None,
        max_batch: int | None = None,
        candidate_sources: dict[str, str] | None = None,
    ) -> list[str]:
        report = await self.describe_due_selection(
            chat_ids,
            now=now,
            horizon_seconds=horizon_seconds,
            max_batch=max_batch,
            candidate_sources=candidate_sources,
        )
        return list(report.get("selected", []) or [])

    async def describe_due_selection(
        self,
        chat_ids: list[str],
        *,
        now: float | None = None,
        horizon_seconds: float | None = None,
        max_batch: int | None = None,
        candidate_sources: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        now_ts = float(now if now is not None else time.time())
        horizon = float(self.HEARTBEAT_DUE_HORIZON_SECONDS if horizon_seconds is None else horizon_seconds)
        batch_limit = int(self.HEARTBEAT_MAX_BATCH if max_batch is None else max_batch)
        ranked: list[tuple[tuple[float, float, float, int, float], str, dict[str, Any]]] = []
        skipped_not_due: list[str] = []
        score_breakdown: dict[str, dict[str, Any]] = {}
        due_phase_mix: dict[str, int] = {}
        states_by_chat: dict[str, ChatLoopState] = {}
        normalized_sources = {
            str(chat_id or ""): str(source or "active")
            for chat_id, source in dict(candidate_sources or {}).items()
            if str(chat_id or "").strip()
        }

        for chat_id in [str(item or "") for item in chat_ids or [] if str(item or "").strip()]:
            candidate_source = normalized_sources.get(chat_id, "active")
            state = await self._state_store.get(chat_id)
            runtime_snapshot = {}
            if self.runtime_coordinator is not None and hasattr(self.runtime_coordinator, "get_activity_snapshot"):
                try:
                    payload = await self.runtime_coordinator.get_activity_snapshot(chat_id)
                    if isinstance(payload, dict):
                        runtime_snapshot = payload
                except Exception as exc:
                    logger.debug(f"[ChatLoopKernel] due-chat runtime snapshot degraded for {chat_id}: {exc}")

            latest_ts = float(runtime_snapshot.get("latest_activity_ts", 0.0) or 0.0)
            if state is None:
                breakdown = {
                    "chat_id": chat_id,
                    "candidate_source": candidate_source,
                    "persistent_due_candidate": candidate_source == "persistent_due",
                    "is_new_chat": True,
                    "due_rank": 0,
                    "phase": "ACTIVE",
                    "quota_bucket": "DIALOGUE",
                    "quota_skip_reason": "",
                    "scheduler_score": 1000000.0,
                    "overdue_score": 1000000.0,
                    "phase_score": self.PHASE_SCORES["ACTIVE"],
                    "starvation_score": 0.0,
                    "fairness_penalty": 0.0,
                    "selection_cooldown_bias": 0.0,
                    "maintenance_boost": 0.0,
                    "maintenance_backlog_score": 0.0,
                    "retry_pressure": 0.0,
                    "retry_backoff_until": 0.0,
                    "starvation_age": 0.0,
                    "missed_due_passes": 0,
                    "starvation_tier": "new_chat",
                    "forced_promotion_eligible": False,
                    "pressure_components": {},
                    "selected_reason": "new_chat_due",
                    "not_selected_reason": "",
                    "latest_activity_ts": latest_ts,
                    "next_tick_at": 0.0,
                }
                score_breakdown[chat_id] = breakdown
                due_phase_mix["ACTIVE"] = int(due_phase_mix.get("ACTIVE", 0) or 0) + 1
                ranked.append(((0.0, float("-inf"), -breakdown["scheduler_score"], 0, -latest_ts), chat_id, breakdown))
                continue

            self._expire_wait_if_needed(state)
            self._prune_expired_cooldowns(state)
            states_by_chat[chat_id] = state
            next_tick_at = float(state.next_tick_at or 0.0)
            if next_tick_at > (now_ts + horizon):
                skipped_not_due.append(chat_id)
                continue
            overdue = now_ts - next_tick_at if next_tick_at > 0 else float("inf")
            breakdown = self._build_due_score_breakdown(
                state,
                runtime_snapshot=runtime_snapshot,
                now=now_ts,
                overdue=overdue,
                latest_activity_ts=latest_ts,
            )
            breakdown["candidate_source"] = candidate_source
            breakdown["persistent_due_candidate"] = candidate_source == "persistent_due"
            score_breakdown[chat_id] = breakdown
            phase_name = str(breakdown.get("phase", "IDLE") or "IDLE")
            due_phase_mix[phase_name] = int(due_phase_mix.get(phase_name, 0) or 0) + 1
            new_due_priority = 0.0 if next_tick_at <= 0 else 1.0
            phase_priority = self.PHASE_PRIORITY.get(phase_name, self.PHASE_PRIORITY["IDLE"])
            ranked.append(((new_due_priority, -float(overdue), -float(breakdown["scheduler_score"]), phase_priority, -latest_ts), chat_id, breakdown))

        ranked.sort()
        poll_mode = self._determine_poll_mode(due_phase_mix)
        batch_pressure = self._build_batch_pressure(score_breakdown, ranked)
        promotion_candidates = [chat_id for _, chat_id, breakdown in ranked if bool(breakdown.get("forced_promotion_eligible", False))]
        persistent_due_candidates = [
            chat_id
            for _, chat_id, breakdown in ranked
            if bool(breakdown.get("persistent_due_candidate", False))
        ]
        batch_plan = self._build_batch_plan(
            batch_limit=batch_limit,
            due_phase_mix=due_phase_mix,
            promotion_candidates=promotion_candidates,
            persistent_due_candidates=persistent_due_candidates,
            batch_pressure=batch_pressure,
            score_breakdown=score_breakdown,
        )
        selection_summary = self._select_due_entries(
            ranked=ranked,
            score_breakdown=score_breakdown,
            batch_plan=batch_plan,
        )
        selected = selection_summary["selected"]
        selected_entries = selection_summary["selected_entries"]
        skipped_by_batch = selection_summary["skipped_by_batch"]
        quota_skipped = selection_summary["quota_skipped"]
        forced_promotions_selected = selection_summary["forced_promotions_selected"]
        dialogue_selected = selection_summary["dialogue_selected"]
        maintenance_selected = selection_summary["maintenance_selected"]
        persistent_due_selected = selection_summary["persistent_due_selected"]
        batch_fill_rate = (len(selected) / float(batch_limit)) if batch_limit > 0 else 0.0
        maintenance_budget_total = self._determine_maintenance_budget(
            due_phase_mix,
            score_breakdown,
            selected,
            skipped_by_batch,
        )
        report = {
            "selected": selected,
            "skipped_not_due": skipped_not_due,
            "skipped_by_batch": skipped_by_batch,
            "score_breakdown": score_breakdown,
            "due_phase_mix": due_phase_mix,
            "poll_mode": poll_mode,
            "poll_mode_reason": self._poll_mode_reason(due_phase_mix),
            "promotion_candidates": promotion_candidates,
            "persistent_due_candidates": persistent_due_candidates,
            "persistent_due_selected": persistent_due_selected,
            "forced_promotions_selected": forced_promotions_selected,
            "dialogue_selected": dialogue_selected,
            "maintenance_selected": maintenance_selected,
            "batch_plan": batch_plan,
            "batch_fill_rate": batch_fill_rate,
            "batch_pressure": batch_pressure,
            "quota_skipped": quota_skipped,
            "quota_skip_counts": {
                "skipped_by_dialogue_quota": len(quota_skipped["skipped_by_dialogue_quota"]),
                "skipped_by_maintenance_quota": len(quota_skipped["skipped_by_maintenance_quota"]),
                "skipped_by_promotion_overflow": len(quota_skipped["skipped_by_promotion_overflow"]),
            },
            "maintenance_budget_total": maintenance_budget_total,
            "maintenance_budget_used": 0,
            "maintenance_budget_remaining": maintenance_budget_total,
            "maintenance_blocked_by_budget": [],
            "busy_backpressure_active": bool(batch_plan.get("busy_backpressure_active", False)),
            "maintenance_backpressure_active": bool(batch_plan.get("maintenance_backpressure_active", False)),
        }
        self._update_quota_pressure_rounds(report)
        self._last_due_selection_report = report
        self._emit_due_selection_observability(report)
        return report

    def _build_due_score_breakdown(
        self,
        state: ChatLoopState,
        *,
        runtime_snapshot: dict[str, Any],
        now: float,
        overdue: float,
        latest_activity_ts: float,
    ) -> dict[str, Any]:
        phase_name = str(state.phase or "IDLE").upper()
        starvation_divisor_seconds = float(
            self._scheduler_policy_value("starvation_divisor_seconds", self.STARVATION_DIVISOR_SECONDS)
        )
        fairness_penalty_multiplier = float(
            self._scheduler_policy_value("fairness_penalty_multiplier", self.FAIRNESS_PENALTY_MULTIPLIER)
        )
        selection_cooldown_soft_limit = int(
            self._scheduler_policy_value("selection_cooldown_soft_limit", self.CONSECUTIVE_SELECTION_SOFT_LIMIT)
        )
        selection_cooldown_bias_multiplier = float(
            self._scheduler_policy_value(
                "selection_cooldown_bias_multiplier",
                self.CONSECUTIVE_SELECTION_BIAS_MULTIPLIER,
            )
        )
        maintenance_boost_divisor_seconds = float(
            self._scheduler_policy_value(
                "maintenance_boost_divisor_seconds",
                self.MAINTENANCE_BOOST_DIVISOR_SECONDS,
            )
        )
        starvation_pass_thresholds = dict(
            self._scheduler_policy_value("forced_promotion_pass_thresholds", self.STARVATION_PASS_THRESHOLDS) or {}
        )
        wait_pressure = self._wait_pressure_score(state, now)
        busy_pressure = 40.0 if int(runtime_snapshot.get("executor_pending", 0) or 0) > 0 else 0.0
        maintenance_backlog_score = self._maintenance_backlog_score(state)
        retry_backoff_until = float(state.retry_backoff_until or 0.0)
        retry_pressure = self.RETRY_PRESSURE_BONUS if retry_backoff_until > now else 0.0
        starvation_age = max(0.0, now - float(state.last_selected_at or 0.0)) if state.last_selected_at > 0 else max(120.0, overdue if overdue != float("inf") else 120.0)
        starvation_score = starvation_age / starvation_divisor_seconds
        fairness_penalty = float(max(0, int(state.consecutive_selected_count or 0))) * fairness_penalty_multiplier
        selection_cooldown_bias = 0.0
        if int(state.consecutive_selected_count or 0) > selection_cooldown_soft_limit:
            selection_cooldown_bias = float(
                int(state.consecutive_selected_count or 0) - selection_cooldown_soft_limit
            ) * selection_cooldown_bias_multiplier
        maintenance_boost = 0.0
        if maintenance_backlog_score > 0.0:
            maintenance_age = max(0.0, now - float(state.last_maintenance_selected_at or 0.0)) if state.last_maintenance_selected_at > 0 else max(180.0, starvation_age)
            maintenance_boost = maintenance_age / maintenance_boost_divisor_seconds
        overdue_score = 300.0 if overdue == float("inf") else max(0.0, float(overdue))
        phase_score = float(self.PHASE_SCORES.get(phase_name, self.PHASE_SCORES["IDLE"]))
        missed_due_passes = int(state.missed_due_passes or 0)
        starvation_threshold = int(starvation_pass_thresholds.get(phase_name, starvation_pass_thresholds.get("IDLE", self.STARVATION_PASS_THRESHOLDS["IDLE"])))
        forced_promotion_eligible = missed_due_passes >= starvation_threshold
        starvation_tier = "forced" if forced_promotion_eligible else ("watch" if missed_due_passes > 0 else "normal")
        scheduler_score = overdue_score + phase_score + starvation_score + wait_pressure + busy_pressure + maintenance_backlog_score + retry_pressure + maintenance_boost - fairness_penalty - selection_cooldown_bias
        return {
            "chat_id": state.chat_id,
            "is_new_chat": False,
            "due_rank": 0,
            "phase": phase_name,
            "quota_bucket": self._quota_bucket(phase_name, maintenance_backlog_score),
            "quota_skip_reason": "",
            "scheduler_score": scheduler_score,
            "overdue_score": overdue_score,
            "phase_score": phase_score,
            "starvation_score": starvation_score,
            "fairness_penalty": fairness_penalty,
            "selection_cooldown_bias": selection_cooldown_bias,
            "maintenance_boost": maintenance_boost,
            "maintenance_backlog_score": maintenance_backlog_score,
            "retry_pressure": retry_pressure,
            "retry_backoff_until": retry_backoff_until,
            "starvation_age": starvation_age,
            "missed_due_passes": missed_due_passes,
            "starvation_tier": starvation_tier,
            "forced_promotion_eligible": forced_promotion_eligible,
            "pressure_components": {
                "wait_pressure": wait_pressure,
                "busy_pressure": busy_pressure,
                "maintenance_pressure": maintenance_backlog_score,
                "retry_pressure": retry_pressure,
            },
            "selected_reason": "",
            "not_selected_reason": "",
            "latest_activity_ts": latest_activity_ts,
            "next_tick_at": float(state.next_tick_at or 0.0),
        }

    def _wait_pressure_score(self, state: ChatLoopState, now: float) -> float:
        if not self._has_active_wait(state):
            return 0.0
        wait_expires_at = float(state.wait_expires_at or 0.0)
        if wait_expires_at <= 0.0:
            return 20.0
        remaining = max(0.0, wait_expires_at - now)
        return max(5.0, 30.0 - min(25.0, remaining))

    @staticmethod
    def _maintenance_backlog_score(state: ChatLoopState) -> float:
        summary = dict((state.pending_signals or {}).get("maintenance_candidates_summary", {}) or {})
        score = 0.0
        if bool(summary.get("compaction", {}).get("eligible", False)):
            score += 45.0
        memory_summary = dict(summary.get("memory", {}) or {})
        if bool(memory_summary.get("eligible", False) or memory_summary.get("candidate_present", False)):
            score += 30.0
        if bool(summary.get("dream", {}).get("eligible", False)):
            score += 15.0
        return score

    def _determine_poll_mode(self, due_phase_mix: dict[str, int]) -> str:
        if any(int(due_phase_mix.get(name, 0) or 0) > 0 for name in ("WAITING", "BUSY", "ACTIVE")):
            return "FAST"
        if any(int(value or 0) > 0 for value in due_phase_mix.values()):
            return "NORMAL"
        return "IDLE"

    @staticmethod
    def _poll_mode_reason(due_phase_mix: dict[str, int]) -> str:
        if any(int(due_phase_mix.get(name, 0) or 0) > 0 for name in ("WAITING", "BUSY", "ACTIVE")):
            return "dialogue_pressure"
        if any(int(value or 0) > 0 for value in due_phase_mix.values()):
            return "background_due_only"
        return "idle_backoff"

    @staticmethod
    def _quota_bucket(phase_name: str, maintenance_backlog_score: float) -> str:
        if phase_name == "MAINTENANCE" or maintenance_backlog_score > 0.0:
            return "MAINTENANCE"
        return "DIALOGUE"

    @staticmethod
    def _build_batch_pressure(
        score_breakdown: dict[str, dict[str, Any]],
        ranked: list[tuple[tuple[float, float, float, int, float], str, dict[str, Any]]],
    ) -> dict[str, Any]:
        due_count = len(ranked)
        if due_count <= 0:
            return {
                "busy_ratio": 0.0,
                "maintenance_backlog_ratio": 0.0,
                "retry_pressure_count": 0,
            }
        busy_count = 0
        maintenance_backlog_count = 0
        retry_count = 0
        for _, chat_id, _ in ranked:
            breakdown = score_breakdown.get(chat_id, {})
            pressure = dict(breakdown.get("pressure_components", {}) or {})
            if float(pressure.get("busy_pressure", 0.0) or 0.0) > 0.0:
                busy_count += 1
            if float(breakdown.get("maintenance_backlog_score", 0.0) or 0.0) > 0.0:
                maintenance_backlog_count += 1
            if float(breakdown.get("retry_pressure", 0.0) or 0.0) > 0.0:
                retry_count += 1
        return {
            "busy_ratio": float(busy_count) / float(due_count),
            "maintenance_backlog_ratio": float(maintenance_backlog_count) / float(due_count),
            "retry_pressure_count": retry_count,
        }

    def _build_batch_plan(
        self,
        *,
        batch_limit: int,
        due_phase_mix: dict[str, int],
        promotion_candidates: list[str],
        persistent_due_candidates: list[str],
        batch_pressure: dict[str, Any],
        score_breakdown: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        total_limit = max(0, int(batch_limit))
        has_dialogue_pressure = any(int(due_phase_mix.get(name, 0) or 0) > 0 for name in ("WAITING", "BUSY", "ACTIVE"))
        has_maintenance_backlog = any(float(item.get("maintenance_backlog_score", 0.0) or 0.0) > 0.0 for item in score_breakdown.values())
        busy_backpressure_ratio = float(
            self._scheduler_policy_value("busy_backpressure_ratio", self.BUSY_BACKPRESSURE_RATIO)
        )
        forced_promotion_max_slots = int(
            self._scheduler_policy_value("forced_promotion_max_slots", self.FORCED_PROMOTION_MAX_SLOTS)
        )
        dialogue_batch_slots = int(
            self._scheduler_policy_value("dialogue_batch_slots", self.DIALOGUE_BATCH_SLOTS)
        )
        maintenance_batch_slots = int(
            self._scheduler_policy_value("maintenance_batch_slots", self.MAINTENANCE_BATCH_SLOTS)
        )
        maintenance_heavy_dialogue_slots = int(
            self._scheduler_policy_value(
                "maintenance_heavy_dialogue_slots",
                self.MAINTENANCE_HEAVY_DIALOGUE_SLOTS,
            )
        )
        maintenance_heavy_batch_slots = int(
            self._scheduler_policy_value(
                "maintenance_heavy_batch_slots",
                self.MAINTENANCE_HEAVY_BATCH_SLOTS,
            )
        )
        idle_dialogue_batch_slots = int(
            self._scheduler_policy_value("idle_dialogue_batch_slots", self.IDLE_DIALOGUE_BATCH_SLOTS)
        )
        busy_backpressure_dialogue_cap = int(
            self._scheduler_policy_value(
                "busy_backpressure_dialogue_cap",
                self.BUSY_BACKPRESSURE_DIALOGUE_CAP,
            )
        )
        busy_backpressure_active = has_dialogue_pressure and float(batch_pressure.get("busy_ratio", 0.0) or 0.0) >= busy_backpressure_ratio
        maintenance_backpressure_active = has_maintenance_backlog and self._maintenance_quota_pressure_rounds > 0
        promotion_slots = min(forced_promotion_max_slots, len(promotion_candidates), total_limit)
        persistent_due_slots = min(
            self.PERSISTENT_DUE_RESERVED_SLOTS,
            len(persistent_due_candidates),
            max(0, total_limit - promotion_slots),
        )
        remaining = max(0, total_limit - promotion_slots - persistent_due_slots)
        if has_dialogue_pressure:
            dialogue_slots = min(dialogue_batch_slots, remaining)
            if busy_backpressure_active:
                dialogue_slots = min(dialogue_slots, busy_backpressure_dialogue_cap)
            maintenance_slots = min(maintenance_batch_slots, max(0, remaining - dialogue_slots))
        elif has_maintenance_backlog:
            maintenance_slots = maintenance_heavy_batch_slots + (
                self.MAINTENANCE_QUOTA_PRESSURE_ESCALATION if maintenance_backpressure_active else 0
            )
            maintenance_slots = min(maintenance_slots, remaining)
            dialogue_slots = min(maintenance_heavy_dialogue_slots, max(0, remaining - maintenance_slots))
        else:
            dialogue_slots = min(idle_dialogue_batch_slots, remaining)
            maintenance_slots = 0
        overflow_slots = max(0, remaining - dialogue_slots - maintenance_slots)
        return {
            "total_limit": total_limit,
            "promotion_slots": promotion_slots,
            "persistent_due_slots": persistent_due_slots,
            "dialogue_slots": dialogue_slots,
            "maintenance_slots": maintenance_slots,
            "overflow_slots": overflow_slots,
            "busy_backpressure_active": busy_backpressure_active,
            "maintenance_backpressure_active": maintenance_backpressure_active,
        }

    def _select_due_entries(
        self,
        *,
        ranked: list[tuple[tuple[float, float, float, int, float], str, dict[str, Any]]],
        score_breakdown: dict[str, dict[str, Any]],
        batch_plan: dict[str, Any],
    ) -> dict[str, Any]:
        entries = [(chat_id, breakdown) for _, chat_id, breakdown in ranked]
        promotion_slots = int(batch_plan.get("promotion_slots", 0) or 0)
        persistent_due_slots = int(batch_plan.get("persistent_due_slots", 0) or 0)
        dialogue_slots = int(batch_plan.get("dialogue_slots", 0) or 0)
        maintenance_slots = int(batch_plan.get("maintenance_slots", 0) or 0)
        overflow_slots = int(batch_plan.get("overflow_slots", 0) or 0)

        promotion_candidates = [(chat_id, breakdown) for chat_id, breakdown in entries if bool(breakdown.get("forced_promotion_eligible", False))]
        forced_promotions_selected = [chat_id for chat_id, _ in promotion_candidates[:promotion_slots]]
        selected_set = set(forced_promotions_selected)

        remaining_entries = [(chat_id, breakdown) for chat_id, breakdown in entries if chat_id not in selected_set]
        persistent_due_candidates = [
            (chat_id, breakdown)
            for chat_id, breakdown in remaining_entries
            if bool(breakdown.get("persistent_due_candidate", False))
        ]
        persistent_due_selected = [chat_id for chat_id, _ in persistent_due_candidates[:persistent_due_slots]]
        selected_set.update(persistent_due_selected)

        remaining_entries = [(chat_id, breakdown) for chat_id, breakdown in entries if chat_id not in selected_set]
        dialogue_candidates = [(chat_id, breakdown) for chat_id, breakdown in remaining_entries if str(breakdown.get("quota_bucket", "DIALOGUE")) != "MAINTENANCE"]
        maintenance_candidates = [(chat_id, breakdown) for chat_id, breakdown in remaining_entries if str(breakdown.get("quota_bucket", "DIALOGUE")) == "MAINTENANCE"]

        dialogue_selected = [chat_id for chat_id, _ in dialogue_candidates[:dialogue_slots]]
        selected_set.update(dialogue_selected)

        maintenance_selected = [chat_id for chat_id, _ in maintenance_candidates[:maintenance_slots]]
        selected_set.update(maintenance_selected)

        remaining_after_quota = [(chat_id, breakdown) for chat_id, breakdown in entries if chat_id not in selected_set]
        reclaimed_reserved_slots = max(0, persistent_due_slots - len(persistent_due_selected))
        overflow_selected = [chat_id for chat_id, _ in remaining_after_quota[:reclaimed_reserved_slots]]
        selected_set.update(overflow_selected)
        ranked_selected = [chat_id for chat_id, _ in entries if chat_id in selected_set and chat_id not in forced_promotions_selected]
        selected_order = list(forced_promotions_selected) + ranked_selected

        selected_entries: list[tuple[tuple[float, float, float, int, float], str, dict[str, Any]]] = []
        ranked_map = {chat_id: item for item, chat_id, _ in ranked}
        quota_skipped = {
            "skipped_by_dialogue_quota": [],
            "skipped_by_maintenance_quota": [],
            "skipped_by_promotion_overflow": [],
        }
        forced_candidate_set = {chat_id for chat_id, _ in promotion_candidates}
        dialogue_candidate_set = {chat_id for chat_id, _ in dialogue_candidates}
        maintenance_candidate_set = {chat_id for chat_id, _ in maintenance_candidates}

        for index, chat_id in enumerate(selected_order, start=1):
            breakdown = score_breakdown.get(chat_id, {})
            breakdown["due_rank"] = index
            if chat_id in forced_candidate_set and chat_id in forced_promotions_selected:
                breakdown["selected_reason"] = "selected_by_forced_promotion"
            elif chat_id in persistent_due_selected:
                breakdown["selected_reason"] = "selected_by_persistent_due_reservation"
            elif chat_id in dialogue_candidate_set and chat_id in dialogue_selected:
                breakdown["selected_reason"] = "selected_by_dialogue_quota"
            elif chat_id in maintenance_candidate_set and chat_id in maintenance_selected:
                breakdown["selected_reason"] = "selected_by_maintenance_quota"
            else:
                breakdown["selected_reason"] = "selected_by_overflow_recovery"
            score_breakdown[chat_id] = breakdown
            selected_entries.append((ranked_map[chat_id], chat_id, breakdown))

        skipped_by_batch: list[str] = []
        for chat_id, breakdown in entries:
            if chat_id in selected_set:
                continue
            skip_reason = "skipped_by_batch"
            if chat_id in forced_candidate_set:
                skip_reason = "skipped_by_promotion_overflow"
                quota_skipped["skipped_by_promotion_overflow"].append(chat_id)
            elif chat_id in maintenance_candidate_set:
                skip_reason = "skipped_by_maintenance_quota"
                quota_skipped["skipped_by_maintenance_quota"].append(chat_id)
            elif chat_id in dialogue_candidate_set:
                skip_reason = "skipped_by_dialogue_quota"
                quota_skipped["skipped_by_dialogue_quota"].append(chat_id)
            breakdown["quota_skip_reason"] = skip_reason
            breakdown["not_selected_reason"] = skip_reason
            score_breakdown[chat_id] = breakdown
            skipped_by_batch.append(chat_id)

        return {
            "selected": selected_order,
            "selected_entries": selected_entries,
            "skipped_by_batch": skipped_by_batch,
            "quota_skipped": quota_skipped,
            "forced_promotions_selected": forced_promotions_selected,
            "persistent_due_selected": [
                chat_id
                for chat_id in selected_order
                if bool(score_breakdown.get(chat_id, {}).get("persistent_due_candidate", False))
            ],
            "dialogue_selected": [chat_id for chat_id in selected_order if chat_id in set(dialogue_selected + overflow_selected) and str(score_breakdown.get(chat_id, {}).get("quota_bucket", "")) != "MAINTENANCE"],
            "maintenance_selected": [chat_id for chat_id in selected_order if str(score_breakdown.get(chat_id, {}).get("quota_bucket", "")) == "MAINTENANCE"],
        }

    async def commit_due_selection_counters(self, report: dict[str, Any] | None) -> None:
        payload = dict(report or {})
        selected = [str(chat_id or "") for chat_id in list(payload.get("selected", []) or []) if str(chat_id or "").strip()]
        skipped_by_batch = [str(chat_id or "") for chat_id in list(payload.get("skipped_by_batch", []) or []) if str(chat_id or "").strip()]
        if not selected and not skipped_by_batch:
            return
        states_by_chat: dict[str, ChatLoopState] = {}
        for chat_id in [*selected, *skipped_by_batch]:
            state = await self._state_store.get(chat_id)
            if state is not None:
                states_by_chat[chat_id] = state
        await self._apply_due_selection_counters(
            states_by_chat=states_by_chat,
            selected=selected,
            skipped_by_batch=skipped_by_batch,
        )

    async def _apply_due_selection_counters(
        self,
        *,
        states_by_chat: dict[str, ChatLoopState],
        selected: list[str],
        skipped_by_batch: list[str],
    ) -> None:
        selected_set = set(selected)
        for chat_id, state in states_by_chat.items():
            changed = False
            if chat_id in skipped_by_batch:
                state.missed_due_passes = int(state.missed_due_passes or 0) + 1
                changed = True
            elif chat_id in selected_set and int(state.missed_due_passes or 0) != 0:
                state.missed_due_passes = 0
                changed = True
            if changed:
                await self._state_store.save(state)

    def _update_quota_pressure_rounds(self, report: dict[str, Any]) -> None:
        quota_skipped = dict(report.get("quota_skipped", {}) or {})
        maintenance_quota_skipped = list(quota_skipped.get("skipped_by_maintenance_quota", []) or [])
        maintenance_selected = list(report.get("maintenance_selected", []) or [])
        maintenance_slots = int(dict(report.get("batch_plan", {}) or {}).get("maintenance_slots", 0) or 0)
        if maintenance_slots > 0 and maintenance_selected and len(maintenance_selected) >= maintenance_slots and maintenance_quota_skipped:
            self._maintenance_quota_pressure_rounds += 1
            return
        self._maintenance_quota_pressure_rounds = 0

    def _determine_maintenance_budget(
        self,
        due_phase_mix: dict[str, int],
        score_breakdown: dict[str, dict[str, Any]],
        selected: list[str],
        skipped_by_batch: list[str],
    ) -> int:
        has_dialogue_pressure = any(int(due_phase_mix.get(name, 0) or 0) > 0 for name in ("WAITING", "BUSY", "ACTIVE"))
        due_backlog = any(
            float(score_breakdown.get(chat_id, {}).get("maintenance_backlog_score", 0.0) or 0.0) > 0.0
            for chat_id in [*(selected or []), *(skipped_by_batch or [])]
        )
        if has_dialogue_pressure:
            self._maintenance_idle_rounds = 0
            return 0
        if not due_backlog:
            self._maintenance_idle_rounds = 0
            return 1 if any(int(value or 0) > 0 for value in due_phase_mix.values()) else 0
        self._maintenance_idle_rounds += 1
        if self._maintenance_idle_rounds >= 2:
            return 2
        return 1

    def _heartbeat_context_for_chat(self, chat_id: str) -> dict[str, Any]:
        context = self._heartbeat_pass_context or {}
        selected = list(context.get("selected", []) or [])
        if str(chat_id or "") not in selected:
            return {}
        return context

    def _maintenance_budget_state(self, chat_id: str) -> dict[str, Any]:
        context = self._heartbeat_context_for_chat(chat_id)
        if not context:
            return {
                "total": 1,
                "used": 0,
                "remaining": 1,
            }
        total = int(context.get("maintenance_budget_total", 0) or 0)
        used = int(context.get("maintenance_budget_used", 0) or 0)
        remaining = max(0, int(context.get("maintenance_budget_remaining", total - used) or 0))
        return {
            "total": total,
            "used": used,
            "remaining": remaining,
        }

    def _consume_maintenance_budget(self, chat_id: str, action: str) -> None:
        context = self._heartbeat_context_for_chat(chat_id)
        if not context:
            return
        total = int(context.get("maintenance_budget_total", 0) or 0)
        used = int(context.get("maintenance_budget_used", 0) or 0) + 1
        context["maintenance_budget_used"] = used
        context["maintenance_budget_remaining"] = max(0, total - used)
        blocked = list(context.get("maintenance_blocked_by_budget", []) or [])
        context["maintenance_blocked_by_budget"] = blocked
        self._heartbeat_pass_context = context

    def _mark_budget_block(self, chat_id: str, action: str) -> None:
        context = self._heartbeat_context_for_chat(chat_id)
        if not context:
            return
        blocked = list(context.get("maintenance_blocked_by_budget", []) or [])
        if action not in blocked:
            blocked.append(action)
        context["maintenance_blocked_by_budget"] = blocked
        self._heartbeat_pass_context = context

    async def _build_snapshot(self, state: ChatLoopState, chat_id: str, trigger: str, event: Any) -> ChatLoopSnapshot:
        threaded_group_mirror = bool(
            getattr(self.group_reply_wait_manager, "threaded_enabled", False)
            and state.wait_scope == "group"
        )
        if threaded_group_mirror:
            await self._sync_wait_from_adapters(state, chat_id, event)
        else:
            self._expire_wait_if_needed(state)
        self._prune_expired_cooldowns(state)

        runtime_snapshot: dict[str, Any] = {}
        if self.runtime_coordinator is not None and hasattr(self.runtime_coordinator, "get_activity_snapshot"):
            try:
                snapshot = await self.runtime_coordinator.get_activity_snapshot(chat_id)
                if isinstance(snapshot, dict):
                    runtime_snapshot = snapshot
            except Exception as exc:
                logger.debug(f"[ChatLoopKernel] runtime snapshot degraded for {chat_id}: {exc}")

        latest_activity = dict(runtime_snapshot or {})
        raw_wait_targets = [str(item) for item in latest_activity.get("wait_targets", []) or [] if str(item or "").strip()]
        if raw_wait_targets and state.wait_mode == "none":
            await self.sync_runtime_wait_targets(chat_id, raw_wait_targets, str(latest_activity.get("wait_target_name", "") or ""))
        executor_pending = int(latest_activity.get("executor_pending", 0) or 0)
        has_new_message = trigger in {"message", "external"}
        message_signal = "user_message" if trigger == "message" else ("external_event" if trigger == "external" else "")

        if state.wait_mode == "none" and not threaded_group_mirror:
            await self._sync_wait_from_adapters(state, chat_id, event)

        group_wait_state = self._wait_state_payload(state) if state.wait_scope == "group" else {}
        private_wait_state = self._wait_state_payload(state) if state.wait_scope == "private" else {}
        wait_targets = list(state.wait_target_ids or []) if state.wait_mode == "runtime_targets" else raw_wait_targets
        wait_signal = self._detect_wait_signal(state, trigger, event, wait_targets)

        quiet_summary = self._collect_quiet_summary()
        quiet_signal = "quiet_hours" if bool(quiet_summary.get("quiet_hours", False)) else ""

        proactive_signal = ""
        proactive_summary: dict[str, Any] = {}
        heartflow_summary: dict[str, Any] = {}
        heartflow_signal = ""
        compaction_summary: dict[str, Any] = {}
        compaction_signal = ""
        memory_signal = ""
        memory_summary: dict[str, Any] = {}
        dream_signal = ""
        dream_summary: dict[str, Any] = {}

        if trigger == "heartbeat":
            proactive_summary = await self._collect_proactive_summary(chat_id, state)
            if proactive_summary.get("eligible"):
                proactive_signal = "wakeup"

            heartflow_summary = await self._collect_heartflow_summary(chat_id, latest_activity)
            if heartflow_summary.get("eligible"):
                heartflow_signal = str(heartflow_summary.get("action_type") or "heartflow")

            compaction_summary = await self._collect_compaction_summary(chat_id, state)
            if compaction_summary.get("eligible"):
                compaction_signal = "eligible"

            memory_summary = await self._collect_memory_summary(chat_id)
            if memory_summary.get("eligible"):
                memory_signal = "eligible"

            dream_summary = await self._collect_dream_signal(chat_id)
            if dream_summary.get("eligible"):
                dream_signal = "eligible"

        snapshot = ChatLoopSnapshot(
            chat_id=chat_id,
            trigger_type=trigger,
            has_new_message=has_new_message,
            latest_activity=latest_activity,
            executor_pending=executor_pending,
            wait_targets=wait_targets,
            message_signal=message_signal,
            wait_signal=wait_signal,
            quiet_signal=quiet_signal,
            attention_signal=message_signal,
            proactive_signal=proactive_signal,
            heartflow_signal=heartflow_signal,
            memory_signal=memory_signal,
            dream_signal=dream_signal,
            compaction_signal=compaction_signal,
            group_wait_state=group_wait_state,
            private_wait_state=private_wait_state,
            proactive_summary=proactive_summary,
            heartflow_summary=heartflow_summary,
            memory_summary=memory_summary,
            compaction_summary=compaction_summary,
            dream_summary=dream_summary,
            quiet_summary=quiet_summary,
            cooldown_state=dict(state.cooldowns or {}),
        )
        snapshot.latest_activity.setdefault("loop_source", self._event_source_name(event))
        snapshot.latest_activity["proactive_summary"] = proactive_summary
        snapshot.latest_activity["memory_summary"] = memory_summary
        snapshot.latest_activity["compaction_summary"] = compaction_summary
        snapshot.latest_activity["dream_summary"] = dream_summary
        return snapshot

    async def _sync_wait_from_adapters(self, state: ChatLoopState, chat_id: str, event: Any) -> None:
        had_threaded_group_wait = bool(
            getattr(self.group_reply_wait_manager, "threaded_enabled", False)
            and state.wait_scope == "group"
        )
        group_payload = await self._collect_group_wait_state(chat_id)
        if group_payload:
            now = monotonic()
            self._apply_wait_arm(
                state,
                wait_mode="group_reply",
                wait_scope="group",
                target_ids=group_payload.get("target_user_ids") or [group_payload.get("target_user_id", "")],
                target_name=str(group_payload.get("target_name", "") or ""),
                thread_signature=str(group_payload.get("thread_signature", "") or ""),
                started_at=now,
                expires_at=now + float(group_payload.get("remaining_seconds", 0.0) or 0.0),
                message_budget=int(group_payload.get("remaining_messages", 0) or 0),
                reason=str(group_payload.get("reason", "group_wait_sync") or "group_wait_sync"),
            )
            return  # ponytail: group wait blocks private wait in same chat
        if had_threaded_group_wait:
            self._clear_wait_state(state, status="expired", reason="group_wait_manager_cleared")

        private_payload = self._collect_private_wait_state(chat_id, event)
        if private_payload and bool(private_payload.get("is_bot_waiting", False)):
            self._apply_wait_arm(
                state,
                wait_mode="private_reply",
                wait_scope="private",
                target_ids=[private_payload.get("user_id", "")],
                target_name=str(private_payload.get("target_name", "") or ""),
                thread_signature="",
                started_at=time.time(),
                expires_at=0.0,
                message_budget=1,
                reason="private_wait_sync",
            )

    async def _collect_group_wait_state(self, chat_id: str) -> dict[str, Any]:
        manager = self.group_reply_wait_manager
        if manager is None or not hasattr(manager, "get_wait_info"):
            return {}
        try:
            if bool(getattr(manager, "threaded_enabled", False)) and hasattr(manager, "list_waits"):
                waits = [dict(item or {}) for item in (manager.list_waits(chat_id) or []) if item]
                if not waits:
                    return {}
                target_ids = self._dedupe_ids([item.get("target_user_id", "") for item in waits])
                return {
                    "target_user_id": target_ids[0] if target_ids else "",
                    "target_user_ids": target_ids,
                    "target_name": str(waits[0].get("target_name", "") or "") if len(waits) == 1 else "",
                    "thread_signature": str(waits[0].get("thread_signature", "") or "") if len(waits) == 1 else "",
                    "remaining_seconds": max(float(item.get("remaining_seconds", 0.0) or 0.0) for item in waits),
                    "remaining_messages": sum(max(int(item.get("remaining_messages", 0) or 0), 0) for item in waits),
                    "reason": str(waits[0].get("reason", "thread_wait") or "thread_wait") if len(waits) == 1 else "threaded_group_wait_aggregate",
                    "wait_count": len(waits),
                }
            payload = manager.get_wait_info(chat_id)
            return dict(payload or {})
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] group wait lookup degraded for {chat_id}: {exc}")
            return {}

    def _collect_private_wait_state(self, chat_id: str, event: Any) -> dict[str, Any]:
        """ponytail: sync getter in async context — acceptable for local dict access in PrivateChatManager."""
        manager = self.private_chat_manager
        if manager is None:
            return {}
        try:
            if event is not None and hasattr(event, "get_group_id") and event.get_group_id():
                return {}
            payload = None
            if chat_id and hasattr(manager, "get_session_info_by_chat_id"):
                payload = manager.get_session_info_by_chat_id(chat_id)
            if payload is None and event is not None and hasattr(manager, "get_session_info"):
                sender_id = self._event_sender_id(event)
                if sender_id:
                    payload = manager.get_session_info(sender_id)
            return dict(payload or {})
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] private wait lookup degraded: {exc}")
            return {}

    async def _collect_proactive_summary(self, chat_id: str, state: ChatLoopState) -> dict[str, Any]:
        service = self.wakeup_service
        if service is None or not hasattr(service, "build_signal"):
            return {}
        try:
            payload = dict(await self._maybe_await(service.build_signal(chat_id)) or {})
            next_ts = float(payload.get("next_wakeup_timestamp", 0.0) or 0.0)
            if next_ts > time.time():
                state.cooldowns["wakeup"] = next_ts
            return payload
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] wakeup signal degraded for {chat_id}: {exc}")
            return {}

    async def _collect_heartflow_summary(self, chat_id: str, runtime_snapshot: dict[str, Any]) -> dict[str, Any]:
        manager = self.heartflow_manager
        if manager is None or not hasattr(manager, "preview_chat"):
            return {}
        try:
            payload = await self._maybe_await(manager.preview_chat(chat_id, snapshot=runtime_snapshot))
            return dict(payload or {})
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] heartflow preview degraded for {chat_id}: {exc}")
            return {}

    async def _collect_compaction_summary(self, chat_id: str, state: ChatLoopState) -> dict[str, Any]:
        engine = self.context_compaction
        if engine is None or not hasattr(engine, "get_trace_status"):
            return {}
        try:
            payload = dict(await self._maybe_await(engine.get_trace_status(chat_id)) or {})
            next_eval_at = int(payload.get("next_eval_at_count", 0) or 0)
            eligible = bool(
                int(payload.get("pending_eval_nodes_count", 0) or 0) > 0
                or bool(payload.get("force_execute_on_next_safe_hook", False))
                or (next_eval_at > 0 and int(payload.get("message_count_since_last_compaction", 0) or 0) >= next_eval_at)
            )
            payload["eligible"] = eligible
            cooldown_until = 0.0
            if hasattr(engine, "get_cooldown_until"):
                cooldown_until = float(await self._maybe_await(engine.get_cooldown_until(chat_id)) or 0.0)
            if cooldown_until > time.time():
                state.cooldowns["compaction"] = cooldown_until
            return payload
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] compaction preview degraded for {chat_id}: {exc}")
            return {}

    async def _collect_memory_summary(self, chat_id: str) -> dict[str, Any]:
        service = self.memory_service
        if service is None:
            return {}
        candidate = getattr(service, "memory_pipeline", None)
        if candidate is None and hasattr(service, "describe_session_eligibility"):
            candidate = service
        if candidate is None or not hasattr(candidate, "describe_session_eligibility"):
            return {}
        try:
            payload = await self._maybe_await(candidate.describe_session_eligibility(chat_id))
            return dict(payload or {})
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] memory summary degraded for {chat_id}: {exc}")
            return {}

    async def _collect_dream_signal(self, chat_id: str) -> dict[str, Any]:
        scheduler = self.dream_scheduler
        if scheduler is None:
            return {}
        now_ts = time.time()
        try:
            if hasattr(scheduler, "describe_session_eligibility_async"):
                payload = await self._maybe_await(scheduler.describe_session_eligibility_async(chat_id, now_ts))
                return dict(payload or {})
            if hasattr(scheduler, "describe_session_eligibility"):
                payload = await self._maybe_await(scheduler.describe_session_eligibility(chat_id, now_ts))
                return dict(payload or {})
            if hasattr(scheduler, "should_run_for_session") and scheduler.should_run_for_session(chat_id, now_ts):
                return {"eligible": True, "reason": "eligible", "throttle_scope": "global"}
            if hasattr(scheduler, "should_run") and scheduler.should_run(now_ts):
                return {"eligible": True, "reason": "eligible", "throttle_scope": "global"}
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] dream signal degraded for {chat_id}: {exc}")
        return {"eligible": False, "reason": "dream_global_cooldown", "throttle_scope": "global"}

    def _collect_quiet_summary(self) -> dict[str, Any]:
        config = None
        for owner in (self.wakeup_service, self.heartflow_manager, self.dream_scheduler):
            config = getattr(owner, "config", None)
            if config is not None:
                break
        if config is None:
            return {"quiet_hours": False, "time_bucket": "", "quiet_ranges": [], "source": "proactive_rhythm"}
        rhythm = evaluate_proactive_rhythm(config)
        return {
            "quiet_hours": bool(rhythm.quiet_hours),
            "time_bucket": str(rhythm.time_bucket or ""),
            "quiet_ranges": list(rhythm.quiet_ranges or ()),
            "base_frequency": float(rhythm.base_frequency or 0.0),
            "base_frequency_factor": float(rhythm.base_frequency_factor or 0.0),
            "source": "proactive_rhythm",
        }

    def _detect_wait_signal(self, state: ChatLoopState, trigger: str, event: Any, wait_targets: list[str]) -> str:
        if trigger == "message":
            if self._message_resumes_wait(state, event):
                return "resume"
            if self._has_active_wait(state):
                if state.wait_mode == "runtime_targets":
                    return "wait_targets"
                if state.wait_mode == "group_reply":
                    return "group_wait"
                if state.wait_mode == "private_reply":
                    return "private_wait"
                return "wait"
        if self._has_active_wait(state):
            if state.wait_mode == "runtime_targets" or wait_targets:
                return "wait_targets"
            if state.wait_mode == "group_reply":
                return "group_wait"
            if state.wait_mode == "private_reply":
                return "private_wait"
        return ""

    @staticmethod
    def _maintenance_candidates(snapshot: ChatLoopSnapshot) -> dict[str, dict[str, Any]]:
        return {
            "compaction": {
                "eligible": bool(snapshot.compaction_summary.get("eligible", False)),
                "signal": str(snapshot.compaction_signal or ""),
                "reason": str(snapshot.compaction_summary.get("reason", "") or ""),
            },
            "memory": {
                "eligible": bool(snapshot.memory_summary.get("eligible", False)),
                "candidate_present": bool(snapshot.memory_summary.get("candidate_present", False)),
                "signal": str(snapshot.memory_signal or ""),
                "reason": str(snapshot.memory_summary.get("reason", "") or ""),
            },
            "dream": {
                "eligible": bool(snapshot.dream_summary.get("eligible", False)),
                "signal": str(snapshot.dream_signal or ""),
                "reason": str(snapshot.dream_summary.get("reason", "") or ""),
                "throttle_scope": str(snapshot.dream_summary.get("throttle_scope", "") or ""),
            },
        }

    def _message_resumes_wait(self, state: ChatLoopState, event: Any) -> bool:
        if event is not None and hasattr(event, "get_extra"):
            try:
                if bool(event.get_extra("astrmai_group_wait_resume", False)):
                    return True
            except Exception:
                pass
        if not self._has_active_wait(state):
            return False
        if state.wait_scope == "group" and bool(
            getattr(self.group_reply_wait_manager, "threaded_enabled", False)
        ):
            return False
        sender_id = self._event_sender_id(event)
        if state.wait_target_ids and sender_id and sender_id in state.wait_target_ids:
            return True
        if state.wait_scope == "private" and event is not None:
            group_id = getattr(event, "get_group_id", lambda: "")()
            if not group_id:
                return True
        return False

    def _decide(self, state: ChatLoopState, snapshot: ChatLoopSnapshot, event: Any) -> ChatLoopDecision:
        if snapshot.trigger_type == "message":
            if snapshot.wait_signal == "resume":
                return self._make_decision(snapshot, "RESUME_WAIT", "wait_resumed", True)
            if self._has_active_wait(state):
                return self._make_decision(snapshot, "INTERRUPT_WAIT", f"wait_interrupted:{state.wait_mode}", True)
            return self._make_decision(snapshot, "INGRESS_MESSAGE", "message_ingress", True)

        if snapshot.trigger_type == "external":
            return self._make_decision(snapshot, "INGRESS_EXTERNAL", "external_ingress", True)

        if snapshot.executor_pending > 0:
            return self._make_decision(snapshot, "SKIP_BUSY", "executor_pending", False)
        if snapshot.wait_signal:
            return self._make_decision(snapshot, "WAIT", f"wait_state:{snapshot.wait_signal}", False)

        cooldown_blocks: list[str] = []
        quiet_blocks: list[str] = []
        maintenance_candidates = self._maintenance_candidates(snapshot)
        maintenance_budget = self._maintenance_budget_state(snapshot.chat_id)
        maintenance_budget_blocked = maintenance_budget["total"] <= 0 or maintenance_budget["remaining"] <= 0
        pending_heartflow = bool((state.pending_signals or {}).get("pending_heartflow", False))
        pending_heartflow_reason = str((state.pending_signals or {}).get("pending_heartflow_reason", "") or "")

        if snapshot.proactive_signal:
            if snapshot.quiet_signal:
                quiet_blocks.append("PROACTIVE_WAKEUP")
            elif self._cooldown_active(snapshot, "wakeup"):
                cooldown_blocks.append("wakeup")
            else:
                proactive_metadata = {}
                if snapshot.heartflow_signal:
                    proactive_metadata["pending_heartflow"] = True
                    proactive_metadata["pending_heartflow_reason"] = f"heartflow_signal:{snapshot.heartflow_signal}"
                return self._bridge_decision(snapshot, "PROACTIVE_WAKEUP", "wakeup_signal", metadata=proactive_metadata)
        elif snapshot.proactive_summary.get("candidate_present") and self._cooldown_active(snapshot, "wakeup"):
            cooldown_blocks.append("wakeup")

        if pending_heartflow and not snapshot.quiet_signal and not self._cooldown_active(snapshot, "heartflow"):
            metadata = {
                "pending_heartflow_consumed": True,
                "pending_heartflow_reason": pending_heartflow_reason or "pending_heartflow_replay",
            }
            return self._bridge_decision(snapshot, "HEARTFLOW_EVALUATE", pending_heartflow_reason or "pending_heartflow", metadata=metadata)

        if snapshot.heartflow_signal:
            if snapshot.quiet_signal:
                quiet_blocks.append("HEARTFLOW_EVALUATE")
            elif self._cooldown_active(snapshot, "heartflow"):
                cooldown_blocks.append("heartflow")
            else:
                return self._bridge_decision(snapshot, "HEARTFLOW_EVALUATE", f"heartflow_signal:{snapshot.heartflow_signal}")

        if snapshot.compaction_signal:
            if self._cooldown_active(snapshot, "compaction"):
                cooldown_blocks.append("compaction")
            elif maintenance_budget_blocked:
                self._mark_budget_block(snapshot.chat_id, "COMPACTION_EVALUATE")
            else:
                self._consume_maintenance_budget(snapshot.chat_id, "COMPACTION_EVALUATE")
                metadata = {
                    "maintenance_candidates": maintenance_candidates,
                    "maintenance_priority_winner": "compaction",
                    "skipped_lower_priority_actions": self._skipped_maintenance_actions(maintenance_candidates, "compaction"),
                    "maintenance_budget_state": self._maintenance_budget_state(snapshot.chat_id),
                }
                return self._bridge_decision(snapshot, "COMPACTION_EVALUATE", "compaction_signal", metadata=metadata)

        if snapshot.memory_signal:
            if maintenance_budget_blocked:
                self._mark_budget_block(snapshot.chat_id, "MEMORY_MAINTENANCE")
            else:
                self._consume_maintenance_budget(snapshot.chat_id, "MEMORY_MAINTENANCE")
                metadata = {
                    "maintenance_candidates": maintenance_candidates,
                    "maintenance_priority_winner": "memory",
                    "skipped_lower_priority_actions": self._skipped_maintenance_actions(maintenance_candidates, "memory"),
                    "maintenance_budget_state": self._maintenance_budget_state(snapshot.chat_id),
                }
                return self._bridge_decision(snapshot, "MEMORY_MAINTENANCE", "memory_signal", metadata=metadata)

        if snapshot.dream_signal:
            if maintenance_budget_blocked:
                self._mark_budget_block(snapshot.chat_id, "DREAM_MAINTENANCE")
            else:
                self._consume_maintenance_budget(snapshot.chat_id, "DREAM_MAINTENANCE")
                metadata = {
                    "maintenance_candidates": maintenance_candidates,
                    "maintenance_priority_winner": "dream",
                    "skipped_lower_priority_actions": self._skipped_maintenance_actions(maintenance_candidates, "dream"),
                    "maintenance_budget_state": self._maintenance_budget_state(snapshot.chat_id),
                }
                return self._bridge_decision(snapshot, "DREAM_MAINTENANCE", "dream_signal", metadata=metadata)

        reason = "no_signal_ready"
        metadata = {
            "maintenance_candidates": maintenance_candidates,
            "maintenance_priority_winner": "",
            "maintenance_budget_state": maintenance_budget,
        }
        if quiet_blocks:
            reason = "quiet_hours"
            metadata["quiet_blocks"] = list(quiet_blocks)
        elif cooldown_blocks:
            reason = "cooldown_blocked"
            metadata["cooldown_blocks"] = list(cooldown_blocks)
            if "compaction" in cooldown_blocks:
                metadata["maintenance_blocked_by"] = "compaction_cooldown"
                metadata["compaction_block_source"] = "cooldown"
        elif maintenance_budget_blocked and self._has_maintenance_candidate(maintenance_candidates):
            reason = "maintenance_budget_blocked"
            blocked_rounds = 1
            previous_signals = dict(state.pending_signals or {})
            if str(state.phase or "").upper() == "MAINTENANCE" and bool(previous_signals.get("maintenance_budget_blocked", False)):
                blocked_rounds = int(previous_signals.get("maintenance_budget_blocked_rounds", 0) or 0) + 1
            metadata["maintenance_budget_blocked"] = True
            metadata["maintenance_budget_blocked_rounds"] = blocked_rounds
            metadata["maintenance_blocked_by_budget"] = True
            metadata["maintenance_blocked_by"] = "maintenance_budget"
            if blocked_rounds >= self.MAINTENANCE_BUDGET_BLOCKED_IDLE_THRESHOLD:
                metadata["maintenance_phase_downgraded"] = True
        elif maintenance_candidates["memory"].get("candidate_present", False):
            metadata["memory_block_source"] = str(maintenance_candidates["memory"].get("reason", "") or "not_selected")
        if maintenance_candidates["dream"].get("eligible", False) and reason != "dream_signal":
            metadata.setdefault("dream_block_source", reason)
        return self._make_decision(snapshot, "NOOP", reason, False, metadata=metadata)

    @staticmethod
    def _skipped_maintenance_actions(maintenance_candidates: dict[str, dict[str, Any]], winner: str) -> list[str]:
        order = ["compaction", "memory", "dream"]
        action_map = {
            "compaction": "COMPACTION_EVALUATE",
            "memory": "MEMORY_MAINTENANCE",
            "dream": "DREAM_MAINTENANCE",
        }
        seen_winner = False
        skipped: list[str] = []
        for item in order:
            if item == winner:
                seen_winner = True
                continue
            if not seen_winner:
                continue
            if maintenance_candidates.get(item, {}).get("eligible", False):
                skipped.append(action_map[item])
        return skipped

    def _bridge_decision(
        self,
        snapshot: ChatLoopSnapshot,
        action: str,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ChatLoopDecision:
        if action not in self._dispatch_bridges:
            return self._make_decision(snapshot, "NOOP", f"{action.lower()}_bridge_unavailable", False)
        return self._make_decision(snapshot, action, reason, True, metadata=metadata)

    def _make_decision(
        self,
        snapshot: ChatLoopSnapshot,
        action: str,
        reason: str,
        should_dispatch: bool,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> ChatLoopDecision:
        scheduler_context = self._heartbeat_context_for_chat(snapshot.chat_id)
        score_breakdown = dict(scheduler_context.get("score_breakdown", {}).get(snapshot.chat_id, {}) or {})
        maintenance_budget_state = self._maintenance_budget_state(snapshot.chat_id)
        merged_metadata = {
            "source": str(snapshot.latest_activity.get("loop_source", "") or ""),
            "signals_summary": self._signals_summary(snapshot),
            "trigger_type": snapshot.trigger_type,
            "dispatch_bridge": "observe_only" if snapshot.trigger_type == "heartbeat" else "",
            "wait_scope": self._resolve_wait_scope(snapshot),
            "heartflow_preview_mode": "readonly",
            "wakeup_candidate_present": bool(snapshot.proactive_summary.get("candidate_present", False)),
            "wakeup_cooldown_until": float(snapshot.proactive_summary.get("next_wakeup_timestamp", 0.0) or 0.0),
            "memory_candidate_present": bool(snapshot.memory_summary.get("candidate_present", False)),
            "memory_reason": str(snapshot.memory_summary.get("reason", "") or ""),
            "dream_throttle_scope": str(snapshot.dream_summary.get("throttle_scope", "") or ""),
            "dream_reason": str(snapshot.dream_summary.get("reason", "") or ""),
            "quiet_active": bool(snapshot.quiet_summary.get("quiet_hours", False)),
            "quiet_source": str(snapshot.quiet_summary.get("source", "") or ""),
            "followup_cooldown_until": float(snapshot.cooldown_state.get("followup", 0.0) or 0.0),
            "due_for_heartbeat": snapshot.trigger_type == "heartbeat",
            "selected_in_pass": snapshot.trigger_type == "heartbeat",
            "scheduler_bucket": "immediate" if snapshot.trigger_type in {"message", "external"} else "",
            "schedule_reason": "",
            "earliest_blocking_cooldown": 0.0,
            "wait_recheck_at": 0.0,
            "scheduler_score": float(score_breakdown.get("scheduler_score", 0.0) or 0.0),
            "starvation_age": float(score_breakdown.get("starvation_age", 0.0) or 0.0),
            "fairness_penalty": float(score_breakdown.get("fairness_penalty", 0.0) or 0.0),
            "missed_due_passes": int(score_breakdown.get("missed_due_passes", 0) or 0),
            "starvation_tier": str(score_breakdown.get("starvation_tier", "") or ""),
            "forced_promotion_eligible": bool(score_breakdown.get("forced_promotion_eligible", False)),
            "selection_cooldown_bias": float(score_breakdown.get("selection_cooldown_bias", 0.0) or 0.0),
            "maintenance_budget_state": maintenance_budget_state,
            "poll_mode": str(scheduler_context.get("poll_mode", "") or ""),
            "poll_mode_reason": str(scheduler_context.get("poll_mode_reason", "") or ""),
            "due_rank": int(score_breakdown.get("due_rank", 0) or 0),
            "pressure_components": dict(score_breakdown.get("pressure_components", {}) or {}),
            "maintenance_backlog_score": float(score_breakdown.get("maintenance_backlog_score", 0.0) or 0.0),
            "retry_backoff_until": float(score_breakdown.get("retry_backoff_until", 0.0) or 0.0),
            "selected_reason": str(score_breakdown.get("selected_reason", "") or ""),
            "not_selected_reason": str(score_breakdown.get("not_selected_reason", "") or ""),
            "quota_bucket": str(score_breakdown.get("quota_bucket", "") or ""),
            "quota_skip_reason": str(score_breakdown.get("quota_skip_reason", "") or ""),
            "batch_plan": dict(scheduler_context.get("batch_plan", {}) or {}),
            "batch_fill_rate": float(scheduler_context.get("batch_fill_rate", 0.0) or 0.0),
            "batch_pressure": dict(scheduler_context.get("batch_pressure", {}) or {}),
            "quota_skip_counts": dict(scheduler_context.get("quota_skip_counts", {}) or {}),
            "busy_backpressure_active": bool(scheduler_context.get("busy_backpressure_active", False)),
            "maintenance_backpressure_active": bool(scheduler_context.get("maintenance_backpressure_active", False)),
            "forced_promotions_selected": list(scheduler_context.get("forced_promotions_selected", []) or []),
        }
        if metadata:
            merged_metadata.update(metadata)
        return ChatLoopDecision(
            action=action,
            reason=reason,
            should_dispatch=should_dispatch,
            next_tick_delay=0.0,
            metadata=merged_metadata,
        )

    @staticmethod
    def _signals_summary(snapshot: ChatLoopSnapshot) -> dict[str, Any]:
        return {
            "message_signal": snapshot.message_signal,
            "wait_signal": snapshot.wait_signal,
            "quiet_signal": snapshot.quiet_signal,
            "proactive_signal": snapshot.proactive_signal,
            "wakeup_candidate_present": bool(snapshot.proactive_summary.get("candidate_present", False)),
            "wakeup_cooldown_until": float(snapshot.proactive_summary.get("next_wakeup_timestamp", 0.0) or 0.0),
            "heartflow_signal": snapshot.heartflow_signal,
            "memory_signal": snapshot.memory_signal,
            "memory_candidate_present": bool(snapshot.memory_summary.get("candidate_present", False)),
            "memory_reason": str(snapshot.memory_summary.get("reason", "") or ""),
            "dream_signal": snapshot.dream_signal,
            "compaction_signal": snapshot.compaction_signal,
            "maintenance_candidates_summary": ChatLoopKernel._maintenance_candidates(snapshot),
            "wait_targets_count": len(snapshot.wait_targets),
            "executor_pending": snapshot.executor_pending,
            "wait_scope": ChatLoopKernel._resolve_wait_scope(snapshot),
            "dream_throttle_scope": str(snapshot.dream_summary.get("throttle_scope", "") or ""),
            "dream_reason": str(snapshot.dream_summary.get("reason", "") or ""),
            "active_cooldowns": dict(snapshot.cooldown_state or {}),
            "followup_cooldown_until": float(snapshot.cooldown_state.get("followup", 0.0) or 0.0),
        }

    @staticmethod
    def _resolve_wait_scope(snapshot: ChatLoopSnapshot) -> str:
        if snapshot.wait_signal == "wait_targets":
            return "runtime_wait_targets"
        if snapshot.wait_signal == "group_wait":
            return "group"
        if snapshot.wait_signal == "private_wait":
            return "private"
        if snapshot.wait_signal == "resume":
            return "resume"
        return ""

    @staticmethod
    def _cooldown_active(snapshot: ChatLoopSnapshot, action: str) -> bool:
        until = float(snapshot.cooldown_state.get(str(action or "").strip().lower(), 0.0) or 0.0)
        return until > time.time()

    def _plan_next_tick(
        self,
        state: ChatLoopState,
        snapshot: ChatLoopSnapshot,
        decision: ChatLoopDecision,
        dispatch_result: Any,
    ) -> None:
        if snapshot.trigger_type in {"message", "external"}:
            decision.next_tick_delay = 0.0
            decision.metadata["scheduler_bucket"] = "immediate"
            decision.metadata["schedule_reason"] = "event_ingress"
            return

        now = time.time()
        action = str(decision.action or "")
        reason = str(decision.reason or "")
        maintenance_candidates = dict(decision.metadata.get("maintenance_candidates", {}) or {})

        if action in {"RESUME_WAIT", "INTERRUPT_WAIT"}:
            decision.next_tick_delay = 0.0
            decision.metadata["scheduler_bucket"] = "immediate"
            decision.metadata["schedule_reason"] = action.lower()
            return

        if action == "SKIP_BUSY":
            decision.next_tick_delay = self.FAST_RECHECK_SECONDS
            decision.metadata["scheduler_bucket"] = "fast_recheck"
            decision.metadata["schedule_reason"] = "executor_busy"
            return

        if action == "WAIT":
            wait_delay = self.WAIT_RECHECK_SECONDS
            wait_expires_at = float(state.wait_expires_at or 0.0)
            if wait_expires_at > now:
                wait_delay = min(self.WAIT_RECHECK_SECONDS, max(1.0, wait_expires_at - now))
                decision.metadata["wait_recheck_at"] = wait_expires_at
                decision.metadata["schedule_reason"] = "wait_expiry_window"
            else:
                decision.metadata["schedule_reason"] = "wait_recheck"
            decision.next_tick_delay = wait_delay
            decision.metadata["scheduler_bucket"] = "wait_recheck"
            return

        if action in {"PROACTIVE_WAKEUP", "HEARTFLOW_EVALUATE"}:
            decision.next_tick_delay = self.POST_DIALOGUE_RECHECK_SECONDS
            decision.metadata["scheduler_bucket"] = "post_dialogue"
            decision.metadata["schedule_reason"] = action.lower()
            return

        if action in {"COMPACTION_EVALUATE", "MEMORY_MAINTENANCE"}:
            decision.next_tick_delay = self.MAINTENANCE_RECHECK_SECONDS
            decision.metadata["scheduler_bucket"] = "maintenance_backoff"
            decision.metadata["schedule_reason"] = action.lower()
            return

        if action == "DREAM_MAINTENANCE":
            decision.next_tick_delay = self.DREAM_RECHECK_SECONDS
            decision.metadata["scheduler_bucket"] = "maintenance_backoff"
            decision.metadata["schedule_reason"] = "dream_maintenance"
            return

        if action == "NOOP":
            if reason == "quiet_hours":
                decision.next_tick_delay = self.IDLE_BACKOFF_SECONDS
                decision.metadata["scheduler_bucket"] = "idle_backoff"
                decision.metadata["schedule_reason"] = "quiet_hours"
                return

            if reason == "cooldown_blocked":
                earliest_cooldown = self._earliest_future_cooldown(state.cooldowns, now)
                if earliest_cooldown > now:
                    decision.next_tick_delay = max(
                        self.COOLDOWN_MIN_SECONDS,
                        min(self.COOLDOWN_MAX_SECONDS, earliest_cooldown - now),
                    )
                    decision.metadata["earliest_blocking_cooldown"] = earliest_cooldown
                    decision.metadata["scheduler_bucket"] = "fast_recheck"
                    decision.metadata["schedule_reason"] = "cooldown_expiry"
                    return

            if bool(decision.metadata.get("maintenance_phase_downgraded", False)):
                decision.next_tick_delay = self.IDLE_BACKOFF_SECONDS
                decision.metadata["scheduler_bucket"] = "idle_backoff"
                decision.metadata["schedule_reason"] = "maintenance_budget_blocked_idle_downgrade"
                return

            if self._has_maintenance_candidate(maintenance_candidates):
                decision.next_tick_delay = self.MAINTENANCE_CANDIDATE_RECHECK_SECONDS
                decision.metadata["scheduler_bucket"] = "maintenance_backoff"
                decision.metadata["schedule_reason"] = "maintenance_candidate_pending"
                return

            decision.next_tick_delay = self.IDLE_BACKOFF_SECONDS
            decision.metadata["scheduler_bucket"] = "idle_backoff"
            decision.metadata["schedule_reason"] = "idle_backoff"
            return

        decision.next_tick_delay = 0.0
        decision.metadata["scheduler_bucket"] = "immediate"
        decision.metadata["schedule_reason"] = "no_followup_recheck"

    @staticmethod
    def _has_maintenance_candidate(maintenance_candidates: dict[str, dict[str, Any]]) -> bool:
        for payload in maintenance_candidates.values():
            if bool(payload.get("eligible", False)) or bool(payload.get("candidate_present", False)):
                return True
        return False

    @staticmethod
    def _earliest_future_cooldown(cooldowns: dict[str, float], now: float) -> float:
        candidates = [float(until or 0.0) for until in (cooldowns or {}).values() if float(until or 0.0) > now]
        if not candidates:
            return 0.0
        return min(candidates)

    def _update_state(self, state: ChatLoopState, snapshot: ChatLoopSnapshot, decision: ChatLoopDecision) -> None:
        now = time.time()
        state.last_trigger = snapshot.trigger_type
        state.last_decision = decision.action
        state.last_tick_at = now
        if snapshot.trigger_type in {"message", "external"}:
            state.last_message_at = now
        if snapshot.trigger_type == "heartbeat":
            state.last_heartbeat_at = now
            if state.last_selected_at > 0 and (now - float(state.last_selected_at or 0.0)) <= (self.IDLE_POLL_SECONDS * 2):
                state.consecutive_selected_count = int(state.consecutive_selected_count or 0) + 1
            else:
                state.consecutive_selected_count = 1
            state.last_selected_at = now
            state.missed_due_passes = 0
            if decision.action in {"COMPACTION_EVALUATE", "MEMORY_MAINTENANCE", "DREAM_MAINTENANCE"}:
                state.last_maintenance_selected_at = now
            if str(decision.metadata.get("selected_reason", "") or "") == "selected_by_forced_promotion":
                state.forced_promotion_count = int(state.forced_promotion_count or 0) + 1
                state.last_forced_promotion_at = now
        else:
            pass  # ponytail: don't reset fairness counter on non-heartbeat message ingress
        state.next_tick_at = now + float(decision.next_tick_delay or 0.0) if decision.next_tick_delay > 0 else 0.0
        state.retry_backoff_until = 0.0
        self._write_base_pending_signals(state, decision)
        state.phase = self._derive_phase(decision)

        if decision.action == "RESUME_WAIT":
            state.wait_resume_reason = decision.reason
            self._clear_wait_state(state, status="resumed", reason=decision.reason)
        elif decision.action == "INTERRUPT_WAIT":
            self._clear_wait_state(state, status="interrupted", reason=decision.reason)
            state.last_interrupt_at = now

    @staticmethod
    def _derive_phase(decision: ChatLoopDecision) -> str:
        action = str(decision.action or "")
        if action in {"INGRESS_MESSAGE", "INGRESS_EXTERNAL", "RESUME_WAIT", "INTERRUPT_WAIT",
                       "PROACTIVE_WAKEUP", "HEARTFLOW_EVALUATE"}:
            return "ACTIVE"
        if action == "WAIT":
            return "WAITING"
        if action == "SKIP_BUSY":
            return "BUSY"
        if action in {"COMPACTION_EVALUATE", "MEMORY_MAINTENANCE", "DREAM_MAINTENANCE"}:
            return "MAINTENANCE"
        if action == "NOOP" and str(decision.reason or "") in {"quiet_hours", "cooldown_blocked"}:
            return "COOLDOWN"
        if action == "NOOP" and str(decision.reason or "") == "maintenance_budget_blocked":
            if bool(decision.metadata.get("maintenance_phase_downgraded", False)):
                return "IDLE"
            return "MAINTENANCE"
        return "IDLE"

    def _write_base_pending_signals(self, state: ChatLoopState, decision: ChatLoopDecision) -> None:
        base_signals = dict(decision.metadata.get("signals_summary", {}) or {})
        base_signals["schedule_reason"] = str(decision.metadata.get("schedule_reason", "") or "")
        base_signals["scheduler_bucket"] = str(decision.metadata.get("scheduler_bucket", "") or "")
        base_signals["due_for_heartbeat"] = bool(decision.metadata.get("due_for_heartbeat", False))
        base_signals["wait_recheck_at"] = float(decision.metadata.get("wait_recheck_at", 0.0) or 0.0)
        base_signals["earliest_blocking_cooldown"] = float(decision.metadata.get("earliest_blocking_cooldown", 0.0) or 0.0)
        base_signals["dispatch_failed"] = bool(decision.metadata.get("dispatch_failed", False))
        base_signals["dispatch_error_type"] = str(decision.metadata.get("dispatch_error_type", "") or "")
        base_signals["dispatch_error_reason"] = str(decision.metadata.get("dispatch_error_reason", "") or "")
        base_signals["dispatch_failure_backoff"] = float(decision.metadata.get("dispatch_failure_backoff", 0.0) or 0.0)
        base_signals["scheduler_score"] = float(decision.metadata.get("scheduler_score", 0.0) or 0.0)
        base_signals["starvation_age"] = float(decision.metadata.get("starvation_age", 0.0) or 0.0)
        base_signals["fairness_penalty"] = float(decision.metadata.get("fairness_penalty", 0.0) or 0.0)
        base_signals["missed_due_passes"] = int(decision.metadata.get("missed_due_passes", 0) or 0)
        base_signals["starvation_tier"] = str(decision.metadata.get("starvation_tier", "") or "")
        base_signals["forced_promotion_eligible"] = bool(decision.metadata.get("forced_promotion_eligible", False))
        base_signals["selection_cooldown_bias"] = float(decision.metadata.get("selection_cooldown_bias", 0.0) or 0.0)
        base_signals["maintenance_budget_state"] = dict(decision.metadata.get("maintenance_budget_state", {}) or {})
        base_signals["poll_mode"] = str(decision.metadata.get("poll_mode", "") or "")
        base_signals["poll_mode_reason"] = str(decision.metadata.get("poll_mode_reason", "") or "")
        base_signals["due_rank"] = int(decision.metadata.get("due_rank", 0) or 0)
        base_signals["pressure_components"] = dict(decision.metadata.get("pressure_components", {}) or {})
        base_signals["maintenance_backlog_score"] = float(decision.metadata.get("maintenance_backlog_score", 0.0) or 0.0)
        base_signals["retry_backoff_until"] = float(decision.metadata.get("retry_backoff_until", 0.0) or 0.0)
        base_signals["selected_reason"] = str(decision.metadata.get("selected_reason", "") or "")
        base_signals["not_selected_reason"] = str(decision.metadata.get("not_selected_reason", "") or "")
        base_signals["quota_bucket"] = str(decision.metadata.get("quota_bucket", "") or "")
        base_signals["quota_skip_reason"] = str(decision.metadata.get("quota_skip_reason", "") or "")
        base_signals["maintenance_budget_blocked"] = bool(decision.metadata.get("maintenance_budget_blocked", False))
        base_signals["maintenance_budget_blocked_rounds"] = int(decision.metadata.get("maintenance_budget_blocked_rounds", 0) or 0)
        base_signals["maintenance_phase_downgraded"] = bool(decision.metadata.get("maintenance_phase_downgraded", False))
        base_signals["maintenance_blocked_by_budget"] = bool(decision.metadata.get("maintenance_blocked_by_budget", False))
        base_signals["pending_heartflow"] = bool(decision.metadata.get("pending_heartflow", False))
        base_signals["pending_heartflow_reason"] = str(decision.metadata.get("pending_heartflow_reason", "") or "")
        base_signals["pending_heartflow_consumed"] = bool(decision.metadata.get("pending_heartflow_consumed", False))
        base_signals["batch_plan"] = dict(decision.metadata.get("batch_plan", {}) or {})
        base_signals["batch_fill_rate"] = float(decision.metadata.get("batch_fill_rate", 0.0) or 0.0)
        base_signals["batch_pressure"] = dict(decision.metadata.get("batch_pressure", {}) or {})
        base_signals["quota_skip_counts"] = dict(decision.metadata.get("quota_skip_counts", {}) or {})
        base_signals["busy_backpressure_active"] = bool(decision.metadata.get("busy_backpressure_active", False))
        base_signals["maintenance_backpressure_active"] = bool(decision.metadata.get("maintenance_backpressure_active", False))
        base_signals["forced_promotions_selected"] = list(decision.metadata.get("forced_promotions_selected", []) or [])

        preserved = dict(state.pending_signals or {})
        for key in base_signals.keys():
            preserved.pop(key, None)
        preserved.update(base_signals)
        state.pending_signals = preserved

    def _apply_dispatch_failure_state(
        self,
        state: ChatLoopState,
        snapshot: ChatLoopSnapshot,
        decision: ChatLoopDecision,
        exc: Exception,
    ) -> None:
        decision.metadata["dispatch_failed"] = True
        decision.metadata["dispatch_error_type"] = exc.__class__.__name__
        decision.metadata["dispatch_error_reason"] = str(exc or "")
        if snapshot.trigger_type == "heartbeat":
            decision.next_tick_delay = self.FAST_RECHECK_SECONDS
            decision.metadata["dispatch_failure_backoff"] = self.FAST_RECHECK_SECONDS
            decision.metadata["schedule_reason"] = "dispatch_failure_recheck"
            decision.metadata["scheduler_bucket"] = "fast_recheck"
            state.retry_backoff_until = time.time() + self.FAST_RECHECK_SECONDS
            decision.metadata["retry_backoff_until"] = state.retry_backoff_until
            state.next_tick_at = state.retry_backoff_until
        self._write_base_pending_signals(state, decision)

    def _apply_post_dispatch_state(self, state: ChatLoopState, decision: ChatLoopDecision, dispatch_result: Any) -> None:
        if decision.action in self.BACKGROUND_ACTIONS:
            state.last_background_action = decision.action
        if decision.action not in {"PROACTIVE_WAKEUP", "HEARTFLOW_EVALUATE", "COMPACTION_EVALUATE"}:
            return
        if not isinstance(dispatch_result, dict):
            return
        cooldown_until = float(dispatch_result.get("cooldown_until", 0.0) or 0.0)
        cooldown_reason = str(dispatch_result.get("cooldown_reason", "") or "")
        if decision.action == "PROACTIVE_WAKEUP" and cooldown_until > 0:
            state.cooldowns["wakeup"] = cooldown_until
            if cooldown_reason:
                state.pending_signals["wakeup_cooldown_reason"] = cooldown_reason
        elif decision.action == "HEARTFLOW_EVALUATE" and cooldown_until > 0:
            state.cooldowns["heartflow"] = cooldown_until
            if cooldown_reason:
                state.pending_signals["heartflow_cooldown_reason"] = cooldown_reason
        elif decision.action == "COMPACTION_EVALUATE" and cooldown_until > 0:
            state.cooldowns["compaction"] = cooldown_until
            if cooldown_reason:
                state.pending_signals["compaction_cooldown_reason"] = cooldown_reason

    async def _dispatch(self, chat_id: str, snapshot: ChatLoopSnapshot, decision: ChatLoopDecision, event: Any) -> Any:
        if decision.action in {"INGRESS_MESSAGE", "INGRESS_EXTERNAL", "RESUME_WAIT", "INTERRUPT_WAIT"}:
            return await self.handle_message_trigger(event, decision)

        if decision.action == "SKIP_BUSY":
            decision.metadata["dispatch_bridge"] = "busy_skip"
            return None

        bridge = self._dispatch_bridges.get(decision.action)
        if bridge is not None and decision.should_dispatch:
            decision.metadata["dispatch_bridge"] = decision.action
            return await self._maybe_await(bridge(chat_id, snapshot, decision))
        decision.metadata["dispatch_bridge"] = "observe_only"
        return await self.handle_heartbeat_trigger(chat_id, snapshot, decision)

    async def handle_message_trigger(self, event: Any, decision: ChatLoopDecision) -> Any:
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("astrmai_loop_action", decision.action)
            event.set_extra("astrmai_loop_reason", decision.reason)
        if self._message_handler is None:
            return None
        return await self._maybe_await(self._message_handler(event))

    async def handle_heartbeat_trigger(self, chat_id: str, snapshot: ChatLoopSnapshot, decision: ChatLoopDecision) -> Any:
        if self._heartbeat_handler is None:
            return {
                "chat_id": chat_id,
                "action": decision.action,
                "reason": decision.reason,
                "dispatch_mode": "observe_only",
            }
        return await self._maybe_await(self._heartbeat_handler(chat_id, snapshot, decision))

    def _trace_tick(
        self,
        state: ChatLoopState,
        snapshot: ChatLoopSnapshot,
        decision: ChatLoopDecision,
        dispatch_result: Any,
        pre_state_summary: dict[str, Any],
    ) -> None:
        logger.debug(
            "[ChatLoopKernel] tick chat=%s trigger=%s action=%s reason=%s bridge=%s pre=%s post=%s quiet=%s cooldown_blocks=%s wait_scope=%s maintenance_winner=%s skipped_lower=%s next_tick=%.1fs due=%s bucket=%s schedule_reason=%s signals=%s",
            state.chat_id,
            snapshot.trigger_type,
            decision.action,
            decision.reason,
            decision.metadata.get("dispatch_bridge", ""),
            pre_state_summary,
            self._summarize_state(state),
            decision.metadata.get("quiet_active", False),
            decision.metadata.get("cooldown_blocks", []),
            decision.metadata.get("wait_scope", ""),
            decision.metadata.get("maintenance_priority_winner", ""),
            decision.metadata.get("skipped_lower_priority_actions", []),
            float(decision.next_tick_delay or 0.0),
            decision.metadata.get("due_for_heartbeat", False),
            decision.metadata.get("scheduler_bucket", ""),
            decision.metadata.get("schedule_reason", ""),
            decision.metadata.get("signals_summary", {}),
        )
        self._emit_tick_observability(state, snapshot, decision, dispatch_result, pre_state_summary)

    def _emit_due_selection_observability(self, report: dict[str, Any]) -> None:
        hub = getattr(self, "observability_hub", None)
        if hub is None:
            return
        selected = list(report.get("selected", []) or [])
        skipped = list(report.get("skipped_by_batch", []) or [])
        poll_mode = str(report.get("poll_mode", "") or "")
        batch_plan = dict(report.get("batch_plan", {}) or {})
        pressure = dict(report.get("batch_pressure", {}) or {})
        pressure_active = (
            float(pressure.get("busy_ratio", 0.0) or 0.0) > 0.0
            or float(pressure.get("maintenance_backlog_ratio", 0.0) or 0.0) > 0.0
            or int(pressure.get("retry_pressure_count", 0) or 0) > 0
        )
        common_detail = {
            "poll_mode": poll_mode,
            "batch_plan": batch_plan,
            "batch_pressure": pressure,
            "quota_skip_counts": dict(report.get("quota_skip_counts", {}) or {}),
            "forced_promotions_selected": list(report.get("forced_promotions_selected", []) or []),
            "selected": selected,
            "skipped_by_batch": skipped,
        }
        safe_create_task(
            hub.record(
                domain="scheduler",
                kind="heartbeat",
                level="warning" if pressure_active else "info",
                chat_id="",
                title="Due selection committed",
                summary=f"selected={len(selected)} skipped={len(skipped)} poll_mode={poll_mode or '-'}",
                tags={
                    "domain": "scheduler",
                    "kind": "heartbeat",
                    "level": "warning" if pressure_active else "info",
                    "phase": "heartbeat",
                    "action": "due_selection_committed",
                    "scheduler_bucket": str(batch_plan.get("selected_bucket", "") or ""),
                },
                facets={
                    "phase": "heartbeat",
                    "action": "due_selection_committed",
                    "poll_mode": poll_mode,
                    "scheduler_bucket": str(batch_plan.get("selected_bucket", "") or ""),
                },
                detail=common_detail,
                raw=dict(report or {}),
            )
        )

    def _emit_tick_observability(
        self,
        state: ChatLoopState,
        snapshot: ChatLoopSnapshot,
        decision: ChatLoopDecision,
        dispatch_result: Any,
        pre_state_summary: dict[str, Any],
    ) -> None:
        hub = getattr(self, "observability_hub", None)
        if hub is None:
            return
        action = str(decision.action or "").strip()
        reason = str(decision.reason or "").strip()
        metadata = dict(decision.metadata or {})
        level = "info"
        if bool(metadata.get("dispatch_failed", False)):
            level = "error"
        elif reason in {"maintenance_budget_blocked", "cooldown_blocked"} or bool(metadata.get("maintenance_blocked_by_budget", False)):
            level = "warning"
        kind = "heartbeat"
        if action in {"COMPACTION_EVALUATE", "MEMORY_MAINTENANCE", "DREAM_MAINTENANCE"}:
            kind = "maintenance"
        elif action in {"PROACTIVE_WAKEUP", "HEARTFLOW_EVALUATE"}:
            kind = "action"
        elif action in {"WAIT", "RESUME_WAIT", "INTERRUPT_WAIT"}:
            kind = "trace"
        summary_bits = [f"action={action or '-'}", f"reason={reason or '-'}"]
        if metadata.get("selected_reason"):
            summary_bits.append(f"selected={metadata.get('selected_reason')}")
        if metadata.get("quota_skip_reason"):
            summary_bits.append(f"skip={metadata.get('quota_skip_reason')}")
        if metadata.get("poll_mode"):
            summary_bits.append(f"poll={metadata.get('poll_mode')}")
        title = action.replace("_", " ").title() if action else "Scheduler tick"
        safe_create_task(
            hub.record(
                domain="scheduler",
                kind=kind,
                level=level,
                chat_id=str(state.chat_id or ""),
                title=title,
                summary=" | ".join(summary_bits),
                tags={
                    "domain": "scheduler",
                    "kind": kind,
                    "level": level,
                    "chat_id": str(state.chat_id or ""),
                    "phase": str(state.phase or ""),
                    "action": action.lower(),
                    "scheduler_bucket": str(metadata.get("scheduler_bucket", "") or ""),
                },
                facets={
                    "phase": str(state.phase or ""),
                    "action": action.lower(),
                    "stage": str(snapshot.trigger_type or ""),
                    "reason": reason,
                    "selected_reason": str(metadata.get("selected_reason", "") or ""),
                    "quota_skip_reason": str(metadata.get("quota_skip_reason", "") or ""),
                    "poll_mode": str(metadata.get("poll_mode", "") or ""),
                    "scheduler_bucket": str(metadata.get("scheduler_bucket", "") or ""),
                },
                detail={
                    "snapshot_trigger_type": str(snapshot.trigger_type or ""),
                    "next_tick_delay": float(decision.next_tick_delay or 0.0),
                    "dispatch_result": dispatch_result if isinstance(dispatch_result, dict) else {},
                    "pre_state_summary": dict(pre_state_summary or {}),
                    "post_state_summary": self._summarize_state(state),
                    "metadata": metadata,
                },
                raw={
                    "chat_id": str(state.chat_id or ""),
                    "trigger_type": str(snapshot.trigger_type or ""),
                    "action": action,
                    "reason": reason,
                    "metadata": metadata,
                    "pre_state_summary": dict(pre_state_summary or {}),
                    "post_state_summary": self._summarize_state(state),
                },
            )
        )

    def _apply_wait_arm(
        self,
        state: ChatLoopState,
        *,
        wait_mode: str,
        wait_scope: str,
        target_ids: list[Any],
        target_name: str,
        thread_signature: str,
        started_at: float,
        expires_at: float,
        message_budget: int,
        reason: str,
    ) -> None:
        state.wait_mode = str(wait_mode or "none")
        state.wait_scope = self._normalize_scope(wait_scope)
        state.wait_status = "armed"
        state.wait_target_ids = self._dedupe_ids(list(target_ids or []))
        state.wait_target_name = str(target_name or "")
        state.wait_thread_signature = str(thread_signature or "")
        state.wait_started_at = float(started_at or 0.0)
        state.wait_expires_at = float(expires_at or 0.0)
        state.wait_message_budget = int(message_budget or 0)
        state.wait_resume_reason = ""
        state.interrupt_reason = ""
        state.pending_signals["wait_reason"] = str(reason or "")

    def _clear_wait_state(self, state: ChatLoopState, *, status: str, reason: str) -> None:
        state.wait_mode = "none"
        state.wait_scope = ""
        state.wait_status = str(status or "idle")
        state.wait_target_ids = []
        state.wait_target_name = ""
        state.wait_thread_signature = ""
        state.wait_started_at = 0.0
        state.wait_expires_at = 0.0
        state.wait_message_budget = 0
        if status == "resumed":
            state.wait_resume_reason = str(reason or "")
        elif status == "interrupted":
            state.interrupt_reason = str(reason or "")
        state.pending_signals["wait_terminal_reason"] = str(reason or "")

    def _expire_wait_if_needed(self, state: ChatLoopState) -> None:
        if not self._has_active_wait(state):
            return
        now = monotonic()
        if state.wait_expires_at > 0 and now >= float(state.wait_expires_at or 0.0):
            self._clear_wait_state(state, status="expired", reason="wait_timeout")
            return
        if state.wait_message_budget < 0:
            self._clear_wait_state(state, status="expired", reason="wait_message_budget_exhausted")

    def _prune_expired_cooldowns(self, state: ChatLoopState) -> None:
        now = time.time()
        for action, until in list((state.cooldowns or {}).items()):
            if float(until or 0.0) <= now:
                state.cooldowns.pop(action, None)
                state.pending_signals.pop(f"{action}_cooldown_reason", None)

    async def _mirror_runtime_wait_targets(self, chat_id: str, targets: list[str], target_name: str) -> None:
        if self.runtime_coordinator is None or not hasattr(self.runtime_coordinator, "update_wait_targets"):
            return
        try:
            await self.runtime_coordinator.update_wait_targets(str(chat_id or ""), list(targets or []), str(target_name or ""))
        except Exception as exc:
            logger.debug(f"[ChatLoopKernel] runtime wait target sync degraded for {chat_id}: {exc}")

    async def _clear_runtime_wait_targets_if_needed(self, chat_id: str) -> None:
        await self._mirror_runtime_wait_targets(chat_id, [], "")

    def _summarize_state(self, state: ChatLoopState) -> dict[str, Any]:
        return {
            "phase": state.phase,
            "next_tick_at": float(state.next_tick_at or 0.0),
            "wait_mode": state.wait_mode,
            "wait_scope": state.wait_scope,
            "wait_status": state.wait_status,
            "wait_target_ids": list(state.wait_target_ids or []),
            "wait_expires_at": float(state.wait_expires_at or 0.0),
            "interrupt_reason": state.interrupt_reason,
            "cooldowns": dict(state.cooldowns or {}),
            "last_background_action": state.last_background_action,
            "last_interrupt_at": float(state.last_interrupt_at or 0.0),
            "last_selected_at": float(state.last_selected_at or 0.0),
            "consecutive_selected_count": int(state.consecutive_selected_count or 0),
            "last_maintenance_selected_at": float(state.last_maintenance_selected_at or 0.0),
            "retry_backoff_until": float(state.retry_backoff_until or 0.0),
            "missed_due_passes": int(state.missed_due_passes or 0),
            "forced_promotion_count": int(state.forced_promotion_count or 0),
            "last_forced_promotion_at": float(state.last_forced_promotion_at or 0.0),
        }


__all__ = ["ChatLoopKernel"]
