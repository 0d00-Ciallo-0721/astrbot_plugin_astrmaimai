from __future__ import annotations

import asyncio
import collections
import hashlib
import inspect
import re
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Dict, List, Set

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp

from ..contracts.conversation_event import ConversationEvent
from ..contracts.dialog_history_policy import DialogHistoryPolicy
from ..contracts.turn_context import ensure_turn_context
from ..contracts.turn_identity import TurnIdentity, build_p0_thread_id
from ..threading.group_thread_resolver import resolve_group_thread
from ...infrastructure.compat.legacy_compat import emit_legacy_focus_thread_extras
from ...infrastructure.gateway.output_guard import validate_visible_output_text
from ...infrastructure.runtime.background_task_budget import (
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ...infrastructure.runtime.trace_runtime import debug_trace, new_trace_id, preview_text
from ...infrastructure.runtime.outbound_send_guard import outbound_send_allowed
from ...infrastructure.persistence.attention_deferred_outbox import AttentionDeferredOutboxStore
from ...infrastructure.runtime.turn_call_ledger import (
    attach_background_task_trace,
    begin_stage,
    clamp_timeout_to_turn_budget,
    finish_stage,
    rebind_turn_telemetry,
)
from ...shared.helpers.plugin_helpers import event_mentions_actor, get_event_self_id
from ..planning.message_renderer import MessageRenderer
from .topic_identity import resolve_attention_topic_identity
from .decision_router import AttentionDecisionRouter
from .event_normalizer import SessionContext, build_normalized_events
from .focus_selector import score_focus_candidate, select_focus_event
from .group_context_snapshot import classify_group_social_signal, is_group_direct_correction
from .perception import PerceptionBuilder
from .thread_builder import build_focus_thread, resolve_thread_root
from .vision_binding import extract_image_base64, extract_image_base64_from_url
from .window_buffer import AttentionWindowBuffer
from ...proactive.dispatcher import append_proactive_stage


class _SyntheticExternalEvent(AstrMessageEvent):
    def __init__(self, data: dict[str, Any]):
        self._data = dict(data or {})
        self._extra = dict(self._data.get("extra", {}) or {})
        if "is_external_bot_reply" in self._data and "is_external_bot_reply" not in self._extra:
            self._extra["is_external_bot_reply"] = bool(self._data.get("is_external_bot_reply"))
        self.message_str = str(self._data.get("message_str", self._data.get("content", "")) or "")
        self.timestamp = float(self._data.get("timestamp", time.time()) or time.time())
        self.unified_msg_origin = str(self._data.get("unified_msg_origin", "") or "")
        self.message_obj = self._data.get("message_obj")  # reserved for future use

    def get_sender_id(self):
        return str(self._data.get("sender_id", "") or "")

    def get_sender_name(self):
        return str(self._data.get("sender_name", "") or "")

    def get_group_id(self):
        return str(self._data.get("group_id", "") or "")

    def get_self_id(self):
        return str(self._data.get("self_id", "") or "")

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class AttentionGate:
    @staticmethod
    def _proactive_activity_snapshot(state: Any) -> dict[str, float | int | str]:
        if state is None:
            return {}
        return {
            "last_real_user_activity_at": float(
                getattr(state, "last_real_user_activity_at", 0.0) or 0.0
            ),
            "next_proactive_due_at": float(
                getattr(state, "next_proactive_due_at", 0.0) or 0.0
            ),
            "unanswered_proactive_count": int(
                getattr(state, "unanswered_proactive_count", 0) or 0
            ),
            "proactive_generation": int(getattr(state, "proactive_generation", 0) or 0),
            "last_proactive_cancel_reason": str(
                getattr(state, "last_proactive_cancel_reason", "") or ""
            ),
            "proactive_claim_token": str(getattr(state, "proactive_claim_token", "") or ""),
            "proactive_claimed_at": float(getattr(state, "proactive_claimed_at", 0.0) or 0.0),
            "next_wakeup_timestamp": float(getattr(state, "next_wakeup_timestamp", 0.0) or 0.0),
            "last_reply_time": float(getattr(state, "last_reply_time", 0.0) or 0.0),
            "chat_kind": str(getattr(state, "chat_kind", "") or ""),
        }

    ATTENTION_WINDOW_TTL_SECONDS = 180.0
    ATTENTION_WINDOW_MAX_EVENTS = 12
    BACKGROUND_TASK_MAX_CONCURRENCY = 8
    MESSAGE_DEDUP_FALLBACK_TTL_SECONDS = 2.0

    def __init__(
        self,
        state_engine,
        judge,
        sensors,
        system2_callback,
        config=None,
        visual_cortex=None,
        persona_summarizer=None,
        frequency_controller=None,
        private_chat_manager=None,
        private_turn_coordinator=None,
        runtime_coordinator=None,
        chat_loop_kernel=None,
        conversation_continuity=None,
        turn_trace_callback=None,
        background_task_budget=None,
        owner_registry=None,
    ):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback
        self.config = config if config else state_engine.config
        self.visual_cortex = visual_cortex
        self.persona_summarizer = persona_summarizer
        self.frequency_controller = frequency_controller
        self.private_chat_manager = private_chat_manager
        self.private_turn_coordinator = private_turn_coordinator
        self.runtime_coordinator = runtime_coordinator
        self.chat_loop_kernel = chat_loop_kernel
        self.conversation_continuity = conversation_continuity
        self.turn_trace_callback = turn_trace_callback
        self.background_task_budget = background_task_budget
        self.owner_registry = owner_registry
        self._proactive_injection_lock: dict[str, asyncio.Lock] = {}
        self._proactive_dispatching: dict[str, bool] = {}
        self._deferred_messages: dict[str, list] = {}  # ponytail: R12 — queue blocked messages
        self.dialogue_store = getattr(state_engine, "dialogue_store", None)
        self.context_compaction = getattr(state_engine, "context_compaction", None)
        if self.context_compaction is None:
            logger.info(
                "[AttentionGate] state_engine.context_compaction is None — "
                "compaction evaluation will be disabled; segments may accumulate unboundedly"
            )

        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()
        # ponytail: guard against unbounded focus_pools growth
        self._last_focus_pool_prune: float = 0.0
        self._background_tasks: set[asyncio.Task] = set()
        self._session_tasks: set[asyncio.Task] = set()
        self._workers_shutdown = False
        self._shutdown_generation = 0
        self._overflow_count = 0
        self._dropped_event_count = 0
        self._worker_invariant_violation_count = 0
        self._background_task_semaphore = asyncio.Semaphore(self.BACKGROUND_TASK_MAX_CONCURRENCY)
        self._deferred_attention_work: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self._deferred_attention_event: asyncio.Event | None = None
        self._deferred_attention_dispatcher: asyncio.Task | None = None
        self._deferred_attention_sequence = 0
        self._deferred_attention_counts: collections.Counter[str] = collections.Counter()
        deferred_db_path = getattr(self.dialogue_store, "db_path", None)
        if not deferred_db_path:
            deferred_db_path = getattr(getattr(state_engine, "db_service", None), "db_path", None)
        self._deferred_attention_outbox = AttentionDeferredOutboxStore(deferred_db_path)
        self._deferred_persist_tasks: set[asyncio.Task] = set()
        self._deferred_persist_by_work: dict[str, asyncio.Task] = {}
        self._deferred_pending_persistence: dict[str, dict[str, Any]] = {}
        self._deferred_persistence_failure_count = 0
        self._deferred_persistence_last_error = ""
        self._deferred_attention_terminal_statuses = frozenset(
            {
                "replayed",
                "expired",
                "exhausted",
                "rejected",
                "shutdown",
                "stale",
                "superseded",
                "cancelled",
                "failed",
                "skipped_already_terminal",
            }
        )
        self._deferred_attention_dispatcher_started_total = 0
        self._deferred_attention_dispatcher_restart_total = 0
        self._deferred_attention_dispatcher_last_started_at = 0.0
        self._deferred_attention_dispatcher_last_progress_at = 0.0
        self._deferred_attention_dispatcher_last_error = ""
        self._deferred_attention_last_enqueued_at = 0.0
        self._deferred_attention_last_terminal_at = 0.0
        self._queue_degradation_counts: collections.Counter[str] = collections.Counter()
        self._queue_degradation_last_log = 0.0
        self._runtime_started_monotonic: float | None = None
        # Platform IDs persist until FIFO eviction; content fallbacks use a short TTL.
        self._global_message_cache = collections.OrderedDict()
        self.perception_builder = PerceptionBuilder(self)
        self.window_buffer = AttentionWindowBuffer(self)
        self.decision_router = AttentionDecisionRouter(self)

    def _register_owner_task(
        self,
        task: asyncio.Task,
        *,
        task_family: str,
        scope_id: str = "GLOBAL",
        run_id: str = "",
    ) -> None:
        registry = getattr(self, "owner_registry", None)
        register = getattr(registry, "register", None)
        if not callable(register):
            return
        try:
            register(
                task,
                task_family=task_family,
                scope_id=scope_id or "GLOBAL",
                run_id=run_id,
                owner="AttentionGate",
                generation=getattr(registry, "generation", 0),
                cancel_status="cancelled",
            )
        except Exception as exc:
            logger.debug("[AttentionGate] owner registry registration degraded: %s", exc)

    @staticmethod
    def _json_safe(value: Any, *, depth: int = 0) -> Any:
        if depth > 4 or value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(key): AttentionGate._json_safe(item, depth=depth + 1)
                for key, item in list(value.items())[:128]
            }
        if isinstance(value, (list, tuple, set)):
            return [AttentionGate._json_safe(item, depth=depth + 1) for item in list(value)[:128]]
        return str(value)

    def _serialize_deferred_event(self, event: Any) -> dict[str, Any]:
        data = getattr(event, "_data", None)
        if not isinstance(data, dict):
            data = {
                "message_str": str(getattr(event, "message_str", "") or ""),
                "unified_msg_origin": str(getattr(event, "unified_msg_origin", "") or ""),
                "timestamp": float(getattr(event, "timestamp", time.time()) or time.time()),
            }
            for name, getter in (
                ("group_id", "get_group_id"),
                ("sender_id", "get_sender_id"),
                ("sender_name", "get_sender_name"),
                ("self_id", "get_self_id"),
            ):
                method = getattr(event, getter, None)
                if callable(method):
                    try:
                        data[name] = str(method() or "")
                    except Exception:
                        data[name] = ""
        extra = getattr(event, "_extra", None)
        if not isinstance(extra, dict) and hasattr(event, "get_extra"):
            extra = {}
            for key in (
                "astrmai_trace_id",
                "astrmai_turn_thread_id",
                "astrmai_turn_generation",
                "astrmai_execution_status",
                "astrmai_reply_sent",
                "astrmai_system2_failure_handled",
                "astrmai_proactive_completed",
            ):
                try:
                    extra[key] = event.get_extra(key, None)
                except Exception:
                    pass
        data = dict(data)
        data["extra"] = self._json_safe(extra or {})
        return self._json_safe(data)

    def _schedule_deferred_persist(self, item: dict[str, Any]) -> None:
        store = getattr(self, "_deferred_attention_outbox", None)
        if store is None or not getattr(store, "db_path", ""):
            return
        work_id = str(item.get("work_id") or "")
        if not work_id:
            return
        previous = self._deferred_persist_by_work.get(work_id)
        if previous is not None and not previous.done():
            return
        delay = max(0.0, self._deferred_next_retry_value(item) - time.monotonic())
        payload = dict(item)
        payload["next_retry_at_wall"] = time.time() + delay
        payload.pop("event", None)
        payload.pop("retry_factory", None)
        payload.pop("session_identity", None)
        event = item.get("event")
        event_data = self._serialize_deferred_event(event)

        async def _persist() -> None:
            try:
                await store.enqueue(payload, event_data=event_data)
                self._deferred_pending_persistence.pop(work_id, None)
            except asyncio.CancelledError:
                if self._workers_shutdown:
                    self._deferred_pending_persistence[work_id] = {
                        "operation": "enqueue",
                        "item": payload,
                        "event_data": event_data,
                        "failed_at": time.time(),
                    }
                raise
            except Exception as exc:
                self._deferred_pending_persistence[work_id] = {
                    "operation": "enqueue",
                    "item": payload,
                    "event_data": event_data,
                    "failed_at": time.time(),
                }
                self._deferred_persistence_failure_count += 1
                self._deferred_persistence_last_error = str(exc)[:500]
                logger.warning("[Attention] deferred outbox enqueue pending retry: %s", exc)

        task = asyncio.create_task(_persist(), name=f"attention-deferred-persist:{work_id}")
        self._deferred_persist_tasks.add(task)
        self._deferred_persist_by_work[work_id] = task
        self._background_tasks.add(task)
        self._register_owner_task(
            task,
            task_family="attention.deferred.persistence",
            scope_id=str(item.get("chat_id") or "GLOBAL"),
            run_id=f"attention-deferred-persist-{work_id}",
        )

        def _done(completed: asyncio.Task) -> None:
            self._deferred_persist_tasks.discard(completed)
            self._background_tasks.discard(completed)
            if self._deferred_persist_by_work.get(work_id) is completed:
                self._deferred_persist_by_work.pop(work_id, None)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_done)

    def _schedule_deferred_finish(self, item: dict[str, Any], status: str, reason: str = "") -> None:
        store = getattr(self, "_deferred_attention_outbox", None)
        token = str(item.get("_outbox_lease_token") or "")
        work_id = str(item.get("work_id") or "")
        if store is None or not getattr(store, "db_path", "") or not work_id:
            return
        pending = self._deferred_persist_by_work.get(work_id)

        async def _finish() -> None:
            if pending is not None and not pending.done():
                await asyncio.gather(pending, return_exceptions=True)
            try:
                changed = await store.finish(
                    work_id,
                    lease_token=token,
                    status="retry_wait" if status == "shutdown" else status,
                    attempts=int(item.get("attempts", 0) or 0),
                    next_retry_at=time.time() if status == "shutdown" else 0.0,
                    error=reason,
                )
                if not changed:
                    raise RuntimeError("deferred outbox settlement lease was not current")
                self._deferred_pending_persistence.pop(work_id, None)
            except asyncio.CancelledError:
                if self._workers_shutdown:
                    self._deferred_pending_persistence[work_id] = {
                        "operation": "finish",
                        "work_id": work_id,
                        "lease_token": token,
                        "status": "retry_wait" if status == "shutdown" else status,
                        "attempts": int(item.get("attempts", 0) or 0),
                        "next_retry_at": time.time() if status == "shutdown" else 0.0,
                        "error": reason,
                        "failed_at": time.time(),
                    }
                raise
            except Exception as exc:
                self._deferred_pending_persistence[work_id] = {
                    "operation": "finish",
                    "work_id": work_id,
                    "lease_token": token,
                    "status": "retry_wait" if status == "shutdown" else status,
                    "attempts": int(item.get("attempts", 0) or 0),
                    "next_retry_at": time.time() if status == "shutdown" else 0.0,
                    "error": reason,
                    "failed_at": time.time(),
                }
                self._deferred_persistence_failure_count += 1
                self._deferred_persistence_last_error = str(exc)[:500]
                logger.warning("[Attention] deferred outbox settlement pending retry: %s", exc)

        task = asyncio.create_task(_finish(), name=f"attention-deferred-finish:{work_id}")
        self._deferred_persist_tasks.add(task)
        self._deferred_persist_by_work[work_id] = task
        self._background_tasks.add(task)
        self._register_owner_task(
            task,
            task_family="attention.deferred.persistence",
            scope_id=str(item.get("chat_id") or "GLOBAL"),
            run_id=f"attention-deferred-finish-{work_id}",
        )

        def _done(completed: asyncio.Task) -> None:
            self._deferred_persist_tasks.discard(completed)
            self._background_tasks.discard(completed)
            if self._deferred_persist_by_work.get(work_id) is completed:
                self._deferred_persist_by_work.pop(work_id, None)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_done)

    async def _restore_deferred_attention(self) -> None:
        store = getattr(self, "_deferred_attention_outbox", None)
        if store is None or not getattr(store, "db_path", "") or self._workers_shutdown:
            return
        try:
            rows = await store.claim_due(
                limit=max(1, int(self._deferred_attention_config("attention_deferred_queue_max", 128))),
                include_future=True,
            )
        except Exception as exc:
            logger.debug("[Attention] deferred outbox restore degraded: %s", exc)
            return
        for row in rows:
            work_id = str(row.get("work_id") or "")
            if not work_id or work_id in self._deferred_attention_work:
                continue
            event = _SyntheticExternalEvent(dict(row.get("event_data") or {}))
            turn_thread_id = str(row.get("turn_thread_id") or "")
            turn_generation = int(row.get("turn_generation", 0) or 0)
            if turn_thread_id or turn_generation:
                event.set_extra(
                    "astrmai_turn_identity",
                    SimpleNamespace(thread_id=turn_thread_id, generation=turn_generation),
                )
            now = time.time()
            if float(row.get("expires_at", now) or now) <= now:
                await store.finish(work_id, lease_token=str(row.get("lease_token") or ""), status="expired", error="ttl_expired")
                continue
            item = {
                "work_id": work_id,
                "chat_id": str(row.get("chat_id") or ""),
                "task_name": str(row.get("task_name") or "attention.misc"),
                "event": event,
                "retry_factory": lambda event=event: self.process_event(event),
                "enqueued_at": now,
                "next_retry_at": time.monotonic() + max(0.0, float(row.get("next_retry_at_wall", now) or now) - now),
                "expires_at": float(row.get("expires_at", now) or now),
                "attempts": int(row.get("attempts", 0) or 0),
                "max_attempts": max(1, int(row.get("max_attempts", 3) or 3)),
                "reason": str(row.get("reason") or "queue_timeout"),
                "session_identity": None,
                "worker_generation": int(row.get("worker_generation", 0) or 0),
                "shutdown_generation": int(self._shutdown_generation),
                "turn_thread_id": turn_thread_id,
                "turn_generation": turn_generation,
                "_outbox_lease_token": str(row.get("lease_token") or ""),
                "_terminal_status": None,
            }
            self._deferred_attention_work[work_id] = item
            self._deferred_attention_sequence += 1
            self._deferred_attention_counts["restored"] += 1
        if rows:
            self._ensure_deferred_attention_dispatcher()
            if self._deferred_attention_event is not None:
                self._deferred_attention_event.set()

    def _schedule_deferred_restore(self) -> None:
        if not getattr(getattr(self, "_deferred_attention_outbox", None), "db_path", ""):
            return
        try:
            task = asyncio.create_task(self._restore_deferred_attention(), name="attention-deferred-restore")
        except RuntimeError:
            return
        self._background_tasks.add(task)
        self._register_owner_task(
            task,
            task_family="attention.deferred.restore",
            scope_id="GLOBAL",
            run_id=f"attention-deferred-restore-{self._shutdown_generation}",
        )

        def _done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_done)

    def refresh_config(self, config):
        self.config = config
        if self.private_turn_coordinator is not None:
            self.private_turn_coordinator.refresh_config(config)
        if self.conversation_continuity is not None:
            refresh_continuity = getattr(self.conversation_continuity, "refresh_config", None)
            if callable(refresh_continuity):
                refresh_continuity(config)

    def _accumulation_pool_limit(self) -> int:
        attention_cfg = getattr(self.config, "attention", None)
        try:
            return max(1, int(getattr(attention_cfg, "accumulation_pool_max_events", 100) or 100))
        except (TypeError, ValueError):
            return 100

    @staticmethod
    def _event_is_priority_for_accumulation(event: Any) -> bool:
        if event is None:
            return False
        keys = (
            "astrmai_group_direct_wakeup",
            "astrmai_at_bot_wakeup",
            "astrmai_reply_wakeup",
            "astrmai_is_command",
            "heartflow_is_command",
        )
        return any(bool(event.get_extra(key, False)) for key in keys if hasattr(event, "get_extra"))

    @staticmethod
    def _event_diagnostic_id(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "")
        if not message_id:
            message_id = "|".join(
                (
                    str(getattr(event, "unified_msg_origin", "") or ""),
                    str(getattr(event, "timestamp", "") or ""),
                    str(getattr(event, "message_str", "") or ""),
                )
            )
        return hashlib.sha256(message_id.encode("utf-8", errors="ignore")).hexdigest()[:16]

    def _append_accumulation_event(self, session: SessionContext, event: Any) -> bool:
        """Append with a bounded per-chat pool, retaining priority events when possible."""
        limit = self._accumulation_pool_limit()
        if len(session.accumulation_pool) < limit:
            session.accumulation_pool.append(event)
            if session.oldest_pending_at <= 0:
                session.oldest_pending_at = time.time()
            return True

        session.overflow_count += 1
        self._overflow_count += 1
        incoming_priority = self._event_is_priority_for_accumulation(event)
        removable_index = next(
            (
                index
                for index, queued in enumerate(session.accumulation_pool)
                if not self._event_is_priority_for_accumulation(queued)
            ),
            None,
        )
        if incoming_priority and removable_index is not None:
            dropped = session.accumulation_pool.pop(removable_index)
            session.accumulation_pool.append(event)
        elif incoming_priority and removable_index is None:
            dropped = session.accumulation_pool.pop(0)
            session.accumulation_pool.append(event)
        else:
            dropped = event
        session.dropped_event_count += 1
        self._dropped_event_count += 1
        try:
            dropped.set_extra("astrmai_attention_accumulation_dropped", True)
            dropped.set_extra(
                "astrmai_attention_accumulation_dropped_event_hash",
                self._event_diagnostic_id(dropped),
            )
        except Exception:
            pass
        return dropped is not event

    def _prepend_accumulation_events(
        self,
        session: SessionContext,
        events: list[Any],
        *,
        oldest_pending_at: float = 0.0,
    ) -> None:
        if not events:
            return
        existing = list(session.accumulation_pool)
        session.accumulation_pool.clear()
        for event in events:
            self._append_accumulation_event(session, event)
        for event in existing:
            self._append_accumulation_event(session, event)
        if oldest_pending_at > 0:
            session.oldest_pending_at = min(
                value for value in (session.oldest_pending_at, oldest_pending_at) if value > 0
            )

    def describe_status(self) -> dict[str, Any]:
        now = time.time()
        pool_lengths = [len(session.accumulation_pool) for session in self.focus_pools.values()]
        oldest = [
            float(session.oldest_pending_at or 0.0)
            for session in self.focus_pools.values()
            if session.accumulation_pool and session.oldest_pending_at > 0
        ]
        active_workers = sum(
            1
            for task in self._session_tasks
            if task is not None and not task.done()
        )
        evaluating = sum(1 for session in self.focus_pools.values() if session.is_evaluating)
        violations = 0
        for session in self.focus_pools.values():
            if session.is_evaluating and (
                session.worker_task is None or session.worker_task.done()
            ):
                violations += 1
        worker_invariant_violation = max(
            self._worker_invariant_violation_count,
            violations,
        )
        deferred = list(self._deferred_attention_work.values())
        oldest_deferred = min(
            (self._deferred_enqueued_value(item, now) for item in deferred),
            default=now,
        )
        current_by_kind = collections.Counter(
            str(item.get("task_name", "attention.misc") or "attention.misc")
            if isinstance(item, dict)
            else "attention.invalid"
            for item in deferred
        )
        current_by_chat = collections.Counter(
            str(item.get("chat_id", "") or "") if isinstance(item, dict) else ""
            for item in deferred
        )
        oldest_item = min(
            deferred,
            key=lambda item: self._deferred_enqueued_value(item, now),
            default=None,
        )
        return {
            "pool_length": sum(pool_lengths),
            "pool_limit": self._accumulation_pool_limit(),
            "oldest_pending_age_ms": round(max(0.0, now - min(oldest)) * 1000, 3) if oldest else 0.0,
            "overflow_count": self._overflow_count,
            "dropped_event_count": self._dropped_event_count,
            "metrics_scope": "attention_gate_instance",
            "worker_count": active_workers,
            "is_evaluating": evaluating > 0,
            "worker_invariant_violation": worker_invariant_violation,
            "shutdown_generation": self._shutdown_generation,
            "workers_shutdown": self._workers_shutdown,
            "startup_warmup_remaining_sec": round(self._startup_warmup_remaining(), 3),
            "startup_warmup_active": self._startup_warmup_remaining() > 0.0,
            "session_count": len(self.focus_pools),
            "attention_deferred_current": len(deferred),
            "attention_deferred_oldest_age_ms": round(max(0.0, now - oldest_deferred) * 1000.0, 3) if deferred else 0.0,
            "attention_deferred_total_by_kind": {
                str(kind): int(count)
                for kind, count in self._deferred_attention_counts.items()
                if str(kind).startswith("current:") is False
            },
            "attention_deferred_current_by_kind": dict(current_by_kind),
            "attention_deferred_current_by_chat": dict(current_by_chat),
            "attention_deferred_oldest_work_id": oldest_item.get("work_id") if isinstance(oldest_item, dict) else None,
            "attention_deferred_oldest_chat_id": oldest_item.get("chat_id") if isinstance(oldest_item, dict) else None,
            "attention_deferred_total": int(self._deferred_attention_counts.get("total", 0)),
            "attention_deferred_replayed_total": int(self._deferred_attention_counts.get("replayed", 0)),
            "attention_deferred_replay_succeeded_total": int(self._deferred_attention_counts.get("succeeded", 0)),
            "attention_deferred_replay_failed_total": int(self._deferred_attention_counts.get("failed", 0)),
            "attention_deferred_expired_total": int(self._deferred_attention_counts.get("expired", 0)),
            "attention_deferred_exhausted_total": int(self._deferred_attention_counts.get("exhausted", 0)),
            "attention_deferred_rejected_total": int(self._deferred_attention_counts.get("rejected", 0)),
            "attention_deferred_shutdown_total": int(self._deferred_attention_counts.get("shutdown", 0)),
            "attention_deferred_dispatcher_failed_total": int(self._deferred_attention_counts.get("dispatcher_failed", 0)),
            "attention_deferred_stale_total": int(self._deferred_attention_counts.get("stale", 0)),
            "attention_deferred_cancelled_total": int(self._deferred_attention_counts.get("cancelled", 0)),
            "attention_deferred_superseded_total": int(self._deferred_attention_counts.get("superseded", 0)),
            "attention_deferred_skipped_terminal_total": int(self._deferred_attention_counts.get("skipped_already_terminal", 0)),
            "attention_deferred_enqueued_total": int(self._deferred_attention_counts.get("total", 0)),
            "attention_deferred_replay_attempt_total": int(self._deferred_attention_counts.get("replay_attempts", 0)),
            "attention_deferred_delayed_total": int(self._deferred_attention_counts.get("delayed", 0)),
            "attention_deferred_dispatcher_running": bool(
                self._deferred_attention_dispatcher is not None
                and not self._deferred_attention_dispatcher.done()
            ),
            "attention_deferred_dispatcher_started_total": int(
                self._deferred_attention_dispatcher_started_total
            ),
            "attention_deferred_dispatcher_restart_total": int(
                self._deferred_attention_dispatcher_restart_total
            ),
            "attention_deferred_dispatcher_last_started_at": float(
                self._deferred_attention_dispatcher_last_started_at
            ),
            "attention_deferred_dispatcher_last_progress_at": float(
                self._deferred_attention_dispatcher_last_progress_at
            ),
            "attention_deferred_dispatcher_last_error": self._deferred_attention_dispatcher_last_error,
            "attention_deferred_last_enqueued_at": float(self._deferred_attention_last_enqueued_at),
            "attention_deferred_last_terminal_at": float(self._deferred_attention_last_terminal_at),
            "attention_deferred_persistence_failed_total": int(self._deferred_persistence_failure_count),
            "attention_deferred_pending_persistence": len(self._deferred_pending_persistence),
            "attention_deferred_persistence_oldest_failure_age_ms": round(
                max(
                    0.0,
                    now
                    - min(
                        (
                            float(item.get("failed_at", now) or now)
                            for item in self._deferred_pending_persistence.values()
                        ),
                        default=now,
                    ),
                )
                * 1000.0,
                3,
            ),
            "attention_deferred_persistence_last_error": self._deferred_persistence_last_error,
            "attention_queue_degradation_total": int(sum(self._queue_degradation_counts.values())),
            "attention_queue_degradation_by_kind": dict(self._queue_degradation_counts),
        }

    def request_shutdown(self) -> None:
        if not self._workers_shutdown:
            self._workers_shutdown = True
            self._shutdown_generation += 1

    def mark_runtime_started(self) -> None:
        """Record the successful runtime transition used by startup warmup."""
        self._runtime_started_monotonic = time.monotonic()
        self._schedule_pending_persistence_retry()
        self._schedule_deferred_restore()

    def _schedule_pending_persistence_retry(self) -> None:
        if self._workers_shutdown or not self._deferred_pending_persistence:
            return
        try:
            task = asyncio.create_task(self._retry_pending_persistence(), name="attention-deferred-persistence-retry")
        except RuntimeError:
            return
        self._background_tasks.add(task)
        self._register_owner_task(task, task_family="attention.deferred.persistence", scope_id="GLOBAL", run_id=f"attention-deferred-persistence-retry-{self._shutdown_generation}")

        def _done(completed: asyncio.Task) -> None:
            self._background_tasks.discard(completed)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(_done)

    async def _retry_pending_persistence(self) -> None:
        store = self._deferred_attention_outbox
        for key, pending in list(self._deferred_pending_persistence.items()):
            if self._workers_shutdown:
                return
            try:
                if pending.get("operation") == "enqueue":
                    await store.enqueue(pending["item"], event_data=pending.get("event_data", {}))
                else:
                    await store.finish(
                        pending["work_id"],
                        lease_token=pending.get("lease_token", ""),
                        status=pending.get("status", "retry_wait"),
                        attempts=pending.get("attempts", 0),
                        next_retry_at=pending.get("next_retry_at", 0.0),
                        error=pending.get("error", ""),
                    )
                self._deferred_pending_persistence.pop(key, None)
            except Exception as exc:
                self._deferred_persistence_last_error = str(exc)[:500]

    def _startup_warmup_remaining(self) -> float:
        started = self._runtime_started_monotonic
        if started is None:
            return 0.0
        attention_cfg = getattr(self.config, "attention", None)
        try:
            window = max(0.0, float(getattr(attention_cfg, "startup_warmup_sec", 120.0) or 0.0))
        except (TypeError, ValueError):
            window = 120.0
        return max(0.0, window - (time.monotonic() - started))

    def _should_skip_startup_warmup(self, event: Any, task_name: str, *, replay: bool = False) -> bool:
        if replay or self._startup_warmup_remaining() <= 0.0:
            return False
        if str(task_name or "") not in {"attention.system2", "attention.compaction"}:
            return False
        if event is None:
            return str(task_name or "") == "attention.compaction"
        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            if any(
                bool(get_extra(key, False))
                for key in (
                    "astrmai_group_direct_wakeup",
                    "astrmai_force_engage",
                    "astrmai_is_strong_wakeup",
                    "astrmai_is_proactive_event",
                )
            ):
                return False
            if str(get_extra("astrmai_attention_prefilter_action", "") or "").strip().lower() == "force_pass":
                return False
        group_id = ""
        get_group_id = getattr(event, "get_group_id", None)
        if callable(get_group_id):
            try:
                group_id = str(get_group_id() or "")
            except Exception:
                group_id = ""
        if not group_id:
            group_id = str(getattr(event, "unified_msg_origin", "") or "")
        return "GroupMessage" in group_id or ":Group" in group_id

    def _record_queue_degradation(self, task: asyncio.Task, exc: BaseException) -> None:
        task_name = str(getattr(task, "_astrmai_task_name", "attention.background") or "attention.background")
        kind = "queue_full" if isinstance(exc, BackgroundTaskQueueFull) else "queue_timeout"
        key = f"{task_name}:{kind}"
        self._queue_degradation_counts[key] += 1
        count = self._queue_degradation_counts[key]
        now = time.monotonic()
        if count == 1 or count % 10 == 0 or now - self._queue_degradation_last_log >= 30.0:
            self._queue_degradation_last_log = now
            logger.warning(
                "[AttentionGate] background admission degraded "
                f"task={task_name} reason={kind} count={count}; deferred or skipped without fallback"
            )

    def reset_runtime_state(self) -> None:
        self._workers_shutdown = False
        self._shutdown_generation += 1
        self._runtime_started_monotonic = None
        for session in self.focus_pools.values():
            session.closed = False
            session.worker_generation += 1
            session.worker_task = None
            session.is_evaluating = False

    async def shutdown_workers(self) -> int:
        self.request_shutdown()
        async with self._pool_lock:
            sessions = list(self.focus_pools.values())
            for session in sessions:
                async with session.lock:
                    session.closed = True
                    session.worker_generation += 1
                    session.accumulation_pool.clear()
                    session.oldest_pending_at = 0.0
                    session.is_evaluating = False
        cancelled = await self._cancel_session_workers()
        dispatcher = self._deferred_attention_dispatcher
        if dispatcher is not None and not dispatcher.done():
            dispatcher.cancel()
            try:
                await dispatcher
            except asyncio.CancelledError:
                pass
        self._deferred_attention_dispatcher = None
        for item in list(self._deferred_attention_work.values()):
            self._record_deferred_transition(item, "shutdown", reason="workers_shutdown")
        self._deferred_attention_work.clear()
        self._deferred_messages.clear()
        self._proactive_dispatching.clear()
        pending_persistence = [
            task for task in self._deferred_persist_tasks if not task.done()
        ]
        if pending_persistence:
            done, pending = await asyncio.wait(pending_persistence, timeout=2.0)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return cancelled

    def _prepare_group_attention_topic(
        self,
        chat_id: str,
        focus_event: AstrMessageEvent,
        focus_thread: Any,
    ) -> DialogHistoryPolicy:
        policy = DialogHistoryPolicy()
        continuity = self.conversation_continuity
        evaluate_group = getattr(continuity, "evaluate_group_message", None)
        if callable(evaluate_group):
            canonical = focus_event.get_extra("astrmai_conversation_event", None)
            try:
                event_timestamp = float(
                    focus_event.get_extra(
                        "astrmai_timestamp",
                        getattr(focus_event, "timestamp", 0.0),
                    )
                    or 0.0
                )
            except (TypeError, ValueError):
                event_timestamp = 0.0
            try:
                policy = evaluate_group(
                    chat_id,
                    str(
                        focus_event.get_extra(
                            "astrmai_rich_text",
                            getattr(focus_event, "message_str", ""),
                        )
                        or ""
                    ),
                    sender_id=str(focus_event.get_sender_id() or "").strip(),
                    has_reply_reference=bool(
                        getattr(canonical, "reply_target_event_id", "")
                        or getattr(canonical, "quote_event_id", "")
                        or str(getattr(focus_thread, "root_reason", "") or "")
                        == "explicit_reply_target"
                    ),
                    approved_event_ids=getattr(
                        getattr(focus_thread, "turn_target", None),
                        "source_event_ids",
                        (),
                    ),
                    now=event_timestamp or None,
                )
            except Exception as exc:
                logger.debug(f"[AttentionGate] group topic preparation degraded: {exc}")
        focus_thread.history_policy = policy
        policy.bind(focus_event)
        identity = resolve_attention_topic_identity(
            history_policy=policy,
            focus_event=focus_event,
            focus_thread=focus_thread,
        )
        focus_thread.attention_topic = identity
        identity.bind(focus_event)
        target = getattr(focus_thread, "turn_target", None)
        if target is not None:
            focus_thread.turn_target = replace(
                target,
                topic_epoch=policy.topic_epoch,
                attention_topic_key=identity.attention_topic_key,
            )
        return policy

    def get_proactive_lock(self, chat_id: str) -> asyncio.Lock:
        """Return a per-chat lock for proactive injection serialization."""
        if chat_id not in self._proactive_injection_lock:
            self._proactive_injection_lock[chat_id] = asyncio.Lock()
        return self._proactive_injection_lock[chat_id]

    async def drain_deferred_messages(self, chat_id: str, limit: int = 5):
        """Replay messages queued during proactive dispatching."""
        queue = self._deferred_messages.pop(chat_id, None)
        if not queue:
            return
        drained = 0
        for event in queue[:limit]:
            try:
                await self.process_event(event)
                drained += 1
            except Exception:
                logger.warning(
                    f"[AttentionGate] drain deferred message failed for {chat_id}",
                    exc_info=True,
                )
        if drained:
            logger.info(f"[AttentionGate] drained {drained} deferred messages for {chat_id}")

    async def clear_chat_state(self, chat_id: str) -> bool:
        removed = False
        removed_session = None
        async with self._pool_lock:
            removed_session = self.focus_pools.pop(chat_id, None)
            if removed_session is not None:
                removed_session.closed = True
                removed_session.worker_generation += 1
            removed = removed_session is not None or removed
            removed = self._proactive_dispatching.pop(chat_id, None) is not None or removed
            removed = self._deferred_messages.pop(chat_id, None) is not None or removed
            removed = self._proactive_injection_lock.pop(chat_id, None) is not None or removed
            deferred_ids = [
                work_id
                for work_id, item in self._deferred_attention_work.items()
                if str(item.get("chat_id", "") or "") == str(chat_id)
            ]
            for work_id in deferred_ids:
                item = self._deferred_attention_work.pop(work_id, None)
                if item is not None:
                    self._set_deferred_terminal(item, "stale")
            removed = bool(deferred_ids) or removed
        removed = bool(
            await self._cancel_session_workers(chat_id, session=removed_session)
        ) or removed
        clear_pending = getattr(self.private_turn_coordinator, "clear_pending_batch", None)
        if callable(clear_pending):
            removed = bool(clear_pending(chat_id)) or removed
        clear_router = getattr(self.decision_router, "clear_chat_state", None)
        if callable(clear_router):
            removed = bool(clear_router(chat_id)) or removed
        clear_continuity = getattr(self.conversation_continuity, "clear", None)
        if callable(clear_continuity):
            had_continuity = bool(
                chat_id in set(self.conversation_continuity.chats())
                if hasattr(self.conversation_continuity, "chats")
                else False
            )
            clear_continuity(chat_id)
            removed = had_continuity or removed
        compaction = getattr(self, "context_compaction", None)
        if compaction is not None and hasattr(compaction, "clear_chat_state"):
            try:
                removed = bool(compaction.clear_chat_state(chat_id)) or removed
            except Exception as exc:
                logger.debug(f"[AttentionGate] context compaction clear degraded for {chat_id}: {exc}")
        return removed

    async def _cancel_session_workers(
        self,
        chat_id: str | None = None,
        *,
        session: SessionContext | None = None,
    ) -> int:
        tasks = []
        sessions = []
        for task in list(self._session_tasks):
            worker_context = getattr(task, "_worker_context", None)
            worker_chat_id = str(getattr(worker_context, "chat_id", "") or "")
            if chat_id is not None and worker_chat_id != str(chat_id):
                continue
            worker_session = getattr(worker_context, "session", None)
            if session is not None and worker_session is not session:
                continue
            tasks.append(task)
            if worker_session is not None and all(existing is not worker_session for existing in sessions):
                sessions.append(worker_session)

        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in sessions:
            async with session.lock:
                session.is_evaluating = False
                session.worker_task = None
                session.accumulation_pool.clear()
                session.oldest_pending_at = 0.0
        return len(tasks)

    async def _extract_image_base64(self, image_component):
        return await extract_image_base64(self, image_component)

    async def _extract_image_base64_from_url(self, url: str):
        return await extract_image_base64_from_url(self, url)

    def _build_normalized_events(self, events, self_id: str):
        return build_normalized_events(self, events, self_id)

    def _score_focus_candidate(self, candidate, normalized_events):
        return score_focus_candidate(self, candidate, normalized_events)

    def _select_focus_event(self, events, self_id: str, normalized_events=None, *, is_private: bool = False):
        return select_focus_event(
            self,
            events,
            self_id,
            normalized_events=normalized_events,
            is_private=is_private,
        )

    def _resolve_thread_root(self, focus_candidate, normalized_events):
        return resolve_thread_root(self, focus_candidate, normalized_events)

    def _build_focus_thread(self, focus_candidate, root_candidate, normalized_events):
        return build_focus_thread(self, focus_candidate, root_candidate, normalized_events)

    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        # ponytail: prune stale focus pools every 300s
        now = time.time()
        if now - self._last_focus_pool_prune > 300:
            self._prune_stale_focus_pools()
            self._last_focus_pool_prune = now

        async with self._pool_lock:
            session = self.focus_pools.get(chat_id)
            if session is None:
                session = SessionContext()
                session.last_message_hash = ""
                session.repeat_count = 0
                session.last_active_user_time = 0.0
                session.last_window_open_ts = 0.0
                session.closed = False
                self.focus_pools[chat_id] = session
            session.last_active_time = time.time()
            return session

    # ponytail: remove focus_pools entries idle > 24h to prevent unbounded growth
    def _prune_stale_focus_pools(self, max_age: float = 86400.0):
        now = time.time()
        stale = [cid for cid, s in self.focus_pools.items() if now - float(s.last_active_time) > max_age]
        for cid in stale:
            self.focus_pools.pop(cid, None)
            self._proactive_injection_lock.pop(cid, None)

    def _is_direct_wakeup_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        if not event:
            return False
        if event.get_extra("astrmai_group_direct_wakeup", False):
            return True
        if event.get_extra("astrmai_bonus_score", 0.0) >= 1.0:
            return True
        if self.sensors is None:
            return False
        try:
            return bool(self.sensors.is_wakeup_signal(event, self_id))
        except Exception:
            logger.warning("[AttentionGate] is_direct_wakeup_event failed", exc_info=True)
            return False

    def _is_at_bot_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        if event.get_extra("astrmai_at_bot_wakeup", False):
            return True
        resolved_self_id = str(self_id or "").strip() or get_event_self_id(event)
        return event_mentions_actor(event, resolved_self_id)

    def _is_reply_to_bot_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        bot_names = [
            str(name).strip()
            for name in getattr(
                getattr(getattr(self, "config", None), "system1", None),
                "nicknames",
                [],
            )
            or []
            if str(name).strip()
        ]
        for component in message:
            component_type = str(getattr(component, "type", component.__class__.__name__)).lstrip("_").lower()
            if component_type != "reply":
                continue
            reply_sender_id = str(getattr(component, "sender_id", "") or "")
            reply_sender_name = str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).strip()
            if reply_sender_id == str(self_id):
                return True
            if reply_sender_name and reply_sender_name in bot_names:
                return True
        return False

    def _resolve_wakeup_flags(self, event: AstrMessageEvent, self_id: str, msg_str: str) -> tuple[bool, bool, bool, bool]:
        is_direct = self._is_direct_wakeup_event(event, self_id)
        is_at_bot = self._is_at_bot_event(event, self_id)
        is_reply = self._is_reply_to_bot_event(event, self_id)
        normalized = str(msg_str or "").strip()
        is_name_only = bool(normalized) and normalized in {
            str(name).strip() for name in getattr(getattr(self.config, "system1", None), "nicknames", []) or [] if str(name).strip()
        }
        return is_direct, is_at_bot, is_reply, is_name_only

    @staticmethod
    def _is_near_context_query_text(message_text: str) -> bool:
        if not isinstance(message_text, str):
            return False
        normalized = message_text.strip()
        if not normalized:
            return False
        trigger_phrases = [
            "为什么",
            "哪里",
            "什么意思",
            "你刚刚",
            "刚刚说",
            "上一个",
            "上一句",
            "不是这个",
            "为啥",
            "咋",
            "什么",
            "啥",
            "啥意思",
            "不可以",
        ]
        return any(phrase in normalized for phrase in trigger_phrases)

    @staticmethod
    def _tokenize_text(text: str) -> Set[str]:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return set()
        return {token for token in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if token}

    def _extract_reply_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in message:
            component_type = getattr(component, "type", component.__class__.__name__).lower()
            if component_type != "reply":
                continue
            target_id = str(getattr(component, "sender_id", "") or "")
            target_name = str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).strip()
            return target_id, target_name
        return "", ""

    def _is_image_only(self, event: AstrMessageEvent) -> bool:
        has_img = bool(
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls"))
            or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls"))
        )
        has_text = bool(str(getattr(event, "message_str", "") or "").strip())
        return has_img and not has_text

    def _check_continuous_images(self, pool: List[AstrMessageEvent]) -> int:
        count = 0
        for candidate in reversed(pool):
            if self._is_image_only(candidate):
                count += 1
            else:
                break
        return count

    async def _run_managed_system2_task(self, coro, event):
        task = asyncio.current_task()
        turn = event.get_extra("astrmai_turn_identity", None) if hasattr(event, "get_extra") else None
        coordinator = self.runtime_coordinator
        registered = True
        if coordinator is not None and hasattr(coordinator, "register_turn_task"):
            registered = await coordinator.register_turn_task(turn, task)
        if not registered:
            if hasattr(coro, "close"):
                coro.close()
            return None
        try:
            return await coro
        except (BackgroundTaskQueueTimeout, BackgroundTaskQueueFull):
            # Queue admission failures are compensable deferred work, not a
            # System2 execution failure.  Let _run_background_task classify
            # and enqueue them without sending a fallback reply.
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_system2_failure(event, exc)
            return None
        finally:
            if coordinator is not None and hasattr(coordinator, "unregister_turn_task"):
                await coordinator.unregister_turn_task(turn, task)

    async def _run_background_slot(self, awaitable_factory, event=None, *, admission_deadline: float | None = None):
        acquired = False
        timing = getattr(getattr(self, "config", None), "timing", None)
        try:
            configured_timeout = float(
                getattr(timing, "attention_background_slot_wait_timeout_sec", 30.0)
                or 30.0
            )
        except (TypeError, ValueError):
            configured_timeout = 30.0
        timeout_sec = clamp_timeout_to_turn_budget(
            event,
            max(0.1, configured_timeout),
            reserve_for_reply=True,
        )
        if admission_deadline is not None:
            timeout_sec = min(
                timeout_sec,
                max(0.0, float(admission_deadline) - time.monotonic()),
            )
        wait_stage = begin_stage(
            event,
            "attention.background_slot_wait",
            critical_path=True,
            metadata={
                "limit": int(self.BACKGROUND_TASK_MAX_CONCURRENCY),
                "timeout_sec": timeout_sec,
            },
        )
        try:
            if timeout_sec <= 0.0:
                raise asyncio.TimeoutError
            if getattr(self._background_task_semaphore, "_value", 0) > 0:
                await self._background_task_semaphore.acquire()
            else:
                await asyncio.wait_for(
                    self._background_task_semaphore.acquire(),
                    timeout=timeout_sec,
                )
            acquired = True
            finish_stage(event, wait_stage, metadata={"timeout_sec": timeout_sec})
        except asyncio.TimeoutError as exc:
            finish_stage(event, wait_stage, status="timeout", reason="queue_timeout")
            if event is not None and hasattr(event, "set_extra"):
                event.set_extra("astrmai_execution_status", "background_queue_timeout")
                event.set_extra(
                    "astrmai_queue_timeout_stage",
                    "attention.background_slot_wait",
                )
                await self._finalize_pre_planner_turn(
                    event,
                    str(getattr(event, "unified_msg_origin", "") or ""),
                    status="background_queue_timeout",
                )
            raise BackgroundTaskQueueTimeout(
                "attention background semaphore wait timed out"
            ) from exc
        except asyncio.CancelledError:
            finish_stage(event, wait_stage, status="cancelled", reason="superseded_or_shutdown")
            raise
        except Exception as exc:
            finish_stage(event, wait_stage, status="error", reason=type(exc).__name__)
            raise
        try:
            return await awaitable_factory()
        finally:
            if acquired:
                self._background_task_semaphore.release()

    def _deferred_attention_config(self, name: str, default: float) -> float:
        attention_cfg = getattr(self.config, "attention", None)
        try:
            return float(getattr(attention_cfg, name, default) or default)
        except (TypeError, ValueError):
            return float(default)

    def _ensure_deferred_attention_dispatcher(self) -> None:
        if self._workers_shutdown:
            return
        task = self._deferred_attention_dispatcher
        if task is not None and not task.done():
            return
        try:
            self._deferred_attention_event = self._deferred_attention_event or asyncio.Event()
            task = asyncio.create_task(
                self._dispatch_deferred_attention_work(),
                name="attention-deferred-dispatcher",
            )
        except RuntimeError:
            return
        self._deferred_attention_dispatcher = task
        self._register_owner_task(
            task,
            task_family="attention.deferred.dispatcher",
            scope_id="GLOBAL",
            run_id=f"attention-deferred-{self._deferred_attention_dispatcher_started_total}",
        )
        self._deferred_attention_dispatcher_started_total += 1
        if self._deferred_attention_dispatcher_started_total > 1:
            self._deferred_attention_dispatcher_restart_total += 1
        self._deferred_attention_dispatcher_last_started_at = time.time()
        self._deferred_attention_dispatcher_last_progress_at = self._deferred_attention_dispatcher_last_started_at
        logger.info(
            "[Attention] deferred dispatcher started "
            f"restart={self._deferred_attention_dispatcher_restart_total} "
            f"queue_depth={len(self._deferred_attention_work)}"
        )

        def _consume(completed: asyncio.Task) -> None:
            if self._deferred_attention_dispatcher is completed:
                self._deferred_attention_dispatcher = None
            try:
                failure = completed.exception()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                failure = exc
            if failure is not None:
                self._deferred_attention_counts["dispatcher_failed"] += 1
                self._deferred_attention_dispatcher_last_error = (
                    f"{type(failure).__name__}: {failure}"[:500]
                )
                logger.error(f"[Attention] deferred dispatcher failed: {failure!r}")
                # A dispatcher failure must not strand deferred work.  Restart
                # on the same loop after the done callback has released the
                # owner reference; the next iteration will validate/pop any
                # malformed item instead of terminating the queue again.
                if self._deferred_attention_work and not self._workers_shutdown:
                    try:
                        asyncio.get_running_loop().call_soon(
                            self._ensure_deferred_attention_dispatcher
                        )
                    except RuntimeError:
                        pass

        task.add_done_callback(_consume)

    def _record_deferred_rejection(self, event: Any, *, reason: str) -> None:
        self._deferred_attention_counts["rejected"] += 1
        self._deferred_attention_last_terminal_at = time.time()
        self._deferred_attention_dispatcher_last_progress_at = self._deferred_attention_last_terminal_at
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("deferred_terminal_status", "rejected")
            event.set_extra("deferred_reason", str(reason))
        logger.warning(
            "[Attention] deferred work rejected "
            f"reason={reason} queue_depth={len(self._deferred_attention_work)}"
        )

    def _defer_attention_work(
        self,
        *,
        event: Any,
        task_name: str,
        retry_factory,
        reason: str,
    ) -> bool:
        if retry_factory is None or self._workers_shutdown:
            if event is not None and hasattr(event, "set_extra"):
                event.set_extra("deferred_terminal_status", "shutdown")
            return False
        limit = max(1, int(self._deferred_attention_config("attention_deferred_queue_max", 128)))
        if len(self._deferred_attention_work) >= limit:
            self._record_deferred_rejection(event, reason="deferred_queue_full")
            return False
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        session = self.focus_pools.get(chat_id) if chat_id else None
        turn = event.get_extra("astrmai_turn_identity", None) if event is not None and hasattr(event, "get_extra") else None
        per_chat_limit = max(1, int(self._deferred_attention_config("attention_deferred_per_chat_max", 4)))
        if sum(1 for item in self._deferred_attention_work.values() if item.get("chat_id") == chat_id) >= per_chat_limit:
            self._record_deferred_rejection(event, reason="deferred_chat_limit")
            return False
        self._deferred_attention_sequence += 1
        work_id = f"attention-deferred-{self._deferred_attention_sequence}"
        now = time.time()
        item = {
            "work_id": work_id,
            "chat_id": chat_id,
            "task_name": str(task_name or "attention.misc"),
            "event": event,
            "retry_factory": retry_factory,
            "enqueued_at": now,
            "next_retry_at": time.monotonic() + max(0.1, self._deferred_attention_config("attention_deferred_backoff_sec", 1.0)),
            "expires_at": now + max(1.0, self._deferred_attention_config("attention_deferred_ttl_sec", 300.0)),
            "attempts": 0,
            "max_attempts": max(1, int(self._deferred_attention_config("attention_deferred_max_attempts", 3))),
            "reason": str(reason or "queue_timeout"),
            "session_identity": id(session) if session is not None else None,
            "worker_generation": int(getattr(session, "worker_generation", 0) or 0) if session is not None else None,
            "shutdown_generation": int(self._shutdown_generation),
            "turn_thread_id": str(getattr(turn, "thread_id", "") or "") if turn is not None else "",
            "turn_generation": int(getattr(turn, "generation", 0) or 0) if turn is not None else 0,
            "_terminal_status": None,
        }
        self._deferred_attention_work[work_id] = item
        self._schedule_deferred_persist(item)
        self._deferred_attention_counts["total"] += 1
        self._deferred_attention_counts[f"kind:{item['task_name']}"] += 1
        self._deferred_attention_last_enqueued_at = now
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("attention_deferred", True)
            event.set_extra("deferred_work_id", work_id)
            event.set_extra("deferred_reason", item["reason"])
            event.set_extra("deferred_attempt", 0)
        self._ensure_deferred_attention_dispatcher()
        if self._deferred_attention_event is not None:
            self._deferred_attention_event.set()
        logger.info(
            "[Attention] deferred work enqueued "
            f"work_id={work_id} chat_id={chat_id} task={item['task_name']} "
            f"reason={item['reason']} queue_depth={len(self._deferred_attention_work)}"
        )
        return True

    def _record_deferred_transition(
        self,
        item: Any,
        status: str,
        *,
        reason: str | None = None,
    ) -> bool:
        status = str(status or "failed")
        if status not in self._deferred_attention_terminal_statuses:
            status = "failed"
        if isinstance(item, dict):
            previous = item.get("_terminal_status")
            if previous:
                return False
            item["_terminal_status"] = status
            self._schedule_deferred_finish(item, status, reason or "")
        self._deferred_attention_counts[status] += 1
        self._deferred_attention_last_terminal_at = time.time()
        event = item.get("event") if isinstance(item, dict) else None
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("deferred_terminal_status", status)
            if reason:
                event.set_extra("deferred_terminal_reason", str(reason))
        self._deferred_attention_dispatcher_last_progress_at = time.time()
        task_name = str(item.get("task_name", "attention.misc") or "attention.misc") if isinstance(item, dict) else "attention.misc"
        work_id = str(item.get("work_id", "") or "") if isinstance(item, dict) else ""
        level = logger.info if status in {"replayed", "skipped_already_terminal"} else logger.warning
        if status in {"failed", "exhausted"}:
            level = logger.error
        level(
            "[Attention] deferred work terminal "
            f"work_id={work_id} task={task_name} status={status} "
            f"reason={reason or ''} queue_depth={len(self._deferred_attention_work)}"
        )
        return True

    def _set_deferred_terminal(
        self,
        item: Any,
        status: str,
        *,
        reason: str | None = None,
    ) -> None:
        self._record_deferred_transition(item, status, reason=reason)

    @staticmethod
    def _deferred_enqueued_value(item: Any, default: float) -> float:
        if not isinstance(item, dict):
            return float(default)
        try:
            return float(item.get("enqueued_at", default) or default)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _deferred_next_retry_value(item: Any) -> float:
        if not isinstance(item, dict):
            return 0.0
        try:
            return float(item.get("next_retry_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _deferred_replay_status(self, item: dict[str, Any]) -> str | None:
        if self._workers_shutdown:
            return "shutdown"
        if int(item.get("shutdown_generation", self._shutdown_generation) or 0) != int(self._shutdown_generation):
            return "shutdown"
        event = item.get("event")
        if event is not None and hasattr(event, "get_extra"):
            status = str(event.get_extra("astrmai_execution_status", "") or "")
            if status in {"sent", "completed", "stale_drop", "shutdown_rejected", "cancelled"}:
                if status in {"sent", "completed"}:
                    return "skipped_already_terminal"
                if status == "stale_drop":
                    return "stale"
                if status == "shutdown_rejected":
                    return "shutdown"
                return "cancelled"
            if bool(event.get_extra("astrmai_reply_sent", False)):
                return "skipped_already_terminal"
            if bool(event.get_extra("astrmai_system2_failure_handled", False)):
                return "skipped_already_terminal"
            if bool(event.get_extra("astrmai_proactive_completed", False)):
                return "skipped_already_terminal"
            if bool(event.get_extra("astrmai_event_cancelled", False)):
                return "cancelled"
            turn = event.get_extra("astrmai_turn_identity", None)
            expected_thread = str(item.get("turn_thread_id", "") or "")
            expected_generation = int(item.get("turn_generation", 0) or 0)
            if expected_generation > 0 and turn is None:
                return "stale"
            if turn is not None and expected_thread:
                if str(getattr(turn, "thread_id", "") or "") != expected_thread or int(getattr(turn, "generation", 0) or 0) != expected_generation:
                    return "superseded"
            elif turn is not None and expected_generation > 0:
                if int(getattr(turn, "generation", 0) or 0) != expected_generation:
                    return "superseded"
        session_identity = item.get("session_identity")
        if session_identity is not None:
            session = self.focus_pools.get(str(item.get("chat_id", "") or ""))
            if session is None or id(session) != int(session_identity):
                return "stale"
            if bool(getattr(session, "closed", False)):
                return "stale"
            expected_generation = item.get("worker_generation")
            if expected_generation is not None and int(getattr(session, "worker_generation", 0) or 0) != int(expected_generation):
                return "superseded"
        return None

    async def _dispatch_deferred_attention_work(self) -> None:
        while not self._workers_shutdown:
            if not self._deferred_attention_work:
                event = self._deferred_attention_event or asyncio.Event()
                self._deferred_attention_event = event
                event.clear()
                await event.wait()
                continue
            now = time.time()
            item_key, item = min(
                self._deferred_attention_work.items(),
                key=lambda pair: self._deferred_next_retry_value(pair[1]),
            )
            work_id = item.get("work_id")
            retry_factory = item.get("retry_factory")
            if not work_id or not callable(retry_factory):
                # Defensive handling for corrupted in-memory entries.  A bad
                # record must not terminate the dispatcher and strand all
                # following deferred work.
                self._deferred_attention_work.pop(item_key, None)
                self._set_deferred_terminal(item, "failed")
                continue
            try:
                replay_status = self._deferred_replay_status(item)
            except Exception as exc:
                logger.warning("[Attention] malformed deferred metadata; dropping item: %r", exc)
                replay_status = "failed"
            if replay_status is not None:
                self._deferred_attention_work.pop(item_key, None)
                self._set_deferred_terminal(item, replay_status)
                continue
            try:
                expires_at = float(item.get("expires_at", now) or now)
            except (TypeError, ValueError):
                expires_at = now
                item["expires_at"] = now
            if now >= expires_at:
                self._deferred_attention_work.pop(item_key, None)
                self._set_deferred_terminal(item, "expired", reason="ttl_expired")
                continue
            try:
                next_retry_at = float(item.get("next_retry_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                next_retry_at = 0.0
                item["next_retry_at"] = 0.0
            delay = max(0.0, next_retry_at - time.monotonic())
            if delay > 0:
                if not item.get("_delay_observed"):
                    item["_delay_observed"] = True
                    self._deferred_attention_counts["delayed"] += 1
                    logger.debug(
                        "[Attention] deferred replay delayed "
                        f"work_id={work_id} delay_sec={delay:.3f} "
                        f"queue_depth={len(self._deferred_attention_work)}"
                    )
                try:
                    await asyncio.sleep(min(delay, 1.0))
                except asyncio.CancelledError:
                    raise
                continue
            try:
                item["attempts"] = int(item.get("attempts", 0) or 0) + 1
                max_attempts = max(1, int(item.get("max_attempts", 1) or 1))
            except (TypeError, ValueError):
                item["attempts"] = 1
                max_attempts = 1
                item["max_attempts"] = 1
            self._deferred_attention_counts["replay_attempts"] += 1
            self._deferred_attention_dispatcher_last_progress_at = time.time()
            logger.info(
                "[Attention] deferred replay started "
                f"work_id={work_id} chat_id={item.get('chat_id', '')} "
                f"task={item.get('task_name', 'attention.misc')} "
                f"attempt={item['attempts']} max_attempts={max_attempts} "
                f"queue_depth={len(self._deferred_attention_work)}"
            )
            try:
                await self._run_background_task(
                    retry_factory(),
                    item.get("event"),
                    task_name=item.get("task_name", "attention.misc"),
                    retry_factory=retry_factory,
                    _deferred_replay=True,
                )
                self._deferred_attention_work.pop(item_key, None)
                self._deferred_attention_counts["succeeded"] += 1
                event = item.get("event")
                if event is not None and hasattr(event, "set_extra"):
                    event.set_extra("deferred_replayed_at", time.time())
                self._set_deferred_terminal(item, "replayed", reason="replay_succeeded")
            except (BackgroundTaskQueueTimeout, asyncio.TimeoutError):
                if item["attempts"] >= max_attempts:
                    self._deferred_attention_work.pop(item_key, None)
                    self._set_deferred_terminal(item, "exhausted", reason="replay_attempts_exhausted")
                else:
                    backoff = min(15.0, 1.0 * (2 ** (item["attempts"] - 1)))
                    item["next_retry_at"] = time.monotonic() + backoff
                    self._schedule_deferred_persist(item)
                    self._deferred_attention_dispatcher_last_progress_at = time.time()
                    logger.warning(
                        "[Attention] deferred replay delayed after queue admission failure "
                        f"work_id={work_id} attempt={item['attempts']} max_attempts={max_attempts} "
                        f"queue_depth={len(self._deferred_attention_work)}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._deferred_attention_work.pop(item_key, None)
                self._set_deferred_terminal(
                    item,
                    "failed",
                    reason=f"{type(exc).__name__}: {exc}"[:500],
                )

    async def _run_background_task(
        self,
        coro,
        event=None,
        *,
        task_name: str = "attention.misc",
        retry_factory=None,
        _deferred_replay: bool = False,
    ):
        started = False
        deferred = False

        async def _execute() -> Any:
            nonlocal started
            started = True
            return await coro

        budget = getattr(self, "background_task_budget", None)

        async def _after_slot() -> Any:
            if budget is not None:
                scope_id = str(getattr(event, "unified_msg_origin", "") or "")
                try:
                    budget_parameters = inspect.signature(budget.run).parameters
                except (TypeError, ValueError):
                    budget_parameters = {}
                supports_scope = "scope_id" in budget_parameters
                supports_acquired_callback = "on_acquired" in budget_parameters
                supports_wait_timeout = "wait_timeout_sec" in budget_parameters or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD for item in budget_parameters.values()
                )
                budget_wait_stage = None
                budget_acquired = False

                def _on_budget_acquired() -> None:
                    nonlocal budget_acquired
                    budget_acquired = True
                    finish_stage(
                        event,
                        budget_wait_stage,
                        metadata={"task_name": task_name, "scope_id": scope_id},
                    )

                run_kwargs = {"task_name": task_name}
                # Background attention is non-critical to the primary reply;
                # cap queue admission independently from the global budget
                # so saturation still degrades within a bounded 30s window.
                if str(task_name or "").startswith("attention.") and supports_wait_timeout:
                    budget_timeout = min(
                        30.0,
                        max(0.1, float(getattr(budget, "wait_timeout_sec", 120.0) or 120.0)),
                    )
                    if admission_deadline is not None:
                        budget_timeout = min(
                            budget_timeout,
                            max(0.0, float(admission_deadline) - time.monotonic()),
                        )
                    run_kwargs["wait_timeout_sec"] = budget_timeout
                if supports_scope:
                    run_kwargs["scope_id"] = scope_id
                if supports_acquired_callback:
                    budget_wait_stage = begin_stage(
                        event,
                        "attention.background_budget_wait",
                        critical_path=True,
                        metadata={"task_name": task_name, "scope_id": scope_id},
                    )
                    run_kwargs["on_acquired"] = _on_budget_acquired
                try:
                    budget_awaitable = budget.run(_execute, **run_kwargs)
                    if admission_deadline is not None:
                        remaining = max(0.0, float(admission_deadline) - time.monotonic())
                        if remaining <= 0.0:
                            if hasattr(budget_awaitable, "close"):
                                budget_awaitable.close()
                            raise BackgroundTaskQueueTimeout("attention admission deadline exhausted")
                        return await asyncio.wait_for(budget_awaitable, timeout=remaining)
                    return await budget_awaitable
                except BackgroundTaskQueueTimeout:
                    if budget_wait_stage and not budget_acquired:
                        finish_stage(
                            event,
                            budget_wait_stage,
                            status="timeout",
                            reason="queue_timeout",
                            metadata={"task_name": task_name, "scope_id": scope_id},
                        )
                    if event is not None and hasattr(event, "set_extra"):
                        event.set_extra("astrmai_execution_status", "background_queue_timeout")
                        event.set_extra(
                            "astrmai_queue_timeout_stage",
                            "attention.background_budget_wait",
                        )
                        await self._finalize_pre_planner_turn(
                            event,
                            scope_id,
                            status="background_queue_timeout",
                        )
                    raise
                except BackgroundTaskQueueFull:
                    if budget_wait_stage and not budget_acquired:
                        finish_stage(
                            event,
                            budget_wait_stage,
                            status="rejected",
                            reason="queue_full",
                            metadata={"task_name": task_name, "scope_id": scope_id},
                        )
                    if event is not None and hasattr(event, "set_extra"):
                        event.set_extra("astrmai_execution_status", "background_queue_rejected")
                        event.set_extra(
                            "astrmai_queue_timeout_stage",
                            "attention.background_budget_wait",
                        )
                        await self._finalize_pre_planner_turn(
                            event,
                            scope_id,
                            status="background_queue_rejected",
                        )
                    raise
                except asyncio.TimeoutError:
                    if budget_wait_stage and not budget_acquired:
                        finish_stage(
                            event,
                            budget_wait_stage,
                            status="timeout",
                            reason="queue_timeout",
                            metadata={"task_name": task_name, "scope_id": scope_id},
                        )
                    if event is not None and hasattr(event, "set_extra"):
                        event.set_extra("astrmai_execution_status", "background_queue_timeout")
                        event.set_extra(
                            "astrmai_queue_timeout_stage",
                            "attention.background_budget_wait",
                        )
                        await self._finalize_pre_planner_turn(
                            event,
                            scope_id,
                            status="background_queue_timeout",
                        )
                    raise BackgroundTaskQueueTimeout("attention budget admission deadline exhausted")
                except asyncio.CancelledError:
                    if budget_wait_stage and not budget_acquired:
                        finish_stage(
                            event,
                            budget_wait_stage,
                            status="cancelled",
                            reason="superseded_or_shutdown",
                        )
                    raise
                except Exception as exc:
                    if budget_wait_stage and not budget_acquired:
                        finish_stage(
                            event,
                            budget_wait_stage,
                            status="error",
                            reason=type(exc).__name__,
                        )
                    raise
            return await _execute()

        try:
            if self._should_skip_startup_warmup(event, task_name, replay=_deferred_replay):
                if event is not None and hasattr(event, "set_extra"):
                    event.set_extra("astrmai_execution_status", "startup_warmup_skipped")
                    event.set_extra("astrmai_startup_warmup_skipped", True)
                logger.debug(
                    "[AttentionGate] startup warmup skipped background task "
                    f"task={task_name} remaining_sec={self._startup_warmup_remaining():.1f}"
                )
                return None
            configured_admission_timeout = 30.0
            try:
                configured_admission_timeout = max(
                    0.1,
                    float(
                        getattr(
                            getattr(getattr(self, "config", None), "timing", None),
                            "attention_background_slot_wait_timeout_sec",
                            30.0,
                        )
                        or 30.0
                    ),
                )
            except (TypeError, ValueError):
                pass
            admission_deadline = time.monotonic() + configured_admission_timeout
            slot_wait_and_execute = self._run_background_slot(
                _after_slot,
                event,
                admission_deadline=admission_deadline,
            )
            if event is not None:
                result = await self._run_managed_system2_task(
                    slot_wait_and_execute,
                    event,
                )
            else:
                result = await slot_wait_and_execute
            if (
                not started
                and not _deferred_replay
                and retry_factory is not None
                and event is not None
                and str(event.get_extra("astrmai_execution_status", "") or "")
                in {"background_queue_timeout", "background_queue_rejected"}
            ):
                deferred = self._defer_attention_work(
                    event=event,
                    task_name=task_name,
                    retry_factory=retry_factory,
                    reason=str(
                        "queue_full"
                        if str(event.get_extra("astrmai_execution_status", "") or "")
                        == "background_queue_rejected"
                        else event.get_extra("astrmai_queue_timeout_stage", "queue_timeout")
                        or "queue_timeout"
                    ),
                )
            return result
        except (BackgroundTaskQueueTimeout, BackgroundTaskQueueFull, asyncio.TimeoutError) as exc:
            if not started and not _deferred_replay:
                deferred = self._defer_attention_work(
                    event=event,
                    task_name=task_name,
                    retry_factory=retry_factory,
                    reason="queue_full"
                    if isinstance(exc, BackgroundTaskQueueFull)
                    else type(exc).__name__,
                )
                # A queue admission failure has been classified as deferred
                # (or rejected by the bounded deferred queue); it is not an
                # execution failure and must not trigger a fallback reply.
                if not _deferred_replay:
                    return None
            raise
        finally:
            if not started and hasattr(coro, "close"):
                coro.close()

    def _fire_background_task(self, coro, event=None, *, task_name: str = "attention.misc", retry_factory=None):
        task = asyncio.create_task(
            self._run_background_task(coro, event, task_name=task_name, retry_factory=retry_factory)
        )
        task._astrmai_inner_coro = coro
        task._astrmai_task_name = task_name
        self._background_tasks.add(task)
        self._register_owner_task(
            task,
            task_family=task_name,
            scope_id=str(getattr(event, "unified_msg_origin", "") or "GLOBAL"),
            run_id=f"attention-{new_trace_id()}",
        )
        attach_background_task_trace(
            task,
            event,
            "attention.background",
            metadata={
                "chat_id": str(getattr(event, "unified_msg_origin", "") or ""),
                "task_name": task_name,
            },
        )
        task.add_done_callback(self._handle_task_result)
        return task

    def _fire_priority_task(self, coro, event=None):
        managed_coro = self._run_managed_system2_task(coro, event) if event is not None else coro
        task = asyncio.create_task(managed_coro)
        task._astrmai_inner_coro = coro
        task._astrmai_task_name = "attention.priority"
        self._background_tasks.add(task)
        self._register_owner_task(
            task,
            task_family="attention.priority",
            scope_id=str(getattr(event, "unified_msg_origin", "") or "GLOBAL"),
            run_id=f"attention-priority-{new_trace_id()}",
        )
        attach_background_task_trace(
            task,
            event,
            "attention.priority",
            metadata={"chat_id": str(getattr(event, "unified_msg_origin", "") or "")},
        )
        task.add_done_callback(self._handle_task_result)
        return task

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        if task.cancelled():
            inner_coro = getattr(task, "_astrmai_inner_coro", None)
            if hasattr(inner_coro, "close"):
                inner_coro.close()
            return
        try:
            task.result()
        except Exception as exc:
            if isinstance(exc, (BackgroundTaskQueueTimeout, BackgroundTaskQueueFull)):
                self._record_queue_degradation(task, exc)
                return
            logger.error(f"[Attention Task Error] {exc}", exc_info=exc)

    def _spawn_session_worker(
        self,
        chat_id: str,
        session: SessionContext,
        self_id: str,
        *,
        is_private: bool = False,
        is_strong_wakeup: bool = False,
        event: Any = None,
        generation: int | None = None,
    ):
        current_session = self.focus_pools.get(str(chat_id))
        if (
            self._workers_shutdown
            or session.closed
            or current_session is not session
            or (generation is not None and generation != session.worker_generation)
        ):
            return None
        existing = session.worker_task
        if existing is not None and not existing.done():
            return existing
        session.worker_token += 1
        worker_token = session.worker_token
        task = asyncio.create_task(
            self._debounce_and_judge(
                chat_id,
                session,
                self_id,
                is_private=is_private,
                is_strong_wakeup=is_strong_wakeup,
                worker_event=event,
            )
        )
        task._worker_context = SimpleNamespace(
            chat_id=chat_id,
            session=session,
            self_id=self_id,
            event=event,
            generation=session.worker_generation,
            token=worker_token,
        )
        session.worker_task = task
        self._session_tasks.add(task)
        self._register_owner_task(
            task,
            task_family="attention.session_worker",
            scope_id=str(chat_id or "GLOBAL"),
            run_id=f"attention-worker-{chat_id}-{worker_token}",
        )
        attach_background_task_trace(
            task,
            event,
            "attention.session_worker",
            metadata={"chat_id": str(chat_id or "")},
        )
        task.add_done_callback(self._handle_session_worker_result)
        return task

    def _handle_session_worker_result(self, task: asyncio.Task):
        self._session_tasks.discard(task)
        worker_context = getattr(task, "_worker_context", None)
        session = getattr(worker_context, "session", None)
        raw_generation = getattr(worker_context, "generation", None)
        raw_token = getattr(worker_context, "token", None)
        identity_valid = bool(
            session is not None
            and session.worker_task is task
            and (raw_generation is None or int(raw_generation) == session.worker_generation)
            and (raw_token is None or int(raw_token) == session.worker_token)
        )
        # A few legacy tests construct a task context by hand.  Preserve their
        # recovery semantics, while real workers always use the strict triple.
        legacy_context = bool(
            session is not None
            and session.worker_task is None
            and raw_generation is None
            and raw_token is None
        )
        current_worker = identity_valid or legacy_context
        if session is not None and raw_token is not None and not identity_valid:
            self._worker_invariant_violation_count += 1
        if identity_valid:
            session.worker_task = None
        if session is not None and session.closed:
            session.is_evaluating = False
        if task.cancelled():
            if session is not None and current_worker and not session.closed:
                session.is_evaluating = False
            if current_worker and worker_context is not None and raw_token is not None:
                recovery = asyncio.create_task(self._recover_failed_session_worker(worker_context))
                self._background_tasks.add(recovery)
                self._register_owner_task(
                    recovery,
                    task_family="attention.session_worker.recovery",
                    scope_id=str(getattr(worker_context, "chat_id", "") or "GLOBAL"),
                    run_id=f"attention-recovery-{new_trace_id()}",
                )
                recovery.add_done_callback(self._handle_background_task_result)
            return
        try:
            task.result()
        except Exception as exc:
            logger.error(f"[Attention Worker Error] {exc}", exc_info=exc)
            if current_worker and worker_context is not None:
                recovery = asyncio.create_task(self._recover_failed_session_worker(worker_context))
                self._background_tasks.add(recovery)
                self._register_owner_task(
                    recovery,
                    task_family="attention.session_worker.recovery",
                    scope_id=str(getattr(worker_context, "chat_id", "") or "GLOBAL"),
                    run_id=f"attention-recovery-{new_trace_id()}",
                )
                attach_background_task_trace(
                    recovery,
                    getattr(worker_context, "event", None),
                    "attention.session_worker_recovery",
                    metadata={"chat_id": str(getattr(worker_context, "chat_id", "") or "")},
                )
                recovery.add_done_callback(self._handle_background_task_result)

    async def _recover_failed_session_worker(self, worker_context):
        chat_id = str(getattr(worker_context, "chat_id", "") or "")
        session = getattr(worker_context, "session", None)
        self_id = str(getattr(worker_context, "self_id", "") or "")
        if not chat_id or session is None:
            return
        raw_generation = getattr(worker_context, "generation", None)
        generation = session.worker_generation if raw_generation is None else int(raw_generation)
        raw_token = getattr(worker_context, "token", None)
        token = session.worker_token if raw_token is None else int(raw_token)
        if (
            self._workers_shutdown
            or session.closed
            or self.focus_pools.get(chat_id) is not session
            or generation != session.worker_generation
            or (raw_token is not None and token != session.worker_token)
            or (
                session.worker_task is not None
                and not session.worker_task.done()
                and (raw_token is None or token != session.worker_token)
            )
        ):
            return
        async with session.lock:
            if (
                self._workers_shutdown
                or session.closed
                or self.focus_pools.get(chat_id) is not session
                or generation != session.worker_generation
                or (raw_token is not None and token != session.worker_token)
                or (
                    session.worker_task is not None
                    and not session.worker_task.done()
                    and session.worker_task is not asyncio.current_task()
                )
            ):
                return
            has_pending = bool(session.accumulation_pool)
            session.is_evaluating = False
            if has_pending:
                session.is_evaluating = True
                self._spawn_session_worker(
                    chat_id,
                    session,
                    self_id,
                    event=getattr(worker_context, "event", None),
                    generation=generation,
                )

    def _handle_background_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        if task.cancelled():
            inner_coro = getattr(task, "_astrmai_inner_coro", None)
            if hasattr(inner_coro, "close"):
                inner_coro.close()
            return
        exc = task.exception()
        if exc is not None:
            if isinstance(exc, (BackgroundTaskQueueTimeout, BackgroundTaskQueueFull)):
                self._record_queue_degradation(task, exc)
                return
            logger.error(f"[AttentionGate] background task failed: {exc}", exc_info=(type(exc), exc, exc.__traceback__))

    def _schedule_compaction_task(self, chat_id: str, focus_context) -> asyncio.Task | None:
        if self.context_compaction is None:
            return None
        def _factory():
            return self.context_compaction.schedule_compaction_evaluation(
                chat_id,
                focus_context=focus_context,
                message_source="user",
            )
        return self._fire_background_task(
            _factory(),
            task_name="attention.compaction",
            retry_factory=_factory,
        )

    def _judge_ignore_cooldown_enabled(self) -> bool:
        attention_cfg = getattr(self.config, "attention", None)
        return bool(getattr(attention_cfg, "judge_ignore_focus_cooldown_enabled", True))

    def _mood_post_judge_enabled(self) -> bool:
        attention_cfg = getattr(self.config, "attention", None)
        return bool(getattr(attention_cfg, "mood_post_judge_enabled", True))

    def _private_skip_judge_enabled(self) -> bool:
        attention_cfg = getattr(self.config, "attention", None)
        return bool(getattr(attention_cfg, "private_skip_judge_enabled", True))

    @staticmethod
    def _has_semantic_topic_text(text: str) -> bool:
        normalized = " ".join(str(text or "").strip().split())
        if not normalized:
            return False
        return any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in normalized)

    @staticmethod
    def _is_pure_text_activity_event(event: AstrMessageEvent) -> bool:
        message = getattr(getattr(event, "message_obj", None), "message", None)
        if not message:
            return bool(str(getattr(event, "message_str", "") or "").strip())
        component_types = {
            str(getattr(component, "type", component.__class__.__name__)).lstrip("_").lower()
            for component in message
        }
        return bool(str(getattr(event, "message_str", "") or "").strip()) and component_types <= {
            "plain",
            "text",
            "reply",
            "at",
        }

    def _classify_topic_activity(
        self,
        chat_id: str,
        event: AstrMessageEvent,
        sender_id: str,
        *,
        is_private: bool,
    ) -> dict[str, Any]:
        result = {
            "valid": False,
            "kind": "",
            "reason": "",
            "source": "human_message",
            "effective_response": False,
            "preview": "",
        }
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            result["source"] = "bot_proactive_event"
            result["reason"] = "proactive_event"
            return result
        if bool(event.get_extra("astrmai_is_external_bot_reply", False)):
            result["source"] = "external_bot_reply"
            result["reason"] = "external_bot_reply"
            return result
        if bool(event.get_extra("astrmai_is_command", False)) or bool(event.get_extra("heartflow_is_command", False)):
            result["reason"] = "command"
            return result
        if not self._is_pure_text_activity_event(event):
            result["reason"] = "non_plain_text"
            return result

        projection = MessageRenderer.project_topic_preview(event, max_chars=120)
        result["preview"] = str(projection.text or "")
        if not projection.safe:
            result["reason"] = str(projection.rejected_reason or "unsafe_topic_preview")
            return result
        if not self._has_semantic_topic_text(projection.text):
            result["reason"] = "nonsemantic_topic_text"
            return result

        self_id = get_event_self_id(event)
        is_at_bot = self._is_at_bot_event(event, self_id)
        is_reply_to_bot = self._is_reply_to_bot_event(event, self_id)
        result["effective_response"] = bool(is_private or is_at_bot or is_reply_to_bot)
        conversation_continuity = getattr(self, "conversation_continuity", None)
        if is_private:
            result["kind"] = "private_message"
            result["reason"] = "private_semantic_message"
        elif conversation_continuity is not None and hasattr(
            conversation_continuity, "evaluate_group_message"
        ):
            try:
                policy = conversation_continuity.evaluate_group_message(
                    chat_id,
                    projection.text,
                    sender_id=sender_id,
                    has_reply_reference=is_reply_to_bot,
                )
                rotation_reason = str(getattr(policy, "rotation_reason", "") or "")
                if str(getattr(policy, "history_mode", "") or "") == "current_topic":
                    result["kind"] = "continuing"
                elif rotation_reason == "explicit_topic_switch":
                    result["kind"] = "switched"
                else:
                    result["kind"] = "new"
                result["reason"] = rotation_reason or ",".join(
                    str(item) for item in getattr(policy, "continuity_evidence", ()) or ()
                ) or "semantic_topic_message"
            except Exception as exc:
                logger.debug(f"[AttentionGate] topic activity classification degraded for {chat_id}: {exc}")
                result["kind"] = "message"
                result["reason"] = "continuity_classifier_degraded"
        else:
            result["kind"] = "message"
            result["reason"] = "semantic_topic_message"
        result["valid"] = True
        return result

    def _set_topic_activity_observation(
        self,
        chat_id: str,
        event: AstrMessageEvent,
        sender_id: str,
        *,
        is_private: bool,
    ) -> dict[str, Any]:
        is_anonymous_sender = str(sender_id or "").startswith("80000000")
        activity_sender_id = "" if is_anonymous_sender else sender_id
        provenance = str(event.get_extra("astrmai_event_provenance", "original") or "original")
        is_real_user_activity = bool(
            activity_sender_id
            and activity_sender_id != str(getattr(self.state_engine, "bot_id", "") or "")
            and activity_sender_id != str(get_event_self_id(event) or "")
            and not str(activity_sender_id).startswith("astrmai_")
            and not bool(event.get_extra("astrmai_is_proactive_event", False))
            and not bool(event.get_extra("astrmai_is_external_bot_reply", False))
            and provenance == "original"
        )
        topic_activity = (
            self._classify_topic_activity(
                chat_id,
                event,
                activity_sender_id,
                is_private=is_private,
            )
            if is_real_user_activity
            else {
                "valid": False,
                "kind": "",
                "reason": "non_user_source",
                "source": (
                    "bot_proactive_event"
                    if bool(event.get_extra("astrmai_is_proactive_event", False))
                    else "non_user_source"
                ),
                "effective_response": False,
                "preview": "",
            }
        )
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            topic_activity["reason"] = "proactive_event"
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_topic_activity_valid", bool(topic_activity.get("valid", False)))
            event.set_extra("astrmai_topic_activity_kind", str(topic_activity.get("kind", "") or ""))
            event.set_extra("astrmai_topic_activity_reason", str(topic_activity.get("reason", "") or ""))
            event.set_extra("astrmai_topic_activity_source", str(topic_activity.get("source", "") or ""))
            event.set_extra("astrmai_topic_activity_preview", str(topic_activity.get("preview", "") or ""))
            event.set_extra("astrmai_effective_user_response", bool(topic_activity.get("effective_response", False)))
            event.set_extra("astrmai_topic_activity_state_transition_status", "not_applicable")
            event.set_extra("astrmai_topic_activity_state_transition_error", "")
        return {
            "is_real_user_activity": is_real_user_activity,
            "activity_sender_id": activity_sender_id,
            "topic_activity": topic_activity,
        }

    async def _record_event_activity(self, chat_id: str, event: AstrMessageEvent, sender_id: str) -> float:
        session = await self._get_or_create_session(chat_id)
        now = time.time()
        observation = self._set_topic_activity_observation(
            chat_id,
            event,
            sender_id,
            is_private=not bool(event.get_group_id()),
        )
        is_real_user_activity = bool(observation["is_real_user_activity"])
        activity_sender_id = str(observation["activity_sender_id"] or "")
        topic_activity = observation["topic_activity"]
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_timestamp", now)
            ensure_turn_context(event).perception.timestamp = now
        if is_real_user_activity and bool(topic_activity.get("valid", False)):
            session.last_active_user_time = now
            recorder = getattr(self.state_engine, "record_real_user_activity", None)
            if callable(recorder):
                event.set_extra("astrmai_topic_activity_state_transition_status", "pending")
                state_loader = getattr(self.state_engine, "get_state", None)
                transition_recorder = getattr(
                    self.state_engine,
                    "record_real_user_activity_transition",
                    None,
                )
                transition_applied = True
                try:
                    if callable(transition_recorder):
                        transition_kwargs = {
                            "chat_kind": "group" if bool(event.get_group_id()) else "private",
                            "occurred_at": now,
                        }
                        try:
                            transition_parameters = inspect.signature(transition_recorder).parameters.values()
                            supports_effective_response = any(
                                parameter.kind is inspect.Parameter.VAR_KEYWORD
                                or parameter.name == "effective_response"
                                for parameter in transition_parameters
                            )
                        except (TypeError, ValueError):
                            supports_effective_response = True
                        if supports_effective_response:
                            transition_kwargs["effective_response"] = bool(
                                topic_activity.get("effective_response", False)
                            )
                        transition = await transition_recorder(chat_id, **transition_kwargs)
                        applied = bool(getattr(transition, "applied", True))
                        transition_reason = str(getattr(transition, "reason", "") or "")
                        state_before, state_after = transition
                        event.set_extra(
                            "astrmai_topic_activity_state_before",
                            self._proactive_activity_snapshot(state_before),
                        )
                        event.set_extra(
                            "astrmai_topic_activity_state_after",
                            self._proactive_activity_snapshot(state_after),
                        )
                        if not applied:
                            event.set_extra(
                                "astrmai_topic_activity_state_transition_status",
                                "superseded",
                            )
                            event.set_extra(
                                "astrmai_topic_activity_state_transition_error",
                                transition_reason or "generation_invalidated",
                            )
                            transition_applied = False
                    else:
                        if callable(state_loader):
                            try:
                                state_before = state_loader(chat_id)
                                if inspect.isawaitable(state_before):
                                    state_before = await state_before
                                snapshot_before = self._proactive_activity_snapshot(state_before)
                                if snapshot_before:
                                    event.set_extra("astrmai_topic_activity_state_before", snapshot_before)
                            except Exception as exc:
                                logger.warning(
                                    f"[AttentionGate] proactive state snapshot before degraded for {chat_id}: {exc}"
                                )
                        try:
                            state = await recorder(
                                chat_id,
                                chat_kind="group" if bool(event.get_group_id()) else "private",
                                occurred_at=now,
                                effective_response=bool(topic_activity.get("effective_response", False)),
                            )
                        except TypeError:
                            state = await recorder(
                                chat_id,
                                chat_kind="group" if bool(event.get_group_id()) else "private",
                                occurred_at=now,
                            )
                        if state is not None:
                            event.set_extra(
                                "astrmai_topic_activity_state_after",
                                self._proactive_activity_snapshot(state),
                            )
                    if transition_applied:
                        event.set_extra("astrmai_proactive_generation_invalidated", True)
                        event.set_extra("astrmai_topic_activity_state_transition_status", "persisted")
                except Exception as exc:
                    event.set_extra("astrmai_topic_activity_state_transition_status", "failed")
                    event.set_extra("astrmai_topic_activity_state_transition_error", type(exc).__name__)
                    logger.warning(f"[AttentionGate] proactive user watermark degraded for {chat_id}: {exc}")
            else:
                event.set_extra("astrmai_topic_activity_state_transition_status", "unavailable")
        if is_real_user_activity and self.runtime_coordinator and hasattr(self.runtime_coordinator, "mark_activity"):
            try:
                # ingress 时刻 astrmai_thread_signature 尚未生成（focus 构建后才写入），
                # 必须用 turn thread id（message_entry 已绑定）保证与 freshness 检查同一标识空间
                thread_identity = ""
                if hasattr(event, "get_extra"):
                    thread_identity = str(event.get_extra("astrmai_turn_thread_id", "") or "").strip()
                if not thread_identity:
                    try:
                        thread_identity = str(resolve_group_thread(event, chat_id).thread_id or "")
                    except Exception:
                        thread_identity = ""
                is_direct = bool(
                    event.get_extra("astrmai_group_direct_wakeup", False)
                    or event.get_extra("is_private_chat", False)
                )
                if not is_direct:
                    try:
                        bot_id = str(getattr(self.state_engine, "bot_id", "") or "")
                        is_direct = bool(
                            self._is_at_bot_event(event, bot_id)
                            or self._is_reply_to_bot_event(event, bot_id)
                        )
                    except Exception:
                        is_direct = False
                try:
                    watermark = await self.runtime_coordinator.mark_activity(
                        chat_id,
                        now,
                        activity_sender_id,
                        event.get_sender_name() if hasattr(event, "get_sender_name") else "",
                        str(getattr(event, "message_str", "") or ""),
                        thread_identity,
                        event_id=self._build_message_id(event),
                        is_direct=is_direct,
                    )
                except TypeError:
                    watermark = await self.runtime_coordinator.mark_activity(
                        chat_id,
                        now,
                        activity_sender_id,
                        event.get_sender_name() if hasattr(event, "get_sender_name") else "",
                        str(getattr(event, "message_str", "") or ""),
                        thread_identity,
                    )
                if hasattr(event, "set_extra"):
                    event.set_extra("astrmai_group_activity_watermark", int(watermark or 0))
            except Exception as exc:
                logger.debug(f"[AttentionGate] runtime activity mark degraded: {exc}")
        return now

    async def _ensure_turn_identity(self, event: AstrMessageEvent, chat_id: str, is_private: bool) -> None:
        if event.get_extra("astrmai_turn_identity", None) is not None:
            return
        conversation_config = getattr(self.config, "conversation", None)
        if not bool(getattr(conversation_config, "conversation_generation_enabled", True)):
            return
        coordinator = self.runtime_coordinator
        if coordinator is None or not hasattr(coordinator, "advance_generation"):
            return
        mode = "private" if is_private else "group"
        if not is_private and bool(getattr(conversation_config, "group_thread_wait_enabled", True)):
            resolution = resolve_group_thread(event, chat_id)
            thread_id = resolution.thread_id
            event.set_extra("astrmai_group_thread_source", resolution.source)
            event.set_extra("astrmai_group_thread_confidence", resolution.confidence)
        else:
            thread_id = build_p0_thread_id(mode, chat_id)
        generation = await coordinator.advance_generation(chat_id, thread_id)
        created_at = time.time()
        turn = TurnIdentity(
            mode=mode,
            chat_id=chat_id,
            thread_id=thread_id,
            generation=generation,
            sender_id=str(event.get_sender_id() or ""),
            input_message_ids=tuple(item for item in (self._build_message_id(event),) if item),
            created_at=created_at,
        )
        event.set_extra("astrmai_turn_identity", turn)
        event.set_extra("astrmai_turn_mode", mode)
        event.set_extra("astrmai_turn_thread_id", thread_id)
        event.set_extra("astrmai_turn_generation", generation)
        event.set_extra("astrmai_turn_created_at", created_at)

    def _bind_private_batch_turn(self, focus_event, batch_events: list[AstrMessageEvent], chat_id: str) -> None:
        if not batch_events:
            return

        focus_event.set_extra("is_private_chat", True)
        focus_event.set_extra("astrmai_turn_mode", "private")
        latest_timestamp = max(
            float(event.get_extra("astrmai_timestamp", getattr(event, "timestamp", 0.0)) or 0.0)
            for event in batch_events
        )
        previous_timestamp = float(
            focus_event.get_extra("astrmai_timestamp", getattr(focus_event, "timestamp", 0.0)) or 0.0
        )
        if previous_timestamp != latest_timestamp:
            focus_event.set_extra("astrmai_focus_original_timestamp", previous_timestamp)
        focus_event.set_extra("astrmai_timestamp", latest_timestamp)
        focus_event.set_extra("astrmai_private_batch_latest_ts", latest_timestamp)

        turns = [
            event.get_extra("astrmai_turn_identity", None)
            for event in batch_events
            if event.get_extra("astrmai_turn_identity", None) is not None
        ]
        if not turns:
            return
        latest_turn = max(
            turns,
            key=lambda turn: (int(getattr(turn, "generation", 0) or 0), float(getattr(turn, "created_at", 0.0) or 0.0)),
        )
        message_ids: list[str] = []
        for event in batch_events:
            turn = event.get_extra("astrmai_turn_identity", None)
            turn_ids = tuple(getattr(turn, "input_message_ids", ()) or ())
            candidates = turn_ids or (self._build_message_id(event),)
            for message_id in candidates:
                normalized_id = str(message_id or "").strip()
                if normalized_id and normalized_id not in message_ids:
                    message_ids.append(normalized_id)

        merged_turn = TurnIdentity(
            mode="private",
            chat_id=str(getattr(latest_turn, "chat_id", "") or chat_id),
            thread_id=str(
                getattr(latest_turn, "thread_id", "")
                or build_p0_thread_id("private", chat_id)
            ),
            generation=int(getattr(latest_turn, "generation", 0) or 0),
            sender_id=str(focus_event.get_sender_id() or getattr(latest_turn, "sender_id", "") or ""),
            input_message_ids=tuple(message_ids),
            created_at=float(getattr(latest_turn, "created_at", 0.0) or latest_timestamp),
        )
        focus_event.set_extra("astrmai_turn_identity", merged_turn)
        focus_event.set_extra("astrmai_turn_thread_id", merged_turn.thread_id)
        focus_event.set_extra("astrmai_turn_generation", merged_turn.generation)
        focus_event.set_extra("astrmai_turn_created_at", merged_turn.created_at)
        burst_basis = f"{chat_id}|{merged_turn.generation}|{'|'.join(message_ids)}"
        focus_event.set_extra(
            "astrmai_private_burst_id",
            hashlib.sha256(burst_basis.encode("utf-8", errors="ignore")).hexdigest()[:16],
        )

    async def _handle_system2_failure(self, event: AstrMessageEvent, exc: Exception) -> None:
        logger.error(f"[Attention Task Error] {exc}", exc_info=exc)
        coordinator = self.runtime_coordinator
        turn = event.get_extra("astrmai_turn_identity", None)
        if coordinator is not None and hasattr(coordinator, "is_current_turn"):
            if not await coordinator.is_current_turn(turn):
                return
        if event.get_extra("astrmai_system2_failure_handled", False):
            return
        event.set_extra("astrmai_system2_failure_handled", True)
        reply_sent = bool(event.get_extra("astrmai_reply_sent", False))
        is_proactive = bool(event.get_extra("astrmai_is_proactive_event", False))
        if not reply_sent and not is_proactive and hasattr(event, "send") and hasattr(event, "plain_result"):
            fallback_text = str(
                getattr(getattr(self.config, "reply", None), "fallback_text", "")
                or "（陷入了短暂的沉默...）"
            )
            try:
                if not outbound_send_allowed(event):
                    return
                await event.send(event.plain_result(fallback_text))
                event.set_extra("astrmai_reply_sent", True)
                reply_sent = True
            except Exception as send_exc:
                logger.error(f"[AttentionGate] failed to send System2 fallback: {send_exc}")
        callback = event.get_extra("astrmai_proactive_completion_callback", None)
        if callback is not None and not event.get_extra("astrmai_proactive_completed", False):
            event.set_extra("astrmai_proactive_completed", True)
            try:
                result = callback(reply_sent, "")
                if inspect.isawaitable(result):
                    await result
            except Exception as callback_exc:
                logger.error(f"[AttentionGate] proactive completion callback failed: {callback_exc}")

    async def _record_dialogue_segment_from_event(self, chat_id: str, event: AstrMessageEvent) -> None:
        store = getattr(self, "dialogue_store", None)
        if not store:
            return
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if sender_id.startswith("80000000"):
            return
        try:
            canonical = self._get_or_build_conversation_event(event)
            await store.append_conversation_event(canonical)
            event.set_extra("astrmai_conversation_event_write_status", "stored")
        except Exception as exc:
            event.set_extra("astrmai_conversation_event_write_status", "degraded")
            event.set_extra("astrmai_conversation_event_write_error", type(exc).__name__)
            logger.debug(f"[AttentionGate] dialogue segment record degraded: {exc}")

    def _ensure_global_msg_cache(self):
        if not isinstance(self._global_message_cache, collections.OrderedDict):
            self._global_message_cache = collections.OrderedDict()
        return self._global_message_cache

    def _build_message_id(self, event: AstrMessageEvent):
        canonical = event.get_extra("astrmai_conversation_event", None)
        canonical_event_id = str(getattr(canonical, "event_id", "") or "")
        if canonical_event_id:
            return canonical_event_id
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if message_id:
            return message_id
        sender_id = str(event.get_sender_id() or "")
        timestamp = float(getattr(event, "timestamp", 0.0) or 0.0)
        return f"{sender_id}:{timestamp}:{preview_text(str(getattr(event, 'message_str', '') or ''), 40)}"

    @staticmethod
    def _event_topic_epoch(event: AstrMessageEvent) -> int:
        history_policy = event.get_extra("astrmai_dialog_history_policy", None)
        if history_policy is None:
            return 0
        value = (
            history_policy.get("topic_epoch", 0)
            if isinstance(history_policy, dict)
            else getattr(history_policy, "topic_epoch", 0)
        )
        return max(0, int(value or 0))

    def _get_or_build_conversation_event(self, event: AstrMessageEvent) -> ConversationEvent:
        existing = event.get_extra("astrmai_conversation_event", None)
        if isinstance(existing, ConversationEvent):
            return existing
        bot_id = str(getattr(self.state_engine, "bot_id", "") or "")
        reply_target_id, reply_target_name = self._extract_reply_target(event)
        direct_refs = list(
            event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))
            or []
        )
        extracted_refs = list(
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", []))
            or []
        )
        canonical = ConversationEvent.from_astr_event(
            event,
            self_id=bot_id,
            rich_text=str(event.get_extra("astrmai_rich_text", event.message_str) or ""),
            image_refs=extracted_refs,
            direct_image_refs=direct_refs,
            reply_target_actor_id=reply_target_id,
            reply_target_actor_name=reply_target_name,
            is_at_bot=self._is_at_bot_event(event, bot_id),
            is_reply_to_bot=self._is_reply_to_bot_event(event, bot_id),
            is_direct_wakeup=self._is_direct_wakeup_event(event, bot_id),
            topic_epoch=self._event_topic_epoch(event),
            provenance=str(
                event.get_extra("astrmai_event_provenance", "original") or "original"
            ),
        )
        event.set_extra("astrmai_conversation_event", canonical)
        event.set_extra("astrmai_conversation_event_schema_version", canonical.schema_version)
        event.set_extra("astrmai_conversation_event_id", canonical.event_id)
        event.set_extra("astrmai_conversation_event_id_source", canonical.event_id_source)
        return canonical

    def _build_message_dedup_key(self, event: AstrMessageEvent) -> tuple[str, bool]:
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if message_id:
            return f"message:{chat_id}:{message_id}", False
        sender_id = str(event.get_sender_id() or "")
        content = str(getattr(event, "message_str", "") or "")
        image_refs = (
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", []))
            or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))
            or []
        )
        content = f"{content}\n{repr(image_refs)}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
        return f"fallback:{chat_id}:{sender_id}:{digest}", True

    def _claim_message(self, event: AstrMessageEvent, *, now: float | None = None) -> bool:
        message_cache = self._ensure_global_msg_cache()
        message_key, is_fallback = self._build_message_dedup_key(event)
        current_time = time.time() if now is None else float(now)
        previous_time = message_cache.get(message_key)
        if previous_time is not None:
            if not is_fallback or current_time - float(previous_time) <= self.MESSAGE_DEDUP_FALLBACK_TTL_SECONDS:
                return False
            message_cache.pop(message_key, None)
        message_cache[message_key] = current_time
        while len(message_cache) > 256:
            message_cache.popitem(last=False)
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_message_dedup_key", message_key)
            event.set_extra("astrmai_message_dedup_claimed_at", current_time)
        return True

    def _release_message_claim(self, event: AstrMessageEvent) -> bool:
        message_cache = self._ensure_global_msg_cache()
        message_key = str(
            event.get_extra("astrmai_message_dedup_key", "")
            if hasattr(event, "get_extra")
            else ""
        )
        claimed_at = (
            event.get_extra("astrmai_message_dedup_claimed_at", None)
            if hasattr(event, "get_extra")
            else None
        )
        if not message_key or claimed_at is None:
            return False
        current = message_cache.get(message_key)
        if current is None or float(current) != float(claimed_at):
            return False
        message_cache.pop(message_key, None)
        return True

    async def _append_dialogue_segment(self, event: AstrMessageEvent) -> None:
        store = getattr(self, "dialogue_store", None)
        if store is None:
            return
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not chat_id:
            return
        sender_id = str(event.get_sender_id() or "")
        if sender_id.startswith("80000000"):
            return
        content = str(getattr(event, "message_str", "") or "").strip()
        if not content and not (
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls"))
            or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls"))
        ):
            return
        try:
            bot_id = str(getattr(self.state_engine, "bot_id", "") or "")
            canonical = self._get_or_build_conversation_event(event)
            is_at_bot = canonical.is_at_bot
            is_reply_to_bot = canonical.is_reply_to_bot
            event_id = canonical.event_id
            topic_epoch = canonical.topic_epoch
            create_pending_direct = bool(
                is_at_bot
                or is_reply_to_bot
                or event.get_extra("astrmai_group_direct_wakeup", False)
                or event.get_extra("astrmai_force_engage", False)
            )
            if (
                create_pending_direct
                and ":GroupMessage:" in chat_id
                and is_group_direct_correction(content)
            ):
                superseded_count = await store.supersede_pending_direct_for_actor(
                    chat_id,
                    actor_id=sender_id,
                    superseded_by_event_id=event_id,
                )
                event.set_extra(
                    "astrmai_group_pending_superseded_count",
                    int(superseded_count or 0),
                )
            await store.append_conversation_event(
                canonical,
                create_pending_direct=create_pending_direct,
            )
            event.set_extra("astrmai_conversation_event_write_status", "stored")
            try:
                if ":GroupMessage:" in chat_id:
                    social_signal = classify_group_social_signal(content)
                    is_direct_to_bot = bool(
                        create_pending_direct
                        or event.get_extra("astrmai_group_direct_wakeup", False)
                        or event.get_extra("astrmai_force_engage", False)
                    )
                    if social_signal in {"apology", "reconciliation"} and is_direct_to_bot:
                        await store.resolve_social_incidents(
                            chat_id,
                            actor_id=sender_id,
                            resolution_event_id=event_id,
                            resolution_kind=social_signal,
                        )
                    elif social_signal and is_direct_to_bot:
                        await store.observe_social_incident(
                            chat_id,
                            kind=social_signal,
                            actor_id=sender_id,
                            actor_name=str(event.get_sender_name() or ""),
                            target_id=bot_id if is_at_bot or is_reply_to_bot else "",
                            target_name="Bot" if is_at_bot or is_reply_to_bot else "",
                            evidence_event_id=event_id,
                            topic_epoch=topic_epoch,
                        )
                    event.set_extra("astrmai_group_social_signal", social_signal)
            except Exception as exc:
                event.set_extra("astrmai_conversation_postprocess_status", "degraded")
                event.set_extra("astrmai_conversation_postprocess_error", type(exc).__name__)
                logger.debug(f"[AttentionGate] dialogue postprocess degraded: {exc}")
        except Exception as exc:
            if event.get_extra("astrmai_conversation_event_write_status", "") != "stored":
                event.set_extra("astrmai_conversation_event_write_status", "degraded")
                event.set_extra("astrmai_conversation_event_write_error", type(exc).__name__)
            logger.debug(f"[AttentionGate] dialogue segment append degraded: {exc}")

    async def record_incoming_without_reply(self, event: AstrMessageEvent) -> bool:
        """Persist one inbound event while bypassing the normal reply scheduler."""
        if hasattr(event, "get_extra") and event.get_extra("astrmai_incoming_recorded", False):
            return False
        if not self._claim_message(event):
            return False
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not chat_id or not sender_id:
            self._release_message_claim(event)
            return False
        try:
            await self._record_event_activity(chat_id, event, sender_id)
            await self._append_dialogue_segment(event)
        except Exception as exc:
            self._release_message_claim(event)
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_incoming_record_failed", True)
                event.set_extra("astrmai_incoming_record_error", type(exc).__name__)
            logger.debug(f"[AttentionGate] inbound record failed: {exc}")
            return False
        dialogue_store = getattr(self, "dialogue_store", None)
        write_status = str(
            event.get_extra("astrmai_conversation_event_write_status", "")
            if hasattr(event, "get_extra")
            else ""
        )
        if dialogue_store is not None and write_status == "degraded":
            self._release_message_claim(event)
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_incoming_record_failed", True)
                event.set_extra("astrmai_incoming_record_error", str(event.get_extra("astrmai_conversation_event_write_error", "dialogue_write_failed")))
            return False
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_incoming_recorded", True)
            event.set_extra("astrmai_incoming_record_mode", "without_reply")
        return True

    def _compute_debounce_delay(self, session: SessionContext, is_private: bool, is_strong_wakeup: bool) -> float:
        return self.window_buffer.compute_debounce_delay(session, is_private, is_strong_wakeup)

    async def _wait_for_pending_vision_pairing(self, session: SessionContext) -> None:
        while True:
            async with session.lock:
                sender_ids = {
                    str(getattr(item, "get_sender_id", lambda: "")() or "")
                    for item in session.accumulation_pool
                }
                sender_ids.discard("")
                remaining = self.window_buffer.pending_pair_wait_seconds(
                    session,
                    sender_ids=sender_ids,
                )
                if remaining <= 0:
                    return
                signal = session.vision_pair_signal
                signal.clear()
            try:
                await asyncio.wait_for(signal.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return

    def _prune_attention_window(self, session: SessionContext, now: float | None = None) -> list[AstrMessageEvent]:
        return self.window_buffer.prune(session, now=now)

    def _append_attention_window(self, session: SessionContext, events: list[AstrMessageEvent], timestamp: float | None = None) -> None:
        self.window_buffer.append(session, events, timestamp=timestamp)

    def _merge_attention_window(self, session: SessionContext, batch_events: list[AstrMessageEvent]) -> list[AstrMessageEvent]:
        return self.window_buffer.merge(session, batch_events)

    def _resolve_event_context(self, event: AstrMessageEvent) -> dict[str, Any]:
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        chat_id = str(getattr(event, "unified_msg_origin", "") or group_id or "default")
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        msg_str = str(getattr(event, "message_str", "") or "")
        extracted_images = list(
            dict.fromkeys(
                list(event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", [])) or [])
                + list(event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", [])) or [])
            )
        )
        is_private = not bool(group_id)
        return {
            "chat_id": chat_id,
            "self_id": self_id,
            "sender_id": sender_id,
            "msg_str": msg_str,
            "extracted_images": extracted_images,
            "is_private": is_private,
        }

    async def _engage_immediately(self, event: AstrMessageEvent, chat_id: str, retrieve_keys: list[str], *, fast_mode: bool) -> str:
        event.set_extra("retrieve_keys", list(retrieve_keys))
        event.set_extra("is_fast_mode", bool(fast_mode))
        event.set_extra("astrmai_trace_id", event.get_extra("astrmai_trace_id", new_trace_id()))
        turn_context = ensure_turn_context(event)
        turn_context.attention.retrieve_keys = list(retrieve_keys)
        turn_context.attention.is_fast_mode = bool(fast_mode)
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        await self._record_event_activity(chat_id, event, sender_id)
        if self.sys2_process:
            self._fire_priority_task(self.sys2_process(event, [event]), event)
        return "ENGAGED"

    async def _handle_force_engage(self, event: AstrMessageEvent, chat_id: str) -> str | None:
        if event.get_extra("astrmai_force_engage", False):
            return await self._engage_immediately(event, chat_id, ["ALL"], fast_mode=False)
        return None

    async def _complete_proactive_candidate(self, event: AstrMessageEvent, *, reason: str) -> None:
        if not bool(event.get_extra("astrmai_is_proactive_event", False)):
            return
        if bool(event.get_extra("astrmai_proactive_completed", False)):
            return
        ledger = event.get_extra("astrmai_proactive_stage_ledger", [])
        normalized_reason = str(reason or "proactive_candidate_skipped")
        if not any(
            isinstance(item, dict)
            and item.get("status") in {"blocked", "error", "cancelled"}
            and item.get("stage") in {"proactive.sensor", "proactive.attention", "proactive.planner"}
            for item in list(ledger or [])
        ):
            stage = (
                "proactive.attention"
                if normalized_reason in {"proactive_judge_wait", "proactive_judge_ignore"}
                else "proactive.sensor"
            )
            append_proactive_stage(event, stage, "blocked", normalized_reason)
        event.set_extra("astrmai_proactive_completed", True)
        decision = event.get_extra("astrmai_proactive_dispatch_decision", None)
        if decision is not None:
            if isinstance(decision, dict):
                decision["reply_sent"] = False
                decision["reply_preview"] = ""
                decision["status"] = "skipped"
                decision["blocked_reason"] = normalized_reason
            else:
                setattr(decision, "reply_sent", False)
                setattr(decision, "reply_preview", "")
                setattr(decision, "status", "skipped")
                setattr(decision, "blocked_reason", normalized_reason)
        callback = event.get_extra("astrmai_proactive_completion_callback", None)
        if callable(callback):
            try:
                result = callback(False, "")
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(f"[AttentionGate] proactive completion callback degraded: {exc}")

    async def _apply_primary_mood_update(self, event: AstrMessageEvent, chat_id: str, msg_str: str) -> None:
        if (
            not msg_str.strip()
            or bool(event.get_extra("astrmai_is_proactive_event", False))
            or bool(event.get_extra("astrmai_primary_mood_applied", False))
            or not hasattr(self.state_engine, "update_mood")
        ):
            return
        try:
            mood_tag, mood_value = await self.state_engine.update_mood(chat_id, msg_str)
            event.set_extra("astrmai_primary_mood_applied", True)
            event.set_extra("astrmai_primary_mood_tag", str(mood_tag or "neutral"))
            event.set_extra("astrmai_primary_mood_value", float(mood_value))
            event.set_extra("astrmai_primary_mood_source", "attention_ingress")
        except Exception as exc:
            logger.debug(f"[AttentionGate] primary mood update degraded: {exc}")

    def _is_simple_wakeup_payload(self, msg_str: str) -> bool:
        normalized = str(msg_str or "").strip()
        if not normalized:
            return False
        return len(normalized) <= 12 and len(self._tokenize_text(normalized)) <= 2

    async def _handle_fast_wakeup(self, event: AstrMessageEvent, chat_id: str, is_strong_wakeup: bool) -> str | None:
        if not is_strong_wakeup:
            return None
        if bool(
            event.get_extra("astrmai_wait_for_image_pair", False)
            or event.get_extra("astrmai_cross_message_vision_bound", False)
        ):
            return None
        if not self._is_simple_wakeup_payload(getattr(event, "message_str", "")):
            return None
        event.set_extra("astrmai_group_direct_wakeup", True)
        await self._append_dialogue_segment(event)
        return await self._engage_immediately(event, chat_id, ["CORE_ONLY"], fast_mode=True)

    async def _passes_sensor_filters(self, event: AstrMessageEvent, msg_str: str) -> bool:
        if hasattr(self.sensors, "is_command"):
            try:
                if await self.sensors.is_command(msg_str):
                    if hasattr(event, "set_extra"):
                        event.set_extra("astrmai_is_command", True)
                    return False
            except Exception:
                try:
                    logger.exception(f"[AttentionGate] sensor is_command check failed on msg={msg_str[:100]!r}")
                except AttributeError:
                    logger.error(f"[AttentionGate] sensor is_command check failed on msg={msg_str[:100]!r}")
        if hasattr(self.sensors, "should_process_message"):
            try:
                return bool(await self.sensors.should_process_message(event))
            except Exception:
                try:
                    logger.exception("[AttentionGate] sensor should_process_message check failed, defaulting to pass")
                except AttributeError:
                    logger.error("[AttentionGate] sensor should_process_message check failed, defaulting to pass")
                return True
        return True

    def _should_ignore_passive_group_image(
        self,
        event: AstrMessageEvent,
        is_private: bool,
        extracted_images: list[Any],
        is_strong_wakeup: bool,
    ) -> bool:
        prefilter_selected = bool(event.get_extra("vision_prefilter_selected", False))
        return (
            bool(extracted_images)
            and not is_private
            and not is_strong_wakeup
            and not prefilter_selected
        )

    def _should_skip_by_throttle(self, msg_str: str, extracted_images: list[Any], chat_state: Any, chat_id: str, is_private: bool, is_strong_wakeup: bool) -> str | None:
        del chat_id
        if is_private or is_strong_wakeup:
            return None
        if extracted_images and not msg_str.strip():
            return None
        should_drop = bool(getattr(chat_state, "should_drop", False))
        return "THROTTLED" if should_drop else None

    def _handle_repeater_echo(self, session: SessionContext, is_private: bool, extracted_images: list[Any], msg_str: str) -> str | None:
        if is_private:
            return None
        msg_hash = f"{msg_str}|{bool(extracted_images)}"
        if getattr(session, "last_message_hash", "") == msg_hash and msg_str.strip():
            session.repeat_count = int(getattr(session, "repeat_count", 0) or 0) + 1
            if session.repeat_count >= 2:
                return "repeater_echo"
        else:
            session.last_message_hash = msg_hash
            session.repeat_count = 0
        return None

    async def _normalize_content_to_str(self, components: Any, depth: int = 0, event: AstrMessageEvent = None) -> str:
        _ = event  # 参数保留用于递归接口一致性
        if depth > 3:
            return "[content depth exceeded]"
        if components is None:
            return ""
        if isinstance(components, str):
            return components
        if isinstance(components, (list, tuple)):
            parts = [await self._normalize_content_to_str(item, depth + 1) for item in components]
            return " ".join(part for part in parts if part).strip()
        if isinstance(components, Comp.Plain):
            return str(getattr(components, "text", "") or "")
        if hasattr(components, "text"):
            return str(getattr(components, "text", "") or "")
        if hasattr(components, "sender_nickname"):
            return str(getattr(components, "sender_nickname", "") or "")
        return str(components or "")

    def _format_interaction_participant(self, name: str, user_id: str, bot_name: str, self_id: str = "") -> str:
        if self_id and str(user_id or "") == str(self_id):
            return bot_name or "Bot"
        return name or user_id or "Unknown"

    def _render_structured_interaction(self, event: AstrMessageEvent, bot_name: str) -> str:
        sender = self._format_interaction_participant(event.get_sender_name(), event.get_sender_id(), bot_name, event.get_self_id())
        return f"[{sender}] {str(getattr(event, 'message_str', '') or '').strip()}".strip()

    def _convert_interaction_to_narrative(self, content: str, bot_name: str, event: AstrMessageEvent = None) -> str:
        _ = (bot_name, event)  # 参数保留用于接口一致性
        return str(content or "").strip()

    def bind_chat_loop_kernel(self, chat_loop_kernel) -> None:
        self.chat_loop_kernel = chat_loop_kernel

    def bind_turn_trace_callback(self, callback) -> None:
        self.turn_trace_callback = callback

    @staticmethod
    def _trace_callback_supports_snapshot_refresh(callback) -> bool:
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == "refresh_snapshot_only"
            for parameter in parameters
        )

    async def _finalize_pre_planner_turn(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        *,
        status: str,
        reply_text: str | None = None,
    ) -> None:
        event.set_extra("astrmai_pre_planner_trace_status", str(status or ""))
        trace_state = str(event.get_extra("astrmai_pre_planner_trace_state", "") or "")
        if trace_state in {"pending", "persisted"} or bool(
            event.get_extra("astrmai_pre_planner_trace_finalized", False)
        ):
            return
        event.set_extra("astrmai_pre_planner_trace_state", "pending")
        # OPT-03/PL-02: pre-planner 终结路径也要填充 proactive 快照，否则合成事件
        # 死在传感器/节流层时 trace.proactive 恒空，三层诊断都看不出主动链死在哪
        try:
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                snapshot = ensure_turn_context(event).proactive
                snapshot.is_proactive = True
                snapshot.source = str(event.get_extra("astrmai_proactive_source", "") or snapshot.source or "")
                snapshot.intent_id = str(event.get_extra("astrmai_proactive_intent_id", "") or snapshot.intent_id or "")
                snapshot.reason = str(event.get_extra("astrmai_proactive_reason", "") or snapshot.reason or "")
                snapshot.guidance_preview = str(event.get_extra("astrmai_proactive_guidance", "") or "")[:160]
                snapshot.dispatch_status = str(status or "")
                if not snapshot.blocked_reason:
                    snapshot.blocked_reason = str(status or "")
        except Exception:
            logger.debug("[AttentionGate] proactive trace fill degraded", exc_info=True)
        callback = self.turn_trace_callback
        if not callable(callback):
            event.set_extra("astrmai_pre_planner_trace_state", "retryable")
            return
        try:
            result = callback(
                str(chat_id or getattr(event, "unified_msg_origin", "") or ""),
                event,
                status=status,
                reply_text=reply_text,
            )
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            event.set_extra("astrmai_pre_planner_trace_state", "retryable")
            raise
        except Exception as exc:
            event.set_extra("astrmai_pre_planner_trace_state", "retryable")
            logger.warning(
                "[AttentionGate] pre-planner turn trace failed "
                f"status={status} error_type={type(exc).__name__}"
            )
            return
        event.set_extra("astrmai_pre_planner_trace_finalized", True)
        event.set_extra("astrmai_pre_planner_trace_state", "persisted")
        if bool(
            event.get_extra(
                "astrmai_judge_validation_trace_refresh_requested",
                False,
            )
        ):
            event.set_extra(
                "astrmai_judge_validation_trace_refresh_requested",
                False,
            )
            try:
                callback_kwargs = {
                    "status": status,
                    "reply_text": reply_text,
                }
                if self._trace_callback_supports_snapshot_refresh(callback):
                    callback_kwargs["refresh_snapshot_only"] = True
                refreshed = callback(
                    str(chat_id or getattr(event, "unified_msg_origin", "") or ""),
                    event,
                    **callback_kwargs,
                )
                if inspect.isawaitable(refreshed):
                    await refreshed
            except asyncio.CancelledError:
                event.set_extra(
                    "astrmai_judge_validation_trace_refresh_requested",
                    True,
                )
                raise
            except Exception as exc:
                event.set_extra(
                    "astrmai_judge_validation_trace_refresh_error",
                    type(exc).__name__,
                )
                logger.warning(
                    "[AttentionGate] Judge validation trace refresh failed "
                    f"status={status} error_type={type(exc).__name__}"
                )

    async def inject_external_event(self, chat_id: str, event_data: dict):
        event = _SyntheticExternalEvent(dict(event_data or {}, unified_msg_origin=chat_id))
        external_result_id = str(event.get_extra("astrmai_external_result_id", "") or "")
        process_started = time.monotonic()
        if external_result_id:
            debug_trace(
                event,
                "external_result.attention_process_start",
                external_result_id=external_result_id,
                chat_id=chat_id,
            )
        source = str(event.get_extra("astrmai_loop_source", "") or "").strip()
        if not source:
            if event.get_extra("astrmai_is_proactive_event", False):
                source = "proactive_dispatcher"
            elif event.get_extra("is_external_bot_reply", False):
                source = "external_result_bridge"
        if source:
            event.set_extra("astrmai_loop_source", source)
        if self.chat_loop_kernel is not None and hasattr(self.chat_loop_kernel, "tick"):
            try:
                tick = await self.chat_loop_kernel.tick(chat_id=chat_id, trigger="external", event=event)
            except Exception as exc:
                if external_result_id:
                    debug_trace(
                        event,
                        "external_result.attention_process_failed",
                        external_result_id=external_result_id,
                        elapsed_ms=round((time.monotonic() - process_started) * 1000.0, 1),
                        error_type=type(exc).__name__,
                    )
                raise
            if external_result_id:
                debug_trace(
                    event,
                    "external_result.attention_process_end",
                    external_result_id=external_result_id,
                    elapsed_ms=round((time.monotonic() - process_started) * 1000.0, 1),
                )
            return tick.dispatch_result
        try:
            result = await self.process_event(event)
        except Exception as exc:
            if external_result_id:
                debug_trace(
                    event,
                    "external_result.attention_process_failed",
                    external_result_id=external_result_id,
                    elapsed_ms=round((time.monotonic() - process_started) * 1000.0, 1),
                    error_type=type(exc).__name__,
                )
            raise
        if external_result_id:
            debug_trace(
                event,
                "external_result.attention_process_end",
                external_result_id=external_result_id,
                elapsed_ms=round((time.monotonic() - process_started) * 1000.0, 1),
            )
        return result

    async def _format_and_filter_messages(self, events: List[AstrMessageEvent]):
        filtered = []
        for event in events:
            if not event:
                continue
            text = str(getattr(event, "message_str", "") or "").strip()
            self_id = get_event_self_id(event)
            is_wakeup = any(self._resolve_wakeup_flags(event, self_id, text))
            if text or event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls")) or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls")) or is_wakeup:
                filtered.append(event)
        return filtered

    async def process_event(self, event):
        trace_id = event.get_extra("astrmai_trace_id", "") or new_trace_id()
        event.set_extra("astrmai_trace_id", trace_id)
        turn_context = ensure_turn_context(event)

        _chat_id = getattr(event, "unified_msg_origin", "") or ""
        # Classify ingress before duplicate/sensor/throttle early exits so every
        # persisted pre-planner trace distinguishes invalid activity from missing instrumentation.
        try:
            self._set_topic_activity_observation(
                _chat_id,
                event,
                str(getattr(event, "get_sender_id", lambda: "")() or ""),
                is_private=not bool(getattr(event, "get_group_id", lambda: "")()),
            )
        except Exception as exc:
            logger.debug(f"[AttentionGate] ingress topic activity observation degraded: {exc}")
        if self._proactive_dispatching.get(_chat_id, False) and not event.get_extra(
            "astrmai_is_proactive_event",
            False,
        ):
            queue = self._deferred_messages.setdefault(_chat_id, [])
            if len(queue) < 5:
                queue.append(event)
            logger.warning(
                f"[AttentionGate] proactive dispatching in progress; queued message for {_chat_id} "
                f"(queue={len(queue)})"
            )
            return "PROACTIVE_BLOCKED"

        incoming_pre_recorded = bool(event.get_extra("astrmai_incoming_recorded", False))
        if not incoming_pre_recorded and not self._claim_message(event):
            is_proactive_event = bool(event.get_extra("astrmai_is_proactive_event", False))
            if is_proactive_event:
                append_proactive_stage(event, "proactive.sensor", "blocked", "duplicate_event")
                await self._complete_proactive_candidate(event, reason="duplicate_event")
                await self._finalize_pre_planner_turn(
                    event,
                    _chat_id,
                    status="skipped_duplicate_event",
                )
            return "DUPLICATED"

        private_ingress = not bool(event.get_group_id())
        sensor_checked = False
        if private_ingress:
            if not await self._passes_sensor_filters(event, str(getattr(event, "message_str", "") or "")):
                self._set_topic_activity_observation(
                    _chat_id,
                    event,
                    str(getattr(event, "get_sender_id", lambda: "")() or ""),
                    is_private=True,
                )
                is_proactive_event = bool(event.get_extra("astrmai_is_proactive_event", False))
                if is_proactive_event:
                    append_proactive_stage(event, "proactive.sensor", "blocked", "sensor_filtered")
                    await self._complete_proactive_candidate(event, reason="sensor_filtered")
                    await self._finalize_pre_planner_turn(
                        event,
                        _chat_id,
                        status="skipped_sensor_filter",
                    )
                return "FILTERED"
            sensor_checked = True

        perception = self.perception_builder.build(event)
        context = perception.as_event_context()
        chat_id = context["chat_id"]
        try:
            # OPT-12/TL-07: trace 的 image_count 派生自 perception.image_urls，
            # 此前从未有人填充（585/585 恒 0），图片轮在观测层不可辨识
            turn_context.perception.image_urls = list(context.get("extracted_images") or [])
        except Exception:
            logger.debug("[AttentionGate] perception image_urls fill degraded", exc_info=True)
        self_id = context["self_id"]
        sender_id = context["sender_id"]
        msg_str = context["msg_str"]
        extracted_images = context["extracted_images"]
        is_private = context["is_private"]

        event.set_extra("is_private_chat", bool(is_private))
        event.set_extra("astrmai_turn_mode", "private" if is_private else "group")

        if is_private and self.private_turn_coordinator is not None:
            note_new_message = getattr(self.private_turn_coordinator, "note_new_message", None)
            if callable(note_new_message):
                note_new_message(chat_id)
        await self._ensure_turn_identity(event, chat_id, is_private)

        debug_trace(event, "attention_ingress", chat_id=chat_id, sender_id=sender_id, preview=preview_text(msg_str, 80))

        session = await self._get_or_create_session(chat_id)

        if not sensor_checked:
            if not await self._passes_sensor_filters(event, msg_str):
                self._set_topic_activity_observation(
                    chat_id,
                    event,
                    sender_id,
                    is_private=is_private,
                )
                rejected_perception = self.perception_builder.build(event)
                if not is_private and rejected_perception.is_strong_wakeup:
                    fast_result = await self._handle_fast_wakeup(
                        event,
                        chat_id,
                        rejected_perception.is_strong_wakeup,
                    )
                    if fast_result:
                        if bool(event.get_extra("astrmai_is_proactive_event", False)):
                            append_proactive_stage(event, "proactive.attention", "success", "fast_wakeup")
                        return fast_result
                if bool(event.get_extra("astrmai_is_proactive_event", False)):
                    append_proactive_stage(event, "proactive.sensor", "blocked", "sensor_filtered")
                await self._complete_proactive_candidate(event, reason="sensor_filtered")
                await self._finalize_pre_planner_turn(
                    event,
                    chat_id,
                    status="skipped_sensor_filter",
                )
                return "FILTERED"
            sensor_checked = True
            perception = self.perception_builder.build(event)
            context = perception.as_event_context()
            msg_str = context["msg_str"]
            extracted_images = context["extracted_images"]
            is_private = context["is_private"]

        is_direct = perception.is_direct_wakeup
        is_at_bot = perception.is_at_bot
        is_reply = perception.is_reply_to_bot
        is_strong_wakeup = perception.is_strong_wakeup
        if not is_private and bool(getattr(getattr(self.config, "vision", None), "enable_vision", True)):
            async with session.lock:
                pairing_mode = self.window_buffer.register_group_vision_pairing(
                    session,
                    event,
                    sender_id=sender_id,
                    is_at_bot=is_at_bot,
                )
            if pairing_mode not in {"none", "pending_image", "pending_at"}:
                perception = self.perception_builder.build(event)
                context = perception.as_event_context()
                msg_str = context["msg_str"]
                extracted_images = context["extracted_images"]
                is_direct = perception.is_direct_wakeup
                is_at_bot = perception.is_at_bot
                is_reply = perception.is_reply_to_bot
                is_strong_wakeup = perception.is_strong_wakeup

        defer_private_context = bool(is_private and self.private_turn_coordinator is not None)
        if not defer_private_context:
            # OPT-08/RT-03: mood LLM 后置——ambient 消息（88% 最终不回复）不再在
            # ingress 无条件付一次 3-40s 情绪调用；强唤醒（必回复）保持即时感知
            if not self._mood_post_judge_enabled() or perception.is_strong_wakeup:
                await self._apply_primary_mood_update(event, chat_id, msg_str)

        forced = await self._handle_force_engage(event, chat_id)
        if forced:
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                append_proactive_stage(event, "proactive.attention", "success", "force_engage")
            return forced

        if is_direct:
            event.set_extra("astrmai_group_direct_wakeup", True)
        if is_at_bot:
            event.set_extra("astrmai_at_bot_wakeup", True)
        if is_reply:
            event.set_extra("astrmai_reply_wakeup", True)

        fast_result = None if is_private else await self._handle_fast_wakeup(event, chat_id, is_strong_wakeup)
        if fast_result:
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                append_proactive_stage(event, "proactive.attention", "success", "fast_wakeup")
            return fast_result

        if self._should_ignore_passive_group_image(
            event,
            is_private,
            extracted_images,
            is_strong_wakeup,
        ):
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                append_proactive_stage(event, "proactive.sensor", "success")
                append_proactive_stage(event, "proactive.attention", "blocked", "passive_group_image")
            await self._complete_proactive_candidate(event, reason="passive_group_image")
            await self._finalize_pre_planner_turn(
                event,
                chat_id,
                status="skipped_passive_group_image",
            )
            return "IGNORED_IMAGE"

        if (
            is_private
            and self.private_chat_manager
            and not event.get_extra("astrmai_private_reply_cycle_checked", False)
        ):
            try:
                wait_signaled = await self.private_chat_manager.signal_new_message(
                    sender_id,
                    msg_str,
                    chat_id=chat_id,
                )
                if wait_signaled:
                    logger.debug(f"[AttentionGate] private wait resumed without consuming message for {chat_id}")
            except Exception as exc:
                logger.warning(
                    f"[AttentionGate] private chat wait signal failed for {chat_id}, "
                    f"falling back to normal attention: {exc}"
                )

        chat_state = None
        if hasattr(self.state_engine, "get_state"):
            try:
                maybe_state = self.state_engine.get_state(chat_id)
                chat_state = await maybe_state if asyncio.iscoroutine(maybe_state) else maybe_state
            except Exception:
                logger.warning("[AttentionGate] get_state failed", exc_info=True)
                chat_state = None

        throttle_result = self._should_skip_by_throttle(msg_str, extracted_images, chat_state, chat_id, is_private, is_strong_wakeup)
        if throttle_result:
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                append_proactive_stage(event, "proactive.attention", "blocked", str(throttle_result).lower())
            await self._complete_proactive_candidate(event, reason=str(throttle_result or "throttled").lower())
            await self._finalize_pre_planner_turn(
                event,
                chat_id,
                status=f"skipped_{str(throttle_result or 'throttled').lower()}",
            )
            return throttle_result

        repeater_result = self._handle_repeater_echo(session, is_private, extracted_images, msg_str)
        if repeater_result:
            if bool(event.get_extra("astrmai_is_proactive_event", False)):
                append_proactive_stage(event, "proactive.attention", "blocked", str(repeater_result).lower())
            await self._complete_proactive_candidate(event, reason=repeater_result)
            await self._finalize_pre_planner_turn(
                event,
                chat_id,
                status=f"skipped_{str(repeater_result).lower()}",
            )
            return repeater_result

        is_proactive_event = bool(event.get_extra("astrmai_is_proactive_event", False))
        if is_proactive_event:
            append_proactive_stage(event, "proactive.sensor", "success")
            now = time.time()
            event.set_extra("astrmai_timestamp", now)
            ensure_turn_context(event).perception.timestamp = now
        elif not incoming_pre_recorded:
            await self._record_event_activity(chat_id, event, sender_id)
        if not defer_private_context and not is_proactive_event and not incoming_pre_recorded:
            await self._append_dialogue_segment(event)

        async with session.lock:
            if (
                self._workers_shutdown
                or session.closed
                or self.focus_pools.get(chat_id) is not session
            ):
                event.set_extra("astrmai_attention_dropped_during_shutdown", True)
                return "DROPPED_SHUTDOWN"
            self._append_accumulation_event(session, event)
            session.last_active_time = time.time()
            if bool(event.get_extra("astrmai_release_vision_pair_waiter", False)):
                session.vision_pair_signal.set()
                event.set_extra("astrmai_release_vision_pair_waiter", False)
            should_schedule = not session.is_evaluating
            session.is_evaluating = True
            if should_schedule:
                spawned = self._spawn_session_worker(
                    chat_id,
                    session,
                    self_id,
                    is_private=is_private,
                    is_strong_wakeup=is_strong_wakeup,
                    event=event,
                    generation=session.worker_generation,
                )
                if spawned is None:
                    session.is_evaluating = False
        return "BUFFERED"

    @staticmethod
    def _build_judge_window_message(events: list[AstrMessageEvent]) -> str:
        return AttentionDecisionRouter.build_judge_window_message(events)

    async def _evaluate_judge_gate(
        self,
        chat_id: str,
        focus_event: AstrMessageEvent,
        focus_thread,
        events: list[AstrMessageEvent],
        *,
        is_strong_wakeup: bool,
    ) -> str:
        is_proactive = bool(focus_event.get_extra("astrmai_is_proactive_event", False))
        if is_proactive:
            append_proactive_stage(focus_event, "proactive.attention", "started")
        try:
            decision = await self.decision_router.evaluate(
                chat_id,
                focus_event,
                focus_thread,
                events,
                is_strong_wakeup=is_strong_wakeup,
            )
        except Exception as exc:
            if is_proactive:
                append_proactive_stage(focus_event, "proactive.attention", "error", type(exc).__name__)
            raise
        if is_proactive:
            blocked = decision.action in {"WAIT", "IGNORE"}
            append_proactive_stage(
                focus_event,
                "proactive.attention",
                "blocked" if blocked else "success",
                str(decision.reason or decision.action).lower() if blocked else "",
            )
        return decision.action

    async def _send_private_topic_confirmation(
        self,
        event: AstrMessageEvent,
        confirmation_text: str,
    ) -> bool:
        if not confirmation_text or not hasattr(event, "send") or not hasattr(event, "plain_result"):
            return False
        safe_text, failure_kind = validate_visible_output_text(confirmation_text)
        if not safe_text:
            safe_text = "这个话题已经隔了一会儿了，还要继续聊吗？回复“继续”就好～"
            event.set_extra("astrmai_topic_confirmation_safe_fallback", True)
            event.set_extra("astrmai_topic_confirmation_guard_reason", failure_kind)
            event.set_extra(
                "astrmai_internal_context_leak_blocked",
                failure_kind == "internal_event_envelope",
            )
            debug_trace(
                event,
                "topic_confirmation_output_guard",
                failure_kind=failure_kind,
                internal_context_leak_blocked=failure_kind == "internal_event_envelope",
            )
        else:
            event.set_extra("astrmai_topic_confirmation_safe_fallback", False)
        try:
            if not outbound_send_allowed(event):
                event.set_extra("astrmai_topic_confirmation_send_rejected", True)
                return False
            await event.send(event.plain_result(safe_text))
            event.set_extra("astrmai_reply_sent", True)
            event.set_extra("astrmai_topic_confirmation_sent", True)
            event.set_extra("astrmai_topic_confirmation_sent_text", safe_text)
            return True
        except Exception as exc:
            logger.warning(f"[AttentionGate] private topic confirmation send failed: {exc}")
            return False

    @staticmethod
    def _vision_barrier_should_abort(outcome: Any, event: AstrMessageEvent) -> bool:
        if bool(event.get_extra("astrmai_vision_required_failed", False)):
            return True
        return bool(getattr(outcome, "should_abort", False))

    async def _send_required_vision_failure(self, event: AstrMessageEvent) -> str | None:
        text = "这张图片暂时没有识别成功，我现在无法确认图片内容，请稍后再发一次。"
        if bool(event.get_extra("astrmai_vision_failure_notice_sent", False)):
            return text
        if not hasattr(event, "send") or not hasattr(event, "plain_result"):
            return None
        try:
            if not outbound_send_allowed(event):
                return None
            await event.send(event.plain_result(text))
            event.set_extra("astrmai_reply_sent", True)
            event.set_extra("astrmai_vision_failure_notice_sent", True)
            return text
        except Exception as exc:
            logger.warning(f"[AttentionGate] required vision failure notice send failed: {exc}")
            return None

    async def _debounce_and_judge(
        self,
        chat_id: str,
        session: SessionContext,
        self_id: str,
        *,
        is_private: bool = False,
        is_strong_wakeup: bool = False,
        worker_event: AstrMessageEvent | None = None,
    ):
        current_is_strong_wakeup = is_strong_wakeup
        while True:
            wait_event = worker_event
            wait_stage = begin_stage(
                wait_event,
                "attention.worker_wait",
                critical_path=True,
                metadata={"chat_id": str(chat_id or ""), "private": bool(is_private)},
            )
            if is_private and self.private_turn_coordinator is not None:
                try:
                    await self.private_turn_coordinator.wait_for_input_stability(session)
                finally:
                    finish_stage(wait_event, wait_stage, metadata={"kind": "input_stability"})
            else:
                try:
                    debounce_stage = begin_stage(
                        wait_event,
                        "attention.debounce",
                        critical_path=True,
                        metadata={"chat_id": str(chat_id or "")},
                    )
                    try:
                        await asyncio.sleep(self._compute_debounce_delay(session, is_private, current_is_strong_wakeup))
                    finally:
                        finish_stage(wait_event, debounce_stage)
                    pairing_stage = begin_stage(
                        wait_event,
                        "attention.vision_pair_wait",
                        critical_path=True,
                        metadata={"chat_id": str(chat_id or "")},
                    )
                    try:
                        await self._wait_for_pending_vision_pairing(session)
                    finally:
                        finish_stage(wait_event, pairing_stage)
                finally:
                    finish_stage(wait_event, wait_stage)
            lock_stage = begin_stage(
                wait_event,
                "attention.session_lock",
                critical_path=True,
                metadata={"chat_id": str(chat_id or "")},
            )
            try:
                async with session.lock:
                    batch_oldest_pending_at = session.oldest_pending_at
                    batch_events = list(session.accumulation_pool)
                    session.accumulation_pool.clear()
                    session.oldest_pending_at = 0.0
            finally:
                finish_stage(wait_event, lock_stage)

            if batch_events:
                # OPT-02/RT-01: 排水循环的晚到批次不得沿用 worker 创建时刻旧 turn 的
                # deadline/账本，逐批把 telemetry contextvar 重绑到本批锚点事件
                rebind_turn_telemetry(batch_events[-1])
                pending_mood: tuple[AstrMessageEvent, str] | None = None
                if is_private and self.private_turn_coordinator is not None:
                    merge_pending_batch = getattr(
                        self.private_turn_coordinator,
                        "merge_pending_batch",
                        None,
                    )
                    if callable(merge_pending_batch):
                        batch_events = merge_pending_batch(chat_id, batch_events)
                    # OPT-07/RT-05: burst deadline 跨合并迭代持久化——旧逻辑每次
                    # prepare_batch 重新起算总额，屏障期间不断有新消息时视觉预算无上界
                    burst_deadline = float(getattr(session, "vision_burst_deadline", 0.0) or 0.0)
                    if burst_deadline <= time.monotonic():
                        budget_getter = getattr(self.private_turn_coordinator, "vision_total_budget_sec", None)
                        burst_budget = float(budget_getter()) if callable(budget_getter) else 180.0
                        burst_deadline = time.monotonic() + max(0.0, burst_budget)
                        session.vision_burst_deadline = burst_deadline
                    try:
                        vision_outcome = await self.private_turn_coordinator.prepare_batch(
                            batch_events, chat_id, deadline=burst_deadline
                        )
                    except TypeError:
                        vision_outcome = await self.private_turn_coordinator.prepare_batch(batch_events, chat_id)
                    async with session.lock:
                        if session.accumulation_pool:
                            self._prepend_accumulation_events(
                                session,
                                batch_events,
                                oldest_pending_at=batch_oldest_pending_at,
                            )
                            current_is_strong_wakeup = False
                            continue
                    session.vision_burst_deadline = 0.0
                    if self._vision_barrier_should_abort(vision_outcome, batch_events[-1]):
                        for prepared_event in batch_events:
                            if not bool(prepared_event.get_extra("astrmai_private_context_recorded", False)):
                                await self._append_dialogue_segment(prepared_event)
                                prepared_event.set_extra("astrmai_private_context_recorded", True)
                        focus_event = batch_events[-1]
                        failure_text = await self._send_required_vision_failure(focus_event)
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                        await self._finalize_pre_planner_turn(
                            focus_event,
                            chat_id,
                            status="skipped_vision_required",
                            reply_text=failure_text,
                        )
                        async with session.lock:
                            if session.accumulation_pool:
                                current_is_strong_wakeup = False
                                continue
                            session.is_evaluating = False
                            return
                    mood_event = None
                    mood_texts: list[str] = []
                    for prepared_event in batch_events:
                        self.perception_builder.build(prepared_event)
                        if not bool(prepared_event.get_extra("astrmai_private_context_recorded", False)):
                            rich_text = str(
                                prepared_event.get_extra(
                                    "astrmai_rich_text",
                                    getattr(prepared_event, "message_str", ""),
                                )
                                or ""
                            )
                            if rich_text.strip():
                                mood_event = prepared_event
                                mood_texts.append(rich_text)
                            await self._append_dialogue_segment(prepared_event)
                            prepared_event.set_extra("astrmai_private_context_recorded", True)
                    if mood_event is not None and mood_texts:
                        if self._mood_post_judge_enabled():
                            # OPT-08/RT-03+ID-09: 私聊情绪感知不再串行阻塞判决前链路，
                            # 判决通过后 fire-and-forget（见下方 judge_action 后置块）
                            pending_mood = (mood_event, "\n".join(mood_texts))
                        else:
                            await self._apply_primary_mood_update(
                                mood_event,
                                chat_id,
                                "\n".join(mood_texts),
                            )
                merged_events = self._merge_attention_window(session, batch_events)
                events = await self._format_and_filter_messages(merged_events)
                if events:
                    # Only a real user event in the current batch can supersede a
                    # proactive candidate.  The merged attention window also
                    # contains history, which must not affect this decision.
                    current_user_events = []
                    current_proactive_events = []
                    for batch_event in batch_events:
                        if bool(batch_event.get_extra("astrmai_is_proactive_event", False)):
                            current_proactive_events.append(batch_event)
                            continue
                        sender_id = str(
                            getattr(batch_event, "get_sender_id", lambda: "")() or ""
                        ).strip()
                        provenance = str(
                            batch_event.get_extra("astrmai_event_provenance", "original")
                            or "original"
                        )
                        is_real_user = bool(
                            sender_id
                            and sender_id != str(getattr(self.state_engine, "bot_id", "") or "")
                            and sender_id != str(get_event_self_id(batch_event) or "")
                            and not sender_id.startswith("astrmai_")
                            and not bool(batch_event.get_extra("astrmai_is_external_bot_reply", False))
                            and provenance == "original"
                        )
                        if is_real_user:
                            current_user_events.append(batch_event)
                    if is_private and current_user_events and current_proactive_events:
                        for proactive_event in current_proactive_events:
                            await self._complete_proactive_candidate(
                                proactive_event,
                                reason="superseded_by_user",
                            )
                            await self._finalize_pre_planner_turn(
                                proactive_event,
                                chat_id,
                                status="skipped_superseded_by_user",
                            )
                        superseded = {id(event) for event in current_proactive_events}
                        batch_events = [
                            event
                            for event in batch_events
                            if id(event) not in superseded
                        ]
                        events = [
                            event
                            for event in events
                            if id(event) not in superseded
                        ]
                    if not events:
                        async with session.lock:
                            if session.accumulation_pool:
                                current_is_strong_wakeup = False
                                continue
                            session.is_evaluating = False
                        return
                    normalized = self._build_normalized_events(events, self_id)
                    focus_event, _, focus_reason = self._select_focus_event(
                        events,
                        self_id,
                        normalized_events=normalized,
                        is_private=is_private,
                    )
                    if focus_event is None:
                        focus_event = events[-1]
                    if is_private:
                        bind_batch_context = getattr(
                            self.private_turn_coordinator,
                            "bind_batch_context",
                            None,
                        )
                        if callable(bind_batch_context):
                            bind_batch_context(batch_events, focus_event)
                        self._bind_private_batch_turn(focus_event, batch_events, chat_id)
                        normalized = self._build_normalized_events(events, self_id)
                    focus_candidate = next((candidate for candidate in normalized if candidate.event is focus_event), None)
                    if focus_candidate is None:
                        focus_candidate = normalized[-1]
                    root_candidate, root_reason = self._resolve_thread_root(focus_candidate, normalized)
                    focus_thread = self._build_focus_thread(focus_candidate, root_candidate, normalized)
                    focus_thread.focus_reason = focus_thread.focus_reason or focus_reason or "selected_focus_event"
                    focus_thread.root_reason = focus_thread.root_reason or root_reason
                    if not is_private:
                        self._prepare_group_attention_topic(chat_id, focus_event, focus_thread)

                    emit_legacy_focus_thread_extras(focus_event, focus_thread, window_events=events)
                    retrieve_keys = ["CORE_ONLY"] if focus_candidate.is_near_context_query else ["ALL"]
                    focus_event.set_extra("retrieve_keys", retrieve_keys)
                    focus_event.set_extra("is_fast_mode", False)
                    turn_context = ensure_turn_context(focus_event)
                    turn_context.attention.window_events = list(events)
                    turn_context.attention.focus_thread = focus_thread
                    turn_context.attention.turn_target = focus_thread.turn_target
                    turn_context.attention.actor_set = focus_thread.actor_set
                    turn_context.attention.retrieve_keys = list(retrieve_keys)
                    turn_context.attention.is_fast_mode = False
                    turn_context.attention.focus_reason = focus_thread.focus_reason
                    turn_context.attention.root_reason = focus_thread.root_reason
                    topic_confirmation_required = False
                    if (
                        is_private
                        and not bool(focus_event.get_extra("astrmai_is_proactive_event", False))
                        and self.conversation_continuity is not None
                    ):
                        evaluate_topic = getattr(
                            self.conversation_continuity,
                            "evaluate_private_message",
                            None,
                        )
                        if callable(evaluate_topic):
                            topic_decision = evaluate_topic(
                                chat_id,
                                str(
                                    focus_event.get_extra(
                                        "astrmai_rich_text",
                                        getattr(focus_event, "message_str", ""),
                                    )
                                    or ""
                                ),
                            )
                            focus_event.set_extra("astrmai_private_topic_status", topic_decision.get("status", ""))
                            focus_event.set_extra("astrmai_private_topic_inherited", bool(topic_decision.get("inherited", False)))
                            focus_event.set_extra("astrmai_private_topic_age_sec", float(topic_decision.get("age_seconds", 0.0) or 0.0))
                            focus_event.set_extra("astrmai_private_topic_label", str(topic_decision.get("topic", "") or ""))
                            prompt_summary = str(topic_decision.get("prompt_summary", "") or "")
                            if prompt_summary:
                                focus_event.set_extra("astrmai_private_topic_context", prompt_summary)
                            topic_confirmation_required = bool(topic_decision.get("requires_confirmation", False))
                            if topic_confirmation_required:
                                focus_event.set_extra(
                                    "astrmai_topic_confirmation_trigger",
                                    str(topic_decision.get("status", "") or ""),
                                )
                                focus_event.set_extra(
                                    "astrmai_private_topic_confirmation_text",
                                    str(topic_decision.get("confirmation_text", "") or ""),
                                )
                    self._schedule_compaction_task(chat_id, focus_thread)
                    should_skip_judge = bool(
                        focus_candidate.is_direct_wakeup
                        or focus_candidate.is_at_bot
                        or focus_candidate.is_reply_to_bot
                        or focus_candidate.has_direct_vision
                        or current_is_strong_wakeup
                        # OPT-08/ID-09: 私聊 16h 内 judge 18/18 全 REPLY——合并窗+settle
                        # 已承担"等对方说完"职能，judge 在私聊是纯延迟（可配置关闭）
                        or (is_private and self._private_skip_judge_enabled())
                    )
                    if topic_confirmation_required:
                        judge_action = "TOPIC_CONFIRM"
                    else:
                        judge_action = await self._evaluate_judge_gate(
                            chat_id,
                            focus_event,
                            focus_thread,
                            events,
                            is_strong_wakeup=should_skip_judge,
                        )
                    focus_event.set_extra("judge_action", judge_action)
                    turn_context.attention.judge_action = judge_action
                    turn_context.attention.prefilter_action = str(
                        focus_event.get_extra("astrmai_attention_prefilter_action", "") or ""
                    )
                    turn_context.attention.prefilter_reason = str(
                        focus_event.get_extra("astrmai_attention_prefilter_reason", "") or ""
                    )
                    if (
                        self._mood_post_judge_enabled()
                        and judge_action not in {"WAIT", "IGNORE"}
                        and not bool(focus_event.get_extra("astrmai_primary_mood_applied", False))
                    ):
                        # OPT-08/RT-03: 判定为回复类动作后才做情绪感知，且不再阻塞
                        # 关键路径（fire-and-forget）；WAIT/IGNORE 消息不付情绪调用
                        mood_target, mood_text = (
                            pending_mood
                            if pending_mood is not None
                            else (focus_event, str(getattr(focus_event, "message_str", "") or ""))
                        )
                        if str(mood_text or "").strip():
                            self._fire_priority_task(
                                self._apply_primary_mood_update(mood_target, chat_id, mood_text),
                                mood_target,
                            )
                    debug_trace(
                        focus_event,
                        "attention_focus_ready",
                        chat_id=chat_id,
                        focus_reason=focus_thread.focus_reason,
                        root_reason=focus_thread.root_reason,
                        focus_preview=preview_text(str(getattr(focus_event, "message_str", "") or ""), 80),
                        judge_action=judge_action,
                        private_burst_size=len(batch_events) if is_private else 0,
                        private_generation=(
                            getattr(focus_event.get_extra("astrmai_turn_identity", None), "generation", 0)
                            if is_private
                            else 0
                        ),
                        private_vision_count=(
                            len(focus_event.get_extra("astrmai_vision_records", []) or [])
                            if is_private
                            else 0
                        ),
                        private_vision_failed=(
                            bool(focus_event.get_extra("astrmai_vision_barrier_failed", False))
                            if is_private
                            else False
                        ),
                    )
                    if judge_action == "TOPIC_CONFIRM":
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                        confirmation_text = str(
                            focus_event.get_extra("astrmai_private_topic_confirmation_text", "")
                            or "这个话题已经隔了一会儿了，还要继续聊吗？回复“继续”就好～"
                        )
                        confirmation_sent = await self._send_private_topic_confirmation(
                            focus_event,
                            confirmation_text,
                        )
                        await self._finalize_pre_planner_turn(
                            focus_event,
                            chat_id,
                            status="executed_topic_confirmation",
                            reply_text=(
                                str(
                                    focus_event.get_extra(
                                        "astrmai_topic_confirmation_sent_text",
                                        confirmation_text,
                                    )
                                    or ""
                                )
                                if confirmation_sent
                                else None
                            ),
                        )
                    elif judge_action == "WAIT":
                        if bool(focus_event.get_extra("astrmai_is_proactive_event", False)):
                            await self._complete_proactive_candidate(focus_event, reason="proactive_judge_wait")
                        if not str(focus_event.get_extra("astrmai_wait_reason", "") or ""):
                            focus_event.set_extra("astrmai_wait_reason", "attention_judge_wait")
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                        await self._finalize_pre_planner_turn(
                            focus_event,
                            chat_id,
                            status="skipped_wait",
                        )
                    elif judge_action == "IGNORE":
                        if bool(focus_event.get_extra("astrmai_is_proactive_event", False)):
                            await self._complete_proactive_candidate(focus_event, reason="proactive_judge_ignore")
                        # G6/RT-02: 记录被忽略轮次——事件放回 window 后，focus 评分层
                        # 按该计数降权，避免同一条消息被反复判决（可配置关闭）
                        if self._judge_ignore_cooldown_enabled():
                            try:
                                previous_rounds = int(focus_event.get_extra("astrmai_judge_ignored_rounds", 0) or 0)
                            except (TypeError, ValueError):
                                previous_rounds = 0
                            focus_event.set_extra("astrmai_judge_ignored_rounds", previous_rounds + 1)
                        async with session.lock:
                            self._append_attention_window(session, [focus_event])
                        await self._finalize_pre_planner_turn(
                            focus_event,
                            chat_id,
                            status="skipped_ignore",
                        )
                    else:
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                        if self.sys2_process:
                            if is_private:
                                begin_pending_batch = getattr(
                                    self.private_turn_coordinator,
                                    "begin_pending_batch",
                                    None,
                                )
                                pending_revision = (
                                    begin_pending_batch(chat_id, batch_events)
                                    if callable(begin_pending_batch)
                                    else 0
                                )
                                try:
                                    reply_result = await self.sys2_process(
                                        focus_event,
                                        focus_thread.all_thread_events(),
                                    )
                                    finish_pending_batch = getattr(
                                        self.private_turn_coordinator,
                                        "finish_pending_batch",
                                        None,
                                    )
                                    if callable(finish_pending_batch):
                                        finish_pending_batch(
                                            chat_id,
                                            pending_revision,
                                            bool(reply_result),
                                        )
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    logger.error(f"[AttentionGate] private System2 dispatch failed for {chat_id}: {exc}", exc_info=True)
                            else:
                                _system2_factory = lambda: self.sys2_process(
                                    focus_event,
                                    focus_thread.all_thread_events(),
                                )
                                self._fire_background_task(
                                    _system2_factory(),
                                    focus_event,
                                    task_name="attention.system2",
                                    retry_factory=_system2_factory,
                                )

            async with session.lock:
                if session.accumulation_pool:
                    current_is_strong_wakeup = False
                    continue
                session.is_evaluating = False
                return


__all__ = ["AttentionGate"]
