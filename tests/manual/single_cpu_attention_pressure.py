from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
import time
from collections import defaultdict
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrmai.app.lifecycle import PluginLifecycleManager
from astrmai.app.runtime_context import (
    CoreServices,
    InteractionServices,
    LifecycleServices,
    PluginRuntimeContext,
)
from astrmai.conversation.attention.gate import AttentionGate
from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway
from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from astrmai.infrastructure.runtime.lane_manager import LaneKey, LaneManager


@dataclass(slots=True)
class PressureOptions:
    duration_sec: float = 300.0
    groups: int = 8
    messages_per_group_per_sec: float = 1.0
    provider_delay_min_sec: float = 30.0
    provider_delay_max_sec: float = 45.0
    reply_every: int = 160
    pool_limit: int = 100
    background_concurrency: int = 2
    background_queue_limit: int = 64
    background_wait_timeout_sec: float = 120.0
    background_execution_timeout_sec: float = 300.0
    gateway_concurrency: int = 3
    gateway_wait_timeout_sec: float = 30.0
    attention_drain_timeout_sec: float = 15.0
    physical_drain_timeout_sec: float = 15.0
    sample_interval_sec: float = 1.0
    heartbeat_interval_sec: float = 0.05
    heartbeat_p95_limit_ms: float = 250.0
    heartbeat_p99_limit_ms: float = 500.0
    cpu_index: int = 0
    apply_cpu_affinity: bool = True
    random_seed: int = 20260825
    report_path: str = ""

    def validate(self) -> None:
        if self.duration_sec <= 0:
            raise ValueError("duration_sec must be positive")
        if self.groups < 1:
            raise ValueError("groups must be at least 1")
        if self.messages_per_group_per_sec <= 0:
            raise ValueError("messages_per_group_per_sec must be positive")
        if self.provider_delay_min_sec < 0:
            raise ValueError("provider_delay_min_sec cannot be negative")
        if self.provider_delay_max_sec < self.provider_delay_min_sec:
            raise ValueError("provider_delay_max_sec must be >= provider_delay_min_sec")
        if self.reply_every < 1:
            raise ValueError("reply_every must be at least 1")


class _Conversation:
    def __init__(self, history: list[dict[str, Any]] | None = None):
        self.history = list(history or [])


class _ConversationManager:
    def __init__(self) -> None:
        self.curr: dict[str, str] = {}
        self.conversations: dict[str, _Conversation] = {}
        self.counter = 0

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        return self.curr.get(unified_msg_origin)

    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
        content: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
    ) -> str:
        del platform_id, title, persona_id
        self.counter += 1
        conversation_id = f"pressure-conv-{self.counter}"
        self.curr[unified_msg_origin] = conversation_id
        self.conversations[conversation_id] = _Conversation(content)
        return conversation_id

    async def get_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        create_if_not_exists: bool = False,
    ) -> _Conversation | None:
        del unified_msg_origin, create_if_not_exists
        return self.conversations.get(conversation_id)

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str | None = None,
        history: list[dict[str, Any]] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
        token_usage: Any = None,
    ) -> None:
        del title, persona_id, token_usage
        resolved_id = conversation_id or self.curr.get(unified_msg_origin)
        if resolved_id:
            self.conversations[resolved_id] = _Conversation(history)


class _ProviderResponse:
    def __init__(self, text: str):
        self.completion_text = text
        self.usage = SimpleNamespace(input=16, input_cached=0, output=4)


class _SlowProviderContext:
    def __init__(self, options: PressureOptions, rng: random.Random):
        self.options = options
        self.rng = rng
        self.started = 0
        self.completed = 0
        self.cancelled = 0
        self.failed = 0
        self.active = 0
        self.peak_active = 0
        self.delays_ms: list[float] = []
        self._delay_lock = asyncio.Lock()

    async def llm_generate(self, **kwargs: Any) -> _ProviderResponse:
        del kwargs
        async with self._delay_lock:
            delay = self.rng.uniform(
                self.options.provider_delay_min_sec,
                self.options.provider_delay_max_sec,
            )
        self.started += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        started_at = time.monotonic()
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        except Exception:
            self.failed += 1
            raise
        else:
            self.completed += 1
            self.delays_ms.append(round((time.monotonic() - started_at) * 1000.0, 3))
            return _ProviderResponse("pressure-ok")
        finally:
            self.active = max(0, self.active - 1)

    async def tool_loop_agent(self, **kwargs: Any) -> _ProviderResponse:
        return await self.llm_generate(**kwargs)

    @staticmethod
    def get_provider_by_id(provider_id: str) -> Any:
        if provider_id in {"stress", "stress/model-a"}:
            return SimpleNamespace(meta=lambda: SimpleNamespace(type="openai"))
        return None


class _PressureSensors:
    @staticmethod
    def is_wakeup_signal(event: Any, self_id: str) -> bool:
        del event, self_id
        return False

    @staticmethod
    async def is_command(msg_str: str) -> bool:
        del msg_str
        return False

    @staticmethod
    async def should_process_message(event: Any) -> bool:
        del event
        return True


class _NthReplyJudge:
    def __init__(self, reply_every: int):
        self.reply_every = max(1, int(reply_every))
        self.calls = 0
        self.replies = 0
        self.force_next_reply = False

    async def evaluate(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.calls += 1
        action = (
            "REPLY"
            if self.force_next_reply or self.calls % self.reply_every == 0
            else "IGNORE"
        )
        self.force_next_reply = False
        if action == "REPLY":
            self.replies += 1
        return SimpleNamespace(action=action, reason="pressure_schedule")

    async def evaluate_shadow(self, *args: Any, **kwargs: Any) -> Any:
        return await self.evaluate(*args, **kwargs)


class _PressureEvent:
    def __init__(self, group_index: int, message_index: int):
        self.message_str = (
            f"single cpu pressure group {group_index} message {message_index} "
            "with enough text to follow the regular attention path"
        )
        self.unified_msg_origin = f"default:GroupMessage:pressure-group-{group_index}"
        self.message_obj = SimpleNamespace(
            message=[],
            message_id=f"pressure-{group_index}-{message_index}",
        )
        self.timestamp = time.time()
        self._extra: dict[str, Any] = {}
        self._group_id = f"pressure-group-{group_index}"
        self._sender_id = f"pressure-user-{group_index}"

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_id

    @staticmethod
    def get_self_id() -> str:
        return "pressure-bot"

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)


class _RuntimeCoordinator:
    def __init__(self) -> None:
        self._states: dict[str, Any] = {}

    async def shutdown(self, timeout_sec: float = 1.0) -> None:
        del timeout_sec
        self._states.clear()


class _MemoryEngine:
    async def stop_background_producers(self) -> None:
        return None

    async def close_background_resources(self) -> None:
        return None


class _EventBus:
    def __init__(self) -> None:
        self.abort_triggered = False

    def trigger_abort(self) -> None:
        self.abort_triggered = True

    async def stop(self) -> None:
        return None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * max(0.0, min(100.0, percentile)) / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _heartbeat_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(values),
        "mean_ms": round(statistics.fmean(values), 3) if values else 0.0,
        "p50_ms": _percentile(values, 50),
        "p95_ms": _percentile(values, 95),
        "p99_ms": _percentile(values, 99),
        "max_ms": round(max(values), 3) if values else 0.0,
    }


def _semaphore_status(semaphore: asyncio.Semaphore | None) -> dict[str, int]:
    if semaphore is None:
        return {"available": 0, "waiters": 0}
    waiters = getattr(semaphore, "_waiters", None) or ()
    return {
        "available": int(getattr(semaphore, "_value", 0) or 0),
        "waiters": sum(1 for waiter in waiters if not waiter.done()),
    }


def _build_config(options: PressureOptions) -> Any:
    return SimpleNamespace(
        attention=SimpleNamespace(
            accumulation_pool_max_events=options.pool_limit,
            max_message_length=500,
            focus_thread_enabled=True,
            focus_thread_core_max_messages=4,
            focus_thread_related_max_messages=3,
            ambient_background_max_messages=2,
            thread_same_speaker_followup_sec=8,
            thread_reply_priority_enabled=True,
            participation_policy_enabled=False,
            mood_post_judge_enabled=True,
            judge_ignore_focus_cooldown_enabled=False,
            judge_validation_sample_rate=0.0,
        ),
        system1=SimpleNamespace(wakeup_words=[], nicknames=["AstrMai"]),
        global_settings=SimpleNamespace(debug_mode=False, enable_private_chat=False),
        provider=SimpleNamespace(fallback_models=[]),
        infra=SimpleNamespace(
            max_concurrent_llm_calls=options.gateway_concurrency,
            critical_path_reserved_slots=1,
            llm_retries=0,
            backoff_factor=1.0,
            api_timeout=max(1.0, options.provider_delay_max_sec + 5.0),
            semaphore_wait_timeout_sec=options.gateway_wait_timeout_sec,
            background_task_concurrency=options.background_concurrency,
            background_task_queue_limit=options.background_queue_limit,
            background_task_wait_timeout_sec=options.background_wait_timeout_sec,
            background_task_execution_timeout_sec=options.background_execution_timeout_sec,
        ),
        vision=SimpleNamespace(enable_vision=False, at_image_pair_window_sec=0.5),
        life=SimpleNamespace(enable_proactive=False),
        reply=SimpleNamespace(meme_probability=0),
        conversation=SimpleNamespace(
            enable_dialogue_store=False,
            enable_context_compaction=False,
            enable_prefix_caching=False,
            enable_token_estimator=False,
        ),
        timing=SimpleNamespace(
            hot_reload_shutdown_budget_sec=max(5.0, options.physical_drain_timeout_sec),
            shutdown_snapshot_timeout_sec=0.2,
            shutdown_cancel_grace_sec=1.0,
        ),
    )


def _build_runtime(
    config: Any,
    provider: _SlowProviderContext,
    gateway: GlobalModelGateway,
    lane_manager: LaneManager,
    budget: BackgroundTaskBudget,
    gate: AttentionGate,
) -> tuple[PluginRuntimeContext, PluginLifecycleManager]:
    runtime = PluginRuntimeContext(
        host_context=provider,
        raw_config={},
        config=config,
        runtime_coordinator=_RuntimeCoordinator(),
        host_bridge=None,
        background_task_budget=budget,
    )
    runtime.core = CoreServices(
        gateway=gateway,
        lane_manager=lane_manager,
        memory_engine=_MemoryEngine(),
        event_bus=_EventBus(),
        state_engine=gate.state_engine,
    )
    runtime.interaction = InteractionServices(attention_gate=gate)
    runtime.lifecycle = LifecycleServices()
    runtime.status.is_running = True
    runtime.status.accepting_events = True
    runtime.status.bootstrap_completed = True
    runtime.status.lifecycle_started = True
    manager = PluginLifecycleManager(runtime)
    runtime.lifecycle.manager = manager
    return runtime, manager


def _runtime_snapshot(
    gate: AttentionGate,
    budget: BackgroundTaskBudget,
    gateway: GlobalModelGateway,
    lane_manager: LaneManager,
    provider: _SlowProviderContext,
) -> dict[str, Any]:
    attention = gate.describe_status()
    sessions = {
        chat_id: {
            "pool_length": len(session.accumulation_pool),
            "is_evaluating": bool(session.is_evaluating),
            "oldest_pending_age_ms": round(
                max(0.0, time.time() - float(session.oldest_pending_at or 0.0)) * 1000.0,
                3,
            )
            if session.accumulation_pool and session.oldest_pending_at > 0
            else 0.0,
        }
        for chat_id, session in gate.focus_pools.items()
    }
    budget_status = budget.status()
    return {
        "elapsed_monotonic": time.monotonic(),
        "attention": attention,
        "sessions": sessions,
        "background_budget": budget_status,
        "gateway_global_semaphore": _semaphore_status(gateway._global_semaphore),
        "gateway_background_semaphore": _semaphore_status(gateway._background_semaphore),
        "lane_active": int(getattr(lane_manager, "_active_lane_count", 0) or 0),
        "provider": {
            "started": provider.started,
            "completed": provider.completed,
            "cancelled": provider.cancelled,
            "failed": provider.failed,
            "active": provider.active,
            "peak_active": provider.peak_active,
        },
        "physical_tasks": {
            "attention_sessions": sum(1 for task in gate._session_tasks if not task.done()),
            "attention_background": sum(1 for task in gate._background_tasks if not task.done()),
            "budget_deferred": sum(1 for task in budget._deferred_tasks if not task.done()),
        },
    }


def _queue_timeout_counts(events: list[_PressureEvent]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for event in events:
        stage = str(event.get_extra("astrmai_queue_timeout_stage", "") or "").strip()
        status = str(event.get_extra("astrmai_execution_status", "") or "").strip()
        if stage:
            counts[stage] += 1
        elif "queue_timeout" in status:
            counts[status] += 1
    return dict(sorted(counts.items()))


def _queue_timeout_total(counts: dict[str, int]) -> int:
    return sum(max(0, int(value or 0)) for value in counts.values())


def _sample_queue_timeout_total(sample: dict[str, Any]) -> int:
    budget = sample.get("background_budget", {}) or {}
    return int(budget.get("timed_out", 0) or 0) + _queue_timeout_total(
        sample.get("queue_timeouts_by_stage", {}) or {}
    )


def _final_window_timeout_growth(
    samples: list[dict[str, Any]],
    window_sec: float = 60.0,
) -> int:
    if not samples:
        return 0
    latest = samples[-1]
    target = float(latest.get("elapsed_sec", 0.0) or 0.0) - max(0.0, window_sec)
    baseline = samples[0]
    for sample in samples:
        if float(sample.get("elapsed_sec", 0.0) or 0.0) <= target:
            baseline = sample
        else:
            break
    return max(
        0,
        _sample_queue_timeout_total(latest) - _sample_queue_timeout_total(baseline),
    )


async def _wait_for_attention_drain(gate: AttentionGate, timeout_sec: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() < deadline:
        status = gate.describe_status()
        if (
            int(status.get("pool_length", 0) or 0) == 0
            and int(status.get("worker_count", 0) or 0) == 0
            and not bool(status.get("is_evaluating", False))
        ):
            return True
        await asyncio.sleep(0.05)
    return False


async def run_pressure(options: PressureOptions) -> dict[str, Any]:
    options.validate()
    rng = random.Random(options.random_seed)
    process = psutil.Process()
    original_affinity = list(process.cpu_affinity())
    selected_cpu: int | None = None
    affinity_error = ""
    if options.apply_cpu_affinity:
        try:
            if not original_affinity:
                raise RuntimeError("process exposes no eligible CPUs")
            selected_cpu = original_affinity[options.cpu_index % len(original_affinity)]
            process.cpu_affinity([selected_cpu])
        except Exception as exc:
            affinity_error = f"{type(exc).__name__}: {exc}"

    config = _build_config(options)
    provider = _SlowProviderContext(options, rng)
    conversation_manager = _ConversationManager()
    lane_manager = LaneManager(conversation_manager, config=config)
    gateway = GlobalModelGateway(provider, config)
    gateway.set_lane_manager(lane_manager)
    budget = BackgroundTaskBudget(
        limit=options.background_concurrency,
        max_queue=options.background_queue_limit,
        wait_timeout_sec=options.background_wait_timeout_sec,
        execution_timeout_sec=options.background_execution_timeout_sec,
    )
    judge = _NthReplyJudge(options.reply_every)
    state_engine = SimpleNamespace(config=config, dialogue_store=None, context_compaction=None)
    dispatch_count = 0
    dispatch_success = 0
    dispatch_failure = 0

    async def system2_callback(event: _PressureEvent, events: list[_PressureEvent]) -> Any:
        nonlocal dispatch_count, dispatch_success, dispatch_failure
        dispatch_count += 1
        scope_id = str(event.get_group_id() or event.unified_msg_origin)
        prompt = "\n".join(str(item.message_str) for item in events[-8:])
        try:
            result = await gateway.chat_in_lane_result(
                lane_key=LaneKey(
                    subsystem="sys2",
                    task_family="dialog",
                    scope_id=scope_id,
                ),
                base_origin=event.unified_msg_origin,
                prompt=prompt,
                system_prompt="single CPU pressure fixture",
                models=["stress/model-a"],
                prefix_hash="single-cpu-pressure-v1",
                use_fallback=False,
                event=event,
                timeout_override=max(1.0, options.provider_delay_max_sec + 5.0),
                max_retries_override=0,
                max_models_override=1,
                critical_path=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            dispatch_failure += 1
            raise
        if result.ok:
            dispatch_success += 1
        else:
            dispatch_failure += 1
        return result

    gate = AttentionGate(
        state_engine=state_engine,
        judge=judge,
        sensors=_PressureSensors(),
        system2_callback=system2_callback,
        config=config,
        background_task_budget=budget,
    )
    runtime, lifecycle = _build_runtime(config, provider, gateway, lane_manager, budget, gate)

    stop = asyncio.Event()
    heartbeat_ms: list[float] = []
    samples: list[dict[str, Any]] = []
    peak_pool_by_chat: defaultdict[str, int] = defaultdict(int)
    ingress_results: defaultdict[str, int] = defaultdict(int)
    producer_errors: list[str] = []
    produced_events: list[_PressureEvent] = []
    produced = 0
    started_at = time.monotonic()

    async def heartbeat() -> None:
        deadline = time.monotonic() + options.heartbeat_interval_sec
        while not stop.is_set():
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            now = time.monotonic()
            heartbeat_ms.append(max(0.0, now - deadline) * 1000.0)
            deadline += options.heartbeat_interval_sec

    async def sampler() -> None:
        while not stop.is_set():
            snapshot = _runtime_snapshot(gate, budget, gateway, lane_manager, provider)
            snapshot["elapsed_sec"] = round(time.monotonic() - started_at, 3)
            for chat_id, session_status in snapshot["sessions"].items():
                peak_pool_by_chat[chat_id] = max(
                    peak_pool_by_chat[chat_id],
                    int(session_status["pool_length"]),
                )
            snapshot["queue_timeouts_by_stage"] = _queue_timeout_counts(produced_events)
            samples.append(snapshot)
            await asyncio.sleep(options.sample_interval_sec)

    async def producer(group_index: int) -> None:
        nonlocal produced
        interval = 1.0 / options.messages_per_group_per_sec
        next_at = time.monotonic()
        message_index = 0
        while not stop.is_set():
            event = _PressureEvent(group_index, message_index)
            produced_events.append(event)
            try:
                result = await gate.process_event(event)
                ingress_results[str(result)] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                producer_errors.append(f"group={group_index} {type(exc).__name__}: {exc}")
            produced += 1
            message_index += 1
            next_at += interval
            await asyncio.sleep(max(0.0, next_at - time.monotonic()))

    heartbeat_task = asyncio.create_task(heartbeat(), name="pressure-heartbeat")
    sampler_task = asyncio.create_task(sampler(), name="pressure-sampler")
    producers = [
        asyncio.create_task(producer(group_index), name=f"pressure-producer-{group_index}")
        for group_index in range(options.groups)
    ]

    try:
        await asyncio.sleep(options.duration_sec)
        stop.set()
        await asyncio.gather(*producers, return_exceptions=True)
        await asyncio.gather(heartbeat_task, sampler_task, return_exceptions=True)
        stopped_snapshot = _runtime_snapshot(gate, budget, gateway, lane_manager, provider)
        stopped_timed_out = int(stopped_snapshot["background_budget"].get("timed_out", 0) or 0)
        stopped_event_timeouts = _queue_timeout_counts(produced_events)

        attention_drained = await _wait_for_attention_drain(
            gate,
            options.attention_drain_timeout_sec,
        )
        before_shutdown = _runtime_snapshot(gate, budget, gateway, lane_manager, provider)
        before_shutdown_event_timeouts = _queue_timeout_counts(produced_events)
        await lifecycle.terminate()
        idle_report = await budget.wait_until_idle(options.physical_drain_timeout_sec)
        after_shutdown = _runtime_snapshot(gate, budget, gateway, lane_manager, provider)
        after_shutdown_event_timeouts = _queue_timeout_counts(produced_events)

        gate.reset_runtime_state()
        budget_resumed = budget.resume_if_idle()
        reload_runtime, reload_lifecycle = _build_runtime(
            config,
            provider,
            gateway,
            lane_manager,
            budget,
            gate,
        )
        reload_event = _PressureEvent(0, produced + 1)
        judge.force_next_reply = True
        provider_completed_before_reload = provider.completed
        reload_result = await gate.process_event(reload_event)
        reload_deadline = time.monotonic() + options.provider_delay_max_sec + 10.0
        while provider.completed <= provider_completed_before_reload and time.monotonic() < reload_deadline:
            await asyncio.sleep(0.05)
        reload_provider_completed = provider.completed > provider_completed_before_reload
        await reload_lifecycle.terminate()
        final_idle_report = await budget.wait_until_idle(options.physical_drain_timeout_sec)
        final_snapshot = _runtime_snapshot(gate, budget, gateway, lane_manager, provider)
        final_event_timeouts = _queue_timeout_counts(produced_events)

        heartbeat = _heartbeat_summary(heartbeat_ms)
        midpoint = max(1, len(heartbeat_ms) // 2)
        heartbeat_early = _heartbeat_summary(heartbeat_ms[:midpoint])
        heartbeat_late = _heartbeat_summary(heartbeat_ms[midpoint:])
        maximum_pool = max(peak_pool_by_chat.values(), default=0)
        post_shutdown_timeout_growth = max(
            0,
            int(final_snapshot["background_budget"].get("timed_out", 0) or 0)
            - int(after_shutdown["background_budget"].get("timed_out", 0) or 0),
        )
        post_shutdown_event_timeout_growth = max(
            0,
            _queue_timeout_total(final_event_timeouts)
            - _queue_timeout_total(after_shutdown_event_timeouts),
        )
        heartbeat_degradation_limit_ms = max(
            50.0,
            float(heartbeat_early["p99_ms"]) * 1.5,
        )
        final_window_timeout_growth = _final_window_timeout_growth(samples)
        peak_budget_active = max(
            (
                int(sample["background_budget"].get("active", 0) or 0)
                for sample in samples
            ),
            default=0,
        )
        peak_budget_queued = max(
            (
                int(sample["background_budget"].get("queued", 0) or 0)
                for sample in samples
            ),
            default=0,
        )
        acceptance = {
            "cpu_affinity_applied": bool(
                selected_cpu is not None and process.cpu_affinity() == [selected_cpu]
            ),
            "per_chat_pool_bounded": maximum_pool <= options.pool_limit,
            "attention_naturally_drained": attention_drained,
            "oldest_pending_cleared": float(final_snapshot["attention"]["oldest_pending_age_ms"]) == 0.0,
            "workers_stopped": int(final_snapshot["attention"]["worker_count"]) == 0
            and not bool(final_snapshot["attention"]["is_evaluating"]),
            "background_budget_idle": int(final_snapshot["background_budget"]["active"]) == 0
            and int(final_snapshot["background_budget"]["queued"]) == 0
            and int(final_snapshot["background_budget"].get("deferred_tasks", 0) or 0) == 0,
            "physical_tasks_zero": max(final_snapshot["physical_tasks"].values(), default=0) == 0
            and int(final_idle_report.get("remaining", 0) or 0) == 0,
            "queue_timeouts_stable_after_shutdown": (
                post_shutdown_timeout_growth == 0
                and post_shutdown_event_timeout_growth == 0
            ),
            "queue_timeouts_not_growing_in_final_minute": final_window_timeout_growth == 0,
            "heartbeat_p95_within_limit": float(heartbeat["p95_ms"]) <= options.heartbeat_p95_limit_ms,
            "heartbeat_p99_within_limit": float(heartbeat["p99_ms"]) <= options.heartbeat_p99_limit_ms,
            "heartbeat_p99_not_degrading": (
                float(heartbeat_late["p99_ms"]) - float(heartbeat_early["p99_ms"])
                <= heartbeat_degradation_limit_ms
            ),
            "reload_accepts_message": reload_result == "BUFFERED",
            "reload_reaches_provider_success": reload_provider_completed,
            "slow_provider_concurrency_exercised": provider.started >= 2
            and provider.peak_active >= min(2, options.background_concurrency),
            "background_budget_exercised": peak_budget_active > 0,
            "lifecycle_shutdown_completed": runtime.status.boot_phase == "shutdown.complete"
            and reload_runtime.status.boot_phase == "shutdown.complete",
            "no_producer_errors": not producer_errors,
        }
        if not options.apply_cpu_affinity:
            acceptance["cpu_affinity_applied"] = True
        result = {
            "measurement_scope": "local_fake_provider_single_cpu_pressure",
            "production_provider_http_validated": False,
            "real_components": [
                "AttentionGate.process_event",
                "AttentionGate._debounce_and_judge",
                "BackgroundTaskBudget",
                "GlobalModelGateway",
                "LaneManager",
                "PluginLifecycleManager.terminate",
            ],
            "simulated_components": ["Provider HTTP transport"],
            "options": asdict(options),
            "cpu": {
                "original_affinity": original_affinity,
                "selected_cpu": selected_cpu,
                "effective_affinity": process.cpu_affinity(),
                "affinity_error": affinity_error,
            },
            "duration_actual_sec": round(time.monotonic() - started_at, 3),
            "produced": produced,
            "ingress_results": dict(ingress_results),
            "judge": {"calls": judge.calls, "replies": judge.replies},
            "dispatch": {
                "started": dispatch_count,
                "succeeded": dispatch_success,
                "failed": dispatch_failure,
            },
            "provider": {
                "started": provider.started,
                "completed": provider.completed,
                "cancelled": provider.cancelled,
                "failed": provider.failed,
                "peak_active": provider.peak_active,
                "latency_ms": {
                    "p50": _percentile(provider.delays_ms, 50),
                    "p95": _percentile(provider.delays_ms, 95),
                    "p99": _percentile(provider.delays_ms, 99),
                },
            },
            "pool": {
                "limit_per_chat": options.pool_limit,
                "peak_per_chat": dict(sorted(peak_pool_by_chat.items())),
                "maximum_observed": maximum_pool,
            },
            "heartbeat": heartbeat,
            "heartbeat_early": heartbeat_early,
            "heartbeat_late": heartbeat_late,
            "queue_timeout": {
                "background_budget": {
                    "at_input_stop": stopped_timed_out,
                    "after_shutdown": int(after_shutdown["background_budget"].get("timed_out", 0) or 0),
                    "final": int(final_snapshot["background_budget"].get("timed_out", 0) or 0),
                    "growth_after_shutdown": post_shutdown_timeout_growth,
                },
                "event_stages": {
                    "at_input_stop": stopped_event_timeouts,
                    "before_shutdown": before_shutdown_event_timeouts,
                    "after_shutdown": after_shutdown_event_timeouts,
                    "final": final_event_timeouts,
                    "growth_after_shutdown": post_shutdown_event_timeout_growth,
                },
                "growth_in_final_60_sec_of_input": final_window_timeout_growth,
            },
            "background_budget_peak": {
                "active": peak_budget_active,
                "queued": peak_budget_queued,
            },
            "attention_drained_before_shutdown": attention_drained,
            "budget_idle_report": idle_report,
            "final_budget_idle_report": final_idle_report,
            "reload": {
                "process_event_result": reload_result,
                "budget_resumed": budget_resumed,
                "provider_completed": reload_provider_completed,
            },
            "snapshots": {
                "input_stopped": stopped_snapshot,
                "before_shutdown": before_shutdown,
                "after_shutdown": after_shutdown,
                "final": final_snapshot,
            },
            "sample_count": len(samples),
            "samples": samples,
            "producer_errors": producer_errors[:50],
            "acceptance": acceptance,
            "passed": all(acceptance.values()),
        }
        return result
    finally:
        stop.set()
        for task in [*producers, heartbeat_task, sampler_task]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*producers, heartbeat_task, sampler_task, return_exceptions=True)
        try:
            await gate.shutdown_workers()
            budget.begin_drain()
            for task in list(gate._background_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*list(gate._background_tasks), return_exceptions=True)
            await budget.wait_until_idle(options.physical_drain_timeout_sec)
        finally:
            if options.apply_cpu_affinity and original_affinity:
                process.cpu_affinity(original_affinity)


def _default_report_path() -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return Path("artifacts") / "single_cpu_pressure" / f"pressure-{timestamp}.json"


def _parse_args() -> PressureOptions:
    parser = argparse.ArgumentParser(
        description=(
            "Exercise the real Attention -> BackgroundTaskBudget -> Gateway -> Lane chain "
            "with a controllable slow async Provider and optional single-CPU affinity."
        )
    )
    parser.add_argument("--duration-sec", type=float, default=300.0)
    parser.add_argument("--groups", type=int, default=8)
    parser.add_argument("--messages-per-group-per-sec", type=float, default=1.0)
    parser.add_argument("--provider-delay-min-sec", type=float, default=30.0)
    parser.add_argument("--provider-delay-max-sec", type=float, default=45.0)
    parser.add_argument(
        "--reply-every",
        type=int,
        default=160,
        help=(
            "Make every Nth Attention judgment dispatch System2. The default keeps "
            "30-45s dual-slot Provider load near its sustainable rate; use 64 for overload."
        ),
    )
    parser.add_argument("--pool-limit", type=int, default=100)
    parser.add_argument("--background-concurrency", type=int, default=2)
    parser.add_argument("--background-queue-limit", type=int, default=64)
    parser.add_argument("--background-wait-timeout-sec", type=float, default=120.0)
    parser.add_argument("--background-execution-timeout-sec", type=float, default=300.0)
    parser.add_argument("--gateway-concurrency", type=int, default=3)
    parser.add_argument("--gateway-wait-timeout-sec", type=float, default=30.0)
    parser.add_argument("--attention-drain-timeout-sec", type=float, default=15.0)
    parser.add_argument("--physical-drain-timeout-sec", type=float, default=15.0)
    parser.add_argument("--sample-interval-sec", type=float, default=1.0)
    parser.add_argument("--heartbeat-interval-sec", type=float, default=0.05)
    parser.add_argument("--heartbeat-p95-limit-ms", type=float, default=250.0)
    parser.add_argument("--heartbeat-p99-limit-ms", type=float, default=500.0)
    parser.add_argument("--cpu-index", type=int, default=0)
    parser.add_argument("--no-cpu-affinity", action="store_true")
    parser.add_argument("--random-seed", type=int, default=20260825)
    parser.add_argument("--report", dest="report_path", default="")
    args = parser.parse_args()
    return PressureOptions(
        duration_sec=args.duration_sec,
        groups=args.groups,
        messages_per_group_per_sec=args.messages_per_group_per_sec,
        provider_delay_min_sec=args.provider_delay_min_sec,
        provider_delay_max_sec=args.provider_delay_max_sec,
        reply_every=args.reply_every,
        pool_limit=args.pool_limit,
        background_concurrency=args.background_concurrency,
        background_queue_limit=args.background_queue_limit,
        background_wait_timeout_sec=args.background_wait_timeout_sec,
        background_execution_timeout_sec=args.background_execution_timeout_sec,
        gateway_concurrency=args.gateway_concurrency,
        gateway_wait_timeout_sec=args.gateway_wait_timeout_sec,
        attention_drain_timeout_sec=args.attention_drain_timeout_sec,
        physical_drain_timeout_sec=args.physical_drain_timeout_sec,
        sample_interval_sec=args.sample_interval_sec,
        heartbeat_interval_sec=args.heartbeat_interval_sec,
        heartbeat_p95_limit_ms=args.heartbeat_p95_limit_ms,
        heartbeat_p99_limit_ms=args.heartbeat_p99_limit_ms,
        cpu_index=args.cpu_index,
        apply_cpu_affinity=not args.no_cpu_affinity,
        random_seed=args.random_seed,
        report_path=args.report_path,
    )


def main() -> int:
    options = _parse_args()
    report = asyncio.run(run_pressure(options))
    report_path = Path(options.report_path) if options.report_path else _default_report_path()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "passed": report["passed"],
        "report": str(report_path.resolve()),
        "produced": report["produced"],
        "provider": report["provider"],
        "pool": report["pool"],
        "heartbeat": report["heartbeat"],
        "queue_timeout": report["queue_timeout"],
        "acceptance": report["acceptance"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
