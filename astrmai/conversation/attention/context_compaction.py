from __future__ import annotations

import asyncio
import re
import time
import uuid
from time import monotonic
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ...infrastructure.context_economy import PromptTemplateId, WorkloadFamily
from ...infrastructure.runtime.lane_manager import LaneKey
from .compaction_providers import CompactionProviderMixin
from .topic_units import ColdSummaryStructure, SECTION_ORDER, TopicUnit


@dataclass(slots=True)
class CompactionResult:
    chat_id: str
    triggered: bool = False
    drained_segments: int = 0
    summary: str = ""
    skipped_reason: str = ""
    state: str = ""
    generation: int = 0
    eligibility_reason: str = ""
    recompact_armed: bool = False
    focus_tail_overlap: bool = False
    delta_old_segments: int = 0
    delta_old_message_load: float = 0.0
    delta_old_long_message_count: int = 0
    message_count_since_last_compaction: int = 0
    active_segment_count: int = 0
    old_segment_count: int = 0
    recent_segment_count: int = 0
    next_eval_at_count: int = 80
    reason: str = ""
    final_score: float = 0.0
    count_score: float = 0.0
    closure_score: float = 0.0
    tail_activity_score: float = 0.0
    topic_density_score: float = 0.0
    stability_score: float = 0.0
    benefit_score: float = 0.0
    is_forced: bool = False
    is_safe_to_compact: bool = False
    closure_signals: list[str] = None
    tail_activity_signals: list[str] = None
    topic_density_signals: list[str] = None
    stability_signals: list[str] = None
    benefit_signals: list[str] = None
    forced_pending_message_delta: int = 0
    last_safe_window_seen_at_count: int = 0
    post_compaction_recovery_rounds: int = 0
    evaluation_count: int = 0
    current_message_count: int = 0
    queued_eval_node: int = 0
    pending_eval_nodes_count: int = 0
    pending_eval_nodes: list[int] = None
    force_execute_on_next_safe_hook: bool = False
    safe_hook_block_reason: str = ""
    last_hook_source: str = ""
    last_safe_hook_checked_at: int = 0


@dataclass(slots=True)
class CompactionDecisionSnapshot:
    chat_id: str
    message_count_since_last_compaction: int = 0
    active_segment_count: int = 0
    old_segment_count: int = 0
    recent_segment_count: int = 0
    closure_score: float = 0.0
    tail_activity_score: float = 0.0
    topic_density_score: float = 0.0
    stability_score: float = 0.0
    benefit_score: float = 0.0
    count_score: float = 0.0
    final_score: float = 0.0
    state: str = "NOT_READY"
    is_forced: bool = False
    is_safe_to_compact: bool = False
    next_eval_at_count: int = 80
    reason: str = ""
    focus_tail_overlap: bool = False
    closure_signals: list[str] = None
    tail_activity_signals: list[str] = None
    topic_density_signals: list[str] = None
    stability_signals: list[str] = None
    benefit_signals: list[str] = None
    forced_pending_message_delta: int = 0
    last_safe_window_seen_at_count: int = 0
    post_compaction_recovery_rounds: int = 0
    evaluation_count: int = 0
    current_message_count: int = 0
    queued_eval_node: int = 0
    pending_eval_nodes_count: int = 0
    pending_eval_nodes: list[int] = None
    force_execute_on_next_safe_hook: bool = False
    safe_hook_block_reason: str = ""
    last_hook_source: str = ""
    last_safe_hook_checked_at: int = 0


class CompactionSafetyAnalyzer:
    def __init__(self, engine: "ContextCompactionEngine"):
        self._engine = engine

    def detect_safe_window(self, snapshot: dict[str, Any], focus_context: Any = None) -> tuple[bool, str, bool, list[str]]:
        return self._engine.detect_safe_window(snapshot, focus_context=focus_context)

    def build_decision_snapshot(
        self,
        chat_id: str,
        snapshot: dict[str, Any],
        focus_context: Any = None,
        *,
        evaluation_count: int | None = None,
        queued_from_node: bool = False,
    ) -> CompactionDecisionSnapshot:
        return self._engine.build_decision_snapshot(
            chat_id,
            snapshot,
            focus_context=focus_context,
            evaluation_count=evaluation_count,
            queued_from_node=queued_from_node,
        )

    def evaluate_compaction_eligibility(
        self,
        chat_id: str,
        snapshot: dict[str, Any],
        focus_context: Any = None,
        *,
        evaluation_count: int | None = None,
        queued_from_node: bool = False,
        record_state: bool = True,
    ) -> CompactionResult:
        return self._engine.evaluate_compaction_eligibility(
            chat_id,
            snapshot,
            focus_context=focus_context,
            evaluation_count=evaluation_count,
            queued_from_node=queued_from_node,
            record_state=record_state,
        )


class CompactionWindowSelector:
    def __init__(self, engine: "ContextCompactionEngine"):
        self._engine = engine

    def bootstrap_message_counter(self, chat_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self._engine._bootstrap_message_counter(chat_id, snapshot)

    def note_message_appended(self, chat_id: str, message_source: str | None = None) -> dict[str, Any]:
        return self._engine._note_message_appended(chat_id, message_source=message_source)

    def pop_pending_eval_node(self, state: dict[str, Any], node: int) -> None:
        self._engine._pop_pending_eval_node(state, node)


class CompactionExecutor:
    def __init__(self, engine: "ContextCompactionEngine"):
        self._engine = engine

    async def maybe_compact(self, chat_id: str, focus_context: Any = None) -> CompactionResult:
        return await self._engine.maybe_compact(chat_id, focus_context=focus_context)

    async def get_trace_status(self, chat_id: str, focus_context: Any = None) -> dict[str, Any]:
        return await self._engine.get_trace_status(chat_id, focus_context=focus_context)


class ContextCompactionEngine(CompactionProviderMixin):
    """ponytail: no unified compaction error recovery mechanism.
    Partial coverage: merge failures set cooldown (P2.12).
    Full recovery (revert to pre-compaction state) requires redesign."""
    def __init__(
        self,
        dialogue_store,
        *,
        compaction_trigger_segments: int = 40,
        compaction_trigger_tokens: int = 1800,
        compaction_keep_recent_segments: int = 16,
        compaction_summary_max_tokens: int = 450,
        provider_id: str = "",
        gateway: Any = None,
        background_task_budget: Any = None,
        owner_registry: Any = None,
    ):
        self.dialogue_store = dialogue_store
        self.compaction_trigger_segments = int(compaction_trigger_segments or 40)
        self.compaction_trigger_tokens = int(compaction_trigger_tokens or 1800)
        self.compaction_keep_recent_segments = int(compaction_keep_recent_segments or 16)
        self.compaction_summary_max_tokens = int(compaction_summary_max_tokens or 450)
        self.provider_id = str(provider_id or "")
        self.gateway = gateway
        self.background_task_budget = background_task_budget
        self.owner_registry = owner_registry
        self._cooldown_by_chat: dict[str, float] = {}
        self._success_cooldown_seconds = 20.0
        self._failure_cooldown_seconds = 10.0
        self._chat_states: dict[str, dict[str, Any]] = {}
        # ponytail: guard against unbounded _chat_states growth
        self._last_chat_state_prune: float = 0.0
        self._pending_tasks: dict[str, Any] = {}
        self.safety_analyzer = CompactionSafetyAnalyzer(self)
        self.window_selector = CompactionWindowSelector(self)
        self.compaction_executor = CompactionExecutor(self)

    def refresh_config(self, config) -> None:
        conversation = getattr(config, "conversation", None)
        if conversation is None:
            return
        self.compaction_trigger_segments = int(getattr(conversation, "compaction_trigger_segments", 40) or 40)
        self.compaction_trigger_tokens = int(getattr(conversation, "compaction_trigger_tokens", 1800) or 1800)
        self.compaction_keep_recent_segments = int(getattr(conversation, "compaction_keep_recent_segments", 16) or 16)
        self.compaction_summary_max_tokens = int(getattr(conversation, "compaction_summary_max_tokens", 450) or 450)
        self.provider_id = str(getattr(conversation, "compaction_provider_id", "") or "")

    @staticmethod
    def _normalize_summary_text(text: str) -> str:
        return " ".join(str(text or "").split()).strip()

    @classmethod
    def _preview_text(cls, text: str, limit: int = 36) -> str:
        cleaned = cls._normalize_summary_text(text)
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)] + "..."

    @classmethod
    def _is_question_like(cls, text: str) -> bool:
        cleaned = cls._normalize_summary_text(text)
        lowered = cleaned.lower()
        if not cleaned:
            return False
        return (
            "?" in cleaned
            or "？" in cleaned
            or any(token in lowered for token in ("how", "what", "why", "when", "where", "吗", "么", "什么", "怎么", "为何", "要不要", "是不是"))
        )

    @classmethod
    def _is_decision_like(cls, text: str) -> bool:
        cleaned = cls._normalize_summary_text(text)
        lowered = cleaned.lower()
        if not cleaned or cls._is_question_like(cleaned):
            return False
        return any(token in lowered for token in ("可以", "确定", "决定", "那就", "按这个", "就这样", "收到", "ok", "deal", "没问题"))

    @classmethod
    def _is_constraint_like(cls, text: str) -> bool:
        cleaned = cls._normalize_summary_text(text)
        lowered = cleaned.lower()
        if not cleaned:
            return False
        return any(token in lowered for token in ("不要", "必须", "记得", "约定", "限制", "只能", "不能", "务必"))

    @classmethod
    def _detect_relation_state(cls, segments) -> str:
        if any(getattr(segment, "is_at_bot", False) or getattr(segment, "is_reply_to_bot", False) for segment in segments):
            return "这段旧对话里，人和我的互动曾明显收束到同一条主线。"
        if any(getattr(segment, "reply_target_sender_id", "") or getattr(segment, "reply_target_sender_name", "") for segment in segments):
            return "这段旧对话里，大家曾沿着同一条回复链持续往下接。"
        return ""

    @classmethod
    def _detect_emotion_state(cls, segments) -> str:
        combined = " ".join(cls._normalize_summary_text(getattr(segment, "content", "")) for segment in segments if getattr(segment, "content", ""))
        lowered = combined.lower()
        if any(token in lowered for token in ("哈哈", "笑死", "233", "hh", "lol")):
            return "这段旧对话整体偏轻松，交流更像顺势接话。"
        if any(token in lowered for token in ("无语", "生气", "急", "烦", "离谱")):
            return "这段旧对话里出现过明显的紧张或不耐烦转折。"
        return ""

    @staticmethod
    def _empty_cold_structure() -> ColdSummaryStructure:
        return ColdSummaryStructure()

    @staticmethod
    def _append_unit(target: list[TopicUnit], slot: str, text: str, event_ids: list[str] | None = None) -> None:
        cleaned = " ".join(str(text or "").split()).strip()
        if not cleaned:
            return
        target.append(TopicUnit(slot=slot, text=cleaned, event_ids=list(event_ids or [])))

    async def schedule_compaction_evaluation(
        self,
        chat_id: str,
        focus_context: Any = None,
        message_source: str | None = None,
    ) -> CompactionResult:
        # ponytail: prune stale chat states every 300s
        now = time.time()
        if now - self._last_chat_state_prune > 300:
            self._prune_stale_chat_states()
            self._last_chat_state_prune = now

        state = self._state_for_chat(chat_id)
        if self.dialogue_store is not None and int(state.get("message_count_since_last_compaction", 0) or 0) <= 0:
            try:
                bootstrap_snapshot = await self.dialogue_store.snapshot_compaction_candidates(
                    chat_id,
                    keep_recent_segments=self.compaction_keep_recent_segments,
                )
                self.window_selector.bootstrap_message_counter(chat_id, bootstrap_snapshot)
            except Exception:
                logger.debug(f"[Compaction] schedule_compaction_evaluation bootstrap failed for {chat_id}", exc_info=True)
                pass
        self.window_selector.note_message_appended(chat_id, message_source=message_source)
        existing = self._pending_tasks.get(chat_id)
        if existing and not existing.done():
            return CompactionResult(
                chat_id=chat_id,
                skipped_reason="evaluation_already_scheduled",
                state=str(state.get("current_state", "NOT_READY") or "NOT_READY"),
                message_count_since_last_compaction=int(state.get("message_count_since_last_compaction", 0) or 0),
                next_eval_at_count=int(state.get("next_eval_at_count", 80) or 80),
                pending_eval_nodes=list(state.get("pending_eval_nodes", []) or []),
                pending_eval_nodes_count=len(list(state.get("pending_eval_nodes", []) or [])),
            )
        task = self._create_task(
            self.compaction_executor.maybe_compact(chat_id, focus_context=focus_context),
            chat_id=chat_id,
        )
        self._pending_tasks[chat_id] = task
        try:
            return await task
        finally:
            if self._pending_tasks.get(chat_id) is task:
                self._pending_tasks.pop(chat_id, None)

    def _create_task(self, coro, *, chat_id: str = "GLOBAL"):
        task = asyncio.create_task(coro)
        registry = getattr(self, "owner_registry", None)
        register = getattr(registry, "register", None)
        if callable(register):
            register(
                task,
                task_family="conversation.compaction",
                scope_id=chat_id or "GLOBAL",
                run_id=f"compaction-{uuid.uuid4().hex[:12]}",
                owner="ContextCompactionEngine",
                generation=getattr(registry, "generation", 0),
                cancel_status="cancelled",
            )
        return task

    def _state_for_chat(self, chat_id: str) -> dict[str, Any]:
        return self._chat_states.setdefault(
            chat_id,
            {
                "generation": 0,
                "message_count_since_last_compaction": 0,
                "current_state": "NOT_READY",
                "last_evaluated_count": 0,
                "next_eval_at_count": 80,
                "forced_pending_since_count": 0,
                "forced_pending_new_messages": 0,
                "cooldown_until": 0.0,
                "last_active_at": time.time(),
                "last_snapshot": None,
                "last_compacted_tail_event_id": "",
                "last_compacted_old_segment_count": 0,
                "last_compacted_old_message_load": 0.0,
                "last_compacted_long_message_count": 0,
                "last_cold_summary_hash": "",
                "last_retained_segment_count": 0,
                "last_retained_event_ids": [],
                "last_state": "NOT_READY",
                "last_eligibility_reason": "",
                "last_recompact_armed": False,
                "last_focus_tail_overlap": False,
                "last_delta_old_segments": 0,
                "last_delta_old_message_load": 0.0,
                "last_delta_old_long_message_count": 0,
                "last_closure_signals": [],
                "last_tail_activity_signals": [],
                "last_topic_density_signals": [],
                "last_stability_signals": [],
                "last_benefit_signals": [],
                "last_safe_window_seen_at_count": 0,
                "post_compaction_recovery_rounds": 0,
                "pending_eval_nodes": [],
                "queued_eval_up_to_count": 0,
                "force_execute_on_next_safe_hook": False,
                "last_hook_source": "",
                "last_safe_hook_checked_at": 0,
            },
        )

    def get_cooldown_until(self, chat_id: str) -> float:
        state = self._state_for_chat(chat_id)
        return float(self._cooldown_by_chat.get(chat_id, 0.0) or state.get("cooldown_until", 0.0) or 0.0)

    def clear_chat_state(self, chat_id: str) -> bool:
        removed = self._chat_states.pop(chat_id, None) is not None
        removed = self._cooldown_by_chat.pop(chat_id, None) is not None or removed
        return removed

    # ponytail: remove _chat_states entries idle > 24h to prevent unbounded growth
    def _prune_stale_chat_states(self, max_age: float = 86400.0):
        now = time.time()
        stale = [cid for cid, s in self._chat_states.items() if now - float(s.get("last_active_at", 0.0) or 0.0) > max_age]
        for cid in stale:
            self._chat_states.pop(cid, None)

    @staticmethod
    def _eval_nodes() -> tuple[int, ...]:
        return (80, 90, 100, 110, 120)

    def _queue_crossed_eval_nodes(self, state: dict[str, Any], previous_count: int, current_count: int) -> None:
        if current_count <= previous_count:
            return
        pending = list(state.get("pending_eval_nodes", []) or [])
        known = {int(node) for node in pending}
        queued_up_to = int(state.get("queued_eval_up_to_count", 0) or 0)
        for node in self._eval_nodes():
            if previous_count < node <= current_count and node > queued_up_to and node not in known:
                pending.append(node)
                known.add(node)
                queued_up_to = node
        state["pending_eval_nodes"] = pending
        state["queued_eval_up_to_count"] = queued_up_to

    @staticmethod
    def _pop_pending_eval_node(state: dict[str, Any], node: int) -> None:
        pending = list(state.get("pending_eval_nodes", []) or [])
        if pending and int(pending[0]) == int(node):
            pending.pop(0)
        else:
            pending = [item for item in pending if int(item) != int(node)]
        state["pending_eval_nodes"] = pending

    @staticmethod
    def _next_eval_node(message_count: int) -> int:
        if message_count < 80:
            return 80
        if message_count < 90:
            return 90
        if message_count < 100:
            return 100
        if message_count < 110:
            return 110
        if message_count < 120:
            return 120
        return 120

    def _note_message_appended(self, chat_id: str, message_source: str | None = None) -> dict[str, Any]:
        state = self._state_for_chat(chat_id)
        previous_count = int(state.get("message_count_since_last_compaction", 0) or 0)
        current_count = previous_count + 1
        state["message_count_since_last_compaction"] = current_count
        state["last_active_at"] = time.time()
        state["last_hook_source"] = str(message_source or "")
        state["last_safe_hook_checked_at"] = current_count
        self._queue_crossed_eval_nodes(state, previous_count, current_count)
        if state.get("forced_pending_since_count", 0):
            state["forced_pending_new_messages"] = int(state.get("forced_pending_new_messages", 0) or 0) + 1
        if message_source == "user" and int(state.get("post_compaction_recovery_rounds", 0) or 0) > 0:
            state["post_compaction_recovery_rounds"] = max(0, int(state.get("post_compaction_recovery_rounds", 0) or 0) - 1)
        return state

    def _bootstrap_message_counter(self, chat_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        state = self._state_for_chat(chat_id)
        active_segments = int(snapshot.get("active_segments", 0) or 0)
        retained_baseline = int(state.get("last_retained_segment_count", 0) or 0)
        inferred = max(0, active_segments - retained_baseline)
        current = int(state.get("message_count_since_last_compaction", 0) or 0)
        if inferred > current:
            state["message_count_since_last_compaction"] = inferred
        return state

    @staticmethod
    def _segment_message_load(segment) -> float:
        text = str(getattr(segment, "content", "") or "")
        length = len(" ".join(text.split()))
        if length <= 0:
            return 0.0
        if length <= 12:
            return 0.5
        if length <= 40:
            return 1.0
        if length <= 100:
            return 1.6
        if length <= 220:
            return 2.4
        return 3.2

    def _compute_recompact_delta(self, snapshot: dict[str, Any], state: dict[str, Any]) -> tuple[int, float, int]:
        if int(state.get("generation", 0) or 0) <= 0:
            delta_zone = list(snapshot.get("compressible_zone_segments", []) or [])
        else:
            baseline_ids = {str(item or "") for item in (state.get("last_retained_event_ids", []) or []) if str(item or "")}
            delta_zone = list(snapshot.get("compressible_zone_segments", []) or [])
            while delta_zone and str(getattr(delta_zone[0], "event_id", "") or "") in baseline_ids:
                delta_zone.pop(0)
        delta_segments = len(delta_zone)
        delta_load = sum(self._segment_message_load(segment) for segment in delta_zone)
        delta_long = sum(1 for segment in delta_zone if len(" ".join(str(getattr(segment, "content", "") or "").split())) > 100)
        return delta_segments, delta_load, delta_long

    def _is_recompact_armed(self, delta_segments: int, delta_load: float, delta_long: int, state: dict[str, Any]) -> bool:
        if int(state.get("generation", 0) or 0) <= 0:
            return True
        return (
            delta_segments >= 10
            or (delta_segments >= 6 and delta_load >= 12.0)
            or (delta_load >= 18.0 and delta_long >= 2)
        )

    @staticmethod
    def _count_bot_directed(segments) -> int:
        return sum(1 for segment in segments if getattr(segment, "is_at_bot", False) or getattr(segment, "is_reply_to_bot", False))

    @staticmethod
    def _message_kind_switch_count(segments) -> int:
        previous = ""
        switches = 0
        for segment in segments:
            current = str(getattr(segment, "message_kind", "") or "")
            if current and previous and current != previous:
                switches += 1
            if current:
                previous = current
        return switches

    @classmethod
    def _is_confirmation_like(cls, text: str) -> bool:
        cleaned = cls._normalize_summary_text(text)
        lowered = cleaned.lower()
        if not cleaned:
            return False
        return any(token in lowered for token in ("ok", "收到", "明白", "好", "好的", "行", "可以", "那就", "按这个", "没问题"))

    @classmethod
    def _is_background_like(cls, segment: Any) -> bool:
        if getattr(segment, "is_at_bot", False) or getattr(segment, "is_reply_to_bot", False):
            return False
        if getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False):
            return False
        text = cls._normalize_summary_text(getattr(segment, "content", ""))
        return bool(text) and not cls._is_question_like(text)

    @staticmethod
    def _event_id_from_focus_event(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "")
        if message_id:
            return message_id
        sender_id = ""
        if hasattr(event, "get_sender_id"):
            try:
                sender_id = str(event.get_sender_id() or "")
            except Exception:
                logger.debug("[Compaction] _event_id_from_focus_event get_sender_id failed", exc_info=True)
                sender_id = ""
        timestamp = float(getattr(event, "timestamp", 0.0) or 0.0)
        message_text = str(getattr(event, "message_str", "") or "")
        preview = message_text[:40]
        return f"{sender_id}:{timestamp}:{preview}" if (sender_id or timestamp or preview) else ""

    def _has_focus_tail_overlap(self, snapshot: dict[str, Any], focus_context: Any = None) -> bool:
        if focus_context is None:
            return False
        old_zone = list(snapshot.get("compressible_zone_segments", []) or [])
        if not old_zone:
            return False
        tail_ids = {str(getattr(segment, "event_id", "") or "") for segment in old_zone[-4:] if str(getattr(segment, "event_id", "") or "")}
        if not tail_ids:
            return False
        focus_events = focus_context.all_thread_events() if hasattr(focus_context, "all_thread_events") else []
        focus_ids = {
            self._event_id_from_focus_event(event)
            for event in (focus_events or [])
            if self._event_id_from_focus_event(event)
        }
        return bool(tail_ids & focus_ids)

    def detect_safe_window(
        self,
        snapshot: dict[str, Any],
        focus_context: Any = None,
    ) -> tuple[bool, str, bool, list[str]]:
        recent_segments = list(snapshot.get("recent_segments", []) or [])
        focus_tail_overlap = self._has_focus_tail_overlap(snapshot, focus_context=focus_context)
        signals: list[str] = []
        if focus_tail_overlap:
            return False, "focus_tail_overlap", True, ["focus_tail_overlap", "active_chain"]
        if not recent_segments:
            return True, "natural_pause", False, ["no_recent_segments", "natural_pause"]
        latest_assistant_index = -1
        for index in range(len(recent_segments) - 1, -1, -1):
            segment = recent_segments[index]
            if getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False):
                latest_assistant_index = index
                break
        trailing = recent_segments[latest_assistant_index + 1 :] if latest_assistant_index >= 0 else []
        directed_trailing = self._count_bot_directed(trailing)
        recent_window = recent_segments[-6:]
        recent_directed = self._count_bot_directed(recent_window)
        if latest_assistant_index >= 0:
            signals.append("assistant_recently_replied")
        if trailing:
            signals.append("has_post_reply_followups")
        if recent_directed <= 1:
            signals.append("bot_directed_intensity_lowered")
        if trailing and directed_trailing == 0:
            signals.append("assistant_reply_followup_settled")
        last_three = recent_segments[-3:]
        if last_three and all(self._is_background_like(segment) for segment in last_three):
            return True, "natural_pause", False, signals + ["tail_is_background_only", "natural_pause"]
        if latest_assistant_index >= 0 and directed_trailing >= 2:
            return False, "awaiting_followup_chain", False, signals + ["active_chain", "reply_chain_active"]
        last_four = recent_segments[-4:]
        if self._count_bot_directed(last_four) >= 2 and any(getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False) for segment in last_four):
            return False, "recent_dense_bot_exchange", False, signals + ["bot_directed_burst", "active_chain"]
        if (not trailing and recent_directed == 0) or (
            trailing
            and directed_trailing <= 1
            and not any(self._is_question_like(getattr(segment, "content", "")) for segment in trailing)
        ):
            signals.append("natural_pause")
        return True, "safe_window", False, signals or ["soft_settled"]

    def _closure_analysis_v2(self, recent_segments: list[Any]) -> tuple[float, list[str]]:
        recent = list(recent_segments[-12:])
        if not recent:
            return 0.0, ["closure_unknown"]
        latest_assistant_index = -1
        for index in range(len(recent) - 1, -1, -1):
            segment = recent[index]
            if getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False):
                latest_assistant_index = index
                break
        if latest_assistant_index >= 0:
            trailing = recent[latest_assistant_index + 1 :]
            if trailing and any(self._is_confirmation_like(getattr(segment, "content", "")) for segment in trailing):
                return 10.0, ["answered_and_confirmed", "closed_loop_detected"]
            if trailing and not any(self._is_question_like(getattr(segment, "content", "")) for segment in trailing):
                return 7.0, ["shifted_to_next_step"]
            if trailing and any(self._is_question_like(getattr(segment, "content", "")) for segment in trailing):
                return 2.0, ["answered_unconfirmed", "question_open"]
        if any(self._is_decision_like(getattr(segment, "content", "")) for segment in recent):
            return 5.0, ["answered_unconfirmed"]
        if any(self._is_question_like(getattr(segment, "content", "")) for segment in recent):
            return 1.0, ["question_open"]
        return 3.0, ["soft_settled"]

    def _tail_activity_analysis_v2(self, recent_segments: list[Any]) -> tuple[float, list[str]]:
        recent = list(recent_segments[-6:])
        if not recent:
            return 0.0, ["tail_idle"]
        directed_count = self._count_bot_directed(recent)
        assistant_recent = any(getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False) for segment in recent[-4:])
        if directed_count >= 3:
            return -14.0, ["bot_directed_burst", "reply_chain_active"]
        last_four = recent[-4:]
        if directed_count >= 2 and any(getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False) for segment in last_four):
            return -10.0, ["reply_chain_active", "assistant_recently_replied"]
        if directed_count >= 1 or any(self._is_question_like(getattr(segment, "content", "")) for segment in recent):
            signals = ["light_followup"]
            if assistant_recent:
                signals.append("assistant_recently_replied")
            return -5.0, signals
        return 0.0, ["tail_is_background_only"]

    def _topic_density_analysis_v2(self, recent_segments: list[Any]) -> tuple[float, list[str]]:
        recent = list(recent_segments[-20:])
        if not recent:
            return 0.0, ["single_thread"]
        token_buckets: set[str] = set()
        reply_targets: set[str] = set()
        message_kinds: set[str] = set()
        for segment in recent:
            tokens = sorted(self._semantic_tokens(getattr(segment, "content", "")))
            if tokens:
                token_buckets.add("|".join(tokens[:2]))
            target = str(getattr(segment, "reply_target_sender_id", "") or getattr(segment, "reply_target_sender_name", "") or "")
            if target:
                reply_targets.add(target)
            kind = str(getattr(segment, "message_kind", "") or "")
            if kind:
                message_kinds.add(kind)
        reply_branches = len(reply_targets)
        kind_switches = self._message_kind_switch_count(recent)
        semantic_buckets = len(token_buckets)
        density = semantic_buckets + reply_branches + max(0, len(message_kinds) - 1) + min(kind_switches, 3)
        if density >= 11 or (semantic_buckets >= 4 and reply_branches >= 2):
            return 8.0, ["parallel_subthreads", "semantic_bucket_rich", "reply_target_forked"]
        if density >= 8:
            return 6.0, ["multi_topic_active", "message_kind_switching"]
        if density >= 4:
            return 4.0, ["multi_topic_light"]
        if density >= 2:
            return 2.0, ["single_thread_with_noise"]
        return 0.0, ["single_thread"]

    def _stability_analysis_v2(
        self,
        is_safe: bool,
        safety_reason: str,
        focus_tail_overlap: bool,
        safe_window_reason: str,
        safe_window_signals: list[str],
        recent_segments: list[Any],
    ) -> tuple[float, list[str]]:
        if is_safe:
            signals = list(safe_window_signals or [])
            if safe_window_reason == "natural_pause" and "natural_pause" not in signals:
                signals.append("natural_pause")
            return 11.0, signals or ["soft_settled"]
        if focus_tail_overlap:
            return 0.0, ["focus_tail_overlap", "active_chain"]
        if safety_reason == "awaiting_followup_chain":
            return 1.0, ["active_chain", "reply_chain_active"]
        if safety_reason == "recent_dense_bot_exchange":
            return 2.0, ["bot_directed_burst", "active_chain"]
        directed_count = self._count_bot_directed(list(recent_segments[-6:]))
        if directed_count <= 1:
            return 6.0, ["soft_settled"]
        return 3.0, ["stability_uncertain"]

    def _benefit_analysis_v2(self, snapshot: dict[str, Any]) -> tuple[float, list[str]]:
        active_segments = max(1, int(snapshot.get("active_segments", 0) or 0))
        old_segments = int(snapshot.get("compressible_segments", 0) or 0)
        old_zone = list(snapshot.get("compressible_zone_segments", []) or [])
        old_ratio = old_segments / float(active_segments)
        duplicate_ratio = 0.0
        if old_zone:
            normalized = [self._normalize_summary_text(getattr(segment, "content", "")) for segment in old_zone]
            non_empty = [item for item in normalized if item]
            if non_empty:
                duplicate_ratio = max(0.0, 1.0 - (len(set(non_empty)) / float(len(non_empty))))
        old_topic_count = len(
            {
                "|".join(sorted(self._semantic_tokens(getattr(segment, "content", "")))[:2])
                for segment in old_zone
                if self._semantic_tokens(getattr(segment, "content", ""))
            }
        )
        score = 0.0
        signals: list[str] = []
        if old_ratio >= 0.65:
            score += 6.0
            signals.append("old_zone_ratio_high")
        elif old_ratio >= 0.45:
            score += 4.0
            signals.append("old_zone_ratio_medium")
        elif old_ratio >= 0.25:
            score += 2.0
            signals.append("old_zone_ratio_low")
        if duplicate_ratio >= 0.35:
            score += 3.0
            signals.append("old_zone_redundancy_high")
        elif duplicate_ratio >= 0.15:
            score += 2.0
            signals.append("old_zone_redundancy_medium")
        if old_topic_count >= 4:
            score += 1.0
            signals.append("old_zone_topic_span_high")
        if float(snapshot.get("compressible_message_load", 0.0) or 0.0) >= 36.0:
            score += 1.0
            signals.append("cold_summary_increment_worthwhile")
        return min(score, 10.0), signals or ["benefit_low"]

    # ponytail: deprecated, replaced by detect_safe_window — remove when confirmed no external callers
    def _safety_analysis(self, snapshot: dict[str, Any], focus_context: Any = None) -> tuple[bool, str, bool]:
        recent_segments = list(snapshot.get("recent_segments", []) or [])
        focus_tail_overlap = self._has_focus_tail_overlap(snapshot, focus_context=focus_context)
        if focus_tail_overlap:
            return False, "focus_tail_overlap", True
        if not recent_segments:
            return True, "", False
        latest_assistant_index = -1
        for index in range(len(recent_segments) - 1, -1, -1):
            segment = recent_segments[index]
            if getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False):
                latest_assistant_index = index
                break
        if latest_assistant_index >= 0:
            trailing = recent_segments[latest_assistant_index + 1 :]
            if self._count_bot_directed(trailing) >= 2:
                return False, "awaiting_followup_chain", False
        last_four = recent_segments[-4:]
        if self._count_bot_directed(last_four) >= 2 and any(getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False) for segment in last_four):
            return False, "recent_dense_bot_exchange", False
        return True, "", False

    def _count_score(self, message_count: int) -> float:
        if message_count < 80:
            return 0.0
        if message_count < 90:
            return 32.0
        if message_count < 100:
            return 40.0
        if message_count < 110:
            return 48.0
        if message_count < 120:
            return 55.0
        return 70.0

    def _closure_score(self, recent_segments: list[Any]) -> float:
        recent = list(recent_segments[-12:])
        if not recent:
            return 0.0
        latest_assistant_index = -1
        for index in range(len(recent) - 1, -1, -1):
            segment = recent[index]
            if getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False):
                latest_assistant_index = index
                break
        if latest_assistant_index >= 0:
            trailing = recent[latest_assistant_index + 1 :]
            if trailing and any(self._is_confirmation_like(getattr(segment, "content", "")) for segment in trailing):
                return 9.0
            if trailing and not any(self._is_question_like(getattr(segment, "content", "")) for segment in trailing):
                return 7.0
            if trailing and any(self._is_question_like(getattr(segment, "content", "")) for segment in trailing):
                return 2.0
        if any(self._is_decision_like(getattr(segment, "content", "")) for segment in recent):
            return 5.0
        return 1.0

    def _tail_activity_score(self, recent_segments: list[Any]) -> float:
        recent = list(recent_segments[-6:])
        if not recent:
            return 0.0
        directed_count = self._count_bot_directed(recent)
        if directed_count >= 3:
            return -14.0
        last_four = recent[-4:]
        if directed_count >= 2 and any(getattr(segment, "role", "") == "assistant" or getattr(segment, "is_bot", False) for segment in last_four):
            return -10.0
        if directed_count >= 1 or any(self._is_question_like(getattr(segment, "content", "")) for segment in recent):
            return -5.0
        return 0.0

    def _topic_density_score(self, recent_segments: list[Any]) -> float:
        recent = list(recent_segments[-20:])
        if not recent:
            return 0.0
        token_buckets: set[str] = set()
        reply_targets: set[str] = set()
        message_kinds: set[str] = set()
        for segment in recent:
            tokens = sorted(self._semantic_tokens(getattr(segment, "content", "")))
            if tokens:
                token_buckets.add("|".join(tokens[:2]))
            target = str(getattr(segment, "reply_target_sender_id", "") or getattr(segment, "reply_target_sender_name", "") or "")
            if target:
                reply_targets.add(target)
            kind = str(getattr(segment, "message_kind", "") or "")
            if kind:
                message_kinds.add(kind)
        density = len(token_buckets) + len(reply_targets) + max(0, len(message_kinds) - 1)
        if density >= 10:
            return 8.0
        if density >= 7:
            return 6.0
        if density >= 4:
            return 4.0
        if density >= 2:
            return 2.0
        return 0.0

    def _stability_score(self, is_safe: bool, safety_reason: str, focus_tail_overlap: bool, recent_segments: list[Any]) -> float:
        if is_safe:
            return 11.0
        if focus_tail_overlap:
            return 0.0
        if safety_reason == "awaiting_followup_chain":
            return 1.0
        if safety_reason == "recent_dense_bot_exchange":
            return 2.0
        directed_count = self._count_bot_directed(list(recent_segments[-6:]))
        if directed_count <= 1:
            return 6.0
        return 3.0

    def _benefit_score(self, snapshot: dict[str, Any]) -> float:
        active_segments = max(1, int(snapshot.get("active_segments", 0) or 0))
        old_segments = int(snapshot.get("compressible_segments", 0) or 0)
        old_zone = list(snapshot.get("compressible_zone_segments", []) or [])
        old_ratio = old_segments / float(active_segments)
        duplicate_ratio = 0.0
        if old_zone:
            normalized = [self._normalize_summary_text(getattr(segment, "content", "")) for segment in old_zone]
            non_empty = [item for item in normalized if item]
            if non_empty:
                duplicate_ratio = max(0.0, 1.0 - (len(set(non_empty)) / float(len(non_empty))))
        score = 0.0
        if old_ratio >= 0.65:
            score += 6.0
        elif old_ratio >= 0.45:
            score += 4.0
        elif old_ratio >= 0.25:
            score += 2.0
        if duplicate_ratio >= 0.35:
            score += 3.0
        elif duplicate_ratio >= 0.15:
            score += 2.0
        if float(snapshot.get("compressible_message_load", 0.0) or 0.0) >= 36.0:
            score += 1.0
        return min(score, 10.0)

    def _record_latest_status(self, chat_id: str, result: CompactionResult, *, state_name: str) -> None:
        state = self._state_for_chat(chat_id)
        state["current_state"] = str(state_name or "")
        state["last_state"] = str(state_name or "")
        state["last_eligibility_reason"] = str(result.reason or result.eligibility_reason or result.skipped_reason or "")
        state["last_recompact_armed"] = bool(result.recompact_armed)
        state["last_focus_tail_overlap"] = bool(result.focus_tail_overlap)
        state["last_delta_old_segments"] = int(result.delta_old_segments or 0)
        state["last_delta_old_message_load"] = float(result.delta_old_message_load or 0.0)
        state["last_delta_old_long_message_count"] = int(result.delta_old_long_message_count or 0)
        state["last_closure_signals"] = list(result.closure_signals or [])
        state["last_tail_activity_signals"] = list(result.tail_activity_signals or [])
        state["last_topic_density_signals"] = list(result.topic_density_signals or [])
        state["last_stability_signals"] = list(result.stability_signals or [])
        state["last_benefit_signals"] = list(result.benefit_signals or [])
        if bool(result.is_safe_to_compact):
            safe_count = int(result.last_safe_window_seen_at_count or result.message_count_since_last_compaction or 0)
            state["last_safe_window_seen_at_count"] = safe_count
        state["safe_hook_block_reason"] = str(result.safe_hook_block_reason or "")
        state["force_execute_on_next_safe_hook"] = bool(result.force_execute_on_next_safe_hook)
        state["last_hook_source"] = str(result.last_hook_source or state.get("last_hook_source", "") or "")
        state["last_safe_hook_checked_at"] = int(result.last_safe_hook_checked_at or state.get("last_safe_hook_checked_at", 0) or 0)
        if state_name == "FORCED_PENDING" and not int(state.get("forced_pending_since_count", 0) or 0):
            state["forced_pending_since_count"] = int(result.evaluation_count or result.message_count_since_last_compaction or 120)
            state["forced_pending_new_messages"] = 0
        state["next_eval_at_count"] = int(result.next_eval_at_count or self._next_eval_node(int(result.message_count_since_last_compaction or 0)))
        state["last_evaluated_count"] = int(result.evaluation_count or 0) if state_name in {"WAIT_NEXT_NODE", "SOFT_APPROVED", "DEFERRED_FOR_STABILITY", "FORCED_PENDING", "COMPACT_NOW"} else int(state.get("last_evaluated_count", 0) or 0)
        state["pending_eval_nodes"] = list(result.pending_eval_nodes or state.get("pending_eval_nodes", []) or [])
        state["queued_eval_up_to_count"] = max(
            int(state.get("queued_eval_up_to_count", 0) or 0),
            max(list(result.pending_eval_nodes or []) or [0]),
        )
        state["last_snapshot"] = {
            "state": str(result.state or ""),
            "reason": str(result.reason or ""),
            "message_count_since_last_compaction": int(result.message_count_since_last_compaction or 0),
            "evaluation_count": int(result.evaluation_count or 0),
            "current_message_count": int(result.current_message_count or 0),
            "final_score": float(result.final_score or 0.0),
            "count_score": float(result.count_score or 0.0),
            "closure_score": float(result.closure_score or 0.0),
            "tail_activity_score": float(result.tail_activity_score or 0.0),
            "topic_density_score": float(result.topic_density_score or 0.0),
            "stability_score": float(result.stability_score or 0.0),
            "benefit_score": float(result.benefit_score or 0.0),
            "is_forced": bool(result.is_forced),
            "is_safe_to_compact": bool(result.is_safe_to_compact),
            "queued_eval_node": int(result.queued_eval_node or 0),
            "pending_eval_nodes_count": int(result.pending_eval_nodes_count or 0),
            "pending_eval_nodes": list(result.pending_eval_nodes or []),
            "force_execute_on_next_safe_hook": bool(result.force_execute_on_next_safe_hook),
            "safe_hook_block_reason": str(result.safe_hook_block_reason or ""),
            "last_hook_source": str(result.last_hook_source or ""),
            "last_safe_hook_checked_at": int(result.last_safe_hook_checked_at or 0),
            "closure_signals": list(result.closure_signals or []),
            "tail_activity_signals": list(result.tail_activity_signals or []),
            "topic_density_signals": list(result.topic_density_signals or []),
            "stability_signals": list(result.stability_signals or []),
            "benefit_signals": list(result.benefit_signals or []),
            "forced_pending_message_delta": int(result.forced_pending_message_delta or 0),
            "last_safe_window_seen_at_count": int(result.last_safe_window_seen_at_count or 0),
            "post_compaction_recovery_rounds": int(result.post_compaction_recovery_rounds or 0),
            "evaluation_count": int(result.evaluation_count or 0),
            "current_message_count": int(result.current_message_count or 0),
            "queued_eval_node": int(result.queued_eval_node or 0),
            "pending_eval_nodes_count": int(result.pending_eval_nodes_count or 0),
            "pending_eval_nodes": list(result.pending_eval_nodes or []),
            "force_execute_on_next_safe_hook": bool(result.force_execute_on_next_safe_hook),
            "safe_hook_block_reason": str(result.safe_hook_block_reason or ""),
            "last_hook_source": str(result.last_hook_source or ""),
            "last_safe_hook_checked_at": int(result.last_safe_hook_checked_at or 0),
        }

    def _snapshot_to_result(self, snapshot: CompactionDecisionSnapshot, *, generation: int, recompact_armed: bool, delta_segments: int, delta_load: float, delta_long: int) -> CompactionResult:
        return CompactionResult(
            chat_id=snapshot.chat_id,
            triggered=snapshot.state == "COMPACT_NOW",
            state=snapshot.state,
            generation=generation,
            eligibility_reason=snapshot.reason,
            recompact_armed=recompact_armed,
            focus_tail_overlap=snapshot.focus_tail_overlap,
            delta_old_segments=delta_segments,
            delta_old_message_load=delta_load,
            delta_old_long_message_count=delta_long,
            message_count_since_last_compaction=snapshot.message_count_since_last_compaction,
            active_segment_count=snapshot.active_segment_count,
            old_segment_count=snapshot.old_segment_count,
            recent_segment_count=snapshot.recent_segment_count,
            next_eval_at_count=snapshot.next_eval_at_count,
            reason=snapshot.reason,
            final_score=snapshot.final_score,
            count_score=snapshot.count_score,
            closure_score=snapshot.closure_score,
            tail_activity_score=snapshot.tail_activity_score,
            topic_density_score=snapshot.topic_density_score,
            stability_score=snapshot.stability_score,
            benefit_score=snapshot.benefit_score,
            is_forced=snapshot.is_forced,
            is_safe_to_compact=snapshot.is_safe_to_compact,
            closure_signals=list(snapshot.closure_signals or []),
            tail_activity_signals=list(snapshot.tail_activity_signals or []),
            topic_density_signals=list(snapshot.topic_density_signals or []),
            stability_signals=list(snapshot.stability_signals or []),
            benefit_signals=list(snapshot.benefit_signals or []),
            forced_pending_message_delta=snapshot.forced_pending_message_delta,
            last_safe_window_seen_at_count=snapshot.last_safe_window_seen_at_count,
            post_compaction_recovery_rounds=snapshot.post_compaction_recovery_rounds,
            evaluation_count=snapshot.evaluation_count,
            current_message_count=snapshot.current_message_count,
            queued_eval_node=snapshot.queued_eval_node,
            pending_eval_nodes_count=snapshot.pending_eval_nodes_count,
            pending_eval_nodes=list(snapshot.pending_eval_nodes or []),
            force_execute_on_next_safe_hook=snapshot.force_execute_on_next_safe_hook,
            safe_hook_block_reason=snapshot.safe_hook_block_reason,
            last_hook_source=snapshot.last_hook_source,
            last_safe_hook_checked_at=snapshot.last_safe_hook_checked_at,
            skipped_reason="" if snapshot.state == "COMPACT_NOW" else snapshot.reason,
        )

    def build_decision_snapshot(
        self,
        chat_id: str,
        snapshot: dict[str, Any],
        focus_context: Any = None,
        *,
        evaluation_count: int | None = None,
        queued_from_node: bool = False,
    ) -> CompactionDecisionSnapshot:
        state = self._state_for_chat(chat_id)
        current_message_count = int(state.get("message_count_since_last_compaction", 0) or 0)
        evaluation_count = int(evaluation_count if evaluation_count is not None else current_message_count or 0)
        active_segment_count = int(snapshot.get("active_segments", 0) or 0)
        old_segment_count = int(snapshot.get("compressible_segments", 0) or 0)
        recent_segment_count = len(list(snapshot.get("recent_segments", []) or []))
        next_eval = self._next_eval_node(evaluation_count)
        pending_eval_nodes = list(state.get("pending_eval_nodes", []) or [])
        if evaluation_count < 80 and current_message_count < 80:
            return CompactionDecisionSnapshot(
                chat_id=chat_id,
                message_count_since_last_compaction=current_message_count,
                active_segment_count=active_segment_count,
                old_segment_count=old_segment_count,
                recent_segment_count=recent_segment_count,
                next_eval_at_count=80,
                state="NOT_READY",
                reason="below_threshold",
                evaluation_count=evaluation_count,
                current_message_count=current_message_count,
                queued_eval_node=evaluation_count if queued_from_node else 0,
                pending_eval_nodes_count=len(pending_eval_nodes),
                pending_eval_nodes=pending_eval_nodes,
                force_execute_on_next_safe_hook=bool(state.get("force_execute_on_next_safe_hook", False)),
                last_hook_source=str(state.get("last_hook_source", "") or ""),
                last_safe_hook_checked_at=int(state.get("last_safe_hook_checked_at", 0) or 0),
                closure_signals=[],
                tail_activity_signals=[],
                topic_density_signals=[],
                stability_signals=[],
                benefit_signals=[],
                last_safe_window_seen_at_count=int(state.get("last_safe_window_seen_at_count", 0) or 0),
                post_compaction_recovery_rounds=int(state.get("post_compaction_recovery_rounds", 0) or 0),
            )
        safe, safe_window_reason, focus_tail_overlap, safe_window_signals = self.detect_safe_window(snapshot, focus_context=focus_context)
        count_score = self._count_score(evaluation_count)
        closure_score, closure_signals = self._closure_analysis_v2(list(snapshot.get("recent_segments", []) or []))
        tail_activity_score, tail_activity_signals = self._tail_activity_analysis_v2(list(snapshot.get("recent_segments", []) or []))
        topic_density_score, topic_density_signals = self._topic_density_analysis_v2(list(snapshot.get("recent_segments", []) or []))
        stability_score, stability_signals = self._stability_analysis_v2(
            safe,
            safe_window_reason,
            focus_tail_overlap,
            safe_window_reason,
            safe_window_signals,
            list(snapshot.get("recent_segments", []) or []),
        )
        benefit_score, benefit_signals = self._benefit_analysis_v2(snapshot)
        final_score = count_score + closure_score + tail_activity_score + topic_density_score + stability_score + benefit_score
        forced_pending_delta = int(state.get("forced_pending_new_messages", 0) or 0) if current_message_count >= 120 else 0
        return CompactionDecisionSnapshot(
            chat_id=chat_id,
            message_count_since_last_compaction=current_message_count,
            active_segment_count=active_segment_count,
            old_segment_count=old_segment_count,
            recent_segment_count=recent_segment_count,
            closure_score=closure_score,
            tail_activity_score=tail_activity_score,
            topic_density_score=topic_density_score,
            stability_score=stability_score,
            benefit_score=benefit_score,
            count_score=count_score,
            final_score=final_score,
            next_eval_at_count=next_eval,
            state="OBSERVING",
            reason=safe_window_reason if not safe else "",
            is_forced=evaluation_count >= 120 or current_message_count >= 120,
            is_safe_to_compact=safe,
            focus_tail_overlap=focus_tail_overlap,
            closure_signals=closure_signals,
            tail_activity_signals=tail_activity_signals,
            topic_density_signals=topic_density_signals,
            stability_signals=stability_signals,
            benefit_signals=benefit_signals,
            forced_pending_message_delta=forced_pending_delta,
            last_safe_window_seen_at_count=int(state.get("last_safe_window_seen_at_count", 0) or 0),
            post_compaction_recovery_rounds=int(state.get("post_compaction_recovery_rounds", 0) or 0),
            evaluation_count=evaluation_count,
            current_message_count=current_message_count,
            queued_eval_node=evaluation_count if queued_from_node else 0,
            pending_eval_nodes_count=len(pending_eval_nodes),
            pending_eval_nodes=pending_eval_nodes,
            force_execute_on_next_safe_hook=bool(state.get("force_execute_on_next_safe_hook", False)),
            safe_hook_block_reason="",
            last_hook_source=str(state.get("last_hook_source", "") or ""),
            last_safe_hook_checked_at=int(state.get("last_safe_hook_checked_at", 0) or 0),
        )

    def advance_decision_state(self, chat_id: str, decision_snapshot: CompactionDecisionSnapshot) -> CompactionDecisionSnapshot:
        state = self._state_for_chat(chat_id)
        decision = decision_snapshot
        message_count = decision.current_message_count or decision.message_count_since_last_compaction
        evaluation_count = int(decision.evaluation_count or message_count or 0)
        current_state = str(state.get("current_state", "NOT_READY") or "NOT_READY")
        cooldown_until = float(state.get("cooldown_until", 0.0) or 0.0)
        if monotonic() < cooldown_until:
            decision.state = "COOLDOWN"
            decision.reason = "cooldown"
            return decision
        if decision.state == "NOT_READY":
            return decision
        if decision.is_forced:
            force_ready = bool(state.get("force_execute_on_next_safe_hook", False))
            if decision.is_safe_to_compact:
                decision.state = "COMPACT_NOW"
                decision.reason = "forced_safe_window_ready" if force_ready else "forced_safe_window"
                return decision
            forced_since = int(state.get("forced_pending_since_count", 0) or 120)
            pending_delta = max(0, message_count - forced_since)
            decision.forced_pending_message_delta = pending_delta
            if force_ready or pending_delta >= 20:
                decision.state = "FORCED_PENDING"
                decision.reason = "forced_waiting_for_safe_hook"
                decision.force_execute_on_next_safe_hook = True
                decision.safe_hook_block_reason = decision.reason
                decision.next_eval_at_count = 120
                return decision
            decision.state = "FORCED_PENDING"
            decision.reason = decision.reason or "forced_pending"
            decision.safe_hook_block_reason = decision.reason
            decision.next_eval_at_count = 120
            return decision
        eval_nodes = {80, 90, 100, 110}
        if evaluation_count not in eval_nodes:
            if current_state == "DEFERRED_FOR_STABILITY":
                if decision.is_safe_to_compact:
                    decision.state = "COMPACT_NOW"
                    decision.reason = "safe_window_reopened"
                    return decision
                decision.state = "DEFERRED_FOR_STABILITY"
                decision.reason = decision.reason or "awaiting_safety_window"
            elif current_state == "WAIT_NEXT_NODE":
                decision.state = "WAIT_NEXT_NODE"
                decision.reason = "awaiting_next_node"
            else:
                decision.state = "OBSERVING"
                decision.reason = "observing"
            return decision
        if decision.final_score >= 75.0:
            decision.state = "SOFT_APPROVED"
            if decision.is_safe_to_compact:
                decision.state = "COMPACT_NOW"
                decision.reason = "soft_approved"
            else:
                decision.state = "DEFERRED_FOR_STABILITY"
                decision.reason = decision.reason or "awaiting_safety_window"
            return decision
        decision.state = "WAIT_NEXT_NODE"
        decision.reason = "score_below_approval_threshold" if decision.final_score < 60.0 else "score_hold_for_next_node"
        return decision

    def evaluate_compaction_eligibility(
        self,
        chat_id: str,
        snapshot: dict[str, Any],
        focus_context: Any = None,
        *,
        evaluation_count: int | None = None,
        queued_from_node: bool = False,
        record_state: bool = True,
    ) -> CompactionResult:
        state = self._state_for_chat(chat_id)
        decision = self.build_decision_snapshot(
            chat_id,
            snapshot,
            focus_context=focus_context,
            evaluation_count=evaluation_count,
            queued_from_node=queued_from_node,
        )
        decision = self.advance_decision_state(chat_id, decision)
        delta_segments, delta_load, delta_long = self._compute_recompact_delta(snapshot, state)
        recompact_armed = self._is_recompact_armed(delta_segments, delta_load, delta_long, state)
        result = self._snapshot_to_result(
            decision,
            generation=int(state.get("generation", 0) or 0),
            recompact_armed=recompact_armed,
            delta_segments=delta_segments,
            delta_load=delta_load,
            delta_long=delta_long,
        )
        if record_state:
            self._record_latest_status(chat_id, result, state_name=result.state)
        return result

    async def get_trace_status(self, chat_id: str, focus_context: Any = None) -> dict[str, Any]:
        state = self._state_for_chat(chat_id)
        result = CompactionResult(
            chat_id=chat_id,
            generation=int(state.get("generation", 0) or 0),
            eligibility_reason=str(state.get("last_eligibility_reason", "") or ""),
            recompact_armed=bool(state.get("last_recompact_armed", False)),
            focus_tail_overlap=bool(state.get("last_focus_tail_overlap", False)),
            delta_old_segments=int(state.get("last_delta_old_segments", 0) or 0),
            delta_old_message_load=float(state.get("last_delta_old_message_load", 0.0) or 0.0),
            delta_old_long_message_count=int(state.get("last_delta_old_long_message_count", 0) or 0),
            state=str(state.get("last_state", "NOT_READY") or "NOT_READY"),
            message_count_since_last_compaction=int(state.get("message_count_since_last_compaction", 0) or 0),
            next_eval_at_count=int(state.get("next_eval_at_count", 80) or 80),
            closure_signals=list(state.get("last_closure_signals", []) or []),
            tail_activity_signals=list(state.get("last_tail_activity_signals", []) or []),
            topic_density_signals=list(state.get("last_topic_density_signals", []) or []),
            stability_signals=list(state.get("last_stability_signals", []) or []),
            benefit_signals=list(state.get("last_benefit_signals", []) or []),
            forced_pending_message_delta=int(state.get("forced_pending_new_messages", 0) or 0),
            last_safe_window_seen_at_count=int(state.get("last_safe_window_seen_at_count", 0) or 0),
            post_compaction_recovery_rounds=int(state.get("post_compaction_recovery_rounds", 0) or 0),
            evaluation_count=int(state.get("last_evaluated_count", 0) or 0),
            current_message_count=int(state.get("message_count_since_last_compaction", 0) or 0),
            queued_eval_node=0,
            pending_eval_nodes_count=len(list(state.get("pending_eval_nodes", []) or [])),
            pending_eval_nodes=list(state.get("pending_eval_nodes", []) or []),
            force_execute_on_next_safe_hook=bool(state.get("force_execute_on_next_safe_hook", False)),
            safe_hook_block_reason=str(state.get("safe_hook_block_reason", "") or ""),
            last_hook_source=str(state.get("last_hook_source", "") or ""),
            last_safe_hook_checked_at=int(state.get("last_safe_hook_checked_at", 0) or 0),
        )
        if self.dialogue_store is None:
            return self._result_to_dict(result)
        try:
            snapshot = await self.dialogue_store.snapshot_compaction_candidates(
                chat_id,
                keep_recent_segments=self.compaction_keep_recent_segments,
            )
            self._bootstrap_message_counter(chat_id, snapshot)
            result = self.evaluate_compaction_eligibility(
                chat_id,
                snapshot,
                focus_context=focus_context,
                record_state=False,
            )
            result.state = str(state.get("last_state", result.state) or result.state)
        except Exception:
            logger.debug(f"[Compaction] get_trace_status snapshot failed for {chat_id}", exc_info=True)
            pass
        return self._result_to_dict(result)

    async def maybe_compact(self, chat_id: str, focus_context: Any = None) -> CompactionResult:
        if not self.dialogue_store:
            return CompactionResult(chat_id=chat_id, skipped_reason="store_unavailable", state="NOT_READY")
        # ponytail: prune stale cooldown entries (>1h old) to prevent unbounded growth
        prune_cutoff = monotonic() - 3600.0
        stale_chats = [c for c, t in self._cooldown_by_chat.items() if t < prune_cutoff]
        for c in stale_chats:
            del self._cooldown_by_chat[c]
        state = self._state_for_chat(chat_id)
        now = monotonic()
        # ponytail: token-based compaction trigger removed — no token_estimator wired.
        # Message-count based trigger is the sole compaction gate.
        state["cooldown_until"] = float(self._cooldown_by_chat.get(chat_id, 0.0) or state.get("cooldown_until", 0.0) or 0.0)
        if int(state.get("message_count_since_last_compaction", 0) or 0) <= 0:
            try:
                bootstrap_snapshot = await self.dialogue_store.snapshot_compaction_candidates(
                    chat_id,
                    keep_recent_segments=self.compaction_keep_recent_segments,
                )
                self._bootstrap_message_counter(chat_id, bootstrap_snapshot)
            except Exception:
                logger.debug(f"[Compaction] maybe_compact bootstrap failed for {chat_id}", exc_info=True)
                bootstrap_snapshot = None
        if now < float(state.get("cooldown_until", 0.0) or 0.0):
            result = CompactionResult(
                chat_id=chat_id,
                skipped_reason="cooldown",
                state="COOLDOWN",
                generation=int(state.get("generation", 0) or 0),
                message_count_since_last_compaction=int(state.get("message_count_since_last_compaction", 0) or 0),
                next_eval_at_count=int(state.get("next_eval_at_count", 80) or 80),
                reason="cooldown",
            )
            self._record_latest_status(chat_id, result, state_name="COOLDOWN")
            return result
        try:
            snapshot = await self.dialogue_store.snapshot_compaction_candidates(
                chat_id,
                keep_recent_segments=self.compaction_keep_recent_segments,
            )
        except Exception as exc:
            logger.debug(f"[{chat_id}] compaction stats failed: {exc}")
            result = CompactionResult(chat_id=chat_id, skipped_reason="stats_failed", state="NOT_READY", reason="stats_failed")
            self._record_latest_status(chat_id, result, state_name="NOT_READY")
            return result
        self._bootstrap_message_counter(chat_id, snapshot)
        eligibility = None
        pending_nodes = list(state.get("pending_eval_nodes", []) or [])
        if pending_nodes:
            while pending_nodes:
                eval_node = int(pending_nodes[0] or 0)
                candidate = self.evaluate_compaction_eligibility(
                    chat_id,
                    snapshot,
                    focus_context=focus_context,
                    evaluation_count=eval_node,
                    queued_from_node=True,
                    record_state=False,
                )
                self._pop_pending_eval_node(state, eval_node)
                candidate.pending_eval_nodes = list(state.get("pending_eval_nodes", []) or [])
                candidate.pending_eval_nodes_count = len(candidate.pending_eval_nodes)
                candidate.queued_eval_node = eval_node
                candidate.current_message_count = int(state.get("message_count_since_last_compaction", 0) or 0)
                self._record_latest_status(chat_id, candidate, state_name=candidate.state)
                eligibility = candidate
                if candidate.triggered or candidate.state in {"DEFERRED_FOR_STABILITY", "FORCED_PENDING"}:
                    break
                pending_nodes = list(state.get("pending_eval_nodes", []) or [])
        else:
            eligibility = self.evaluate_compaction_eligibility(
                chat_id,
                snapshot,
                focus_context=focus_context,
                record_state=False,
            )
            eligibility.pending_eval_nodes = list(state.get("pending_eval_nodes", []) or [])
            eligibility.pending_eval_nodes_count = len(eligibility.pending_eval_nodes)
            eligibility.current_message_count = int(state.get("message_count_since_last_compaction", 0) or 0)
            self._record_latest_status(chat_id, eligibility, state_name=eligibility.state)
        if eligibility is None:
            eligibility = CompactionResult(chat_id=chat_id, state="NOT_READY", reason="not_ready")
        if not eligibility.triggered:
            return eligibility

        try:
            drained = await self.dialogue_store.peek_old_segments(
                chat_id,
                keep_recent_segments=self.compaction_keep_recent_segments,
            )
        except Exception as exc:
            logger.debug(f"[{chat_id}] compaction drain failed: {exc}")
            result = CompactionResult(
                chat_id=chat_id,
                skipped_reason="drain_failed",
                state="OBSERVING",
                generation=eligibility.generation,
                recompact_armed=eligibility.recompact_armed,
                focus_tail_overlap=eligibility.focus_tail_overlap,
                delta_old_segments=eligibility.delta_old_segments,
                delta_old_message_load=eligibility.delta_old_message_load,
                delta_old_long_message_count=eligibility.delta_old_long_message_count,
                message_count_since_last_compaction=eligibility.message_count_since_last_compaction,
                next_eval_at_count=eligibility.next_eval_at_count,
                reason="drain_failed",
            )
            self._record_latest_status(chat_id, result, state_name="OBSERVING")
            return result

        if not drained:
            result = CompactionResult(
                chat_id=chat_id,
                skipped_reason="nothing_to_compact",
                state="WAIT_NEXT_NODE",
                generation=eligibility.generation,
                recompact_armed=eligibility.recompact_armed,
                focus_tail_overlap=eligibility.focus_tail_overlap,
                delta_old_segments=eligibility.delta_old_segments,
                delta_old_message_load=eligibility.delta_old_message_load,
                delta_old_long_message_count=eligibility.delta_old_long_message_count,
                message_count_since_last_compaction=eligibility.message_count_since_last_compaction,
                next_eval_at_count=eligibility.next_eval_at_count,
                reason="nothing_to_compact",
            )
            self._record_latest_status(chat_id, result, state_name="WAIT_NEXT_NODE")
            return result

        summary = await self._build_summary_with_provider_v2(chat_id, drained)
        if not summary:
            summary = self._build_summary_v2(drained)
        if not summary:
            self._cooldown_by_chat[chat_id] = now + self._failure_cooldown_seconds
            state["cooldown_until"] = self._cooldown_by_chat[chat_id]
            result = CompactionResult(
                chat_id=chat_id,
                drained_segments=len(drained),
                skipped_reason="summary_empty",
                state="COOLDOWN",
                generation=eligibility.generation,
                recompact_armed=eligibility.recompact_armed,
                focus_tail_overlap=eligibility.focus_tail_overlap,
                delta_old_segments=eligibility.delta_old_segments,
                delta_old_message_load=eligibility.delta_old_message_load,
                delta_old_long_message_count=eligibility.delta_old_long_message_count,
                message_count_since_last_compaction=eligibility.message_count_since_last_compaction,
                next_eval_at_count=eligibility.next_eval_at_count,
                reason="summary_empty",
            )
            self._record_latest_status(chat_id, result, state_name="COOLDOWN")
            return result

        try:
            current = await self.dialogue_store.get_cold_summary(chat_id)
            current_structure = None
            if hasattr(self.dialogue_store, "get_cold_summary_structure"):
                current_structure = await self.dialogue_store.get_cold_summary_structure(chat_id)
            if current_structure is None:
                current_structure = self._structure_from_summary_text(current)
            addition_structure = self._structure_from_summary_text(summary)
            merged_structure = self._merge_cold_structure(current_structure, addition_structure)
            combined = self._render_cold_summary(merged_structure)
            if not combined:
                self._cooldown_by_chat[chat_id] = now + self._failure_cooldown_seconds
                result = CompactionResult(
                    chat_id=chat_id,
                    drained_segments=len(drained),
                    skipped_reason="summary_merge_empty",
                    state="COOLDOWN",
                    generation=eligibility.generation,
                    recompact_armed=eligibility.recompact_armed,
                    focus_tail_overlap=eligibility.focus_tail_overlap,
                    delta_old_segments=eligibility.delta_old_segments,
                    delta_old_message_load=eligibility.delta_old_message_load,
                    delta_old_long_message_count=eligibility.delta_old_long_message_count,
                    message_count_since_last_compaction=eligibility.message_count_since_last_compaction,
                    next_eval_at_count=eligibility.next_eval_at_count,
                    reason="summary_merge_empty",
                )
                state["cooldown_until"] = self._cooldown_by_chat[chat_id]
                self._record_latest_status(chat_id, result, state_name="COOLDOWN")
                return result
            await self.dialogue_store.set_cold_summary(chat_id, combined)
            if hasattr(self.dialogue_store, "set_cold_summary_structure"):
                await self.dialogue_store.set_cold_summary_structure(chat_id, merged_structure)
            drained = await self.dialogue_store.commit_drain_old_segments(
                chat_id,
                keep_recent_segments=self.compaction_keep_recent_segments,
            )
        except Exception as exc:
            logger.debug(f"[{chat_id}] compaction summary save failed: {exc}")
            self._cooldown_by_chat[chat_id] = now + self._failure_cooldown_seconds
            result = CompactionResult(
                chat_id=chat_id,
                drained_segments=len(drained),
                skipped_reason="summary_save_failed",
                state="COOLDOWN",
                generation=eligibility.generation,
                recompact_armed=eligibility.recompact_armed,
                focus_tail_overlap=eligibility.focus_tail_overlap,
                delta_old_segments=eligibility.delta_old_segments,
                delta_old_message_load=eligibility.delta_old_message_load,
                delta_old_long_message_count=eligibility.delta_old_long_message_count,
                message_count_since_last_compaction=eligibility.message_count_since_last_compaction,
                next_eval_at_count=eligibility.next_eval_at_count,
                reason="summary_save_failed",
            )
            state["cooldown_until"] = self._cooldown_by_chat[chat_id]
            self._record_latest_status(chat_id, result, state_name="COOLDOWN")
            return result

        state = self._state_for_chat(chat_id)
        state["generation"] = int(state.get("generation", 0) or 0) + 1
        state["message_count_since_last_compaction"] = 0
        state["last_evaluated_count"] = 0
        state["next_eval_at_count"] = 80
        state["forced_pending_since_count"] = 0
        state["forced_pending_new_messages"] = 0
        state["pending_eval_nodes"] = []
        state["queued_eval_up_to_count"] = 0
        state["force_execute_on_next_safe_hook"] = False
        state["safe_hook_block_reason"] = ""
        state["last_compacted_tail_event_id"] = str(snapshot.get("tail_event_id", "") or "")
        state["last_compacted_old_segment_count"] = int(snapshot.get("compressible_segments", 0) or 0)
        state["last_compacted_old_message_load"] = float(snapshot.get("compressible_message_load", 0.0) or 0.0)
        state["last_compacted_long_message_count"] = int(snapshot.get("compressible_long_message_count", 0) or 0)
        state["last_cold_summary_hash"] = hash(combined)
        state["last_retained_event_ids"] = [
            str(getattr(segment, "event_id", "") or "")
            for segment in list(snapshot.get("recent_segments", []) or [])
            if str(getattr(segment, "event_id", "") or "")
        ]
        state["last_retained_segment_count"] = len(list(snapshot.get("recent_segments", []) or []))
        state["last_safe_window_seen_at_count"] = int(eligibility.message_count_since_last_compaction or 0)
        state["post_compaction_recovery_rounds"] = 2
        self._cooldown_by_chat[chat_id] = now + self._success_cooldown_seconds
        state["cooldown_until"] = self._cooldown_by_chat[chat_id]
        result = CompactionResult(
            chat_id=chat_id,
            triggered=True,
            drained_segments=len(drained),
            summary=combined,
            state="COOLDOWN",
            generation=int(state.get("generation", 0) or 0),
            eligibility_reason=eligibility.eligibility_reason,
            recompact_armed=True,
            focus_tail_overlap=eligibility.focus_tail_overlap,
            delta_old_segments=eligibility.delta_old_segments,
            delta_old_message_load=eligibility.delta_old_message_load,
            delta_old_long_message_count=eligibility.delta_old_long_message_count,
            message_count_since_last_compaction=0,
            next_eval_at_count=80,
            reason=eligibility.reason or "compacted",
            final_score=eligibility.final_score,
            count_score=eligibility.count_score,
            closure_score=eligibility.closure_score,
            tail_activity_score=eligibility.tail_activity_score,
            topic_density_score=eligibility.topic_density_score,
            stability_score=eligibility.stability_score,
            benefit_score=eligibility.benefit_score,
            is_forced=eligibility.is_forced,
            is_safe_to_compact=eligibility.is_safe_to_compact,
            closure_signals=list(eligibility.closure_signals or []),
            tail_activity_signals=list(eligibility.tail_activity_signals or []),
            topic_density_signals=list(eligibility.topic_density_signals or []),
            stability_signals=list(eligibility.stability_signals or []),
            benefit_signals=list(eligibility.benefit_signals or []),
            forced_pending_message_delta=0,
            last_safe_window_seen_at_count=int(eligibility.message_count_since_last_compaction or 0),
            post_compaction_recovery_rounds=int(state.get("post_compaction_recovery_rounds", 0) or 0),
            evaluation_count=int(eligibility.evaluation_count or 0),
            current_message_count=0,
            queued_eval_node=int(eligibility.queued_eval_node or 0),
            pending_eval_nodes_count=0,
            pending_eval_nodes=[],
            force_execute_on_next_safe_hook=False,
            safe_hook_block_reason="",
            last_hook_source=str(state.get("last_hook_source", "") or ""),
            last_safe_hook_checked_at=int(state.get("last_safe_hook_checked_at", 0) or 0),
        )
        self._record_latest_status(chat_id, result, state_name="COOLDOWN")
        return result

    def _parse_cold_summary_text(self, text: str) -> ColdSummaryStructure:
        cleaned = str(text or "").strip()
        structure = self._empty_cold_structure()
        if not cleaned:
            return structure
        current_section = "topics"
        for raw_line in cleaned.splitlines():
            line = str(raw_line or "").strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section_name = line[1:-1].strip()
                if section_name in SECTION_ORDER:
                    current_section = section_name
                continue
            if line.startswith("- "):
                line = line[2:].strip()
            self._append_to_structure(structure, current_section, line, [])
        return structure

    def _append_to_structure(self, structure: ColdSummaryStructure, section: str, text: str, event_ids: list[str] | None = None) -> None:
        mapping = {
            "topics": structure.topics,
            "decisions": structure.decisions,
            "open_items": structure.open_items,
            "relationship_changes": structure.relationship_changes,
            "emotional_turns": structure.emotional_turns,
            "visual_notes": structure.visual_notes,
            "long_term_constraints": structure.long_term_constraints,
        }
        target = mapping.get(section)
        if target is None:
            target = structure.topics
        self._append_unit(target, section, text, event_ids)

    def _structure_from_segments(self, drained_segments) -> ColdSummaryStructure:
        structure = self._empty_cold_structure()
        if not drained_segments:
            return structure
        recent = list(drained_segments[-8:])
        latest = recent[-1]
        if any(getattr(segment, "is_at_bot", False) or getattr(segment, "is_reply_to_bot", False) for segment in recent):
            self._append_to_structure(
                structure,
                "topics",
                f"这段旧对话主要围绕 bot 直接参与的那条主线展开，后段落点是“{self._preview_text(getattr(latest, 'content', ''), 32)}”。",
                [getattr(segment, "event_id", "") for segment in recent if getattr(segment, "event_id", "")],
            )
        elif any(getattr(segment, "has_direct_vision", False) or getattr(segment, "is_image_only", False) or getattr(segment, "message_kind", "") in {"image", "mixed"} for segment in recent):
            self._append_to_structure(
                structure,
                "topics",
                f"这段旧对话主要在围绕图片/图文线索推进，后段聚焦在“{self._preview_text(getattr(latest, 'content', ''), 32)}”。",
                [getattr(segment, "event_id", "") for segment in recent if getattr(segment, "event_id", "")],
            )
        else:
            self._append_to_structure(
                structure,
                "topics",
                f"这段旧对话主要延续同一个话题，后段落在“{self._preview_text(getattr(latest, 'content', ''), 32)}”。",
                [getattr(latest, "event_id", "")] if getattr(latest, "event_id", "") else [],
            )

        for segment in drained_segments:
            content = self._normalize_summary_text(getattr(segment, "content", ""))
            if not content:
                continue
            event_ids = [getattr(segment, "event_id", "")] if getattr(segment, "event_id", "") else []
            speaker = getattr(segment, "speaker_name", "") or getattr(segment, "speaker_id", "") or ("Bot" if getattr(segment, "is_bot", False) else "User")
            if self._is_decision_like(content):
                self._append_to_structure(structure, "decisions", f"{speaker} 明确表态“{self._preview_text(content, 36)}”。", event_ids)
            elif self._is_question_like(content):
                self._append_to_structure(structure, "open_items", f"仍待回答的问题集中在“{self._preview_text(content, 36)}”。", event_ids)
            if self._is_constraint_like(content):
                self._append_to_structure(structure, "long_term_constraints", f"讨论里形成了约束或约定：“{self._preview_text(content, 40)}”。", event_ids)
            if getattr(segment, "has_direct_vision", False) or getattr(segment, "is_image_only", False) or getattr(segment, "message_kind", "") in {"image", "mixed"}:
                self._append_to_structure(structure, "visual_notes", f"出现过图像线索，相关内容是“{self._preview_text(content, 36)}”。", event_ids)

        relation_text = self._detect_relation_state(drained_segments)
        if relation_text:
            self._append_to_structure(structure, "relationship_changes", relation_text, [])
        emotion_text = self._detect_emotion_state(drained_segments)
        if emotion_text:
            self._append_to_structure(structure, "emotional_turns", emotion_text, [])
        return structure

    def _structure_from_summary_text(self, text: str) -> ColdSummaryStructure:
        cleaned = str(text or "").strip()
        if not cleaned:
            return self._empty_cold_structure()
        parsed = self._parse_cold_summary_text(cleaned)
        if any(parsed.section_counts().values()):
            return parsed
        structure = self._empty_cold_structure()
        for line in cleaned.splitlines():
            normalized = self._normalize_summary_text(line.lstrip("- ").strip())
            if not normalized:
                continue
            if self._is_decision_like(normalized):
                self._append_to_structure(structure, "decisions", normalized, [])
            elif self._is_question_like(normalized):
                self._append_to_structure(structure, "open_items", normalized, [])
            else:
                self._append_to_structure(structure, "topics", normalized, [])
        return structure

    @staticmethod
    def _merge_section(existing: list[TopicUnit], additions: list[TopicUnit], *, limit: int = 6) -> list[TopicUnit]:
        merged: list[TopicUnit] = []
        seen: list[str] = []
        for unit in [*existing, *additions]:
            text = " ".join(str(unit.text or "").split()).strip()
            if not text:
                continue
            lowered = text.lower()
            if any(lowered == item or lowered in item or item in lowered for item in seen):
                continue
            merged.append(TopicUnit(slot=unit.slot, text=text, event_ids=list(unit.event_ids or [])))
            seen.append(lowered)
            if len(merged) >= limit:
                break
        return merged

    @classmethod
    def _semantic_tokens(cls, text: str) -> set[str]:
        normalized = cls._normalize_summary_text(text).lower()
        if not normalized:
            return set()
        return {
            token
            for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{2,}", normalized)
            if len(token) >= 2
        }

    @classmethod
    def _decision_resolves_open_item(cls, open_unit: TopicUnit, decision_units: list[TopicUnit]) -> bool:
        open_tokens = cls._semantic_tokens(open_unit.text)
        if not open_tokens:
            return False
        for decision_unit in decision_units:
            decision_tokens = cls._semantic_tokens(decision_unit.text)
            if len(open_tokens & decision_tokens) >= 3:
                return True
        return False

    def _merge_cold_structure(self, current: ColdSummaryStructure, addition: ColdSummaryStructure) -> ColdSummaryStructure:
        merged = self._empty_cold_structure()
        merged.topics = self._merge_section(current.topics, addition.topics, limit=6)
        merged.decisions = self._merge_section(current.decisions, addition.decisions, limit=6)
        open_items = self._merge_section(current.open_items, addition.open_items, limit=8)
        merged.open_items = [
            unit
            for unit in open_items
            if not self._decision_resolves_open_item(unit, merged.decisions)
        ]
        merged.relationship_changes = self._merge_section(current.relationship_changes, addition.relationship_changes, limit=4)
        merged.emotional_turns = self._merge_section(current.emotional_turns, addition.emotional_turns, limit=4)
        merged.visual_notes = self._merge_section(current.visual_notes, addition.visual_notes, limit=4)
        merged.long_term_constraints = self._merge_section(current.long_term_constraints, addition.long_term_constraints, limit=6)
        return merged

    def _render_cold_summary(self, structure: ColdSummaryStructure) -> str:
        priority_order = ["open_items", "decisions", "long_term_constraints", "topics", "relationship_changes", "emotional_turns", "visual_notes"]
        section_map = {
            "topics": structure.topics,
            "decisions": structure.decisions,
            "open_items": structure.open_items,
            "relationship_changes": structure.relationship_changes,
            "emotional_turns": structure.emotional_turns,
            "visual_notes": structure.visual_notes,
            "long_term_constraints": structure.long_term_constraints,
        }
        selected: dict[str, list[str]] = {key: [] for key in SECTION_ORDER}
        budget = 0
        for section in priority_order:
            for unit in section_map[section]:
                line = f"- {self._normalize_summary_text(unit.text)}"
                if not line.strip("- ").strip():
                    continue
                estimate = self._estimate_tokens(line)
                if self.compaction_summary_max_tokens > 0 and budget + estimate > self.compaction_summary_max_tokens:
                    continue
                selected[section].append(line)
                budget += estimate
        rendered_lines: list[str] = []
        for section in SECTION_ORDER:
            lines = selected.get(section, [])
            if not lines:
                continue
            rendered_lines.append(f"[{section}]")
            rendered_lines.extend(lines)
        return "\n".join(rendered_lines).strip()

    def _merge_cold_summary(self, current: str, addition: str) -> str:
        current_structure = self._structure_from_summary_text(current)
        addition_structure = self._structure_from_summary_text(addition)
        merged_structure = self._merge_cold_structure(current_structure, addition_structure)
        return self._render_cold_summary(merged_structure)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        cleaned = str(text or "").strip()
        if not cleaned:
            return 0
        return max(1, len(cleaned) // 2)

    def _clip_summary(self, summary: str) -> str:
        cleaned = str(summary or "").strip()
        if not cleaned:
            return ""
        if self.compaction_summary_max_tokens <= 0:
            return cleaned
        lines: list[str] = []
        budget = 0
        for line in cleaned.splitlines():
            normalized = str(line or "").strip()
            if not normalized:
                continue
            estimate = self._estimate_tokens(normalized)
            if budget + estimate > self.compaction_summary_max_tokens:
                break
            lines.append(normalized)
            budget += estimate
        return "\n".join(lines).strip()

    @staticmethod
    def _segment_to_summary_line(segment) -> str:
        speaker = segment.speaker_name or segment.speaker_id or ("Bot" if segment.is_bot else "User")
        text = str(segment.content or "").strip()
        if not text:
            return ""
        return f"- {speaker}: {text}"

    def _build_summary(self, drained_segments) -> str:
        lines: list[str] = []
        token_budget = 0
        for segment in drained_segments:
            line = self._segment_to_summary_line(segment)
            if not line:
                continue
            estimate = self._estimate_tokens(line)
            if self.compaction_summary_max_tokens > 0 and token_budget + estimate > self.compaction_summary_max_tokens:
                break
            lines.append(line)
            token_budget += estimate
        if not lines:
            return ""
        return "近期对话压缩摘要：\n" + "\n".join(lines)
    def _build_summary_v2(self, drained_segments) -> str:
        structure = self._structure_from_segments(drained_segments)
        return self._render_cold_summary(structure)

    @staticmethod
    def _result_to_dict(result: CompactionResult) -> dict[str, Any]:
        return {
            "chat_id": result.chat_id,
            "triggered": bool(result.triggered),
            "drained_segments": int(result.drained_segments or 0),
            "summary": str(result.summary or ""),
            "skipped_reason": str(result.skipped_reason or ""),
            "state": str(result.state or ""),
            "generation": int(result.generation or 0),
            "eligibility_reason": str(result.eligibility_reason or ""),
            "recompact_armed": bool(result.recompact_armed),
            "focus_tail_overlap": bool(result.focus_tail_overlap),
            "delta_old_segments": int(result.delta_old_segments or 0),
            "delta_old_message_load": float(result.delta_old_message_load or 0.0),
            "delta_old_long_message_count": int(result.delta_old_long_message_count or 0),
            "message_count_since_last_compaction": int(result.message_count_since_last_compaction or 0),
            "active_segment_count": int(result.active_segment_count or 0),
            "old_segment_count": int(result.old_segment_count or 0),
            "recent_segment_count": int(result.recent_segment_count or 0),
            "next_eval_at_count": int(result.next_eval_at_count or 80),
            "reason": str(result.reason or ""),
            "final_score": float(result.final_score or 0.0),
            "count_score": float(result.count_score or 0.0),
            "closure_score": float(result.closure_score or 0.0),
            "tail_activity_score": float(result.tail_activity_score or 0.0),
            "topic_density_score": float(result.topic_density_score or 0.0),
            "stability_score": float(result.stability_score or 0.0),
            "benefit_score": float(result.benefit_score or 0.0),
            "is_forced": bool(result.is_forced),
            "is_safe_to_compact": bool(result.is_safe_to_compact),
            "closure_signals": list(result.closure_signals or []),
            "tail_activity_signals": list(result.tail_activity_signals or []),
            "topic_density_signals": list(result.topic_density_signals or []),
            "stability_signals": list(result.stability_signals or []),
            "benefit_signals": list(result.benefit_signals or []),
            "forced_pending_message_delta": int(result.forced_pending_message_delta or 0),
            "last_safe_window_seen_at_count": int(result.last_safe_window_seen_at_count or 0),
            "post_compaction_recovery_rounds": int(result.post_compaction_recovery_rounds or 0),
            "evaluation_count": int(result.evaluation_count or 0),
            "current_message_count": int(result.current_message_count or 0),
            "queued_eval_node": int(result.queued_eval_node or 0),
            "pending_eval_nodes_count": int(result.pending_eval_nodes_count or 0),
            "pending_eval_nodes": list(result.pending_eval_nodes or []),
            "force_execute_on_next_safe_hook": bool(result.force_execute_on_next_safe_hook),
            "safe_hook_block_reason": str(result.safe_hook_block_reason or ""),
            "last_hook_source": str(result.last_hook_source or ""),
            "last_safe_hook_checked_at": int(result.last_safe_hook_checked_at or 0),
        }
