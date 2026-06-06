from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.observability import RuntimeObservabilityHub
from ..contracts.observability import ChatMemoryStatusSnapshot, MemoryObservationEvent, MemoryRuntimeStatusSnapshot


class MemoryObserver:
    def __init__(
        self,
        raw_trace_store: Any = None,
        *,
        max_recent_events: int = 500,
        max_events_per_chat: int = 100,
        observability_hub: RuntimeObservabilityHub | None = None,
    ):
        self.raw_trace_store = raw_trace_store
        self.max_recent_events = max(1, int(max_recent_events or 500))
        self.max_events_per_chat = max(1, int(max_events_per_chat or 100))
        self.observability_hub = observability_hub
        self._lock = asyncio.Lock()
        self._recent_events: list[dict[str, Any]] = []
        self._events_by_chat: dict[str, list[dict[str, Any]]] = {}
        self._recent_errors: list[dict[str, Any]] = []
        self._counters = {
            "recent_error_count": 0,
            "recent_warning_count": 0,
            "last_gate_hit_at": 0.0,
            "last_backfill_success_at": 0.0,
            "last_summarize_success_at": 0.0,
            "last_summarize_failure_at": 0.0,
        }
        self._last_stage_by_chat: dict[str, dict[str, str]] = {}

    async def record(
        self,
        *,
        chat_id: str,
        component: str,
        stage: str,
        level: str = "info",
        turn_id: str = "",
        memory_id: str = "",
        reason: str = "",
        summary: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = MemoryObservationEvent(
            event_id=f"memobs_{uuid.uuid4().hex[:12]}",
            chat_id=str(chat_id or ""),
            component=str(component or ""),
            stage=str(stage or ""),
            level=str(level or "info").lower() or "info",
            turn_id=str(turn_id or ""),
            memory_id=str(memory_id or ""),
            reason=str(reason or ""),
            summary=str(summary or ""),
            timestamp=time.time(),
            payload=dict(payload or {}),
        )
        data = asdict(event)
        async with self._lock:
            self._recent_events.append(data)
            self._recent_events = self._recent_events[-self.max_recent_events :]
            chat_items = list(self._events_by_chat.get(event.chat_id, []) or [])
            chat_items.append(data)
            self._events_by_chat[event.chat_id] = chat_items[-self.max_events_per_chat :]
            if event.level in {"warning", "error"}:
                self._recent_errors.append(data)
                self._recent_errors = self._recent_errors[-self.max_recent_events :]
            self._update_counters(event)
            stage_state = dict(self._last_stage_by_chat.get(event.chat_id, {}) or {})
            if event.component == "instant_gate":
                stage_state["last_gate_stage"] = event.stage
            elif event.component == "memory_pipeline":
                stage_state["last_backfill_stage"] = event.stage if "backfill" in event.stage else stage_state.get("last_backfill_stage", "")
                if "maintenance" in event.stage or "idle_timeout" in event.stage or "worker" in event.stage:
                    stage_state["last_pipeline_stage"] = event.stage
            elif event.component == "session_summarizer":
                stage_state["last_summarize_stage"] = event.stage
            self._last_stage_by_chat[event.chat_id] = stage_state
        await self._record_global_observability(data)
        await self._append_trace_event(data)
        return data

    def _update_counters(self, event: MemoryObservationEvent) -> None:
        recent_events = self._recent_events[-self.max_recent_events :]
        self._counters["recent_error_count"] = sum(1 for item in recent_events if str(item.get("level", "")).lower() == "error")
        self._counters["recent_warning_count"] = sum(1 for item in recent_events if str(item.get("level", "")).lower() == "warning")
        if event.component == "instant_gate" and event.stage == "gate_hit":
            self._counters["last_gate_hit_at"] = event.timestamp
        if event.component == "instant_gate" and event.stage == "backfill_success":
            self._counters["last_backfill_success_at"] = event.timestamp
        if event.component == "session_summarizer" and event.stage == "canonical_write_success":
            self._counters["last_summarize_success_at"] = event.timestamp
        if event.component == "session_summarizer" and event.level == "error":
            self._counters["last_summarize_failure_at"] = event.timestamp

    async def _append_trace_event(self, event: dict[str, Any]) -> None:
        store = self.raw_trace_store
        if store is None or not hasattr(store, "append"):
            return
        try:
            await store.append(
                {
                    "chat_id": event.get("chat_id", ""),
                    "created_at": event.get("timestamp", 0.0),
                    "trace_id": event.get("event_id", ""),
                    "stage": f"memory.{event.get('component', '')}.{event.get('stage', '')}",
                    "level": event.get("level", "info"),
                    "reason": event.get("reason", ""),
                    "summary": event.get("summary", ""),
                    "payload": dict(event.get("payload", {}) or {}),
                    "memory_component": event.get("component", ""),
                    "memory_stage": event.get("stage", ""),
                    "memory_event": event,
                }
            )
        except Exception as exc:
            logger.warning(f"[MemoryObserver] raw trace append degraded: {exc}")

    async def _record_global_observability(self, event: dict[str, Any]) -> None:
        hub = self.observability_hub
        if hub is None or not hasattr(hub, "record"):
            return
        try:
            formatted = self.format_timeline_item(event)
            await hub.record(
                event_id=str(event.get("event_id", "") or ""),
                timestamp=float(event.get("timestamp", 0.0) or 0.0),
                domain="memory",
                kind=self._infer_kind(event),
                level=str(event.get("level", "info") or "info"),
                chat_id=str(event.get("chat_id", "") or ""),
                title=str(formatted.get("display_title", "") or formatted.get("stage", "") or "memory"),
                summary=str(event.get("summary", "") or event.get("reason", "") or ""),
                tags={
                    "domain": "memory",
                    "kind": self._infer_kind(event),
                    "level": str(event.get("level", "info") or "info"),
                    "chat_id": str(event.get("chat_id", "") or ""),
                    "component": str(event.get("component", "") or ""),
                    "stage": str(event.get("stage", "") or ""),
                    "source": str(event.get("component", "") or ""),
                },
                facets={
                    "component": str(event.get("component", "") or ""),
                    "stage": str(event.get("stage", "") or ""),
                    "reason": str(event.get("reason", "") or ""),
                    "memory_id": str(event.get("memory_id", "") or ""),
                    "turn_id": str(event.get("turn_id", "") or ""),
                    "display_title": str(formatted.get("display_title", "") or ""),
                },
                detail={"payload": dict(event.get("payload", {}) or {})},
                raw=dict(formatted or {}),
            )
        except Exception as exc:
            logger.warning(f"[MemoryObserver] global observability degraded: {exc}")

    @staticmethod
    def _infer_kind(event: dict[str, Any]) -> str:
        component = str(event.get("component", "") or "")
        stage = str(event.get("stage", "") or "")
        if component == "instant_gate":
            return "action"
        if component == "memory_pipeline":
            if "maintenance" in stage or "idle_timeout" in stage:
                return "maintenance"
            return "trace"
        if component == "session_summarizer":
            return "maintenance"
        return "trace"

    async def runtime_snapshot(
        self,
        *,
        instant_gate_ready: bool,
        memory_pipeline_ready: bool,
        session_summarizer_ready: bool,
        pipeline_status: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = MemoryRuntimeStatusSnapshot(
            instant_gate_ready=bool(instant_gate_ready),
            memory_pipeline_ready=bool(memory_pipeline_ready),
            session_summarizer_ready=bool(session_summarizer_ready),
            pipeline_running=bool((pipeline_status or {}).get("running", False)),
            sweep_task_running=bool((pipeline_status or {}).get("sweep_task_running", False)),
            buffered_chats=int((pipeline_status or {}).get("buffered_chats", 0) or 0),
            tracked_chats=int((pipeline_status or {}).get("tracked_chats", 0) or 0),
            active_worker_count=int((pipeline_status or {}).get("active_worker_count", 0) or 0),
            active_worker_chats=list((pipeline_status or {}).get("active_worker_chats", []) or []),
            recent_error_count=int(self._counters["recent_error_count"]),
            recent_warning_count=int(self._counters["recent_warning_count"]),
            last_gate_hit_at=float(self._counters["last_gate_hit_at"]),
            last_backfill_success_at=float(self._counters["last_backfill_success_at"]),
            last_summarize_success_at=float(self._counters["last_summarize_success_at"]),
            last_summarize_failure_at=float(self._counters["last_summarize_failure_at"]),
        )
        return asdict(payload)

    async def chat_snapshot(
        self,
        *,
        chat_id: str,
        pipeline_buffer: dict[str, Any] | None = None,
        worker_active: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        chat = str(chat_id or "")
        stage_state = dict(self._last_stage_by_chat.get(chat, {}) or {})
        recent_events = await self.recent_events(chat_id=chat, limit=limit)
        payload = ChatMemoryStatusSnapshot(
            chat_id=chat,
            pending_messages=int((pipeline_buffer or {}).get("pending_messages", 0) or 0),
            cooldown_until=float((pipeline_buffer or {}).get("cooldown_until", 0.0) or 0.0),
            failures=int((pipeline_buffer or {}).get("failures", 0) or 0),
            last_update=float((pipeline_buffer or {}).get("last_update", 0.0) or 0.0),
            last_memory_run_at=float((pipeline_buffer or {}).get("last_memory_run_at", 0.0) or 0.0),
            worker_active=bool(worker_active),
            last_gate_stage=str(stage_state.get("last_gate_stage", "") or ""),
            last_backfill_stage=str(stage_state.get("last_backfill_stage", "") or stage_state.get("last_pipeline_stage", "") or ""),
            last_summarize_stage=str(stage_state.get("last_summarize_stage", "") or ""),
            recent_events=recent_events,
        )
        return asdict(payload)

    async def recent_events(
        self,
        *,
        chat_id: str | None = None,
        component: str = "",
        level: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 500))
        async with self._lock:
            source = list(self._events_by_chat.get(str(chat_id), []) or []) if chat_id else list(self._recent_events)
        items = list(reversed(source))
        if component:
            items = [item for item in items if str(item.get("component", "") or "") == str(component)]
        if level:
            items = [item for item in items if str(item.get("level", "") or "").lower() == str(level).lower()]
        return items[:safe_limit]

    @staticmethod
    def format_timeline_item(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        component = str(payload.get("component", "") or "")
        stage = str(payload.get("stage", "") or "")
        level = str(payload.get("level", "info") or "info").lower()
        display_title = {
            ("instant_gate", "gate_entered"): "进入即时记忆门控",
            ("instant_gate", "gate_hit"): "即时记忆命中",
            ("instant_gate", "gate_miss"): "即时记忆未命中",
            ("instant_gate", "backfill_started"): "LLM 补漏开始",
            ("instant_gate", "backfill_success"): "LLM 补漏成功",
            ("instant_gate", "backfill_failed"): "LLM 补漏失败",
            ("instant_gate", "backfill_skipped"): "LLM 补漏跳过",
            ("memory_pipeline", "turn_recorded"): "Turn 已写入缓冲",
            ("memory_pipeline", "event_published"): "后台事件已发布",
            ("memory_pipeline", "worker_spawned"): "会话 Worker 已启动",
            ("memory_pipeline", "worker_consumed"): "Worker 已消费事件",
            ("memory_pipeline", "backfill_started"): "Pipeline 触发补漏",
            ("memory_pipeline", "backfill_finished"): "Pipeline 补漏结束",
            ("memory_pipeline", "backfill_skipped"): "Pipeline 跳过补漏",
            ("memory_pipeline", "idle_timeout"): "会话超时触发维护",
            ("memory_pipeline", "maintenance_started"): "开始长期整理",
            ("memory_pipeline", "maintenance_summarized"): "长期整理完成",
            ("memory_pipeline", "maintenance_rolled_back"): "长期整理回滚",
            ("memory_pipeline", "maintenance_skipped"): "长期整理跳过",
            ("session_summarizer", "summarize_started"): "总结器启动",
            ("session_summarizer", "canonical_write_success"): "权威记忆写入成功",
            ("session_summarizer", "legacy_event_write_success"): "Legacy 事件回写成功",
            ("session_summarizer", "legacy_event_write_failed"): "Legacy 事件回写失败",
            ("session_summarizer", "topic_summarizer_degraded"): "Topic 总结降级",
            ("session_summarizer", "memory_processor_invalid"): "Memory Processor 返回异常",
            ("session_summarizer", "cognitive_feedback_degraded"): "认知反馈写入降级",
            ("session_summarizer", "summarize_skipped"): "总结器跳过本轮总结",
        }.get((component, stage), f"{component}.{stage}")
        payload["display_title"] = display_title
        payload["display_badge"] = component
        payload["display_group"] = "memory"
        payload["display_kind"] = stage
        payload["is_error_like"] = level in {"warning", "error"}
        payload["payload_preview"] = str(payload.get("summary", "") or payload.get("reason", "") or "")[:240]
        return payload

    async def recent_errors(self, *, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 500))
        if chat_id:
            events = await self.recent_events(chat_id=chat_id, limit=safe_limit * 2)
            return [item for item in events if str(item.get("level", "")).lower() in {"warning", "error"}][:safe_limit]
        async with self._lock:
            items = list(reversed(self._recent_errors))
        return items[:safe_limit]

    async def reset(self) -> None:
        async with self._lock:
            self._recent_events = []
            self._events_by_chat = {}
            self._recent_errors = []
            self._last_stage_by_chat = {}
            for key in self._counters:
                self._counters[key] = 0.0 if key.startswith("last_") else 0


__all__ = ["MemoryObserver"]
