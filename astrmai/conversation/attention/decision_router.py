from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.background_task_budget import (
    BackgroundTaskExecutionTimeout,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ...infrastructure.runtime.turn_call_ledger import (
    clamp_timeout_to_turn_budget,
    detach_turn_telemetry,
)
from ...shared.helpers.plugin_helpers import is_direct_call_event
from ..contracts.attention_topic import AttentionTopicIdentity
from ..reply_shape_policy import resolve_reply_shape_policy
from ..runtime.architecture_rollout import (
    ArchitectureTimer,
    record_architecture_observation,
    rollout_enabled,
)
from .participation_policy import (
    ParticipationPolicy,
    ParticipationResult,
    ParticipationState,
)
from .topic_identity import attention_topic_anchors_match


@dataclass(slots=True)
class AttentionDecision:
    action: str = "PASS"
    raw_action: str = "PASS"
    reason: str = ""


@dataclass(slots=True)
class AttentionPrefilterDecision:
    action: str = "NEED_JUDGE"
    reason: str = "ambiguous_group_message"


class AttentionDecisionRouter:
    """Runs System1 Judge as a lightweight attention gate."""

    def __init__(self, gate: Any):
        self.gate = gate
        self._consecutive_timeouts = 0
        self.participation_policy = ParticipationPolicy()
        self._participation_states: dict[str, ParticipationState] = {}
        self._judge_ignore_cache: dict[str, tuple[float, str, int]] = {}
        self._ambient_ignore_cache: dict[
            str,
            tuple[float, str, AttentionTopicIdentity] | float,
        ] = {}
        self._diagnostic_counts: dict[str, int] = {}
        self._validation_tasks: set[asyncio.Task] = set()
        self._validation_tasks_by_chat: dict[str, asyncio.Task] = {}

    def _count(self, name: str, amount: int = 1) -> None:
        key = str(name or "unknown")
        self._diagnostic_counts[key] = self._diagnostic_counts.get(key, 0) + int(amount or 0)

    def describe_status(self) -> dict[str, Any]:
        self._prune_ignore_caches()
        self._prune_participation_states()
        return {
            "counts": dict(self._diagnostic_counts),
            "judge_ignore_cache_size": len(self._judge_ignore_cache),
            "ambient_ignore_cache_size": len(self._ambient_ignore_cache),
            "participation_state_count": len(self._participation_states),
            "judge_validation_active": len(self._validation_tasks),
        }

    @staticmethod
    def _consume_background_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _judge_supports_context_kwargs(judge_evaluate: Any) -> bool:
        try:
            parameters = inspect.signature(judge_evaluate).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
            parameter.name == "focus_thread" for parameter in parameters
        )

    @staticmethod
    def _trace_callback_supports_snapshot_refresh(callback: Any) -> bool:
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            or parameter.name == "refresh_snapshot_only"
            for parameter in parameters
        )

    @staticmethod
    def _is_private_event(event: Any) -> bool:
        try:
            return not bool(event.get_group_id())
        except Exception:
            return False

    @staticmethod
    def _is_direct_event(event: Any) -> bool:
        try:
            return bool(is_direct_call_event(event))
        except Exception:
            return False

    @staticmethod
    def _event_text(event: Any) -> str:
        if event is None:
            return ""
        if hasattr(event, "get_extra"):
            rich_text = str(event.get_extra("astrmai_rich_text", "") or "").strip()
            if rich_text:
                return rich_text
        return str(getattr(event, "message_str", "") or "").strip()

    @staticmethod
    def _event_timestamp(event: Any) -> float:
        if event is None:
            return 0.0
        if hasattr(event, "get_extra"):
            value = event.get_extra("astrmai_timestamp", getattr(event, "timestamp", 0.0))
        else:
            value = getattr(event, "timestamp", 0.0)
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _event_id(event: Any) -> str:
        if event is None:
            return ""
        canonical = (
            event.get_extra("astrmai_conversation_event", None)
            if hasattr(event, "get_extra")
            else None
        )
        return str(
            getattr(canonical, "event_id", "")
            or (
                event.get_extra("astrmai_conversation_event_id", "")
                if hasattr(event, "get_extra")
                else ""
            )
            or getattr(getattr(event, "message_obj", None), "message_id", "")
            or ""
        ).strip()

    def _strong_wakeup_event_ids(
        self,
        events: list[Any],
        focus_event: Any,
        *,
        is_strong_wakeup: bool,
    ) -> tuple[str, ...]:
        event_ids: list[str] = []
        for event in events:
            canonical = (
                event.get_extra("astrmai_conversation_event", None)
                if hasattr(event, "get_extra")
                else None
            )
            is_direct = self._is_direct_event(event) or bool(
                getattr(canonical, "is_at_bot", False)
                or getattr(canonical, "is_reply_to_bot", False)
                or getattr(canonical, "is_direct_wakeup", False)
            )
            if event is focus_event and is_strong_wakeup:
                is_direct = True
            if not is_direct:
                continue
            event_id = self._event_id(event)
            if event_id and event_id not in event_ids:
                event_ids.append(event_id)
        return tuple(event_ids)

    def _attention_config(self) -> Any:
        return getattr(getattr(self.gate, "config", None), "attention", None)

    def _judge_ignore_cache_ttl(self) -> float:
        config = self._attention_config()
        try:
            return max(0.0, float(getattr(config, "judge_ignore_cache_ttl_sec", 8.0) or 0.0))
        except (TypeError, ValueError):
            return 8.0

    def _ambient_ignore_cache_ttl(self) -> float:
        config = self._attention_config()
        try:
            return max(0.0, float(getattr(config, "judge_ambient_cooldown_sec", 3.0) or 0.0))
        except (TypeError, ValueError):
            return 3.0

    def _validation_sample_rate(self) -> float:
        config = self._attention_config()
        try:
            return max(
                0.0,
                min(
                    1.0,
                    float(getattr(config, "judge_validation_sample_rate", 0.0) or 0.0),
                ),
            )
        except (TypeError, ValueError):
            return 0.0

    def _should_sample_validation(self, chat_id: str, event: Any, source: str) -> bool:
        rate = self._validation_sample_rate()
        if rate <= 0.0 or event is None or self._is_private_event(event) or self._is_direct_event(event):
            return False
        if hasattr(event, "get_extra") and bool(
            event.get_extra("astrmai_is_proactive_event", False)
        ):
            return False
        material = "\x1f".join(
            (
                str(chat_id or ""),
                self._event_id(event),
                str(source or ""),
                self._event_text(event),
            )
        )
        bucket = int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:8], 16)
        return bucket / 0xFFFFFFFF <= rate

    async def _sample_avoided_decision(
        self,
        chat_id: str,
        focus_event: Any,
        focus_thread: Any,
        events: list[Any],
        *,
        source: str,
    ) -> None:
        if focus_event is None or not hasattr(focus_event, "set_extra"):
            return
        focus_event.set_extra("astrmai_judge_validation_source", source)
        sampled = self._should_sample_validation(chat_id, focus_event, source)
        focus_event.set_extra("astrmai_judge_validation_sampled", sampled)
        if not sampled:
            return
        self._count("judge_validation_sampled")
        judge = getattr(self.gate, "judge", None)
        judge_evaluate = getattr(judge, "evaluate_shadow", None)
        if not callable(judge_evaluate):
            focus_event.set_extra("astrmai_judge_validation_status", "unavailable")
            self._count("judge_validation_unavailable")
            return
        active_task = self._validation_tasks_by_chat.get(str(chat_id or ""))
        if active_task is not None and not active_task.done():
            focus_event.set_extra("astrmai_judge_validation_status", "busy")
            self._count("judge_validation_busy")
            return
        message = self.build_judge_window_message(events) or self._event_text(focus_event)
        timeout = float(
            getattr(
                self._attention_config(),
                "judge_timeout",
                getattr(getattr(self.gate, "config", None), "judge_timeout", 20.0),
            )
            or 20.0
        )
        focus_event.set_extra("astrmai_judge_validation_status", "scheduled")

        async def _refresh_persisted_trace() -> None:
            trace_state = str(
                focus_event.get_extra("astrmai_pre_planner_trace_state", "") or ""
            )
            if trace_state == "pending":
                focus_event.set_extra(
                    "astrmai_judge_validation_trace_refresh_requested",
                    True,
                )
                return
            if trace_state != "persisted":
                return
            callback = getattr(self.gate, "turn_trace_callback", None)
            if not callable(callback):
                return
            status = str(
                focus_event.get_extra("astrmai_pre_planner_trace_status", "")
                or "skipped_ignore"
            )
            try:
                callback_kwargs = {"status": status, "reply_text": None}
                if self._trace_callback_supports_snapshot_refresh(callback):
                    callback_kwargs["refresh_snapshot_only"] = True
                refreshed = callback(chat_id, focus_event, **callback_kwargs)
                if inspect.isawaitable(refreshed):
                    await refreshed
                self._count("judge_validation_trace_refreshed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                focus_event.set_extra(
                    "astrmai_judge_validation_trace_error",
                    type(exc).__name__,
                )
                self._count("judge_validation_trace_refresh_failed")

        async def _run_validation() -> None:
            try:
                if self._judge_supports_context_kwargs(judge_evaluate):
                    coroutine = judge_evaluate(
                        chat_id,
                        message,
                        False,
                        "",
                        len(events),
                        False,
                        focus_thread=focus_thread,
                        window_events=events,
                        focus_event=focus_event,
                    )
                else:
                    coroutine = judge_evaluate(chat_id, message, False, "", len(events), False)
                result = await self._run_judge_with_hard_deadline(
                    coroutine,
                    timeout_sec=max(0.1, timeout),
                )
            except asyncio.TimeoutError:
                focus_event.set_extra("astrmai_judge_validation_status", "timeout")
                self._count("judge_validation_timeout")
                await _refresh_persisted_trace()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                focus_event.set_extra("astrmai_judge_validation_status", "degraded")
                focus_event.set_extra("astrmai_judge_validation_error", type(exc).__name__)
                self._count("judge_validation_degraded")
                await _refresh_persisted_trace()
                return
            action = str(getattr(result, "action", "") or "").upper()
            if not action:
                focus_event.set_extra("astrmai_judge_validation_status", "empty_response")
                self._count("judge_validation_empty_response")
                await _refresh_persisted_trace()
                return
            agreement = action == "IGNORE"
            false_filter_candidate = action in {"PASS", "REPLY", "TOOL_CALL"}
            focus_event.set_extra("astrmai_judge_validation_status", "success")
            focus_event.set_extra("astrmai_judge_validation_action", action)
            focus_event.set_extra("astrmai_judge_validation_agreement", agreement)
            focus_event.set_extra(
                "astrmai_judge_validation_false_filter_candidate",
                false_filter_candidate,
            )
            self._count(f"judge_validation_action_{action.lower()}")
            self._count(
                "judge_validation_agreement_true"
                if agreement
                else "judge_validation_agreement_false"
            )
            await _refresh_persisted_trace()

        normalized_chat_id = str(chat_id or "")

        async def _run_budgeted_validation() -> None:
            detach_turn_telemetry()

            async def _settle_budget_failure(status: str, error: BaseException) -> None:
                focus_event.set_extra("astrmai_judge_validation_status", status)
                focus_event.set_extra(
                    "astrmai_judge_validation_error",
                    type(error).__name__,
                )
                self._count(f"judge_validation_{status}")
                await _refresh_persisted_trace()

            try:
                budget = getattr(self.gate, "background_task_budget", None)
                budget_run = getattr(budget, "run", None)
                if callable(budget_run):
                    await budget_run(
                        _run_validation,
                        task_name="attention.judge_validation",
                        scope_id=normalized_chat_id,
                        execution_timeout_sec=max(0.1, timeout),
                    )
                    return
                await _run_validation()
            except BackgroundTaskQueueFull as exc:
                await _settle_budget_failure("queue_full", exc)
            except BackgroundTaskQueueTimeout as exc:
                await _settle_budget_failure("queue_timeout", exc)
            except BackgroundTaskExecutionTimeout as exc:
                await _settle_budget_failure("execution_timeout", exc)
            except asyncio.CancelledError as exc:
                await _settle_budget_failure("cancelled", exc)
                raise
            except Exception as exc:
                await _settle_budget_failure("degraded", exc)

        task = asyncio.create_task(_run_budgeted_validation())
        self._validation_tasks.add(task)
        self._validation_tasks_by_chat[normalized_chat_id] = task
        gate_background_tasks = getattr(self.gate, "_background_tasks", None)
        if isinstance(gate_background_tasks, set):
            gate_background_tasks.add(task)

        def _done(completed: asyncio.Task) -> None:
            self._validation_tasks.discard(completed)
            if isinstance(gate_background_tasks, set):
                gate_background_tasks.discard(completed)
            if self._validation_tasks_by_chat.get(normalized_chat_id) is completed:
                self._validation_tasks_by_chat.pop(normalized_chat_id, None)
            if completed.cancelled():
                if focus_event.get_extra("astrmai_judge_validation_status", "") == "scheduled":
                    focus_event.set_extra("astrmai_judge_validation_status", "cancelled")
                    self._count("judge_validation_cancelled")
                return
            try:
                error = completed.exception()
            except BaseException:
                error = None
            if error is not None and focus_event.get_extra(
                "astrmai_judge_validation_status",
                "",
            ) == "scheduled":
                focus_event.set_extra("astrmai_judge_validation_status", "degraded")
                focus_event.set_extra("astrmai_judge_validation_error", type(error).__name__)
                self._count("judge_validation_degraded")

        task.add_done_callback(_done)

    async def wait_for_validation_tasks(self) -> None:
        tasks = tuple(self._validation_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _attention_topic(focus_event: Any, focus_thread: Any) -> AttentionTopicIdentity:
        thread_identity = getattr(focus_thread, "attention_topic", None)
        if thread_identity is not None:
            return AttentionTopicIdentity.from_value(thread_identity)
        return AttentionTopicIdentity.from_event(focus_event)

    def _judge_ignore_cache_key(self, chat_id: str, focus_event: Any, focus_thread: Any) -> str:
        actor_id = ""
        if focus_event is not None:
            try:
                actor_id = str(focus_event.get_sender_id() or "").strip()
            except Exception:
                actor_id = ""
        identity = self._attention_topic(focus_event, focus_thread)
        if not identity.exact_cacheable:
            return ""
        text = self._event_text(focus_event)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        return "\x1f".join(
            (str(chat_id or ""), actor_id, identity.attention_topic_key, digest)
        )

    def _cached_judge_ignore(self, chat_id: str, focus_event: Any, focus_thread: Any) -> bool:
        ttl = self._judge_ignore_cache_ttl()
        if ttl <= 0.0:
            return False
        key = self._judge_ignore_cache_key(chat_id, focus_event, focus_thread)
        if not key:
            return False
        entry = self._judge_ignore_cache.get(key)
        if entry is None:
            return False
        expires_at, _, _ = entry
        if expires_at <= time.monotonic():
            if self._judge_ignore_cache.pop(key, None) is not None:
                self._count("judge_ignore_cache_ttl_evicted")
            return False
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_cache_hit", True)
            focus_event.set_extra("astrmai_judge_cache_action", "IGNORE")
            focus_event.set_extra("astrmai_judge_cache_scope", "exact_message")
            focus_event.set_extra("astrmai_judge_avoided", True)
        return True

    def _cache_judge_ignore(self, chat_id: str, focus_event: Any, focus_thread: Any) -> None:
        ttl = self._judge_ignore_cache_ttl()
        if ttl <= 0.0:
            return
        self._prune_ignore_caches()
        key = self._judge_ignore_cache_key(chat_id, focus_event, focus_thread)
        if not key:
            return
        self._judge_ignore_cache[key] = (time.monotonic() + ttl, str(chat_id or ""), int(time.time()))

    def _ambient_ignore_cache_key(self, chat_id: str, focus_event: Any, focus_thread: Any) -> str:
        identity = self._attention_topic(focus_event, focus_thread)
        if not identity.ambient_cacheable:
            return ""
        return f"{str(chat_id or '')}\x1f{identity.attention_topic_key}"

    def _cached_ambient_ignore(self, chat_id: str, focus_event: Any, focus_thread: Any) -> bool:
        ttl = self._ambient_ignore_cache_ttl()
        key = self._ambient_ignore_cache_key(chat_id, focus_event, focus_thread)
        if ttl <= 0.0 or not key:
            return False
        now = time.monotonic()
        identity = self._attention_topic(focus_event, focus_thread)
        matched_key = ""
        for candidate_key, raw_entry in self._ambient_ignore_cache.items():
            if isinstance(raw_entry, tuple):
                expires_at, entry_chat_id, entry_identity = raw_entry
            else:
                expires_at = float(raw_entry or 0.0)
                entry_chat_id = candidate_key.split("\x1f", 1)[0]
                entry_identity = AttentionTopicIdentity()
            if float(expires_at or 0.0) <= now or str(entry_chat_id or "") != str(chat_id or ""):
                continue
            same_causal_root = bool(
                candidate_key == key
                and identity.source == "causal_root"
                and entry_identity.source == "causal_root"
                and identity.root_event_id
                and identity.root_event_id == entry_identity.root_event_id
            )
            if same_causal_root:
                matched_key = candidate_key
                break
            if not (
                identity.is_known
                and entry_identity.is_known
                and identity.history_topic_epoch == entry_identity.history_topic_epoch
            ):
                continue
            if attention_topic_anchors_match(
                entry_identity.anchor_text,
                identity.anchor_text,
            ):
                matched_key = candidate_key
                break
        if not matched_key:
            return False
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_cache_hit", True)
            focus_event.set_extra("astrmai_judge_cache_action", "IGNORE")
            focus_event.set_extra("astrmai_judge_cache_scope", "ambient_topic")
            focus_event.set_extra("astrmai_judge_avoided", True)
        return True

    def _cache_ambient_ignore(self, chat_id: str, focus_event: Any, focus_thread: Any) -> None:
        ttl = self._ambient_ignore_cache_ttl()
        key = self._ambient_ignore_cache_key(chat_id, focus_event, focus_thread)
        if ttl <= 0.0 or not key or self._is_direct_event(focus_event):
            return
        self._prune_ignore_caches()
        self._ambient_ignore_cache[key] = (
            time.monotonic() + ttl,
            str(chat_id or ""),
            self._attention_topic(focus_event, focus_thread),
        )

    def _prune_ignore_caches(self) -> None:
        """Keep short-lived Judge caches bounded during long-running bot sessions."""
        now = time.monotonic()
        judge_before = len(self._judge_ignore_cache)
        self._judge_ignore_cache = {
            key: entry
            for key, entry in self._judge_ignore_cache.items()
            if float(entry[0] or 0.0) > now
        }
        ambient_before = len(self._ambient_ignore_cache)
        self._ambient_ignore_cache = {
            key: entry
            for key, entry in self._ambient_ignore_cache.items()
            if float(entry[0] if isinstance(entry, tuple) else entry or 0.0) > now
        }
        self._count("judge_ignore_cache_ttl_evicted", judge_before - len(self._judge_ignore_cache))
        self._count("ambient_ignore_cache_ttl_evicted", ambient_before - len(self._ambient_ignore_cache))
        max_entries = 2048
        if len(self._judge_ignore_cache) > max_entries:
            capacity_evicted = len(self._judge_ignore_cache) - max_entries
            newest = sorted(
                self._judge_ignore_cache.items(),
                key=lambda item: float(item[1][0] or 0.0),
                reverse=True,
            )[:max_entries]
            self._judge_ignore_cache = dict(newest)
            self._count("judge_ignore_cache_capacity_evicted", capacity_evicted)
        if len(self._ambient_ignore_cache) > max_entries:
            capacity_evicted = len(self._ambient_ignore_cache) - max_entries
            newest = sorted(
                self._ambient_ignore_cache.items(),
                key=lambda item: float(
                    item[1][0] if isinstance(item[1], tuple) else item[1] or 0.0
                ),
                reverse=True,
            )[:max_entries]
            self._ambient_ignore_cache = dict(newest)
            self._count("ambient_ignore_cache_capacity_evicted", capacity_evicted)

    def _prune_participation_states(self) -> None:
        ttl = float(
            getattr(
                self._attention_config(),
                "participation_hysteresis_ttl_sec",
                180.0,
            )
            or 180.0
        )
        now = time.time()
        before = len(self._participation_states)
        self._participation_states = {
            chat_id: state
            for chat_id, state in self._participation_states.items()
            if state.updated_at > 0.0 and now - state.updated_at <= max(1.0, ttl)
        }
        self._count("participation_state_ttl_evicted", before - len(self._participation_states))
        max_entries = 2048
        if len(self._participation_states) > max_entries:
            capacity_evicted = len(self._participation_states) - max_entries
            newest = sorted(
                self._participation_states.items(),
                key=lambda item: float(item[1].updated_at or 0.0),
                reverse=True,
            )[:max_entries]
            self._participation_states = dict(newest)
            self._count("participation_state_capacity_evicted", capacity_evicted)

    def clear_chat_state(self, chat_id: str) -> bool:
        normalized = str(chat_id or "")
        removed = self._participation_states.pop(normalized, None) is not None
        validation_task = self._validation_tasks_by_chat.pop(normalized, None)
        if validation_task is not None and not validation_task.done():
            validation_task.cancel()
            removed = True
        exact_keys = [
            key
            for key, entry in self._judge_ignore_cache.items()
            if str(entry[1] or "") == normalized
        ]
        ambient_prefix = f"{normalized}\x1f"
        ambient_keys = []
        for key, entry in self._ambient_ignore_cache.items():
            entry_chat_id = (
                str(entry[1] or "")
                if isinstance(entry, tuple)
                else key.split("\x1f", 1)[0]
            )
            if entry_chat_id == normalized or key.startswith(ambient_prefix):
                ambient_keys.append(key)
        for key in exact_keys:
            self._judge_ignore_cache.pop(key, None)
        for key in ambient_keys:
            self._ambient_ignore_cache.pop(key, None)
        return bool(removed or exact_keys or ambient_keys)

    async def _recent_committed_turn(
        self,
        chat_id: str,
        focus_event: Any,
        *,
        ttl_seconds: float,
    ) -> Any:
        store = getattr(self.gate, "dialogue_store", None)
        if store is None or not hasattr(store, "get_recent_bot_turns"):
            return None
        try:
            sender_id = str(focus_event.get_sender_id() or "").strip()
        except Exception:
            sender_id = ""
        if not sender_id:
            return None
        now = self._event_timestamp(focus_event) or None
        try:
            turns = await store.get_recent_bot_turns(
                chat_id,
                target_sender_id=sender_id,
                ttl_seconds=ttl_seconds,
                max_items=1,
                now=now,
            )
        except Exception as exc:
            logger.debug(f"[AttentionGate] committed turn lookup degraded: {exc}")
            return None
        return turns[-1] if turns else None

    @staticmethod
    def _record_participation(event: Any, result: ParticipationResult) -> None:
        if event is None or not hasattr(event, "set_extra"):
            return
        event.set_extra("astrmai_participation_score", result.score)
        event.set_extra("astrmai_participation_signals", list(result.signals))
        event.set_extra("astrmai_participation_phase", result.phase)
        event.set_extra("astrmai_participation_phase_age_ms", result.phase_age_ms)
        event.set_extra("astrmai_prefilter_shadow_action", result.action.lower())
        event.set_extra(
            "astrmai_strong_wakeup_override",
            bool(result.strong_wakeup_event_ids),
        )
        event.set_extra(
            "astrmai_strong_wakeup_event_ids",
            list(result.strong_wakeup_event_ids),
        )
        event.set_extra(
            "astrmai_observation_invalidated_reason",
            result.invalidated_reason,
        )

    @staticmethod
    def _record_judge_agreement(
        event: Any,
        *,
        shadow_action: str,
        judge_action: str,
    ) -> None:
        if event is None or not hasattr(event, "set_extra"):
            return
        normalized_shadow = str(shadow_action or "NEED_JUDGE").upper()
        normalized_judge = str(judge_action or "").upper()
        if normalized_shadow == "FORCE_PASS":
            agreement: bool | None = normalized_judge in {"PASS", "REPLY", "TOOL_CALL"}
        elif normalized_shadow == "DROP":
            agreement = normalized_judge == "IGNORE"
        else:
            agreement = None
        event.set_extra("astrmai_prefilter_judge_agreement", agreement)

    def _is_active_bot_continuation(
        self,
        focus_event: Any,
        focus_thread: Any,
        events: list[Any],
    ) -> bool:
        if self._is_private_event(focus_event):
            return False
        try:
            focus_index = next(index for index, event in enumerate(events) if event is focus_event)
        except StopIteration:
            return False
        previous_event = None
        for event in reversed(events[:focus_index]):
            if self._event_text(event):
                previous_event = event
                break
        if previous_event is None:
            return False
        bot_id = str(getattr(getattr(self.gate, "state_engine", None), "bot_id", "") or "")
        try:
            previous_sender_id = str(previous_event.get_sender_id() or "")
        except Exception:
            previous_sender_id = ""
        if not bot_id or previous_sender_id != bot_id:
            return False
        current_ts = self._event_timestamp(focus_event)
        previous_ts = self._event_timestamp(previous_event)
        if current_ts > 0.0 and previous_ts > 0.0 and current_ts - previous_ts > 180.0:
            return False
        text = self._event_text(focus_event).strip().lower()
        explicit_continuations = {
            "?",
            "？",
            "嗯",
            "嗯嗯",
            "好",
            "好的",
            "行",
            "可以",
            "对",
            "不对",
            "继续",
            "然后呢",
            "那个呢",
            "还有呢",
            "是吗",
            "为什么",
            "啥意思",
            "什么意思",
            "我呢",
            "那我呢",
            "再说一遍",
        }
        root_reason = str(getattr(focus_thread, "root_reason", "") or "")
        return text in explicit_continuations or root_reason == "recent_assistant_turn"

    def _classify_prefilter(
        self,
        focus_event: Any,
        focus_thread: Any,
        events: list[Any],
        *,
        is_strong_wakeup: bool,
    ) -> AttentionPrefilterDecision:
        if is_strong_wakeup:
            return AttentionPrefilterDecision("FORCE_PASS", "strong_wakeup")
        if not self._is_private_event(focus_event) and self._is_direct_event(focus_event):
            return AttentionPrefilterDecision("FORCE_PASS", "direct_request")
        if self._is_active_bot_continuation(focus_event, focus_thread, events):
            return AttentionPrefilterDecision("FORCE_PASS", "active_bot_continuation")

        interaction_kind = ""
        image_refs: list[Any] = []
        if focus_event is not None and hasattr(focus_event, "get_extra"):
            interaction_kind = str(
                focus_event.get_extra("astrmai_interaction_kind", "") or ""
            ).strip()
            image_refs = list(
                focus_event.get_extra(
                    "extracted_image_refs",
                    focus_event.get_extra("extracted_image_urls", []),
                )
                or []
            )
        if (
            not self._is_private_event(focus_event)
            and not self._event_text(focus_event)
            and not interaction_kind
            and not image_refs
        ):
            return AttentionPrefilterDecision("DROP", "empty_group_event")
        return AttentionPrefilterDecision()

    @staticmethod
    def _record_prefilter(event: Any, decision: AttentionPrefilterDecision) -> None:
        if event is None or not hasattr(event, "set_extra"):
            return
        event.set_extra("astrmai_attention_prefilter_action", decision.action.lower())
        event.set_extra("astrmai_attention_prefilter_reason", decision.reason)

    def _fallback_decision(
        self,
        focus_event: Any,
        *,
        interaction_kind: str,
        reason: str,
    ) -> AttentionDecision:
        """Choose a conservative fallback when System1 has no usable result.

        Private messages and explicit bot mentions are direct requests and must
        remain responsive. Unmentioned group traffic should not be promoted to
        a reply merely because the judge failed.
        """
        if interaction_kind == "peer_poke":
            action = "IGNORE"
            fallback_reason = "peer_poke"
        elif self._is_private_event(focus_event) or self._is_direct_event(focus_event):
            action = "PASS"
            fallback_reason = "direct"
        else:
            action = "WAIT"
            fallback_reason = "group_unmentioned"
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_fallback_action", action.lower())
            focus_event.set_extra("astrmai_judge_fallback_reason", fallback_reason)
        return AttentionDecision(
            action=action,
            raw_action=action,
            reason=(
                f"peer_poke_{reason}"
                if interaction_kind == "peer_poke"
                else f"{reason}:{fallback_reason}"
            ),
        )

    async def _run_judge_with_hard_deadline(
        self,
        coroutine: Any,
        *,
        timeout_sec: float,
    ) -> Any:
        task = asyncio.create_task(coroutine)
        done, _ = await asyncio.wait({task}, timeout=timeout_sec)
        if task in done:
            return task.result()
        task.cancel()
        task.add_done_callback(self._consume_background_result)
        raise asyncio.TimeoutError

    @staticmethod
    def build_judge_window_message(events: list[Any]) -> str:
        lines: list[str] = []
        for event in events:
            text = str(event.get_extra("astrmai_rich_text", getattr(event, "message_str", "")) or "").strip()
            if not text:
                continue
            speaker = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
            lines.append(f"{speaker or 'User'}: {text}")
        return "\n".join(lines).strip()

    async def evaluate(
        self,
        chat_id: str,
        focus_event: Any,
        focus_thread: Any,
        events: list[Any],
        *,
        is_strong_wakeup: bool,
    ) -> AttentionDecision:
        attention_config = self._attention_config()
        participation_enabled = bool(
            getattr(attention_config, "participation_policy_enabled", True)
        )
        participation_result: ParticipationResult | None = None
        is_private = self._is_private_event(focus_event)
        attention_eligible = bool(
            not is_private
            and not self._is_direct_event(focus_event)
            and not bool(
                focus_event.get_extra("astrmai_is_proactive_event", False)
                if focus_event is not None and hasattr(focus_event, "get_extra")
                else False
            )
        )
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_attention_eligible", attention_eligible)
        if attention_eligible:
            self._count("attention_eligible")
        if participation_enabled and not is_private:
            self._prune_participation_states()
            ttl_seconds = float(
                getattr(
                    attention_config,
                    "participation_hysteresis_ttl_sec",
                    180.0,
                )
                or 180.0
            )
            strong_event_ids = self._strong_wakeup_event_ids(
                events,
                focus_event,
                is_strong_wakeup=is_strong_wakeup,
            )
            committed_timer = ArchitectureTimer()
            shadow_committed_turn = await self._recent_committed_turn(
                chat_id,
                focus_event,
                ttl_seconds=ttl_seconds,
            )
            committed_history_enabled = rollout_enabled(
                getattr(self.gate, "config", None),
                "committed_history_enabled",
                True,
            )
            recent_committed_turn = (
                shadow_committed_turn if committed_history_enabled else None
            )
            record_architecture_observation(
                focus_event,
                "committed_history",
                {
                    "read_enabled": committed_history_enabled,
                    "shadow_found": shadow_committed_turn is not None,
                    "commit_id": str(
                        getattr(shadow_committed_turn, "commit_id", "") or ""
                    ),
                    "target_actor_id": str(
                        getattr(shadow_committed_turn, "target_actor_id", "") or ""
                    ),
                    "elapsed_ms": committed_timer.elapsed_ms,
                },
            )
            participation_result, next_state = self.participation_policy.evaluate(
                focus_event=focus_event,
                batch_events=events,
                strong_wakeup_event_ids=strong_event_ids,
                recent_committed_turn=recent_committed_turn,
                previous_state=self._participation_states.get(chat_id),
                topic_identity=self._attention_topic(focus_event, focus_thread),
                ttl_seconds=ttl_seconds,
            )
            self._participation_states[chat_id] = next_state
            self._record_participation(focus_event, participation_result)

            force_pass_enabled = bool(
                getattr(attention_config, "participation_force_pass_enabled", True)
            )
            drop_enabled = bool(
                getattr(attention_config, "participation_drop_enabled", False)
            )
            legacy_continuation = (
                getattr(self.gate, "dialogue_store", None) is None
                and self._is_active_bot_continuation(
                    focus_event,
                    focus_thread,
                    events,
                )
            )
            if legacy_continuation:
                prefilter = AttentionPrefilterDecision(
                    "FORCE_PASS",
                    "active_bot_continuation",
                )
            elif participation_result.action == "FORCE_PASS" and (
                force_pass_enabled or bool(strong_event_ids)
            ):
                if is_strong_wakeup:
                    reason = "strong_wakeup"
                elif self._is_direct_event(focus_event):
                    reason = "direct_request"
                else:
                    reason = participation_result.reason
                prefilter = AttentionPrefilterDecision("FORCE_PASS", reason)
            elif participation_result.action == "DROP" and (
                drop_enabled or "empty_event" in participation_result.signals
            ):
                reason = (
                    "empty_group_event"
                    if "empty_event" in participation_result.signals
                    else participation_result.reason
                )
                prefilter = AttentionPrefilterDecision("DROP", reason)
            else:
                prefilter = AttentionPrefilterDecision()
        else:
            prefilter = self._classify_prefilter(
                focus_event,
                focus_thread,
                events,
                is_strong_wakeup=is_strong_wakeup,
            )
        self._record_prefilter(focus_event, prefilter)
        self._count(f"prefilter_{prefilter.action.lower()}")
        if prefilter.action == "FORCE_PASS":
            self._count("judge_avoided_force_pass")
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_avoided", True)
            return AttentionDecision(
                action="PASS",
                raw_action="PASS",
                reason=f"prefilter:{prefilter.reason}",
            )
        if prefilter.action == "DROP":
            self._count("judge_avoided_drop")
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_avoided", True)
            await self._sample_avoided_decision(
                chat_id,
                focus_event,
                focus_thread,
                events,
                source="prefilter_drop",
            )
            return AttentionDecision(
                action="IGNORE",
                raw_action="IGNORE",
                reason=f"prefilter:{prefilter.reason}",
            )
        if self._cached_judge_ignore(chat_id, focus_event, focus_thread):
            self._count("judge_avoided_exact_cache")
            await self._sample_avoided_decision(
                chat_id,
                focus_event,
                focus_thread,
                events,
                source="exact_cache",
            )
            return AttentionDecision(action="IGNORE", raw_action="IGNORE", reason="judge_ignore_cache")
        if self._cached_ambient_ignore(chat_id, focus_event, focus_thread):
            self._count("judge_avoided_ambient_cache")
            await self._sample_avoided_decision(
                chat_id,
                focus_event,
                focus_thread,
                events,
                source="ambient_cache",
            )
            return AttentionDecision(action="IGNORE", raw_action="IGNORE", reason="judge_ambient_cooldown")
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_avoided", False)
        if not self.gate.judge or not hasattr(self.gate.judge, "evaluate"):
            return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_unavailable")
        interaction_kind = ""
        if focus_event is not None and hasattr(focus_event, "get_extra"):
            interaction_kind = str(focus_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        if (
            focus_event is not None
            and hasattr(focus_event, "get_extra")
            and hasattr(focus_event, "set_extra")
            and not bool(focus_event.get_extra("astrmai_is_proactive_event", False))
            and not bool(focus_event.get_extra("astrmai_primary_mood_applied", False))
            # OPT-08/RT-03: mood 后置开启时，判决前不再做情绪 LLM（由 gate 在
            # judge_action 确定后 fire-and-forget，WAIT/IGNORE 不付调用）
            and not (
                callable(getattr(self.gate, "_mood_post_judge_enabled", None))
                and self.gate._mood_post_judge_enabled()
            )
            and hasattr(self.gate, "state_engine")
            and hasattr(self.gate.state_engine, "update_mood")
        ):
            focus_text = str(getattr(focus_event, "message_str", "") or "").strip()
            reply_config = getattr(getattr(self.gate, "config", None), "reply", None)
            shape_policy = resolve_reply_shape_policy(focus_event, focus_text, reply_config)
            skip_micro_mood = str(shape_policy.get("mode", "")) == "micro"
            if skip_micro_mood:
                focus_event.set_extra("astrmai_primary_mood_skipped_reason", "micro_utterance")
            elif focus_text:
                try:
                    mood_tag, mood_value = await self.gate.state_engine.update_mood(chat_id, focus_text)
                    focus_event.set_extra("astrmai_primary_mood_applied", True)
                    focus_event.set_extra("astrmai_primary_mood_tag", str(mood_tag or "neutral"))
                    focus_event.set_extra("astrmai_primary_mood_value", float(mood_value))
                    focus_event.set_extra("astrmai_primary_mood_source", "attention_pre_judge")
                except Exception as exc:
                    logger.debug(f"[AttentionGate] primary mood update degraded: {exc}")
        message = self.build_judge_window_message(events) or str(getattr(focus_event, "message_str", "") or "")
        # Keep the standalone fallback aligned with TimingConfig; a partial host
        # config must not silently restore the old 3-second budget.
        attention_config = getattr(getattr(self.gate, "config", None), "attention", None)
        judge_timeout = float(
            getattr(attention_config, "judge_timeout", getattr(getattr(self.gate, "config", None), "judge_timeout", 20.0))
            or 20.0
        )
        judge_timeout = clamp_timeout_to_turn_budget(
            focus_event,
            judge_timeout,
            reserve_for_reply=True,
        )
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_timeout_sec", round(judge_timeout, 3))
            focus_event.set_extra("astrmai_judge_timeout", False)
        if judge_timeout <= 0.0:
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "budget_exhausted")
                focus_event.set_extra("astrmai_judge_failure_type", "budget_exhausted")
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_budget_exhausted",
            )

        judge_evaluate = self.gate.judge.evaluate
        self._count("judge_called")
        if self._judge_supports_context_kwargs(judge_evaluate):
            judge_coroutine = judge_evaluate(
                chat_id,
                message,
                False,
                "",
                len(events),
                False,
                focus_thread=focus_thread,
                window_events=events,
                focus_event=focus_event,
            )
        else:
            judge_coroutine = judge_evaluate(chat_id, message, False, "", len(events), False)
        try:
            result = await self._run_judge_with_hard_deadline(
                judge_coroutine,
                timeout_sec=judge_timeout,
            )
        except asyncio.TimeoutError:
            self._consecutive_timeouts += 1
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "timeout")
                focus_event.set_extra("astrmai_judge_timeout", True)
                focus_event.set_extra("astrmai_judge_failure_type", "timeout")
            if self._consecutive_timeouts % 10 == 1:
                logger.warning(
                    f"[AttentionGate] Judge timeout x{self._consecutive_timeouts} for {chat_id}; passing through"
                )
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_timeout",
            )
        except Exception as exc:
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "degraded")
                focus_event.set_extra("astrmai_judge_failure_type", type(exc).__name__)
            logger.debug(f"[AttentionGate] Judge degraded: {exc}")
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_degraded",
            )

        if result is None or not str(getattr(result, "action", "") or "").strip():
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "empty_response")
                focus_event.set_extra("astrmai_judge_failure_type", "empty_response")
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_empty_response",
            )
        raw_action = str(getattr(result, "action", "PASS") or "PASS").upper()
        self._count(f"judge_action_{raw_action.lower()}")
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_outcome", raw_action.lower())
        if participation_result is not None:
            self._record_judge_agreement(
                focus_event,
                shadow_action=participation_result.action,
                judge_action=raw_action,
            )
            agreement = focus_event.get_extra("astrmai_prefilter_judge_agreement", None) if hasattr(focus_event, "get_extra") else None
            if agreement is True:
                self._count("prefilter_judge_agreement_true")
            elif agreement is False:
                self._count("prefilter_judge_agreement_false")
        self._consecutive_timeouts = 0  # 重置计数器
        if raw_action in {"WAIT", "IGNORE", "TOOL_CALL"}:
            if raw_action == "IGNORE":
                self._cache_judge_ignore(chat_id, focus_event, focus_thread)
                self._cache_ambient_ignore(chat_id, focus_event, focus_thread)
            return AttentionDecision(action=raw_action, raw_action=raw_action, reason="judge_gate")
        return AttentionDecision(action="PASS", raw_action=raw_action, reason="judge_pass")


__all__ = [
    "AttentionDecision",
    "AttentionDecisionRouter",
    "AttentionPrefilterDecision",
]
