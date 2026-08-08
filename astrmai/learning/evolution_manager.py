from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any, Dict, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..infrastructure.runtime.lane_manager import LaneKey
from ..memory.contracts.memory_query import MemoryWriteRequest
from .contracts.learning_events import (
    BotReplyRecordedEvent,
    MiningCompletedEvent,
    UserMessageRecordedEvent,
)
from .logging.bot_reply_recorder import BotReplyRecorder
from .logging.message_recorder import MessageRecorder
from .dedup import GLOBAL_JARGON_SESSION_ID, jargon_fingerprint, normalize_jargon_term
from .mining.expression_miner import ExpressionMiner
from .mining.expression_results import PatternSaveReport
from .mining.jargon_miner import JargonMiner


class EvolutionManager:
    def __init__(self, db, gateway, config=None, event_bus=None):
        self.db = db
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.event_bus = event_bus
        self.expression_miner = ExpressionMiner(
            gateway,
            self.config,
            memory_engine=getattr(self.db, "memory_engine", None),
        )
        self.jargon_miner = JargonMiner(
            self.expression_miner,
            memory_engine=getattr(self.db, "memory_engine", None),
        )
        self.recorder = MessageRecorder(
            window_seconds=getattr(self.config.evolution, "mining_window_sec", 60),
            min_messages=getattr(
                self.config.evolution,
                "mining_window_min_messages",
                getattr(self.config.evolution, "mining_trigger", 20),
            ),
            cooldown_seconds=getattr(self.config.evolution, "mining_cooldown_sec", 60),
        )
        self.bot_reply_recorder = BotReplyRecorder(
            db,
            fallback_text=getattr(self.config.reply, "fallback_text", "（陷入了短暂的沉默...）"),
        )
        self._mining_locks: Dict[str, asyncio.Lock] = {}
        self._lock_mutex = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self._mining_tasks: Dict[str, asyncio.Task] = {}
        self._backlog_task: asyncio.Task | None = None
        self._backlog_failure_until: dict[str, float] = {}
        # Runtime cache mirrors persisted checkpoint failures; evidence is retained on failure.
        self._backlog_failure_counts: dict[str, int] = {}
        self._last_backlog_report: dict[str, Any] = {}
        self._last_expression_backfill: dict[str, Any] = {}
        self._last_mining_outcomes: dict[str, dict[str, Any]] = {}
        self._pipeline_failure_counts: dict[str, int] = {}
        self._last_learning_run_purge_at = 0.0
        self._last_learning_run_purge: dict[str, Any] = {}

    def refresh_config(self, config):
        self.config = config
        evolution = getattr(config, "evolution", None)
        self.recorder.window_seconds = max(int(getattr(evolution, "mining_window_sec", 60) or 60), 10)
        self.recorder.min_messages = max(
            int(
                getattr(
                    evolution,
                    "mining_window_min_messages",
                    getattr(evolution, "mining_trigger", 20),
                )
                or 20
            ),
            2,
        )
        self.recorder.cooldown_seconds = max(int(getattr(evolution, "mining_cooldown_sec", 60) or 60), 5)
        self.bot_reply_recorder.fallback_text = getattr(
            getattr(config, "reply", None),
            "fallback_text",
            self.bot_reply_recorder.fallback_text,
        )
        self.expression_miner.config = config
        self.expression_miner.candidate_extractor.min_count = max(
            int(getattr(evolution, "expression_min_count", 2) or 2),
            1,
        )
        self.expression_miner.enricher.config = config
        self.jargon_miner.candidate_extractor.min_count = max(
            int(getattr(evolution, "jargon_min_count", 2) or 2),
            1,
        )
        self.jargon_miner.min_messages = self._pipeline_threshold("jargon")
        if self.jargon_miner.enricher is not None:
            self.jargon_miner.enricher.config = config
        if not self._backlog_enabled() and self._backlog_task is not None:
            self._backlog_task.cancel()

    def _evolution_config(self):
        return getattr(self.config, "evolution", None)

    def _backlog_enabled(self) -> bool:
        evolution = self._evolution_config()
        return bool(getattr(evolution, "enable_backlog_mining", True))

    def _backlog_threshold(self) -> int:
        evolution = self._evolution_config()
        return max(
            int(getattr(evolution, "backlog_min_unprocessed_logs", 40) or 40),
            int(getattr(evolution, "min_mining_context", 10) or 10),
            1,
        )

    def _backlog_batch_size(self) -> int:
        evolution = self._evolution_config()
        return max(int(getattr(evolution, "backlog_batch_size", 120) or 120), self._backlog_threshold())

    def _backlog_group_limit(self) -> int:
        evolution = self._evolution_config()
        return max(1, int(getattr(evolution, "backlog_group_limit", 2) or 2))

    def _backlog_scan_interval(self) -> int:
        evolution = self._evolution_config()
        return max(60, int(getattr(evolution, "backlog_scan_interval_sec", 900) or 900))

    def _backlog_failure_cooldown(self) -> int:
        evolution = self._evolution_config()
        return max(60, int(getattr(evolution, "backlog_failure_cooldown_sec", 1800) or 1800))

    def _learning_run_retention_days(self) -> int:
        return max(
            1,
            int(getattr(self._evolution_config(), "learning_run_retention_days", 30) or 30),
        )

    def _learning_run_max_per_pipeline_chat(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._evolution_config(),
                    "learning_run_max_per_pipeline_chat",
                    500,
                )
                or 500
            ),
        )

    async def _get_mining_lock(self, group_id: str) -> asyncio.Lock:
        async with self._lock_mutex:
            if group_id not in self._mining_locks:
                self._mining_locks[group_id] = asyncio.Lock()
            return self._mining_locks[group_id]

    def _fire_background_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)
        return task

    def _schedule_mining_if_triggered(self, group_id: str, triggered: bool) -> None:
        group_id = str(group_id or "")
        if not triggered or not group_id:
            return
        current = self._mining_tasks.get(group_id)
        if current is not None and not current.done():
            return
        task = self._fire_background_task(self._try_trigger_mining(group_id))
        self._mining_tasks[group_id] = task

        def _release(done_task: asyncio.Task) -> None:
            if self._mining_tasks.get(group_id) is done_task:
                self._mining_tasks.pop(group_id, None)

        task.add_done_callback(_release)

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[Evolution Task Error] {exc}", exc_info=exc)
        except asyncio.CancelledError:
            logger.debug(f"[Evolution Task] task cancelled: {task.get_name()}")

    async def _append_message_log(
        self,
        *,
        group_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        conversation_event=None,
    ):
        kwargs = {
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
        }
        if conversation_event is not None:
            kwargs["conversation_event"] = conversation_event
        if hasattr(self.db, "add_message_log_async"):
            await self.db.add_message_log_async(**kwargs)
            return
        await asyncio.to_thread(self.db.add_message_log, **kwargs)

    async def _publish_learning_event(self, publisher_name: str, payload: dict) -> None:
        if not self.event_bus:
            return
        publisher = getattr(self.event_bus, publisher_name, None)
        if publisher:
            await publisher(payload)

    async def _load_unprocessed_logs(self, group_id: str, limit: int):
        if hasattr(self.db, "get_unprocessed_logs_async"):
            return await self.db.get_unprocessed_logs_async(group_id, limit=limit)
        return self.db.get_unprocessed_logs(group_id, limit=limit)

    async def _load_recent_logs(
        self,
        group_id: str,
        *,
        limit: int,
        max_age_seconds: float,
    ):
        if hasattr(self.db, "get_recent_message_logs_async"):
            return await self.db.get_recent_message_logs_async(
                group_id,
                limit=limit,
                max_age_seconds=max_age_seconds,
                include_processed=True,
            )
        return await asyncio.to_thread(
            self.db.get_recent_message_logs,
            group_id,
            limit,
            max_age_seconds,
            True,
        )

    async def _list_unprocessed_log_groups(self, *, min_count: int, limit: int) -> list[dict[str, Any]]:
        if hasattr(self.db, "list_unprocessed_log_groups_async"):
            return list(await self.db.list_unprocessed_log_groups_async(min_count=min_count, limit=limit) or [])
        if hasattr(self.db, "list_unprocessed_log_groups"):
            return list(await asyncio.to_thread(self.db.list_unprocessed_log_groups, min_count=min_count, limit=limit) or [])
        return []

    async def _mark_logs_processed(self, log_ids: List[int]) -> None:
        if hasattr(self.db, "mark_logs_processed_async"):
            await self.db.mark_logs_processed_async(log_ids)
            return
        self.db.mark_logs_processed(log_ids)

    def _pipeline_threshold(self, pipeline: str) -> int:
        evolution = self._evolution_config()
        if pipeline == "expression":
            return max(
                1,
                int(
                    getattr(
                        evolution,
                        "expression_min_valid_messages",
                        getattr(evolution, "min_mining_context", 10),
                    )
                    or 30
                ),
            )
        return max(
            2,
            int(
                getattr(
                    evolution,
                    "jargon_min_valid_messages",
                    getattr(evolution, "min_mining_context", 10),
                )
                or 20
            ),
        )

    def _pipeline_overlap(self, pipeline: str) -> int:
        evolution = self._evolution_config()
        attribute = "expression_overlap_messages" if pipeline == "expression" else "jargon_overlap_messages"
        default = 30 if pipeline == "expression" else 10
        return max(0, int(getattr(evolution, attribute, default) or 0))

    def _pipeline_replay_recent(self, pipeline: str) -> int:
        if pipeline != "expression":
            return 0
        evolution = self._evolution_config()
        return max(0, int(getattr(evolution, "expression_evidence_replay_messages", 300) or 0))

    def _pipeline_enabled(self, pipeline: str) -> bool:
        if pipeline == "expression":
            return bool(getattr(self._evolution_config(), "enable_expression_mining", True))
        return True

    @staticmethod
    def _is_group_learning_scope(group_id: str, logs: List["MessageLog"]) -> bool:
        origin = str(group_id or "").lower()
        if "friendmessage" in origin or origin.startswith("private:"):
            return False
        if "groupmessage" in origin or origin.startswith("group:"):
            return True
        kinds = {
            str(EvolutionManager._field(item, "chat_kind", "") or "").strip().lower()
            for item in logs or []
        }
        if kinds & {"private", "friend", "direct", "dm"}:
            return False
        return True

    def _supports_pipeline_checkpoints(self) -> bool:
        return hasattr(self.db, "get_learning_logs_async") or hasattr(self.db, "get_learning_logs")

    async def _load_pipeline_logs(self, pipeline: str, group_id: str, limit: int):
        replay_recent = self._pipeline_replay_recent(pipeline)
        if hasattr(self.db, "get_learning_logs_async"):
            return await self.db.get_learning_logs_async(
                pipeline,
                group_id,
                limit=limit,
                replay_recent=replay_recent,
            )
        if hasattr(self.db, "get_learning_logs"):
            return await asyncio.to_thread(
                self.db.get_learning_logs,
                pipeline,
                group_id,
                limit,
                replay_recent=replay_recent,
            )
        return await self._load_unprocessed_logs(group_id, limit=limit)

    async def _list_pipeline_log_groups(
        self,
        pipeline: str,
        *,
        min_count: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        replay_recent = self._pipeline_replay_recent(pipeline)
        if hasattr(self.db, "list_learning_log_groups_async"):
            return list(
                await self.db.list_learning_log_groups_async(
                    pipeline,
                    min_count=min_count,
                    limit=limit,
                    replay_recent=replay_recent,
                )
                or []
            )
        if hasattr(self.db, "list_learning_log_groups"):
            return list(
                await asyncio.to_thread(
                    self.db.list_learning_log_groups,
                    pipeline,
                    min_count=min_count,
                    limit=limit,
                    replay_recent=replay_recent,
                )
                or []
            )
        return await self._list_unprocessed_log_groups(min_count=min_count, limit=limit)

    async def _advance_pipeline_checkpoint(
        self,
        pipeline: str,
        group_id: str,
        cursor_log_id: int,
        **kwargs,
    ) -> None:
        if hasattr(self.db, "advance_learning_checkpoint_async"):
            await self.db.advance_learning_checkpoint_async(
                pipeline,
                group_id,
                cursor_log_id,
                **kwargs,
            )
            return
        if hasattr(self.db, "advance_learning_checkpoint"):
            await asyncio.to_thread(
                self.db.advance_learning_checkpoint,
                pipeline,
                group_id,
                cursor_log_id,
                **kwargs,
            )

    async def _get_pipeline_checkpoint(
        self,
        pipeline: str,
        group_id: str,
    ) -> dict[str, Any]:
        replay_recent = self._pipeline_replay_recent(pipeline)
        if hasattr(self.db, "ensure_learning_checkpoint_async"):
            return dict(
                await self.db.ensure_learning_checkpoint_async(
                    pipeline,
                    group_id,
                    replay_recent=replay_recent,
                )
                or {}
            )
        if hasattr(self.db, "ensure_learning_checkpoint"):
            return dict(
                await asyncio.to_thread(
                    self.db.ensure_learning_checkpoint,
                    pipeline,
                    group_id,
                    replay_recent=replay_recent,
                )
                or {}
            )
        return {}

    async def _cleanup_legacy_processed_flags(self, group_id: str) -> None:
        if hasattr(self.db, "mark_logs_processed_through_learning_checkpoints_async"):
            await self.db.mark_logs_processed_through_learning_checkpoints_async(group_id)
        elif hasattr(self.db, "mark_logs_processed_through_learning_checkpoints"):
            await asyncio.to_thread(
                self.db.mark_logs_processed_through_learning_checkpoints,
                group_id,
            )

    def _next_pipeline_cursor(self, logs: List["MessageLog"], pipeline: str) -> tuple[int, int]:
        if not logs:
            return 0, 0
        if self._supports_pipeline_checkpoints():
            configured_overlap = self._pipeline_overlap(pipeline)
        else:
            min_context = max(
                1,
                int(getattr(self._evolution_config(), "min_mining_context", 10) or 10),
            )
            configured_overlap = max(2, min(10, min_context // 2))
        overlap = min(configured_overlap, max(0, len(logs) - 1))
        index = len(logs) - overlap - 1
        cursor = int(self._field(logs[index], "id", 0) or 0)
        return cursor, overlap

    async def _record_pipeline_run(self, payload: dict[str, Any]) -> None:
        if hasattr(self.db, "record_learning_mining_run_async"):
            await self.db.record_learning_mining_run_async(payload)
        elif hasattr(self.db, "record_learning_mining_run"):
            await asyncio.to_thread(self.db.record_learning_mining_run, payload)

    async def _list_pipeline_checkpoints(self, **kwargs) -> list[dict[str, Any]]:
        if hasattr(self.db, "list_learning_checkpoints_async"):
            return list(await self.db.list_learning_checkpoints_async(**kwargs) or [])
        if hasattr(self.db, "list_learning_checkpoints"):
            return list(await asyncio.to_thread(self.db.list_learning_checkpoints, **kwargs) or [])
        return []

    async def _list_pipeline_runs(self, **kwargs) -> list[dict[str, Any]]:
        if hasattr(self.db, "list_learning_mining_runs_async"):
            return list(await self.db.list_learning_mining_runs_async(**kwargs) or [])
        if hasattr(self.db, "list_learning_mining_runs"):
            return list(await asyncio.to_thread(self.db.list_learning_mining_runs, **kwargs) or [])
        return []

    async def _count_pipeline_checkpoints(self, **kwargs) -> int:
        if hasattr(self.db, "count_learning_checkpoints_async"):
            return int(await self.db.count_learning_checkpoints_async(**kwargs) or 0)
        if hasattr(self.db, "count_learning_checkpoints"):
            return int(await asyncio.to_thread(self.db.count_learning_checkpoints, **kwargs) or 0)
        return len(await self._list_pipeline_checkpoints(limit=1000, **kwargs))

    async def _count_pipeline_runs(self, **kwargs) -> int:
        if hasattr(self.db, "count_learning_mining_runs_async"):
            return int(await self.db.count_learning_mining_runs_async(**kwargs) or 0)
        if hasattr(self.db, "count_learning_mining_runs"):
            return int(await asyncio.to_thread(self.db.count_learning_mining_runs, **kwargs) or 0)
        return len(await self._list_pipeline_runs(limit=1000, **kwargs))

    async def _purge_learning_runs(self) -> dict[str, Any]:
        kwargs = {
            "retention_days": self._learning_run_retention_days(),
            "max_per_pipeline_chat": self._learning_run_max_per_pipeline_chat(),
        }
        if hasattr(self.db, "purge_learning_mining_runs_async"):
            return dict(await self.db.purge_learning_mining_runs_async(**kwargs) or {})
        if hasattr(self.db, "purge_learning_mining_runs"):
            return dict(await asyncio.to_thread(self.db.purge_learning_mining_runs, **kwargs) or {})
        return {"unsupported": True, **kwargs}

    async def _maybe_purge_learning_runs(self, *, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and now - self._last_learning_run_purge_at < 86400:
            return dict(self._last_learning_run_purge or {})
        report = await self._purge_learning_runs()
        self._last_learning_run_purge_at = now
        self._last_learning_run_purge = {**report, "purged_at": now}
        return dict(self._last_learning_run_purge)

    async def retry_learning_pipeline(self, pipeline: str, chat_id: str) -> dict[str, Any]:
        if hasattr(self.db, "reset_learning_checkpoint_async"):
            return dict(await self.db.reset_learning_checkpoint_async(pipeline, chat_id) or {})
        if hasattr(self.db, "reset_learning_checkpoint"):
            return dict(await asyncio.to_thread(self.db.reset_learning_checkpoint, pipeline, chat_id) or {})
        raise RuntimeError("learning checkpoint reset is unavailable")

    async def purge_learning_run_history(self) -> dict[str, Any]:
        return await self._maybe_purge_learning_runs(force=True)

    async def backlog_overview(self) -> dict[str, Any]:
        threshold = self._backlog_threshold()
        try:
            pipeline_groups = {
                pipeline: await self._list_pipeline_log_groups(pipeline, min_count=1, limit=10)
                for pipeline in ("expression", "jargon")
                if self._pipeline_enabled(pipeline)
            }
            aggregate: dict[str, dict[str, Any]] = {}
            for pipeline, items in pipeline_groups.items():
                for item in items:
                    chat_id = str(item.get("group_id", "") or "")
                    if not chat_id:
                        continue
                    merged = aggregate.setdefault(
                        chat_id,
                        {"group_id": chat_id, "count": 0, "pipelines": {}},
                    )
                    merged["count"] = max(int(merged["count"]), int(item.get("count", 0) or 0))
                    merged["oldest_timestamp"] = item.get("oldest_timestamp")
                    merged["latest_timestamp"] = item.get("latest_timestamp")
                    merged["pipelines"][pipeline] = int(item.get("count", 0) or 0)
            groups = list(aggregate.values())[:10]
            degraded = ""
        except Exception as exc:
            groups = []
            pipeline_groups = {}
            degraded = str(exc)
        eligible = [item for item in groups if int(item.get("count", 0) or 0) >= threshold]
        return {
            "enabled": self._backlog_enabled(),
            "threshold": threshold,
            "batch_size": self._backlog_batch_size(),
            "group_limit": self._backlog_group_limit(),
            "scan_interval_sec": self._backlog_scan_interval(),
            "failure_cooldown_sec": self._backlog_failure_cooldown(),
            "top_unprocessed_groups": groups,
            "pipeline_groups": pipeline_groups,
            "eligible_groups": eligible,
            "last_report": dict(self._last_backlog_report or {}),
            "worker_running": bool(self._backlog_task is not None and not self._backlog_task.done()),
            "degraded": bool(degraded),
            "degraded_reason": degraded,
        }

    async def learning_pipeline_diagnostics(
        self,
        *,
        pipeline: str = "",
        chat_id: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        filters = {
            "pipeline": pipeline,
            "chat_id": chat_id,
            "status": status,
        }
        checkpoints, runs, checkpoint_total, run_total = await asyncio.gather(
            self._list_pipeline_checkpoints(limit=limit, offset=offset, **filters),
            self._list_pipeline_runs(limit=limit, offset=offset, **filters),
            self._count_pipeline_checkpoints(**filters),
            self._count_pipeline_runs(**filters),
        )
        now = time.time()
        pipeline_stats: dict[str, dict[str, Any]] = {}
        for pipeline in ("expression", "jargon"):
            pipeline_checkpoints = [item for item in checkpoints if item.get("pipeline") == pipeline]
            pipeline_runs = [item for item in runs if item.get("pipeline") == pipeline]
            status_counts: dict[str, int] = {}
            for item in pipeline_runs:
                status = str(item.get("status") or "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            pipeline_stats[pipeline] = {
                "enabled": self._pipeline_enabled(pipeline),
                "required_valid_messages": self._pipeline_threshold(pipeline),
                "overlap_messages": self._pipeline_overlap(pipeline),
                "checkpoint_count": len(pipeline_checkpoints),
                "quarantined_count": sum(
                    1 for item in pipeline_checkpoints if float(item.get("retry_at", 0.0) or 0.0) > now
                ),
                "status_counts": status_counts,
                "latest_run": pipeline_runs[0] if pipeline_runs else {},
            }
        return {
            "pipelines": pipeline_stats,
            "checkpoints": checkpoints,
            "recent_runs": runs,
            "pagination": {
                "limit": max(1, int(limit or 100)),
                "offset": max(0, int(offset or 0)),
                "checkpoint_total": checkpoint_total,
                "run_total": run_total,
            },
            "filters": filters,
            "retention": {
                "days": self._learning_run_retention_days(),
                "max_per_pipeline_chat": self._learning_run_max_per_pipeline_chat(),
                "last_purge": dict(self._last_learning_run_purge or {}),
            },
            "generated_at": now,
        }

    def describe_learning_runtime(self) -> dict[str, Any]:
        return {
            "recorder": {
                "window_seconds": int(getattr(self.recorder, "window_seconds", 0) or 0),
                "min_messages": int(getattr(self.recorder, "min_messages", 0) or 0),
                "cooldown_seconds": int(getattr(self.recorder, "cooldown_seconds", 0) or 0),
            },
            "backlog": {
                "enabled": self._backlog_enabled(),
                "threshold": self._backlog_threshold(),
                "batch_size": self._backlog_batch_size(),
                "group_limit": self._backlog_group_limit(),
                "scan_interval_sec": self._backlog_scan_interval(),
                "failure_cooldown_sec": self._backlog_failure_cooldown(),
                "worker_running": bool(self._backlog_task is not None and not self._backlog_task.done()),
                "last_report": dict(self._last_backlog_report or {}),
            },
            "expression_backfill": dict(self._last_expression_backfill or {}),
            "mining": {
                "expression": dict(getattr(self.expression_miner, "last_report", {}) or {}),
                "jargon": dict(getattr(self.jargon_miner, "last_report", {}) or {}),
                "last_outcomes": dict(self._last_mining_outcomes),
            },
            "run_retention": {
                "days": self._learning_run_retention_days(),
                "max_per_pipeline_chat": self._learning_run_max_per_pipeline_chat(),
                "last_purge": dict(self._last_learning_run_purge or {}),
            },
        }

    async def run_expression_backfill(
        self,
        group_id: str,
        *,
        limit: int = 120,
        max_age_seconds: float = 604800,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        group_id = str(group_id or "").strip()
        if not group_id:
            return {"status": "error", "message": "chat_id is required"}
        limit = max(10, min(int(limit or 120), 500))
        max_age_seconds = max(3600.0, min(float(max_age_seconds or 604800), 2592000.0))
        group_lock = await self._get_mining_lock(group_id)
        async with group_lock:
            logs = list(
                await self._load_recent_logs(
                    group_id,
                    limit=limit,
                    max_age_seconds=max_age_seconds,
                )
                or []
            )
            patterns = await self.expression_miner.mine(group_id, logs)
            expression_report = dict(getattr(self.expression_miner, "last_report", {}) or {})
            enrichment = expression_report.get("enrichment")
            terminal = not isinstance(enrichment, dict) or bool(enrichment.get("terminal"))
            result: dict[str, Any] = {
                "status": "ok" if terminal else "degraded",
                "chat_id": group_id,
                "dry_run": bool(dry_run),
                "input_count": len(logs),
                "candidate_count": int(expression_report.get("candidate_count", 0) or 0),
                "pattern_count": len(patterns),
                "candidate_ids": [str(item.get("candidate_id") or "") for item in patterns],
                "expression": expression_report,
                "processed_flags_changed": False,
                "checked_at": time.time(),
            }
            if not terminal:
                result["message"] = "表达增强未完成，历史消息保持不变，可稍后重试"
            elif not dry_run:
                batch_id = self._mining_batch_id(group_id, logs, prefix="backfill")
                persistence = await self._save_patterns(
                    patterns,
                    mining_batch_id=batch_id,
                    source="learning_expression_backfill",
                )
                result["persistence"] = persistence.to_report()
                if not persistence.complete:
                    result["status"] = "degraded"
                    result["message"] = "部分表达候选未能持久化，可安全重试"
            self._last_expression_backfill = result
            memory_engine = getattr(self.db, "memory_engine", None)
            store = getattr(memory_engine, "v2_store", None)
            setter = getattr(store, "set_meta", None)
            if callable(setter):
                key = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:24]
                await setter(f"expression_backfill:{key}", json.dumps(result, ensure_ascii=False))
            return result

    @staticmethod
    def _field(item, key, default=None):
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @staticmethod
    def _normalize_pattern_review_status(value) -> str:
        normalized = str(value or "pending").strip().lower()
        if normalized == "approved":
            return "pending"
        if normalized in {"pending", "pending_human", "rejected"}:
            return normalized
        return "pending"

    @staticmethod
    def _normalize_jargon_review_status(value) -> str:
        normalized = str(value or "review_pending").strip().lower()
        if normalized == "active":
            return "review_pending"
        if normalized in {"review_pending", "pending_human", "rejected"}:
            return normalized
        return "review_pending"

    @classmethod
    def _mining_batch_id(cls, group_id: str, logs: List["MessageLog"], *, prefix: str = "live") -> str:
        evidence = [str(cls._field(log, "id", "")) for log in logs if cls._field(log, "id") is not None]
        payload = f"{prefix}|{group_id}|{'|'.join(evidence)}"
        return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]}"

    async def _save_patterns(
        self,
        patterns,
        *,
        mining_batch_id: str = "",
        source: str = "learning_expression_pattern",
    ) -> PatternSaveReport:
        report = PatternSaveReport(attempted=len(patterns or []))
        memory_engine = getattr(self.db, "memory_engine", None)
        service = getattr(memory_engine, "expression_pattern_service", None) if memory_engine else None
        if not service or not hasattr(service, "write_pattern"):
            if report.attempted:
                report.failed = report.attempted
                report.failures.append("expression_pattern_service_unavailable")
            return report
        for pattern in patterns or []:
            review_status = self._normalize_pattern_review_status(self._field(pattern, "review_status", "pending"))
            group_id = str(self._field(pattern, "group_id", "") or "")
            expression = str(self._field(pattern, "expression", "") or "")
            situation = str(self._field(pattern, "situation", "") or "")
            shared_scope = group_id
            existing = None
            try:
                habit_type = str(self._field(pattern, "habit_type", "sentence_pattern") or "sentence_pattern")
                dedup_key = service.build_dedup_key(group_id, situation, expression, shared_scope, habit_type)
                existing = await service.store.get_by_dedup_key(dedup_key, include_inactive=True)
                memory_id = await service.write_pattern(
                    group_id,
                    {
                        "expression": self._field(pattern, "expression", ""),
                        "situation": self._field(pattern, "situation", ""),
                        "style": self._field(pattern, "style", ""),
                        "habit_type": self._field(pattern, "habit_type", "sentence_pattern"),
                        "content_kind": self._field(pattern, "content_kind", "expression"),
                        "normalized_pattern": self._field(pattern, "normalized_expression", ""),
                        "scope_kind": "group",
                        "distinct_turn_count": int(self._field(pattern, "distinct_turn_count", len(self._field(pattern, "evidence_message_ids", []) or [])) or 0),
                        "distinct_day_count": int(self._field(pattern, "distinct_day_count", 0) or 0),
                        "distinct_contributor_count": int(self._field(pattern, "distinct_contributor_count", 0) or 0),
                        "content_samples": list(self._field(pattern, "content_samples", []) or []),
                        "count": int(self._field(pattern, "count", 1) or 1),
                        "think_level": int(self._field(pattern, "think_level", 0) or 0),
                        "review_status": review_status,
                        "review_reason": self._field(pattern, "review_reason", ""),
                        "review_suggestion": self._field(pattern, "review_suggestion", ""),
                        "weight": float(self._field(pattern, "weight", 1.0) or 1.0),
                        "shared_scope": self._field(pattern, "shared_scope", shared_scope),
                        "summary": self._field(pattern, "summary", self._field(pattern, "expression", "")),
                        "confidence": float(self._field(pattern, "confidence", self._field(pattern, "activation_score", 0.65)) or 0.65),
                        "activation_score": float(self._field(pattern, "activation_score", 0.65) or 0.65),
                        "candidate_id": self._field(pattern, "candidate_id", ""),
                        "mining_batch_id": mining_batch_id,
                        "source_ref": f"{source}:{mining_batch_id}:{self._field(pattern, 'candidate_id', '')}",
                    },
                    source=source,
                )
                if not memory_id:
                    raise RuntimeError("write_pattern returned an empty memory id")
                report.memory_ids.append(str(memory_id))
                if existing:
                    report.deduplicated += 1
                else:
                    report.saved += 1
            except Exception as exc:
                report.failed += 1
                report.failures.append(f"{self._field(pattern, 'candidate_id', '') or expression[:24]}: {exc}")
                logger.warning(f"[Evolution-Expression] persistence failed: {exc}")
        return report

    async def _save_jargons(
        self,
        group_id: str,
        jargons,
        *,
        mining_batch_id: str = "",
    ) -> int:
        memory_engine = getattr(self.db, "memory_engine", None)
        writer = getattr(memory_engine, "write_service", None) if memory_engine else None
        if not writer or not hasattr(writer, "write"):
            return 0

        requests: list[MemoryWriteRequest] = []
        store = getattr(memory_engine, "v2_store", None)
        for jargon in jargons:
            content = str(self._field(jargon, "content", "") or "").strip()
            if not content:
                continue
            meaning = str(self._field(jargon, "meaning", "") or "").strip()
            raw_content = str(self._field(jargon, "raw_content", "") or content).strip()
            confidence = float(self._field(jargon, "confidence", 0.0) or 0.0)
            activation_score = float(self._field(jargon, "activation_score", 0.0) or 0.0)
            is_jargon = bool(self._field(jargon, "is_jargon", False))
            scene = str(self._field(jargon, "scene", "") or "").strip()
            examples = [str(item).strip() for item in (self._field(jargon, "examples", []) or []) if str(item).strip()]
            review_status = self._normalize_jargon_review_status(self._field(jargon, "review_status", "review_pending"))
            status = "rejected" if review_status == "rejected" else "review_pending"
            visibility = "maintenance_only"
            dedup_key = jargon_fingerprint(content)
            existing = await store.get_by_dedup_key(dedup_key, include_inactive=True) if store else None
            existing_metadata = dict(getattr(existing, "metadata", {}) or {})
            existing_status = str(getattr(existing, "status", "") or "").strip().lower()
            if existing_status == "active":
                status = "active"
                review_status = "approved"
                visibility = "auto_and_tool"
            elif existing_status == "rejected":
                status = "rejected"
                review_status = "rejected"
            elif existing_status == "stale":
                status = "stale"
            applied_batches = [
                str(item)
                for item in (existing_metadata.get("applied_mining_batch_ids") or [])
                if str(item or "").strip()
            ]
            if mining_batch_id and mining_batch_id in applied_batches:
                continue
            aliases = [
                str(item).strip()
                for item in [
                    *(existing_metadata.get("aliases") or []),
                    raw_content,
                    content,
                ]
                if str(item or "").strip()
            ]
            merged_examples = list(
                dict.fromkeys(
                    [
                        *[str(item).strip() for item in (existing_metadata.get("examples") or []) if str(item).strip()],
                        *examples,
                    ]
                )
            )[:12]
            incoming_count = int(self._field(jargon, "count", 1) or 1)
            requests.append(
                MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id=GLOBAL_JARGON_SESSION_ID,
                    content=str(getattr(existing, "content", "") or content),
                    summary=meaning or str(getattr(existing, "summary", "") or content),
                    tags=["jargon", "learning"],
                    importance=min(1.0, max(0.35, activation_score)),
                    confidence=max(0.1, min(confidence or activation_score or 0.55, 1.0)),
                    metadata={
                        "raw_content": raw_content,
                        "meaning": meaning or str(existing_metadata.get("meaning") or ""),
                        "confidence": max(confidence, float(existing_metadata.get("confidence") or 0.0)),
                        "activation_score": max(activation_score, float(existing_metadata.get("activation_score") or 0.0)),
                        "examples": merged_examples,
                        "aliases": list(dict.fromkeys(aliases))[:12],
                        "scene": scene or str(existing_metadata.get("scene") or ""),
                        "review_status": review_status,
                        "count": int(existing_metadata.get("count") or 0) + incoming_count,
                        "source_groups": list(
                            dict.fromkeys(
                                [
                                    *[str(item) for item in (existing_metadata.get("source_groups") or []) if str(item)],
                                    str(group_id or ""),
                                ]
                            )
                        )[-64:],
                        "applied_mining_batch_ids": list(
                            dict.fromkeys([*applied_batches, *([mining_batch_id] if mining_batch_id else [])])
                        )[-128:],
                    },
                    dedup_key=dedup_key,
                    source_ref=f"learning_jargon:{normalize_jargon_term(content)}",
                    visibility=visibility,
                    status=status,
                )
            )

        count = 0
        failures: list[str] = []
        for request in requests:
            try:
                memory_id = await writer.write(request)
            except Exception as exc:
                failures.append(f"{request.content}: {exc}")
                continue
            if memory_id:
                count += 1
        if failures:
            raise RuntimeError("jargon write failures: " + "; ".join(failures[:3]))
        return count

    async def process_feedback(self, event: AstrMessageEvent, is_command: bool = False):
        bot_id = getattr(event.message_obj, "self_id", "SELF_BOT")
        if hasattr(event, "bot") and getattr(event, "bot", None):
            bot_id = getattr(event.bot, "self_id", bot_id)
        raw_content = event.message_str
        processed_content = f"(系统指令执行结果): {raw_content}" if is_command else raw_content
        await self._append_message_log(
            group_id=event.unified_msg_origin,
            sender_id=str(bot_id),
            sender_name="SELF",
            content=processed_content,
        )
        triggered = self.recorder.record(event.unified_msg_origin)
        self._schedule_mining_if_triggered(event.unified_msg_origin, triggered)

    async def record_user_message(self, event: AstrMessageEvent):
        rich_text = event.get_extra("astrmai_rich_text", event.message_str)
        conversation_event = event.get_extra("astrmai_conversation_event", None)
        await self._append_message_log(
            group_id=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            content=rich_text,
            conversation_event=conversation_event,
        )
        triggered = self.recorder.record(event.unified_msg_origin)
        self._schedule_mining_if_triggered(event.unified_msg_origin, triggered)
        payload = UserMessageRecordedEvent(
            chat_id=str(event.unified_msg_origin),
            sender_id=str(event.get_sender_id() or ""),
            sender_name=str(event.get_sender_name() or ""),
            content=str(rich_text or ""),
        ).to_payload()
        await self._publish_learning_event("publish_learning_message_recorded", payload)

    async def _record_pipeline_state(
        self,
        *,
        pipeline: str,
        group_id: str,
        logs: List["MessageLog"],
        batch_id: str,
        status: str,
        reason: str,
        cursor_before: int,
        cursor_after: int,
        retained_count: int,
        report: dict[str, Any],
        saved_count: int = 0,
        deduplicated_count: int = 0,
        retryable: bool = False,
        error_type: str = "",
        duration_ms: float = 0.0,
    ) -> dict[str, Any]:
        outcome = {
            "pipeline": pipeline,
            "group_id": str(group_id),
            "batch_id": batch_id,
            "status": status,
            "reason": reason,
            "input_count": len(logs),
            "normalized_count": int(report.get("normalized_messages", 0) or 0),
            "required_count": self._pipeline_threshold(pipeline),
            "candidate_count": int(report.get("candidate_count", 0) or 0),
            "saved_count": int(saved_count or 0),
            "deduplicated_count": int(deduplicated_count or 0),
            "cursor_before": int(cursor_before or 0),
            "cursor_after": int(cursor_after or 0),
            "retained_count": int(retained_count or 0),
            "retryable": bool(retryable),
            "error_type": str(error_type or ""),
            "duration_ms": float(duration_ms or 0.0),
            "report": dict(report or {}),
            "recorded_at": time.time(),
        }
        key = f"{pipeline}:{group_id}"
        self._last_mining_outcomes[key] = outcome
        if len(self._last_mining_outcomes) > 40:
            self._last_mining_outcomes.pop(next(iter(self._last_mining_outcomes)), None)
        await self._record_pipeline_run(
            {
                "pipeline": pipeline,
                "chat_id": group_id,
                "batch_id": batch_id,
                "raw_count": len(logs),
                "normalized_count": outcome["normalized_count"],
                "required_count": outcome["required_count"],
                "candidate_count": outcome["candidate_count"],
                "saved_count": saved_count,
                "deduplicated_count": deduplicated_count,
                "cursor_before": cursor_before,
                "cursor_after": cursor_after,
                "retained_count": retained_count,
                "status": status,
                "reason": reason,
                "duration_ms": duration_ms,
                "retryable": retryable,
                "error_type": error_type,
                "details": report,
            }
        )
        memory_engine = getattr(self.db, "memory_engine", None)
        store = getattr(memory_engine, "v2_store", None)
        setter = getattr(store, "set_meta", None)
        if callable(setter):
            digest = hashlib.sha256(str(group_id).encode("utf-8")).hexdigest()[:24]
            await setter(
                f"learning_mining_ledger:{pipeline}:{digest}",
                json.dumps(outcome, ensure_ascii=False),
            )
        return outcome

    async def _skip_non_group_pipeline(
        self,
        pipeline: str,
        group_id: str,
        logs: List["MessageLog"],
    ) -> dict[str, Any]:
        cursor_before = max(0, int(self._field(logs[0], "id", 1) or 1) - 1) if logs else 0
        cursor_after = int(self._field(logs[-1], "id", cursor_before) or cursor_before) if logs else cursor_before
        batch_id = self._mining_batch_id(group_id, logs, prefix=pipeline)
        await self._advance_pipeline_checkpoint(
            pipeline,
            group_id,
            cursor_after,
            batch_id=batch_id,
            status="skipped_non_group",
        )
        return await self._record_pipeline_state(
            pipeline=pipeline,
            group_id=group_id,
            logs=logs,
            batch_id=batch_id,
            status="skipped",
            reason="non_group_scope",
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            retained_count=0,
            report={"chat_scope": "private", "reason": "non_group_scope"},
        )

    async def _run_learning_pipeline(
        self,
        pipeline: str,
        group_id: str,
        logs: List["MessageLog"],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        cursor_before = max(0, int(self._field(logs[0], "id", 1) or 1) - 1) if logs else 0
        batch_id = self._mining_batch_id(group_id, logs, prefix=pipeline)
        failure_key = f"{pipeline}:{group_id}"
        checkpoint = await self._get_pipeline_checkpoint(pipeline, group_id)
        failure_report: dict[str, Any] = {}
        try:
            if pipeline == "expression":
                items = await self.expression_miner.mine(group_id, logs)
                report = dict(getattr(self.expression_miner, "last_report", {}) or {})
                enrichment = report.get("enrichment")
                terminal = not isinstance(enrichment, dict) or bool(enrichment.get("terminal"))
                retryable = isinstance(enrichment, dict) and bool(enrichment.get("retryable"))
                reason = str(report.get("reason") or "completed")
                if reason == "insufficient_context":
                    await self._advance_pipeline_checkpoint(
                        pipeline,
                        group_id,
                        cursor_before,
                        batch_id=batch_id,
                        status="waiting_for_evidence",
                    )
                    return await self._record_pipeline_state(
                        pipeline=pipeline,
                        group_id=group_id,
                        logs=logs,
                        batch_id=batch_id,
                        status="waiting",
                        reason=reason,
                        cursor_before=cursor_before,
                        cursor_after=cursor_before,
                        retained_count=len(logs),
                        report=report,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                if reason == "all_candidates_in_flight":
                    terminal = False
                    retryable = True
                persistence = await self._save_patterns(items, mining_batch_id=batch_id)
                failure_report = {**report, "persistence": persistence.to_report()}
                if not terminal or not persistence.complete:
                    raise RuntimeError(
                        "expression_enrichment_incomplete"
                        if not terminal
                        else "expression_persistence_incomplete"
                    )
                saved_count = persistence.saved
                deduplicated_count = persistence.deduplicated
                extra_report = failure_report
            else:
                if not getattr(getattr(self.db, "memory_engine", None), "write_service", None):
                    raise RuntimeError("jargon_write_service_unavailable")
                items = await self.jargon_miner.mine(group_id, logs)
                report = dict(getattr(self.jargon_miner, "last_report", {}) or {})
                reason = str(report.get("reason") or "completed")
                if reason == "insufficient_context":
                    await self._advance_pipeline_checkpoint(
                        pipeline,
                        group_id,
                        cursor_before,
                        batch_id=batch_id,
                        status="waiting_for_evidence",
                    )
                    return await self._record_pipeline_state(
                        pipeline=pipeline,
                        group_id=group_id,
                        logs=logs,
                        batch_id=batch_id,
                        status="waiting",
                        reason=reason,
                        cursor_before=cursor_before,
                        cursor_after=cursor_before,
                        retained_count=len(logs),
                        report=report,
                        duration_ms=(time.perf_counter() - started) * 1000,
                    )
                enrichment = report.get("enrichment")
                failure_report = report
                terminal = not isinstance(enrichment, dict) or bool(enrichment.get("terminal"))
                if reason == "all_candidates_in_flight":
                    terminal = False
                if not terminal:
                    raise RuntimeError("jargon_enrichment_failed_closed")
                saved_count = await self._save_jargons(group_id, items, mining_batch_id=batch_id)
                deduplicated_count = max(0, len(items) - saved_count)
                extra_report = report

            cursor_after, retained_count = self._next_pipeline_cursor(logs, pipeline)
            await self._advance_pipeline_checkpoint(
                pipeline,
                group_id,
                cursor_after,
                batch_id=batch_id,
                status="completed",
                failure_count=0,
                retry_at=0.0,
                last_error="",
            )
            self._pipeline_failure_counts.pop(failure_key, None)
            return await self._record_pipeline_state(
                pipeline=pipeline,
                group_id=group_id,
                logs=logs,
                batch_id=batch_id,
                status="completed",
                reason="candidates_saved" if saved_count or deduplicated_count else "no_candidates",
                cursor_before=cursor_before,
                cursor_after=cursor_after,
                retained_count=retained_count,
                report=extra_report,
                saved_count=saved_count,
                deduplicated_count=deduplicated_count,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:
            persisted_failures = int(checkpoint.get("failure_count", 0) or 0)
            failures = max(
                persisted_failures,
                int(self._pipeline_failure_counts.get(failure_key, 0) or 0),
            ) + 1
            self._pipeline_failure_counts[failure_key] = failures
            max_failures = max(
                1,
                int(getattr(self._evolution_config(), "learning_pipeline_max_failures", 3) or 3),
            )
            retry_at = 0.0
            status = "failed"
            if failures >= max_failures:
                retry_at = time.time() + max(
                    60,
                    int(getattr(self._evolution_config(), "learning_pipeline_quarantine_sec", 3600) or 3600),
                )
                status = "quarantined"
            await self._advance_pipeline_checkpoint(
                pipeline,
                group_id,
                cursor_before,
                batch_id=batch_id,
                status=status,
                failure_count=failures,
                retry_at=retry_at,
                last_error=str(exc),
            )
            logger.warning(
                f"[Evolution-{pipeline}] mining {status} for {group_id}: {exc}"
            )
            return await self._record_pipeline_state(
                pipeline=pipeline,
                group_id=group_id,
                logs=logs,
                batch_id=batch_id,
                status=status,
                reason=str(exc),
                cursor_before=cursor_before,
                cursor_after=cursor_before,
                retained_count=len(logs),
                report={
                    **failure_report,
                    "reason": str(exc),
                    "failure_count": failures,
                    "retry_at": retry_at,
                },
                retryable=True,
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

    async def process_logs_and_mine(self, group_id: str, logs: List["MessageLog"]):
        if not logs:
            return {}
        group_lock = await self._get_mining_lock(group_id)
        async with group_lock:
            enabled_thresholds = [
                self._pipeline_threshold(pipeline)
                for pipeline in ("expression", "jargon")
                if self._pipeline_enabled(pipeline)
            ]
            limit = max(self._backlog_batch_size(), len(logs), *enabled_thresholds)
            outcomes: dict[str, dict[str, Any]] = {}
            pattern_count = 0
            jargon_count = 0
            for pipeline in ("expression", "jargon"):
                if not self._pipeline_enabled(pipeline):
                    continue
                if (
                    pipeline == "jargon"
                    and not self._supports_pipeline_checkpoints()
                    and not getattr(getattr(self.db, "memory_engine", None), "write_service", None)
                ):
                    continue
                pipeline_logs = list(await self._load_pipeline_logs(pipeline, group_id, limit=limit) or [])
                if not pipeline_logs:
                    continue
                if not self._is_group_learning_scope(group_id, pipeline_logs):
                    outcome = await self._skip_non_group_pipeline(pipeline, group_id, pipeline_logs)
                else:
                    outcome = await self._run_learning_pipeline(pipeline, group_id, pipeline_logs)
                outcomes[pipeline] = outcome
                if pipeline == "expression":
                    pattern_count = int(outcome.get("saved_count", 0) or 0) + int(
                        outcome.get("deduplicated_count", 0) or 0
                    )
                else:
                    jargon_count = int(outcome.get("saved_count", 0) or 0)

            if self._supports_pipeline_checkpoints():
                await self._cleanup_legacy_processed_flags(group_id)
            else:
                terminal_statuses = {"completed", "skipped"}
                completed_cursors = []
                if outcomes and all(item.get("status") in terminal_statuses for item in outcomes.values()):
                    completed_cursors = [
                        int(item.get("cursor_after", 0) or 0)
                        for item in outcomes.values()
                    ]
                if completed_cursors:
                    legacy_cursor = min(completed_cursors)
                    processed_ids = [
                        self._field(item, "id")
                        for item in logs
                        if int(self._field(item, "id", 0) or 0) <= legacy_cursor
                    ]
                    if processed_ids:
                        await self._mark_logs_processed(processed_ids)

            payload = MiningCompletedEvent(
                group_id=str(group_id),
                pattern_count=pattern_count,
                jargon_count=jargon_count,
            ).to_payload()
            await self._publish_learning_event("publish_learning_mining_completed", payload)
            if self.event_bus and jargon_count > 0:
                self.event_bus.trigger_knowledge_update()
            terminal_statuses = {"completed", "skipped"}
            aggregate_status = (
                "completed"
                if outcomes and all(item.get("status") in terminal_statuses for item in outcomes.values())
                else "degraded"
            )
            aggregate = {
                "group_id": str(group_id),
                "status": aggregate_status,
                "reason": "pipelines_completed" if aggregate_status == "completed" else "pipeline_incomplete",
                "input_count": len(logs),
                "processed_count": sum(
                    max(0, int(item.get("cursor_after", 0) or 0) - int(item.get("cursor_before", 0) or 0))
                    for item in outcomes.values()
                ),
                "retained_overlap": max(
                    (
                        int(item.get("retained_count", 0) or 0)
                        for item in outcomes.values()
                        if item.get("status") in terminal_statuses
                    ),
                    default=0,
                ),
                "retryable": any(bool(item.get("retryable")) for item in outcomes.values()),
                "pattern_count": pattern_count,
                "jargon_count": jargon_count,
                "expression": dict(outcomes.get("expression", {}).get("report", {}) or {}),
                "jargon": dict(outcomes.get("jargon", {}).get("report", {}) or {}),
                "persistence": dict(
                    outcomes.get("expression", {}).get("report", {}).get("persistence", {}) or {}
                ),
                "pipelines": outcomes,
                "recorded_at": time.time(),
            }
            self._last_mining_outcomes[str(group_id)] = aggregate
            return outcomes

    async def _record_mining_outcome(
        self,
        group_id: str,
        logs: List["MessageLog"],
        *,
        status: str,
        reason: str,
        pattern_count: int = 0,
        jargon_count: int = 0,
        processed_count: int = 0,
        retained_overlap: int = 0,
        expression_report: dict[str, Any] | None = None,
        jargon_report: dict[str, Any] | None = None,
        persistence_report: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        outcome = {
            "group_id": str(group_id or ""),
            "status": str(status or "unknown"),
            "reason": str(reason or ""),
            "input_count": len(logs or []),
            "processed_count": int(processed_count or 0),
            "retained_overlap": int(retained_overlap or 0),
            "retryable": bool(retryable),
            "pattern_count": int(pattern_count or 0),
            "jargon_count": int(jargon_count or 0),
            "first_log_id": self._field(logs[0], "id") if logs else None,
            "last_log_id": self._field(logs[-1], "id") if logs else None,
            "recorded_at": time.time(),
            "expression": dict(expression_report or {}),
            "jargon": dict(jargon_report or {}),
            "persistence": dict(persistence_report or {}),
        }
        self._last_mining_outcomes[str(group_id or "")] = outcome
        if len(self._last_mining_outcomes) > 20:
            self._last_mining_outcomes.pop(next(iter(self._last_mining_outcomes)), None)
        memory_engine = getattr(self.db, "memory_engine", None)
        store = getattr(memory_engine, "v2_store", None)
        setter = getattr(store, "set_meta", None)
        if callable(setter):
            key = hashlib.sha256(str(group_id or "").encode("utf-8")).hexdigest()[:24]
            await setter(f"learning_mining_ledger:{key}", json.dumps(outcome, ensure_ascii=False))

    async def analyze_and_get_goal(self, chat_id: str, recent_messages: str) -> str:
        if not recent_messages or not recent_messages.strip():
            return "陪伴用户，提供有趣且连贯的对话"
        trimmed = recent_messages.strip()[:200]
        prompt = f"""根据对话总结当前核心话题(<=15字):\n{trimmed}\nJSON: {{"goal": "string"}}"""
        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=LaneKey(subsystem="sys2", task_family="goal", scope_id=chat_id),
                base_origin=chat_id,
            )
            if isinstance(result, dict):
                return str(result.get("goal", "陪伴用户，提供有趣且连贯的对话"))
            if isinstance(result, str):
                match = re.search(r"\{.*?\}", result, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    if isinstance(data, dict):
                        return str(data.get("goal", "陪伴用户，提供有趣且连贯的对话"))
        except Exception as exc:
            logger.error(f"[Evolution-Processor] goal analysis failed: {exc}")
        return "陪伴用户，提供有趣且连贯的对话"

    def _get_expression_pattern_service(self):
        memory_engine = getattr(self.db, "memory_engine", None)
        return getattr(memory_engine, "expression_pattern_service", None) if memory_engine else None

    async def get_active_patterns_canonical_async(self, chat_id: str, limit: int = 5) -> str:
        service = self._get_expression_pattern_service()
        if service and hasattr(service, "render_active_patterns"):
            return await service.render_active_patterns(chat_id, limit=limit)
        return ""

    async def get_active_patterns_async(self, chat_id: str, limit: int = 5) -> str:
        return await self.get_active_patterns_canonical_async(chat_id, limit=limit)

    def get_active_patterns(self, chat_id: str, limit: int = 5) -> str:
        return self.get_active_patterns_canonical(chat_id, limit=limit)

    def get_active_patterns_canonical(self, chat_id: str, limit: int = 5) -> str:
        service = self._get_expression_pattern_service()
        if service and hasattr(service, "render_active_patterns"):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                raise RuntimeError(
                    "get_active_patterns_canonical() is sync-only; "
                    "call await get_active_patterns_canonical_async(...) in async contexts"
                )
            return asyncio.run(self.get_active_patterns_canonical_async(chat_id, limit=limit))
        return ""

    get_active_patterns = get_active_patterns_canonical

    async def _try_trigger_mining(self, group_id: str):
        limit = max(100, self._backlog_batch_size())
        seed_logs: List["MessageLog"] = []
        for pipeline in ("expression", "jargon"):
            if not self._pipeline_enabled(pipeline):
                continue
            logs = list(await self._load_pipeline_logs(pipeline, group_id, limit=limit) or [])
            threshold = max(int(self.recorder.min_messages or 0), self._pipeline_threshold(pipeline))
            if len(logs) >= threshold and (not seed_logs or len(logs) > len(seed_logs)):
                seed_logs = logs
        if seed_logs:
            await self.process_logs_and_mine(group_id, seed_logs)

    async def run_backlog_mining_once(self) -> dict[str, Any]:
        purge_report = await self._maybe_purge_learning_runs()
        if not self._backlog_enabled():
            report = {
                "enabled": False,
                "checked_at": time.time(),
                "run_retention": purge_report,
                "processed_groups": [],
                "skipped_groups": [],
                "errors": [],
            }
            self._last_backlog_report = report
            return report

        now = time.time()
        threshold = self._backlog_threshold()
        group_limit = self._backlog_group_limit()
        batch_size = self._backlog_batch_size()
        group_map: dict[str, dict[str, Any]] = {}
        for pipeline in ("expression", "jargon"):
            if not self._pipeline_enabled(pipeline):
                continue
            pipeline_groups = await self._list_pipeline_log_groups(
                pipeline,
                min_count=threshold,
                limit=group_limit * 3,
            )
            for item in pipeline_groups:
                group_id = str(item.get("group_id", "") or "")
                if not group_id:
                    continue
                merged = group_map.setdefault(
                    group_id,
                    {
                        "group_id": group_id,
                        "count": 0,
                        "oldest_timestamp": item.get("oldest_timestamp"),
                        "latest_timestamp": item.get("latest_timestamp"),
                        "pipelines": [],
                    },
                )
                merged["count"] = max(int(merged.get("count", 0) or 0), int(item.get("count", 0) or 0))
                merged["pipelines"].append(
                    {"pipeline": pipeline, "count": int(item.get("count", 0) or 0)}
                )
        groups = sorted(
            group_map.values(),
            key=lambda item: (-int(item.get("count", 0) or 0), str(item.get("group_id", ""))),
        )
        report: dict[str, Any] = {
            "enabled": True,
            "checked_at": now,
            "threshold": threshold,
            "batch_size": batch_size,
            "group_limit": group_limit,
            "candidate_groups": groups,
            "processed_groups": [],
            "skipped_groups": [],
            "errors": [],
            "run_retention": purge_report,
        }

        processed_count = 0
        for group in groups:
            group_id = str(group.get("group_id", "") or "")
            if not group_id:
                continue
            if processed_count >= group_limit:
                break
            if self._mining_tasks.get(group_id) is not None and not self._mining_tasks[group_id].done():
                report["skipped_groups"].append({"group_id": group_id, "reason": "already_mining"})
                continue
            failure_until = float(self._backlog_failure_until.get(group_id, 0.0) or 0.0)
            if failure_until > now:
                report["skipped_groups"].append(
                    {"group_id": group_id, "reason": "failure_cooldown", "retry_after": failure_until}
                )
                continue
            eligible_pipelines = list(group.get("pipelines", []) or [])
            seed_pipeline = str(eligible_pipelines[0].get("pipeline", "expression")) if eligible_pipelines else "expression"
            logs = list(await self._load_pipeline_logs(seed_pipeline, group_id, limit=batch_size) or [])
            if len(logs) < threshold:
                report["skipped_groups"].append(
                    {
                        "group_id": group_id,
                        "reason": "below_threshold",
                        "count": len(logs),
                        "pipeline": seed_pipeline,
                    }
                )
                continue
            try:
                outcomes = await self.process_logs_and_mine(group_id, logs)
                processed_count += 1
                report["processed_groups"].append(
                    {
                        "group_id": group_id,
                        "log_count": len(logs),
                        "pipelines": {
                            name: {
                                "status": outcome.get("status"),
                                "reason": outcome.get("reason"),
                            }
                            for name, outcome in outcomes.items()
                        },
                    }
                )
                self._backlog_failure_until.pop(group_id, None)
                self._backlog_failure_counts.pop(group_id, None)
                outcome_summary = ",".join(
                    f"{name}:{item.get('status')}" for name, item in outcomes.items()
                )
                logger.info(
                    f"[Evolution-Backlog] mined group={group_id} logs={len(logs)} "
                    f"processed_total={processed_count} outcomes={outcome_summary}"
                )
            except Exception as exc:
                failure_count = int(self._backlog_failure_counts.get(group_id, 0) or 0) + 1
                self._backlog_failure_counts[group_id] = failure_count
                self._backlog_failure_until[group_id] = time.time() + self._backlog_failure_cooldown()
                logger.warning(f"[Evolution-Backlog] mining failed for {group_id}: {exc}")
                report["errors"].append({"group_id": group_id, "error": str(exc)})

        self._last_backlog_report = report
        return report

    async def _backlog_mining_loop(self) -> None:
        try:
            await asyncio.sleep(30)
            while True:
                try:
                    await self.run_backlog_mining_once()
                except Exception as exc:
                    logger.warning(f"[Evolution-Backlog] scan degraded: {exc}")
                await asyncio.sleep(self._backlog_scan_interval())
        except asyncio.CancelledError:
            logger.info("[Evolution-Backlog] backlog mining worker stopped")
            raise

    async def start_background_tasks(self) -> None:
        if self._backlog_task is not None and not self._backlog_task.done():
            return
        self._backlog_task = self._fire_background_task(self._backlog_mining_loop())

    async def stop_background_tasks(self) -> None:
        tasks = [task for task in [self._backlog_task, *self._background_tasks] if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._backlog_task = None

    async def process_bot_reply(self, chat_id: str, bot_id: str, reply_text: str):
        recorded = await self.bot_reply_recorder.record(chat_id, bot_id, reply_text)
        if recorded:
            triggered = self.recorder.record(chat_id)
            self._schedule_mining_if_triggered(chat_id, triggered)
            payload = BotReplyRecordedEvent(
                chat_id=str(chat_id),
                bot_id=str(bot_id),
                content=str(reply_text or ""),
            ).to_payload()
            await self._publish_learning_event("publish_learning_bot_reply_recorded", payload)
