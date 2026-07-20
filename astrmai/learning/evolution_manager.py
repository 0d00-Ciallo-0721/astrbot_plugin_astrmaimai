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
        self._last_backlog_report: dict[str, Any] = {}
        self._last_expression_backfill: dict[str, Any] = {}
        self._last_mining_outcomes: dict[str, dict[str, Any]] = {}

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
        if self.jargon_miner.enricher is not None:
            self.jargon_miner.enricher.config = config
        if not self._backlog_enabled() and self._backlog_task is not None:
            self._backlog_task.cancel()

    def _evolution_config(self):
        return getattr(self.config, "evolution", None)

    def _backlog_enabled(self) -> bool:
        evolution = self._evolution_config()
        return bool(
            getattr(evolution, "enable_expression_mining", True)
            and getattr(evolution, "enable_backlog_mining", True)
        )

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

    async def _append_message_log(self, *, group_id: str, sender_id: str, sender_name: str, content: str):
        if hasattr(self.db, "add_message_log_async"):
            await self.db.add_message_log_async(
                group_id=group_id,
                sender_id=sender_id,
                sender_name=sender_name,
                content=content,
            )
            return
        await asyncio.to_thread(
            self.db.add_message_log,
            group_id=group_id,
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
        )

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

    async def backlog_overview(self) -> dict[str, Any]:
        threshold = self._backlog_threshold()
        try:
            groups = await self._list_unprocessed_log_groups(min_count=1, limit=10)
            degraded = ""
        except Exception as exc:
            groups = []
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
            "eligible_groups": eligible,
            "last_report": dict(self._last_backlog_report or {}),
            "worker_running": bool(self._backlog_task is not None and not self._backlog_task.done()),
            "degraded": bool(degraded),
            "degraded_reason": degraded,
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
            shared_scope = str(self._field(pattern, "shared_scope", "") or group_id)
            existing = None
            try:
                dedup_key = service.build_dedup_key(group_id, situation, expression, shared_scope)
                existing = await service.store.get_by_dedup_key(dedup_key, include_inactive=True)
                memory_id = await service.write_pattern(
                    group_id,
                    {
                        "expression": self._field(pattern, "expression", ""),
                        "situation": self._field(pattern, "situation", ""),
                        "style": self._field(pattern, "style", ""),
                        "content_samples": list(self._field(pattern, "content_samples", []) or []),
                        "count": int(self._field(pattern, "count", 1) or 1),
                        "think_level": int(self._field(pattern, "think_level", 0) or 0),
                        "review_status": review_status,
                        "review_reason": self._field(pattern, "review_reason", ""),
                        "review_suggestion": self._field(pattern, "review_suggestion", ""),
                        "weight": float(self._field(pattern, "weight", 1.0) or 1.0),
                        "shared_scope": self._field(pattern, "shared_scope", ""),
                        "summary": self._field(pattern, "summary", self._field(pattern, "expression", "")),
                        "confidence": float(self._field(pattern, "confidence", self._field(pattern, "activation_score", 0.65)) or 0.65),
                        "activation_score": float(self._field(pattern, "activation_score", 0.65) or 0.65),
                        "candidate_id": self._field(pattern, "candidate_id", ""),
                        "evidence_message_ids": list(self._field(pattern, "evidence_message_ids", []) or []),
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

    async def _save_jargons(self, group_id: str, jargons) -> int:
        memory_engine = getattr(self.db, "memory_engine", None)
        writer = getattr(memory_engine, "write_service", None) if memory_engine else None
        if not writer or not hasattr(writer, "write"):
            return 0

        def _normalized_key(value: str) -> str:
            return "".join(str(value or "").strip().lower().split())

        requests: list[MemoryWriteRequest] = []
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
            requests.append(
                MemoryWriteRequest(
                    source="learning_jargon",
                    kind="jargon",
                    session_id=str(group_id or ""),
                    content=content,
                    summary=meaning or content,
                    tags=["jargon", "learning"],
                    importance=min(1.0, max(0.35, activation_score)),
                    confidence=max(0.1, min(confidence or activation_score or 0.55, 1.0)),
                    metadata={
                        "raw_content": raw_content,
                        "meaning": meaning,
                        "confidence": confidence,
                        "activation_score": activation_score,
                        "examples": examples,
                        "scene": scene,
                        "review_status": review_status,
                        "count": int(self._field(jargon, "count", 1) or 1),
                    },
                    dedup_key=f"jargon:{group_id}:{_normalized_key(content)}",
                    source_ref=f"learning_jargon:{group_id}:{_normalized_key(content)}",
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
        await self._append_message_log(
            group_id=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            content=rich_text,
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

    async def process_logs_and_mine(self, group_id: str, logs: List["MessageLog"]):
        if not logs:
            return
        group_lock = await self._get_mining_lock(group_id)
        async with group_lock:
            current_unprocessed = await self._load_unprocessed_logs(group_id, limit=999)
            requested_ids = {
                self._field(log, "id")
                for log in logs
                if self._field(log, "id") is not None
            }
            logs = [
                log
                for log in current_unprocessed
                if self._field(log, "id") in requested_ids
            ]
            if not logs:
                return

            batch_id = self._mining_batch_id(group_id, logs)
            patterns = await self.expression_miner.mine(group_id, logs)
            expression_report = dict(getattr(self.expression_miner, "last_report", {}) or {})
            enrichment_report = expression_report.get("enrichment")
            enrichment_terminal = True
            enrichment_retryable = False
            if isinstance(enrichment_report, dict):
                enrichment_terminal = bool(enrichment_report.get("terminal"))
                enrichment_retryable = bool(enrichment_report.get("retryable"))
            elif expression_report.get("reason") == "enrichment_empty" and int(
                expression_report.get("candidate_count", 0) or 0
            ) > 0:
                enrichment_terminal = False
                enrichment_retryable = True

            pattern_save_report = await self._save_patterns(patterns, mining_batch_id=batch_id)
            if not enrichment_terminal or not pattern_save_report.complete:
                await self._record_mining_outcome(
                    group_id,
                    logs,
                    status="degraded",
                    reason=(
                        "expression_enrichment_incomplete"
                        if not enrichment_terminal
                        else "expression_persistence_incomplete"
                    ),
                    pattern_count=pattern_save_report.saved + pattern_save_report.deduplicated,
                    expression_report=expression_report,
                    persistence_report=pattern_save_report.to_report(),
                    retryable=enrichment_retryable or not pattern_save_report.complete,
                )
                raise RuntimeError("expression mining did not reach a durable terminal state")

            jargon_count = 0
            if getattr(getattr(self.db, "memory_engine", None), "write_service", None):
                jargons = await self.jargon_miner.mine(group_id, logs)
                jargon_report = dict(getattr(self.jargon_miner, "last_report", {}) or {})
                if jargon_report.get("reason") == "enrichment_empty" and int(
                    jargon_report.get("candidate_count", 0) or 0
                ) > 0:
                    await self._record_mining_outcome(
                        group_id,
                        logs,
                        status="degraded",
                        reason="jargon_enrichment_failed_closed",
                    )
                    raise RuntimeError("jargon enrichment failed closed")
                jargon_count = await self._save_jargons(group_id, jargons)

            evolution = self._evolution_config()
            min_context = max(1, int(getattr(evolution, "min_mining_context", 10) or 10))
            overlap = max(2, min(10, min_context // 2)) if len(logs) >= min_context else 0
            processable = logs[:-overlap] if overlap and len(logs) > overlap else logs
            processed_ids = [self._field(log, "id") for log in processable if self._field(log, "id") is not None]
            if processed_ids:
                await self._mark_logs_processed(processed_ids)
            await self._record_mining_outcome(
                group_id,
                logs,
                status="completed",
                reason="candidates_saved" if patterns or jargon_count else "no_candidates_rolling_overlap",
                pattern_count=pattern_save_report.saved + pattern_save_report.deduplicated,
                jargon_count=jargon_count,
                processed_count=len(processed_ids),
                retained_overlap=len(logs) - len(processed_ids),
                expression_report=expression_report,
                persistence_report=pattern_save_report.to_report(),
            )
            payload = MiningCompletedEvent(
                group_id=str(group_id),
                pattern_count=pattern_save_report.saved + pattern_save_report.deduplicated,
                jargon_count=jargon_count,
            ).to_payload()
            await self._publish_learning_event("publish_learning_mining_completed", payload)
            if self.event_bus and jargon_count > 0:
                self.event_bus.trigger_knowledge_update()

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
        unprocessed_logs = await self._load_unprocessed_logs(group_id, limit=100)
        evolution = self._evolution_config()
        threshold = max(int(self.recorder.min_messages or 0), int(getattr(evolution, "min_mining_context", 10) or 10))
        if len(unprocessed_logs) >= threshold:
            await self.process_logs_and_mine(group_id, unprocessed_logs)

    async def run_backlog_mining_once(self) -> dict[str, Any]:
        if not self._backlog_enabled():
            report = {
                "enabled": False,
                "checked_at": time.time(),
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
        groups = await self._list_unprocessed_log_groups(min_count=threshold, limit=group_limit * 3)
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
            logs = await self._load_unprocessed_logs(group_id, limit=batch_size)
            if len(logs) < threshold:
                report["skipped_groups"].append(
                    {"group_id": group_id, "reason": "below_threshold", "count": len(logs)}
                )
                continue
            try:
                await self.process_logs_and_mine(group_id, logs)
                processed_count += 1
                report["processed_groups"].append({"group_id": group_id, "log_count": len(logs)})
                self._backlog_failure_until.pop(group_id, None)
            except Exception as exc:
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
        if not self._backlog_enabled():
            return
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
