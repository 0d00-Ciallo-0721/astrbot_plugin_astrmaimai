from __future__ import annotations

import asyncio
import json
import re
from typing import Dict, List

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

    async def _mark_logs_processed(self, log_ids: List[int]) -> None:
        if hasattr(self.db, "mark_logs_processed_async"):
            await self.db.mark_logs_processed_async(log_ids)
            return
        self.db.mark_logs_processed(log_ids)

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

    async def _save_patterns(self, patterns) -> None:
        memory_engine = getattr(self.db, "memory_engine", None)
        service = getattr(memory_engine, "expression_pattern_service", None) if memory_engine else None
        if service and hasattr(service, "write_pattern"):
            for pattern in patterns or []:
                review_status = self._normalize_pattern_review_status(self._field(pattern, "review_status", "pending"))
                await service.write_pattern(
                    str(self._field(pattern, "group_id", "") or ""),
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
                    },
                    source="learning_expression_pattern",
            )
            return
        return

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
            is_jargon = bool(self._field(jargon, "is_jargon", True))
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

            patterns = await self.expression_miner.mine(group_id, logs)
            await self._save_patterns(patterns)

            jargon_count = 0
            if getattr(getattr(self.db, "memory_engine", None), "write_service", None):
                jargons = await self.jargon_miner.mine(group_id, logs)
                jargon_count = await self._save_jargons(group_id, jargons)

            await self._mark_logs_processed([self._field(log, "id") for log in logs])
            payload = MiningCompletedEvent(
                group_id=str(group_id),
                pattern_count=len(patterns),
                jargon_count=jargon_count,
            ).to_payload()
            await self._publish_learning_event("publish_learning_mining_completed", payload)
            if self.event_bus and jargon_count > 0:
                self.event_bus.trigger_knowledge_update()

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
        threshold = self.recorder.min_messages
        if len(unprocessed_logs) >= threshold:
            await self.process_logs_and_mine(group_id, unprocessed_logs)

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
