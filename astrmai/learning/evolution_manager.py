from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ..infrastructure.runtime.lane_manager import LaneKey
from ..infrastructure.gateway.json_utils import parse_json_contract, parse_json_payload
from ..memory.contracts.memory_query import MemoryWriteRequest
from .contracts.learning_events import (
    BotReplyRecordedEvent,
    MiningCompletedEvent,
    UserMessageRecordedEvent,
)
from .contracts.learning_envelope import LearningMessageEnvelope
from ..infrastructure.persistence.learning_ingest_outbox import LearningIngressOutboxStore
from ..infrastructure.runtime.background_task_ledger import BackgroundTaskLedger, settle_task_lease
from .logging.bot_reply_recorder import BotReplyRecorder
from .logging.message_recorder import MessageRecorder
from .dedup import GLOBAL_JARGON_SESSION_ID, jargon_fingerprint, normalize_jargon_term
from .mining.expression_miner import ExpressionMiner
from .mining.expression_results import PatternSaveReport
from .mining.jargon_miner import JargonMiner
from .mining.learning_evidence import merge_evidence_metadata
from .mining.learning_input_policy import LearningMessageView
from .mining.jargon_senses import merge_jargon_senses


def _jargon_sense_evidence(evidence: dict[str, Any], sense: dict[str, Any]) -> dict[str, Any]:
    supported_by = list(dict.fromkeys(
        str(item) for item in (sense.get("supported_by") or []) if str(item or "").strip()
    ))
    contradicted_by = list(dict.fromkeys(
        str(item) for item in (sense.get("contradicted_by") or []) if str(item or "").strip()
    ))
    citation_ids = set(supported_by) | set(contradicted_by)
    supported_ids = set(supported_by)
    source_ids = [
        str(item) for item in (evidence.get("source_message_ids") or [])
        if str(item) in supported_ids
    ]
    source_spans = [
        dict(item) for item in (evidence.get("source_spans") or [])
        if isinstance(item, dict) and str(item.get("message_id") or "") in set(source_ids)
    ]
    context_windows = [
        dict(window) for window in (evidence.get("context_windows") or [])
        if isinstance(window, dict) and any(
            str(message.get("message_id") or "") in citation_ids
            for message in (window.get("messages") or [])
            if isinstance(message, dict)
        )
    ]
    reply_relations = [
        dict(item) for item in (evidence.get("reply_relations") or [])
        if isinstance(item, dict) and (
            str(item.get("source_message_id") or "") in citation_ids
            or str(item.get("target_message_id") or "") in citation_ids
        )
    ]
    source_examples = list(dict.fromkeys(
        str(item.get("text") or "").strip() for item in source_spans
        if str(item.get("text") or "").strip()
    ))
    digest_payload = {
        "meaning": str(sense.get("meaning") or ""),
        "scene": str(sense.get("scene") or ""),
        "supported_by": source_ids,
        "contradicted_by": contradicted_by,
    }
    return {
        **evidence,
        "source_examples": source_examples,
        "source_message_ids": source_ids,
        "context_windows": context_windows,
        "reply_relations": reply_relations,
        "source_spans": source_spans,
        "support_count": len(source_ids),
        "contradiction_count": len(contradicted_by),
        "evidence_digest": hashlib.sha256(
            json.dumps(digest_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24],
    }


class EvolutionManager:
    def __init__(
        self,
        db,
        gateway,
        config=None,
        event_bus=None,
        background_task_budget=None,
        ingest_spool_path: str | Path | None = None,
        owner_registry=None,
    ):
        self.db = db
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.event_bus = event_bus
        # A compatibility host may omit the runtime-shared budget.  Keep the
        # same admission contract with a bounded local budget instead of
        # silently running provider work unbounded.
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self.owner_registry = owner_registry
        self.expression_miner = ExpressionMiner(
            gateway,
            self.config,
            memory_engine=getattr(self.db, "memory_engine", None),
            background_task_budget=self.background_task_budget,
        )
        self.jargon_miner = JargonMiner(
            self.expression_miner,
            memory_engine=getattr(self.db, "memory_engine", None),
            background_task_budget=self.background_task_budget,
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
        self._mining_rerun_requested: set[str] = set()
        self._backlog_task: asyncio.Task | None = None
        self._backlog_mining_task: asyncio.Task | None = None
        self._pipeline_limit = self._learning_pipeline_concurrency()
        self._pipeline_semaphore = asyncio.Semaphore(self._pipeline_limit)
        self._active_pipeline_tasks = 0
        self._backlog_failure_until: dict[str, float] = {}
        # Runtime cache mirrors persisted checkpoint failures; evidence is retained on failure.
        self._backlog_failure_counts: dict[str, int] = {}
        self._last_backlog_report: dict[str, Any] = {}
        self._last_expression_backfill: dict[str, Any] = {}
        self._last_mining_outcomes: dict[str, dict[str, Any]] = {}
        self._last_mining_at: dict[str, float] = {}
        self._mining_timestamp_loaded: set[str] = set()
        self._pipeline_failure_counts: dict[str, int] = {}
        self._last_learning_run_purge_at = 0.0
        self._last_learning_run_purge: dict[str, Any] = {}
        self._last_message_log_purge: dict[str, Any] = {}
        persistence = getattr(db, "persistence", None)
        db_path = getattr(persistence, "db_path", None)
        self._ingest_outbox = LearningIngressOutboxStore(db_path) if db_path else None
        spool_path = Path(ingest_spool_path) if ingest_spool_path else None
        if spool_path is None and db_path:
            spool_path = Path(db_path).with_name("learning_ingest_spool.db")
        if spool_path is None:
            cache_dir = getattr(persistence, "cache_dir", None)
            if cache_dir:
                spool_path = Path(cache_dir) / "learning_ingest_spool.db"
        self._ingest_fallback_outbox = (
            LearningIngressOutboxStore(spool_path)
            if spool_path is not None
            and (db_path is None or Path(db_path) != spool_path)
            else None
        )
        self._ingest_worker: asyncio.Task | None = None
        self._ingest_processing: set[str] = set()
        self._task_ledger = BackgroundTaskLedger(db_path) if db_path else None

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
        configured_limit = self._learning_pipeline_concurrency()
        if configured_limit != self._pipeline_limit and self._active_pipeline_tasks == 0:
            self._pipeline_limit = configured_limit
            self._pipeline_semaphore = asyncio.Semaphore(configured_limit)
        if self.jargon_miner.enricher is not None:
            self.jargon_miner.enricher.config = config
        if not self._backlog_enabled() and self._backlog_task is not None:
            self._backlog_task.cancel()

    def _evolution_config(self):
        return getattr(self.config, "evolution", None)

    def _learning_pipeline_concurrency(self) -> int:
        evolution = self._evolution_config()
        return max(1, min(4, int(getattr(evolution, "learning_pipeline_concurrency", 1) or 1)))

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

    def _mining_interval(self) -> float:
        evolution = self._evolution_config()
        try:
            configured = getattr(evolution, "learning_mining_interval_sec", None)
            if configured is None:
                configured = getattr(evolution, "mining_interval_sec", 21600)
            return max(60.0, float(configured or 21600))
        except (TypeError, ValueError):
            return 21600.0

    def _message_log_retention_days(self) -> int:
        return max(7, int(getattr(self._evolution_config(), "message_log_retention_days", 90) or 90))

    def _message_log_cleanup_batch_size(self) -> int:
        return max(10, min(5000, int(getattr(self._evolution_config(), "message_log_cleanup_batch_size", 500) or 500)))

    def _message_log_cleanup_interval(self) -> float:
        return max(3600.0, float(getattr(self._evolution_config(), "message_log_cleanup_interval_sec", 86400) or 86400))

    def _mining_cooldown_active(self, group_id: str, now: float | None = None) -> bool:
        last = float(self._last_mining_at.get(str(group_id or ""), 0.0) or 0.0)
        return last > 0.0 and (time.time() if now is None else now) - last < self._mining_interval()

    @staticmethod
    def _mining_timestamp_key(group_id: str) -> str:
        digest = hashlib.sha256(str(group_id or "").encode("utf-8")).hexdigest()[:24]
        return f"learning_mining_last_at:{digest}"

    async def _load_mining_timestamp(self, group_id: str) -> None:
        normalized = str(group_id or "").strip()
        if not normalized or normalized in self._mining_timestamp_loaded:
            return
        store = getattr(getattr(self.db, "memory_engine", None), "v2_store", None)
        getter = getattr(store, "get_meta", None)
        if not callable(getter):
            self._mining_timestamp_loaded.add(normalized)
            return
        try:
            raw = await getter(self._mining_timestamp_key(normalized), "")
            timestamp = float(raw or 0.0)
            if timestamp > 0.0:
                self._last_mining_at[normalized] = timestamp
            self._mining_timestamp_loaded.add(normalized)
        except (TypeError, ValueError):
            logger.debug("[Evolution] invalid persisted mining timestamp for %s", normalized)
            self._mining_timestamp_loaded.add(normalized)
        except Exception:
            # Leave the key unmarked so a transient store outage can recover
            # on the next admission check instead of allowing an early run.
            logger.debug("[Evolution] failed to restore mining timestamp for %s", normalized, exc_info=True)

    async def _persist_mining_timestamp(self, group_id: str, timestamp: float) -> None:
        normalized = str(group_id or "").strip()
        if not normalized:
            return
        store = getattr(getattr(self.db, "memory_engine", None), "v2_store", None)
        setter = getattr(store, "set_meta", None)
        if not callable(setter):
            return
        try:
            await setter(self._mining_timestamp_key(normalized), str(float(timestamp)))
        except Exception:
            # Persistence is best-effort; the in-memory fence still protects
            # the current process and a failed write must not lose the result.
            logger.debug("[Evolution] failed to persist mining timestamp for %s", normalized, exc_info=True)

    async def _settle_cancelled_mining(
        self,
        group_id: str,
        previous_mining_at: float,
        lease,
        *,
        run_id: str = "",
    ) -> None:
        normalized = str(group_id or "")
        self._last_mining_at[normalized] = float(previous_mining_at or 0.0)
        await self._persist_mining_timestamp(normalized, previous_mining_at)
        if self._task_ledger is not None and lease is not None:
            await settle_task_lease(
                self._task_ledger,
                lease,
                run_id=run_id,
                status="retry_wait",
                error="cancelled",
                retry_after_seconds=0.0,
            )

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

    def _fire_background_task(
        self,
        awaitable_factory,
        *,
        task_name: str = "learning.triggered",
        scope_id: str = "",
        run_id: str = "",
    ):
        if self.background_task_budget is not None:
            coro = self.background_task_budget.run(
                awaitable_factory,
                task_name=task_name,
                scope_id=scope_id,
                defer_release_on_timeout=True,
            )
        else:
            coro = awaitable_factory()
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        registry = getattr(self, "owner_registry", None)
        register = getattr(registry, "register", None)
        if callable(register):
            try:
                register(
                    task,
                    task_family=task_name,
                    scope_id=scope_id or "GLOBAL",
                    run_id=run_id,
                    owner="EvolutionManager",
                    generation=getattr(registry, "generation", 0),
                    cancel_status="cancelled",
                )
            except Exception as exc:
                logger.debug("[Evolution] owner registry registration degraded: %s", exc)
        task.add_done_callback(self._handle_task_result)
        return task

    def _register_owner_task(
        self,
        task: asyncio.Task,
        *,
        task_family: str,
        scope_id: str = "GLOBAL",
        run_id: str = "",
        cancel_status: str = "cancelled",
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
                owner="EvolutionManager",
                generation=getattr(registry, "generation", 0),
                cancel_status=cancel_status,
            )
        except Exception as exc:
            logger.debug("[Evolution] owner registry registration degraded: %s", exc)

    async def _run_backlog_mining_budgeted(self) -> None:
        budget = self.background_task_budget
        if budget is None:
            await self.run_backlog_mining_once()
            return
        await budget.run(
            self.run_backlog_mining_once,
            task_name="learning.backlog",
            scope_id="GLOBAL",
            defer_release_on_timeout=True,
        )

    def _schedule_mining_if_triggered(self, group_id: str, triggered: bool) -> None:
        group_id = str(group_id or "")
        if not triggered or not group_id:
            return
        current = self._mining_tasks.get(group_id)
        if current is not None and not current.done():
            self._mining_rerun_requested.add(group_id)
            return
        task = self._fire_background_task(
            lambda: self._try_trigger_mining(group_id),
            task_name="learning.triggered",
            scope_id=group_id,
            run_id=f"learning-trigger-{uuid.uuid4().hex[:12]}",
        )
        self._mining_tasks[group_id] = task

        def _release(done_task: asyncio.Task) -> None:
            if self._mining_tasks.get(group_id) is done_task:
                self._mining_tasks.pop(group_id, None)
            if group_id in self._mining_rerun_requested:
                self._mining_rerun_requested.discard(group_id)
                self._schedule_mining_if_triggered(group_id, True)

        task.add_done_callback(_release)

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                if isinstance(exc, (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout)):
                    logger.warning(f"[Evolution Task] background task skipped: {exc}")
                else:
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
    ) -> bool:
        kwargs = {
            "group_id": group_id,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
        }
        if conversation_event is not None:
            kwargs["conversation_event"] = conversation_event
        event_id = ""
        if isinstance(conversation_event, dict):
            event_id = str(conversation_event.get("event_id") or "").strip()
        conditional_append = getattr(
            self.db,
            "add_message_log_if_absent_async",
            None,
        )
        if event_id and callable(conditional_append):
            return bool(await conditional_append(**kwargs))
        if hasattr(self.db, "add_message_log_async"):
            await self.db.add_message_log_async(**kwargs)
            return True
        await asyncio.to_thread(self.db.add_message_log, **kwargs)
        return True

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
            logs = await self.db.get_learning_logs_async(
                pipeline,
                group_id,
                limit=limit,
                replay_recent=replay_recent,
            )
        elif hasattr(self.db, "get_learning_logs"):
            logs = await asyncio.to_thread(
                self.db.get_learning_logs,
                pipeline,
                group_id,
                limit,
                replay_recent=replay_recent,
            )
        else:
            logs = await self._load_unprocessed_logs(group_id, limit=limit)
        return await self._attach_learning_visual_context(group_id, list(logs or []))

    async def _load_learning_snapshot(self, group_id: str, limit: int) -> dict[str, Any]:
        pipelines = tuple(
            pipeline
            for pipeline in ("expression", "jargon")
            if self._pipeline_enabled(pipeline)
        )
        replay_recent = {
            pipeline: self._pipeline_replay_recent(pipeline)
            for pipeline in pipelines
        }
        db = getattr(self, "db", None)
        if db is None:
            pipeline_logs = {
                pipeline: list(
                    await self._load_pipeline_logs(pipeline, group_id, limit=limit)
                    or []
                )
                for pipeline in pipelines
            }
            union = {
                int(self._field(item, "id", 0) or 0): item
                for logs in pipeline_logs.values()
                for item in logs
            }
            return {
                "chat_id": str(group_id),
                "logs": [union[key] for key in sorted(union)],
                "pipeline_logs": pipeline_logs,
                "created_at": time.time(),
                "transactional": False,
            }
        loader = getattr(db, "load_learning_snapshot_async", None)
        if callable(loader):
            snapshot = dict(
                await loader(
                    group_id,
                    limit=limit,
                    pipelines=pipelines,
                    replay_recent=replay_recent,
                )
                or {}
            )
        else:
            sync_loader = getattr(db, "load_learning_snapshot", None)
            if callable(sync_loader):
                snapshot = dict(
                    await asyncio.to_thread(
                        sync_loader,
                        group_id,
                        limit,
                        pipelines=pipelines,
                        replay_recent=replay_recent,
                    )
                    or {}
                )
            else:
                pipeline_logs = {
                    pipeline: list(
                        await self._load_pipeline_logs(pipeline, group_id, limit=limit)
                        or []
                    )
                    for pipeline in pipelines
                }
                union = {
                    int(self._field(item, "id", 0) or 0): item
                    for logs in pipeline_logs.values()
                    for item in logs
                }
                return {
                    "chat_id": str(group_id),
                    "logs": [union[key] for key in sorted(union)],
                    "pipeline_logs": pipeline_logs,
                    "created_at": time.time(),
                    "transactional": False,
                }
        raw_logs = list(snapshot.get("logs") or [])
        enriched = await self._attach_learning_visual_context(group_id, raw_logs)
        enriched_by_id = {
            int(self._field(item, "id", 0) or 0): item
            for item in enriched
        }
        snapshot["logs"] = enriched
        snapshot["pipeline_logs"] = {
            pipeline: [
                enriched_by_id.get(int(self._field(item, "id", 0) or 0), item)
                for item in list(logs or [])
            ]
            for pipeline, logs in dict(snapshot.get("pipeline_logs") or {}).items()
        }
        snapshot["transactional"] = True
        return snapshot

    async def _attach_learning_visual_context(self, group_id: str, logs: list[Any]) -> list[Any]:
        resolver = getattr(self.db, "get_learning_visual_context_async", None)
        if not logs or not callable(resolver):
            return logs
        ids_by_log: list[list[str]] = []
        all_ids: list[str] = []
        for index, item in enumerate(logs):
            ids = list(
                dict.fromkeys(
                    str(self._field(item, field, "") or "").strip()
                    for field in ("event_id", "platform_message_id", "id")
                    if str(self._field(item, field, "") or "").strip()
                )
            )
            if not ids:
                ids = [f"row:{index}"]
            ids_by_log.append(ids)
            all_ids.extend(ids)
        try:
            visual_map = dict(await resolver(str(group_id or ""), all_ids) or {})
        except Exception as exc:
            logger.debug(f"[Evolution-LearningContext] visual context lookup degraded: {exc}")
            return logs
        enriched: list[Any] = []
        for item, ids in zip(logs, ids_by_log):
            records = [record for message_id in ids for record in visual_map.get(message_id, [])]
            raw_content = str(self._field(item, "content", "") or "").strip()
            image_refs = str(self._field(item, "image_refs", "") or "").strip()
            if records:
                lines = [raw_content] if raw_content else []
                for record in records:
                    label = "表情包转述" if str(record.get("type") or "").lower() == "emoji" else "图片转述"
                    lines.append(f"[{label}：{str(record.get('description') or '').strip()}]")
                context_content = "\n".join(line for line in lines if line).strip()
                source_kind = "human_text_with_image" if raw_content else "image_transcription"
            elif image_refs and image_refs != "[]":
                context_content = "\n".join(item for item in (raw_content, "[图片]") if item)
                source_kind = "human_text_with_image" if raw_content else "image_placeholder"
            else:
                enriched.append(item)
                continue
            enriched.append(
                LearningMessageView(
                    item,
                    raw_content,
                    source_kind,
                    context_content=context_content,
                    evidence_eligible=bool(raw_content),
                )
            )
        return enriched

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

    async def _maybe_purge_message_logs(self, *, force: bool = False) -> dict[str, Any]:
        purge = getattr(self.db, "purge_consumed_message_logs_async", None)
        if not callable(purge):
            return {"unsupported": True}
        lease = None
        if self._task_ledger is not None:
            lease = await self._task_ledger.claim(
                task_family="message_log_retention",
                scope_id="global",
                input_fingerprint="message-log-retention-v1",
                lease_seconds=300.0,
                min_interval_seconds=0.0 if force else self._message_log_cleanup_interval(),
            )
            if lease is None:
                return {**self._last_message_log_purge, "skipped": "cooldown_or_active_lease"}
        try:
            report = dict(
                await purge(
                    retention_days=self._message_log_retention_days(),
                    batch_size=self._message_log_cleanup_batch_size(),
                )
                or {}
            )
            report["purged_at"] = time.time()
            self._last_message_log_purge = report
            if lease is not None:
                await settle_task_lease(
                    self._task_ledger,
                    lease,
                    run_id=f"retention-{int(time.time())}",
                    status="succeeded",
                    checkpoint_after={"deleted": int(report.get("deleted", 0) or 0)},
                )
            return dict(report)
        except Exception as exc:
            if lease is not None:
                await settle_task_lease(
                    self._task_ledger,
                    lease,
                    run_id=f"retention-{int(time.time())}",
                    status="retry_wait",
                    error=str(exc),
                    retry_after_seconds=3600.0,
                )
            logger.warning("[Evolution-Retention] message log cleanup degraded: %s", exc)
            return {"error": str(exc), "deleted": 0}

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
            "pipeline_runtime": {
                "configured_concurrency": int(self._pipeline_limit),
                "active_tasks": int(self._active_pipeline_tasks),
                "available_slots": max(0, int(getattr(self._pipeline_semaphore, "_value", 0) or 0)),
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

    @classmethod
    def _mining_run_id(cls, group_id: str, logs: List["MessageLog"]) -> str:
        """Return a stable run id for one immutable learning snapshot.

        Retries and reload recovery must update the same learning run rather
        than create a second audit row for identical input evidence.
        """
        batch_id = cls._mining_batch_id(group_id, logs, prefix="learning")
        digest = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:20]
        return f"learning_{digest}"

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
                        "candidate_origin": self._field(pattern, "candidate_origin", "expression_miner"),
                        "classification": self._field(pattern, "classification", "expression"),
                        "classification_reason": self._field(pattern, "classification_reason", ""),
                        "quality_tier": self._field(pattern, "quality_tier", "review"),
                        "quality_flags": list(self._field(pattern, "quality_flags", []) or []),
                        "evidence_version": self._field(pattern, "evidence_version", 3),
                        "source_examples": list(self._field(pattern, "source_examples", []) or []),
                        "source_message_ids": list(self._field(pattern, "source_message_ids", []) or []),
                        "source_group_ids": list(self._field(pattern, "source_group_ids", []) or []),
                        "context_windows": list(self._field(pattern, "context_windows", []) or []),
                        "reply_relations": list(self._field(pattern, "reply_relations", []) or []),
                        "source_spans": list(self._field(pattern, "source_spans", []) or []),
                        "support_count": int(self._field(pattern, "support_count", 0) or 0),
                        "contradiction_count": int(self._field(pattern, "contradiction_count", 0) or 0),
                        "contributor_count": int(self._field(pattern, "contributor_count", 0) or 0),
                        "model_examples": list(self._field(pattern, "model_examples", []) or []),
                        "evidence_digest": self._field(pattern, "evidence_digest", ""),
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
            observed_content = str(self._field(jargon, "content", "") or "").strip()
            content = str(self._field(jargon, "canonical_form", "") or observed_content).strip()
            if not content or not observed_content:
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
            applied_batches = [
                str(item)
                for item in (existing_metadata.get("applied_mining_batch_ids") or [])
                if str(item or "").strip()
            ]
            if mining_batch_id and mining_batch_id in applied_batches:
                continue
            incoming_evidence = {
                key: self._field(jargon, key, [] if key.endswith("s") else "")
                for key in (
                    "evidence_version",
                    "source_examples",
                    "source_message_ids",
                    "source_group_ids",
                    "context_windows",
                    "reply_relations",
                    "source_spans",
                    "support_count",
                    "contradiction_count",
                    "contributor_count",
                    "model_examples",
                    "evidence_digest",
                )
            }
            merged_evidence = merge_evidence_metadata(existing_metadata, incoming_evidence)
            proposed_senses = [
                item for item in (self._field(jargon, "proposed_senses", []) or [])
                if isinstance(item, dict) and str(item.get("meaning") or "").strip()
            ] or [{
                "meaning": meaning,
                "scene": scene,
                "confidence": confidence,
                "review_status": review_status,
                "supported_by": list(self._field(jargon, "supported_by", []) or []),
                "contradicted_by": list(self._field(jargon, "contradicted_by", []) or []),
            }]
            senses = list(existing_metadata.get("senses") or [])
            incoming_sense_id = ""
            is_new_sense = False
            sense_revision_reopened = False
            for proposed_sense in proposed_senses:
                sense_evidence = _jargon_sense_evidence(incoming_evidence, proposed_sense)
                incoming_sense_payload = {
                    **sense_evidence,
                    **proposed_sense,
                    "examples": list(sense_evidence.get("source_examples") or []),
                }
                senses, incoming_sense_id, new_sense, reopened_sense = merge_jargon_senses(
                    {**existing_metadata, "senses": senses},
                    incoming_sense_payload,
                    group_id=str(group_id or ""),
                    record_status=existing_status,
                )
                is_new_sense = is_new_sense or new_sense
                sense_revision_reopened = sense_revision_reopened or reopened_sense
            stronger_new_evidence = (
                bool(incoming_evidence.get("evidence_digest"))
                and str(incoming_evidence.get("evidence_digest")) != str(existing_metadata.get("evidence_digest") or "")
                and int(incoming_evidence.get("support_count") or 0) >= 2
            )
            if existing_status == "active":
                status = "active"
                review_status = "approved"
                visibility = "auto_and_tool"
            elif existing_status == "rejected" and (sense_revision_reopened or (is_new_sense and stronger_new_evidence)):
                status = "review_pending"
                review_status = "revision_needed"
            elif existing_status == "rejected":
                status = "rejected"
                review_status = "rejected"
            elif existing_status == "stale":
                status = "review_pending" if stronger_new_evidence else "stale"
                review_status = "revision_needed" if stronger_new_evidence else review_status
            aliases = [
                str(item).strip()
                for item in [
                    *(existing_metadata.get("aliases") or []),
                    *(self._field(jargon, "surface_forms", []) or []),
                    *(self._field(jargon, "aliases", []) or []),
                    observed_content,
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
            approved_senses = [item for item in senses if str(item.get("review_status") or "") == "approved"]
            primary_sense = approved_senses[0] if approved_senses else next(
                (item for item in senses if str(item.get("sense_id") or "") == incoming_sense_id),
                {},
            )
            primary_meaning = str(primary_sense.get("meaning") or meaning or existing_metadata.get("meaning") or "")
            primary_scene = str(primary_sense.get("scene") or scene or existing_metadata.get("scene") or "")
            requests.append(
                MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id=GLOBAL_JARGON_SESSION_ID,
                    content=str(getattr(existing, "content", "") or content),
                    summary=primary_meaning or str(getattr(existing, "summary", "") or content),
                    tags=["jargon", "learning"],
                    importance=min(1.0, max(0.35, activation_score)),
                    confidence=max(0.1, min(confidence or activation_score or 0.55, 1.0)),
                    metadata={
                        **merged_evidence,
                        "raw_content": raw_content,
                        "canonical_term": content,
                        "surface_forms": list(dict.fromkeys(aliases))[:12],
                        "meaning": primary_meaning,
                        "confidence": max(confidence, float(existing_metadata.get("confidence") or 0.0)),
                        "activation_score": max(activation_score, float(existing_metadata.get("activation_score") or 0.0)),
                        "examples": list(primary_sense.get("examples") or merged_examples)[:12],
                        "model_examples": list(merged_evidence.get("model_examples") or [])[:12],
                        "aliases": list(dict.fromkeys(aliases))[:12],
                        "scene": primary_scene,
                        "review_status": review_status,
                        "senses": senses,
                        "sense_count": len(senses),
                        "active_sense_count": len(approved_senses),
                        "pending_sense_count": sum(
                            1 for item in senses
                            if str(item.get("review_status") or "") in {"review_pending", "revision_needed"}
                        ),
                        "last_candidate_sense_id": incoming_sense_id,
                        "last_candidate_was_new_sense": is_new_sense,
                        "sense_revision_reopened": sense_revision_reopened,
                        "candidate_origin": self._field(jargon, "candidate_origin", "jargon_miner"),
                        "classification": self._field(jargon, "classification", "jargon"),
                        "classification_reason": self._field(jargon, "classification_reason", ""),
                        "quality_tier": self._field(jargon, "quality_tier", "review"),
                        "quality_flags": list(self._field(jargon, "quality_flags", []) or []),
                        "term_type": self._field(jargon, "term_type", "jargon"),
                        "semantic_novelty": bool(self._field(jargon, "semantic_novelty", True)),
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
            conversation_event={
                "chat_kind": (
                    "group"
                    if "groupmessage" in str(event.unified_msg_origin or "").lower()
                    else "private"
                ),
                "role": "assistant",
                "message_kind": "text",
                "is_bot": True,
                "provenance": "bot_echo",
            },
        )
        triggered = self.recorder.record(event.unified_msg_origin)
        self._schedule_mining_if_triggered(event.unified_msg_origin, triggered)

    async def record_user_message(self, event: AstrMessageEvent | LearningMessageEnvelope):
        envelope = event if isinstance(event, LearningMessageEnvelope) else None
        if envelope is not None:
            event_id = str(envelope.event_id or "").strip()
            if event_id:
                exists = False
                checker = getattr(self.db, "message_log_event_exists_async", None)
                if callable(checker):
                    exists = bool(await checker(event_id))
                if exists:
                    return {"recorded": False, "reason": "duplicate_event", "event_id": event_id}
            rich_text = envelope.content
            group_id = envelope.chat_id
            sender_id = envelope.sender_id
            sender_name = envelope.sender_name
            conversation_event = dict(envelope.conversation_event or {})
            if event_id:
                conversation_event.setdefault("event_id", event_id)
        else:
            event_id = str(
                event.get_extra("astrmai_event_id", None)
                or getattr(getattr(event, "message_obj", None), "message_id", "")
                or ""
            ).strip()
            rich_text = event.get_extra("astrmai_rich_text", event.message_str)
            group_id = event.unified_msg_origin
            sender_id = event.get_sender_id()
            sender_name = event.get_sender_name()
            conversation_event = event.get_extra("astrmai_conversation_event", None)
        inserted = await self._append_message_log(
            group_id=group_id,
            sender_id=sender_id,
            sender_name=sender_name,
            content=rich_text,
            conversation_event={
                **dict(conversation_event or {}),
                **({"event_id": event_id} if event_id else {}),
            },
        )
        if not inserted:
            return {"recorded": False, "reason": "duplicate_event", "event_id": event_id}
        triggered = self.recorder.record(group_id)
        self._schedule_mining_if_triggered(group_id, triggered)
        payload = UserMessageRecordedEvent(
            chat_id=str(group_id),
            sender_id=str(sender_id or ""),
            sender_name=str(sender_name or ""),
            content=str(rich_text or ""),
        ).to_payload()
        await self._publish_learning_event("publish_learning_message_recorded", payload)
        return {"recorded": True, "event_id": event_id}

    async def enqueue_user_message(self, envelope: LearningMessageEnvelope) -> bool:
        """Durably enqueue an immutable learning ingress and kick the worker."""
        stores = [
            store
            for store in (
                getattr(self, "_ingest_outbox", None),
                getattr(self, "_ingest_fallback_outbox", None),
            )
            if store is not None
        ]
        if not stores:
            return False
        inserted = False
        primary = stores[0]
        try:
            inserted = await primary.enqueue(envelope)
            if not inserted and await primary.contains(envelope.event_id):
                inserted = True
        except Exception as exc:
            logger.warning("[Evolution] primary learning ingress outbox unavailable: %s", exc)
        if not inserted and len(stores) > 1:
            try:
                inserted = await stores[1].enqueue(envelope)
                if not inserted and await stores[1].contains(envelope.event_id):
                    inserted = True
            except Exception as exc:
                logger.warning("[Evolution] fallback learning ingress spool unavailable: %s", exc)
        if self._ingest_worker is None or self._ingest_worker.done():
            self._ingest_worker = asyncio.create_task(self._drain_ingest_outbox(), name="astrmai-learning-ingest")
            self._background_tasks.add(self._ingest_worker)
            self._register_owner_task(
                self._ingest_worker,
                task_family="learning.ingest",
                scope_id="GLOBAL",
                run_id=f"learning-ingest-{uuid.uuid4().hex[:12]}",
            )
            self._ingest_worker.add_done_callback(self._handle_task_result)
        return inserted

    async def _drain_ingest_outbox(self) -> None:
        stores = [
            store
            for store in (
                getattr(self, "_ingest_outbox", None),
                getattr(self, "_ingest_fallback_outbox", None),
            )
            if store is not None
        ]
        if not stores:
            return
        for store in stores:
            try:
                claim_due = getattr(store, "claim_due", None)
                entries = (
                    await claim_due(limit=50, lease_seconds=300.0)
                    if callable(claim_due)
                    else await store.list_due(limit=50)
                )
                await self._drain_ingest_entries(store, entries)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One unavailable store must not prevent the fallback spool
                # (or any later store) from being drained.
                logger.warning(
                    "[Evolution] learning ingest store degraded store=%s: %s",
                    type(store).__name__,
                    exc,
                )

    async def _drain_ingest_entries(self, store, entries) -> None:
        for entry in entries:
            if entry.event_id in self._ingest_processing:
                continue
            self._ingest_processing.add(entry.event_id)
            try:
                await self.record_user_message(entry.envelope)
                await store.delete(
                    entry.event_id,
                    lease_token=str(getattr(entry, "lease_token", "") or ""),
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    store.release_lease(
                        entry.event_id,
                        lease_token=str(getattr(entry, "lease_token", "") or ""),
                        next_retry_at=0.0,
                        error="cancelled",
                    )
                )
                raise
            except Exception as exc:
                retry_status = await store.mark_retry(
                    entry.event_id,
                    entry.attempts + 1,
                    str(exc),
                    lease_token=str(getattr(entry, "lease_token", "") or ""),
                )
                if retry_status == "exhausted":
                    logger.warning(
                        "[Evolution] learning ingest exhausted "
                        f"event_id={entry.event_id} attempts={entry.attempts + 1}: {exc}"
                    )
            finally:
                self._ingest_processing.discard(entry.event_id)

    async def _ingest_outbox_loop(self) -> None:
        while True:
            try:
                await self._drain_ingest_outbox()
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[Evolution] learning ingest outbox degraded: {exc}")
                await asyncio.sleep(5.0)

    async def _record_pipeline_state(
        self,
        *,
        run_id: str = "",
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
        # Direct/legacy callers may omit ``run_id``. Derive it from the same
        # immutable evidence fingerprint used by realtime and backlog paths so
        # retries cannot create a second ledger row for one mining batch.
        mining_run_id = str(run_id or self._mining_run_id(group_id, logs))
        pipeline_run_id = f"{mining_run_id}:{pipeline}"
        outcome = {
            "run_id": pipeline_run_id,
            "mining_run_id": mining_run_id,
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
                "run_id": pipeline_run_id,
                "mining_run_id": mining_run_id,
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
        *,
        run_id: str = "",
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
            run_id=run_id,
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
        *,
        run_id: str = "",
    ) -> dict[str, Any]:
        await self._pipeline_semaphore.acquire()
        self._active_pipeline_tasks += 1
        try:
            method = self._run_learning_pipeline_unlimited
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "run_id" in parameters:
                return await method(pipeline, group_id, logs, run_id=run_id)
            return await method(pipeline, group_id, logs)
        finally:
            self._active_pipeline_tasks = max(0, self._active_pipeline_tasks - 1)
            self._pipeline_semaphore.release()

    async def _run_learning_pipeline_unlimited(
        self,
        pipeline: str,
        group_id: str,
        logs: List["MessageLog"],
        *,
        run_id: str = "",
    ) -> dict[str, Any]:
        started = time.perf_counter()
        timeout_sec = max(
            0.01,
            float(getattr(self._evolution_config(), "learning_pipeline_timeout_sec", 60.0) or 60.0),
        )

        async def _within_budget(awaitable):
            remaining = timeout_sec - (time.perf_counter() - started)
            if remaining <= 0:
                if hasattr(awaitable, "close"):
                    awaitable.close()
                raise TimeoutError(f"learning_pipeline_timeout:{timeout_sec:g}s")
            try:
                return await asyncio.wait_for(awaitable, timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"learning_pipeline_timeout:{timeout_sec:g}s") from exc

        cursor_before = max(0, int(self._field(logs[0], "id", 1) or 1) - 1) if logs else 0
        batch_id = self._mining_batch_id(group_id, logs, prefix=pipeline)
        failure_key = f"{pipeline}:{group_id}"
        checkpoint = await self._get_pipeline_checkpoint(pipeline, group_id)
        failure_report: dict[str, Any] = {}
        try:
            if pipeline == "expression":
                items = await _within_budget(self.expression_miner.mine(group_id, logs))
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
                        run_id=run_id,
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
                persistence = await _within_budget(self._save_patterns(items, mining_batch_id=batch_id))
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
                items = await _within_budget(self.jargon_miner.mine(group_id, logs))
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
                        run_id=run_id,
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
                saved_count = await _within_budget(self._save_jargons(group_id, items, mining_batch_id=batch_id))
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
                run_id=run_id,
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
                run_id=run_id,
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
                    "pipeline_timeout_sec": timeout_sec,
                    "failure_count": failures,
                    "retry_at": retry_at,
                },
                retryable=True,
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

    async def process_logs_and_mine(
        self,
        group_id: str,
        logs: List["MessageLog"],
        *,
        run_id: str | None = None,
        snapshot_is_authoritative: bool = False,
        pipeline_snapshots: dict[str, List["MessageLog"]] | None = None,
    ):
        if not logs:
            return {}
        run_id = str(run_id or self._mining_run_id(group_id, logs))
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
                pipeline_logs = (
                    list((pipeline_snapshots or {}).get(pipeline, logs) or [])
                    if snapshot_is_authoritative
                    else list(await self._load_pipeline_logs(pipeline, group_id, limit=limit) or [])
                )
                if not pipeline_logs:
                    continue
                if not self._is_group_learning_scope(group_id, pipeline_logs):
                    outcome = await self._skip_non_group_pipeline(
                        pipeline, group_id, pipeline_logs, run_id=run_id
                    )
                else:
                    outcome = await self._run_learning_pipeline(
                        pipeline, group_id, pipeline_logs, run_id=run_id
                    )
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
                "run_id": run_id,
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
        run_id: str | None = None,
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
        stable_run_id = str(run_id or self._mining_run_id(group_id, logs))
        outcome = {
            "run_id": stable_run_id,
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
            parsed = parse_json_contract(
                result,
                required_keys=("goal",),
                field_types={"goal": str},
                allow_extra_keys=False,
                allow_naked_members=True,
            )
            if parsed.schema_valid:
                return str(parsed.value.get("goal", "陪伴用户，提供有趣且连贯的对话"))
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
        await self._load_mining_timestamp(group_id)
        if self._mining_cooldown_active(group_id):
            return
        limit = max(100, self._backlog_batch_size())
        snapshot = await self._load_learning_snapshot(group_id, limit)
        seed_logs = list(snapshot.get("logs") or [])
        eligible = False
        for pipeline in ("expression", "jargon"):
            if not self._pipeline_enabled(pipeline):
                continue
            logs = list(dict(snapshot.get("pipeline_logs") or {}).get(pipeline, ()) or ())
            threshold = max(int(self.recorder.min_messages or 0), self._pipeline_threshold(pipeline))
            eligible = eligible or len(logs) >= threshold
        if seed_logs and eligible:
            run_id = self._mining_run_id(group_id, seed_logs)
            mining_fingerprint = self._mining_batch_id(
                group_id, seed_logs, prefix="learning"
            )
            lease = None
            if self._task_ledger is not None:
                lease = await self._task_ledger.claim(
                    task_family="learning_mining",
                    scope_id=str(group_id),
                    input_fingerprint=self._mining_batch_id(group_id, seed_logs, prefix="learning"),
                    lease_seconds=max(300.0, float(getattr(self._evolution_config(), "learning_pipeline_timeout_sec", 60.0) or 60.0)),
                    min_interval_seconds=max(0.0, self._mining_interval()),
                    payload={"run_id": run_id, "batch_id": mining_fingerprint},
                )
                if lease is None:
                    return
            previous_mining_at = float(self._last_mining_at.get(str(group_id), 0.0) or 0.0)
            self._last_mining_at[str(group_id)] = time.time()
            await self._persist_mining_timestamp(group_id, self._last_mining_at[str(group_id)])
            try:
                outcomes = await self._process_mining_snapshot(
                    group_id, snapshot, run_id=run_id
                )
                all_failed = bool(outcomes) and all(
                    str(item.get("status", "")).lower() in {"failed", "quarantined"}
                    for item in outcomes.values()
                )
                if all_failed:
                    self._last_mining_at[str(group_id)] = previous_mining_at
                    await self._persist_mining_timestamp(group_id, previous_mining_at)
                if lease is not None:
                    await settle_task_lease(
                        self._task_ledger,
                        lease,
                        run_id=run_id,
                        status="retry_wait" if all_failed else "succeeded",
                        error="all_pipelines_failed" if all_failed else "",
                        retry_after_seconds=self._backlog_failure_cooldown() if all_failed else 0.0,
                    )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._settle_cancelled_mining(
                        group_id,
                        previous_mining_at,
                        lease,
                        run_id=run_id,
                    )
                )
                raise
            except Exception as exc:
                self._last_mining_at[str(group_id)] = previous_mining_at
                await self._persist_mining_timestamp(group_id, previous_mining_at)
                if lease is not None:
                    await settle_task_lease(
                        self._task_ledger,
                        lease,
                        run_id=run_id,
                        status="retry_wait",
                        error=str(exc),
                        retry_after_seconds=self._backlog_failure_cooldown(),
                    )
                raise

    async def run_backlog_mining_once(self) -> dict[str, Any]:
        purge_report = await self._maybe_purge_learning_runs()
        message_log_purge = await self._maybe_purge_message_logs()
        if not self._backlog_enabled():
            report = {
                "enabled": False,
                "checked_at": time.time(),
                "run_retention": purge_report,
                "message_log_retention": message_log_purge,
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
            "message_log_retention": message_log_purge,
        }

        processed_count = 0
        for group in groups:
            group_id = str(group.get("group_id", "") or "")
            if not group_id:
                continue
            if processed_count >= group_limit:
                break
            await self._load_mining_timestamp(group_id)
            if self._mining_tasks.get(group_id) is not None and not self._mining_tasks[group_id].done():
                report["skipped_groups"].append({"group_id": group_id, "reason": "already_mining"})
                continue
            if self._mining_cooldown_active(group_id, now):
                report["skipped_groups"].append({"group_id": group_id, "reason": "mining_interval"})
                continue
            failure_until = float(self._backlog_failure_until.get(group_id, 0.0) or 0.0)
            if failure_until > now:
                report["skipped_groups"].append(
                    {"group_id": group_id, "reason": "failure_cooldown", "retry_after": failure_until}
                )
                continue
            snapshot = await self._load_learning_snapshot(group_id, batch_size)
            logs = list(snapshot.get("logs") or [])
            pipeline_logs = dict(snapshot.get("pipeline_logs") or {})
            eligible_pipelines = [
                pipeline
                for pipeline, items in pipeline_logs.items()
                if len(list(items or [])) >= max(threshold, self._pipeline_threshold(pipeline))
            ]
            if not eligible_pipelines:
                report["skipped_groups"].append(
                    {
                        "group_id": group_id,
                        "reason": "below_threshold",
                        "count": len(logs),
                        "pipeline": "snapshot",
                    }
                )
                continue
            lease = None
            previous_mining_at = None
            run_id = self._mining_run_id(group_id, logs)
            mining_fingerprint = self._mining_batch_id(
                group_id, logs, prefix="learning"
            )
            try:
                if self._task_ledger is not None:
                    lease = await self._task_ledger.claim(
                        task_family="learning_mining",
                        scope_id=group_id,
                        input_fingerprint=mining_fingerprint,
                        lease_seconds=max(300.0, float(getattr(self._evolution_config(), "learning_pipeline_timeout_sec", 60.0) or 60.0)),
                        min_interval_seconds=max(0.0, self._mining_interval()),
                        payload={"run_id": run_id, "batch_id": mining_fingerprint},
                    )
                    if lease is None:
                        report["skipped_groups"].append({"group_id": group_id, "reason": "lease_busy"})
                        continue
                previous_mining_at = float(self._last_mining_at.get(group_id, 0.0) or 0.0)
                self._last_mining_at[group_id] = time.time()
                await self._persist_mining_timestamp(group_id, self._last_mining_at[group_id])
                outcomes = await self._process_mining_snapshot(
                    group_id, snapshot, run_id=run_id
                )
                all_failed = bool(outcomes) and all(
                    str(item.get("status", "")).lower() in {"failed", "quarantined"}
                    for item in outcomes.values()
                )
                if all_failed:
                    self._last_mining_at[group_id] = previous_mining_at
                    await self._persist_mining_timestamp(group_id, previous_mining_at)
                if lease is not None:
                    await settle_task_lease(
                        self._task_ledger,
                        lease,
                        run_id=run_id,
                        status="retry_wait" if all_failed else "succeeded",
                        error="all_pipelines_failed" if all_failed else "",
                        retry_after_seconds=self._backlog_failure_cooldown() if all_failed else 0.0,
                    )
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
            except asyncio.CancelledError:
                if previous_mining_at is not None:
                    await asyncio.shield(
                        self._settle_cancelled_mining(
                            group_id,
                            previous_mining_at,
                            lease,
                            run_id=run_id,
                        )
                    )
                elif self._task_ledger is not None and lease is not None:
                    await asyncio.shield(
                        settle_task_lease(
                            self._task_ledger,
                            lease,
                            run_id=run_id,
                            status="retry_wait",
                            error="cancelled",
                            retry_after_seconds=0.0,
                        )
                    )
                raise
            except Exception as exc:
                if previous_mining_at is not None:
                    self._last_mining_at[group_id] = previous_mining_at
                    await self._persist_mining_timestamp(group_id, previous_mining_at)
                if self._task_ledger is not None and lease is not None:
                    await settle_task_lease(
                        self._task_ledger,
                        lease,
                        run_id=run_id,
                        status="retry_wait",
                        error=str(exc),
                        retry_after_seconds=self._backlog_failure_cooldown(),
                    )
                failure_count = int(self._backlog_failure_counts.get(group_id, 0) or 0) + 1
                self._backlog_failure_counts[group_id] = failure_count
                self._backlog_failure_until[group_id] = time.time() + self._backlog_failure_cooldown()
                logger.warning(f"[Evolution-Backlog] mining failed for {group_id}: {exc}")
                report["errors"].append({"group_id": group_id, "error": str(exc)})

        self._last_backlog_report = report
        return report

    async def _process_mining_snapshot(
        self,
        group_id: str,
        snapshot: Any,
        *,
        run_id: str = "",
    ):
        """Call the snapshot-aware API while retaining legacy test adapters."""
        method = self.process_logs_and_mine
        if isinstance(snapshot, dict):
            logs = list(snapshot.get("logs") or [])
            pipeline_snapshots = dict(snapshot.get("pipeline_logs") or {})
        else:
            logs = list(snapshot or [])
            pipeline_snapshots = {}
        try:
            parameters = inspect.signature(method).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs = {}
        if "snapshot_is_authoritative" in parameters:
            kwargs["snapshot_is_authoritative"] = True
        if "pipeline_snapshots" in parameters:
            kwargs["pipeline_snapshots"] = pipeline_snapshots
        if "run_id" in parameters:
            kwargs["run_id"] = str(run_id or "")
        if kwargs:
            return await method(group_id, logs, **kwargs)
        return await method(group_id, logs)

    async def _backlog_mining_loop(self) -> None:
        try:
            await asyncio.sleep(30)
            while True:
                mining_task = asyncio.create_task(
                    self._run_backlog_mining_budgeted(),
                    name="astrmai-learning-backlog-mining",
                )
                self._backlog_mining_task = mining_task
                self._register_owner_task(
                    mining_task,
                    task_family="learning.backlog",
                    scope_id="GLOBAL",
                    run_id=f"learning-backlog-{uuid.uuid4().hex[:12]}",
                )
                try:
                    await mining_task
                except Exception as exc:
                    logger.warning(f"[Evolution-Backlog] scan degraded: {exc}")
                finally:
                    if self._backlog_mining_task is mining_task:
                        self._backlog_mining_task = None
                await asyncio.sleep(self._backlog_scan_interval())
        except asyncio.CancelledError:
            mining_task = self._backlog_mining_task
            if mining_task is not None and not mining_task.done():
                mining_task.cancel()
                await asyncio.gather(mining_task, return_exceptions=True)
            self._backlog_mining_task = None
            logger.info("[Evolution-Backlog] backlog mining worker stopped")
            raise

    async def start_background_tasks(self) -> None:
        if self._task_ledger is not None:
            try:
                recovered = await self._task_ledger.recover_expired_leases()
                if recovered:
                    logger.info(
                        "[Evolution] recovered expired background leases count=%s",
                        recovered,
                    )
            except Exception as exc:
                logger.warning("[Evolution] background lease recovery degraded: %s", exc)
        if self._backlog_task is None or self._backlog_task.done():
            self._backlog_task = asyncio.create_task(
                self._backlog_mining_loop(),
                name="astrmai-learning-backlog-scheduler",
            )
            self._background_tasks.add(self._backlog_task)
            self._register_owner_task(
                self._backlog_task,
                task_family="learning.backlog.scheduler",
                scope_id="GLOBAL",
                run_id=f"learning-backlog-scheduler-{uuid.uuid4().hex[:12]}",
            )
            self._backlog_task.add_done_callback(self._handle_task_result)
        if (
            self._ingest_outbox is not None
            or self._ingest_fallback_outbox is not None
        ) and (self._ingest_worker is None or self._ingest_worker.done()):
            self._ingest_worker = asyncio.create_task(
                self._ingest_outbox_loop(), name="astrmai-learning-ingest-worker"
            )
            self._background_tasks.add(self._ingest_worker)
            self._register_owner_task(
                self._ingest_worker,
                task_family="learning.ingest.worker",
                scope_id="GLOBAL",
                run_id=f"learning-ingest-worker-{uuid.uuid4().hex[:12]}",
            )
            self._ingest_worker.add_done_callback(self._handle_task_result)

    async def stop_background_tasks(self) -> None:
        self._mining_rerun_requested.clear()
        tasks = [
            task
            for task in [self._backlog_task, self._backlog_mining_task, *self._background_tasks]
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._backlog_task = None
        self._ingest_worker = None
        self._ingest_processing.clear()

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
