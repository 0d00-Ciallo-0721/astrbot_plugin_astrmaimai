from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import re
import time
from typing import Dict, List, Optional

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest
from .memory_claim_service import MemoryClaimExtractor, MemoryConflictResolver
from .memory_processor import MemoryProcessor
from .topic_summarizer import TopicSummarizer


class SessionMemorySummarizer:
    """Authoritative long-horizon memory summarization implementation."""

    def __init__(self, context, gateway, engine, config=None):
        self.context = context
        self.gateway = gateway
        self.engine = engine
        self.config = config if config else gateway.config
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)
        self.check_interval = getattr(self.config.memory, "cleanup_interval", 3600)
        self.msg_threshold = getattr(self.config.memory, "summary_threshold", 30)
        self.processor = MemoryProcessor(gateway)
        self.topic_summarizer = TopicSummarizer(gateway, config)
        self.claim_extractor = MemoryClaimExtractor(gateway)
        self.conflict_resolver = MemoryConflictResolver()

    def refresh_config(self, config) -> None:
        self.config = config
        memory_config = getattr(config, "memory", None)
        self.check_interval = getattr(memory_config, "cleanup_interval", 3600)
        self.msg_threshold = getattr(memory_config, "summary_threshold", 30)
        refresh = getattr(self.topic_summarizer, "refresh_config", None)
        if callable(refresh):
            refresh(config)
        else:
            self.topic_summarizer.config = config

    async def extract_and_summarize_history(self, session_id: str, days: int = 1):
        plugin = getattr(self.context, "astrmai_plugin", None) or getattr(self.gateway.context, "astrmai", None)
        if not plugin or not hasattr(plugin, "db_service"):
            return
        db = plugin.db_service
        try:
            from sqlmodel import select
            from ...infrastructure.persistence import MessageLog

            now = time.time()
            cutoff_time = now - (days * 86400)
            if cutoff_time > now:  # ponytail: NTP backward jump guard
                logger.warning(f"[AstrMai-summarizer] NTP backward jump: cutoff={cutoff_time} > now={now}, clamping")
                cutoff_time = 0.0

            watermark = await self._get_summary_watermark(session_id)

            def fetch_logs_sync():
                with db.get_session() as session:
                    filters = [
                        MessageLog.group_id == session_id,
                        MessageLog.timestamp >= cutoff_time,
                    ]
                    if watermark > 0:
                        filters.append(MessageLog.id > watermark)
                    statement = select(MessageLog).where(*filters).order_by(MessageLog.id.asc())
                    results = session.exec(statement).all()
                    return [MessageLog.model_validate(r.model_dump()) for r in results]

            logs = await asyncio.to_thread(fetch_logs_sync)
            if not logs:
                return
            if len(logs) < max(1, int(self.msg_threshold or 1)):
                await self._observe(
                    session_id,
                    "summarize_deferred",
                    reason="below_message_threshold",
                    summary=f"new_messages={len(logs)}, threshold={self.msg_threshold}",
                )
                return

            history_lines = []
            topic_messages = []
            for index, log in enumerate(logs):
                content = log.content
                if not content:
                    continue
                if len(content) > 2000:
                    content = content[:2000] + "..."
                time_str = time.strftime("%H:%M:%S", time.localtime(log.timestamp))
                history_lines.append(f"[{time_str}] {log.sender_name}: {content}")
                topic_messages.append(
                    {
                        "message_id": int(log.id or 0),
                        "sender_id": str(log.sender_id or ""),
                        "sender": log.sender_name,
                        "content": content,
                        "timestamp": log.timestamp if log.timestamp is not None else float(index),
                    }
                )

            full_history = "\n".join(history_lines)
            if full_history:
                completed = await self.summarize_session(session_id, full_history, messages=topic_messages)
                last_log_id = max((int(log.id or 0) for log in logs), default=0)
                if completed is not False and last_log_id > watermark:
                    await self._set_summary_watermark(session_id, last_log_id)
        except Exception as exc:
            logger.error(f"[SessionMemorySummarizer] batch history extract failed: {exc}", exc_info=True)

    async def summarize_session(self, session_id: str, chat_history_text: str, persona_id: Optional[str] = None, messages: Optional[List[Dict]] = None):
        if not str(chat_history_text or "").strip():
            return False

        logger.info(f"[SessionMemorySummarizer] summarizing session {session_id}")
        await self._observe(session_id, "summarize_started", summary="session summarize started")
        try:
            topic_messages = messages or self._build_topic_messages(chat_history_text)
            topic_segments = []
            if topic_messages:
                topic_segments = await self.topic_summarizer.process_history(messages=topic_messages, session_id=session_id)
            if topic_segments and hasattr(self.engine, "store_topic_results"):
                await self.engine.store_topic_results(topic_results=topic_segments, session_id=session_id, persona_id=persona_id)
        except Exception as exc:
            logger.warning(f"[SessionMemorySummarizer] topic summarization degraded: {exc}")
            await self._observe(session_id, "topic_summarizer_degraded", level="warning", reason="topic_summarizer_degraded", summary=str(exc))

        try:
            memory_data = await self.processor.process_conversation(chat_history_text, session_id=session_id)
        except TypeError:
            memory_data = await self.processor.process_conversation(chat_history_text)
        if not isinstance(memory_data, dict):
            logger.warning(f"[SessionMemorySummarizer] invalid processor result for {session_id}")
            await self._observe(session_id, "memory_processor_invalid", level="warning", reason="memory_processor_invalid")
            return False

        summary = memory_data.get("summary", "")
        key_facts = memory_data.get("key_facts", [])
        topics = memory_data.get("topics", [])
        sentiment = memory_data.get("sentiment", "neutral")
        reflection = memory_data.get("reflection", "")
        nodes = memory_data.get("nodes", [])
        try:
            importance = float(memory_data.get("importance", 0.0))
        except (TypeError, ValueError):
            importance = 0.0
        if not isinstance(key_facts, list):
            key_facts = [str(key_facts)] if key_facts else []
        if not isinstance(topics, list):
            topics = [str(topics)] if topics else []
        if not key_facts and summary == "对话记录":
            await self._observe(session_id, "summarize_skipped", reason="no_effective_facts", summary="processor produced no effective facts")
            return True
        if importance < 0.2:
            await self._observe(session_id, "summarize_skipped", reason="low_importance", summary=f"importance={importance:.2f}")
            return True

        plugin = getattr(self.context, "astrmai_plugin", None) or getattr(self.gateway.context, "astrmai", None)
        if plugin and hasattr(plugin, "db_service"):
            db = plugin.db_service
            from ...infrastructure.persistence import MemoryNode

            if nodes and hasattr(db, "update_nodes_async"):
                node_objs = [MemoryNode(**item) for item in nodes if isinstance(item, dict)]
                await db.update_nodes_async(node_objs)

        content_lines = [f"【摘要】{summary}"]
        valid_facts = [str(item) for item in key_facts if str(item).strip()]
        if valid_facts:
            content_lines.append("【核心事实】\n- " + "\n- ".join(valid_facts))
        valid_topics = [str(item) for item in topics if str(item).strip()]
        if reflection:
            content_lines.append(f"【深度反思】{reflection}")
        if valid_topics:
            content_lines.append(f"【话题标签】{', '.join(valid_topics)}")
        final_content = "\n".join(content_lines)

        topic_messages = messages or self._build_topic_messages(chat_history_text)
        history_digest = self._history_digest(chat_history_text, topic_messages)
        event_id = f"evt_summary_{history_digest[:20]}"
        subject_id = self._default_subject_id(session_id)
        evidence_message_ids = [
            int(item.get("message_id") or 0)
            for item in topic_messages
            if int(item.get("message_id") or 0) > 0
        ]
        canonical_id = ""
        if hasattr(self.engine, "write_service"):
            claims = await self.claim_extractor.extract(
                user_text="\n".join(valid_facts) if valid_facts else str(summary or ""),
                assistant_text="",
                subject_id=subject_id,
                turn_id=event_id,
                context_hint="session_memory_summarizer",
                lane_scope_id=session_id,
                lane_scope_kind="chat",
            )
            decision = self.conflict_resolver.resolve(claims)
            metadata = {
                "legacy_event_id": event_id,
                "reflection": reflection,
                "sentiment": sentiment,
                "canonical_write": True,
                "history_digest": history_digest,
                "evidence_message_ids": evidence_message_ids,
                "speaker_ids": sorted(
                    {
                        str(item.get("sender_id") or "").strip()
                        for item in topic_messages
                        if str(item.get("sender_id") or "").strip()
                    }
                ),
            }
            kind = "memory"
            dedup_key = f"memory_summary:{session_id}:{history_digest}"
            if decision.metadata:
                metadata.update(dict(decision.metadata))
            if decision.action == "authority_override" and claims:
                claim = claims[0]
                kind = "fact"
                metadata.update(
                    {
                        "authority_eav": True,
                        "promotion_entity": claim.entity,
                        "promotion_attribute": claim.attribute,
                        "promotion_value": claim.value,
                        "supports": [],
                        "contradicts": [],
                        "corrects": [],
                    }
                )
                dedup_key = f"{claim.subject_id}:{claim.entity}:{claim.attribute}"
            canonical_id = await self.engine.write_service.write(
                MemoryWriteRequest(
                    source="memory_summary",
                    kind=kind,
                    session_id=str(session_id),
                    sender_id=str(claims[0].subject_id if claims else subject_id),
                    persona_id=str(persona_id or ""),
                    content=final_content,
                    summary=str(summary or "")[:240],
                    tags=valid_topics,
                    importance=float(importance or 0.5),
                    confidence=min(0.8, max(0.5, float(importance or 0.5))),
                    metadata=metadata,
                    dedup_key=dedup_key,
                    source_ref=f"MessageLogWindow:{history_digest}",
                )
            )
            await self._observe(
                session_id,
                "canonical_write_success",
                summary=str(summary or "")[:120],
                memory_id=str(canonical_id or ""),
            )
        else:
            await self.engine.add_memory(
                content=final_content,
                session_id=str(session_id),
                persona_id=persona_id,
                importance=importance,
            )

        from ...infrastructure.persistence import MemoryEvent

        event = MemoryEvent(
            event_id=event_id,
            session_id=str(session_id),
            date=datetime.datetime.now().strftime("%Y-%m-%d"),
            narrative="\n".join(valid_facts),
            emotion=sentiment,
            importance=int(importance * 10),
            emotional_intensity=int(importance * 10),
            reflection=reflection,
            memory_kind="topic" if valid_topics else "fact",
            source_layer="topic" if valid_topics else "fact",
            tags=json.dumps([*valid_topics, f"canonical_id:{canonical_id}"] if canonical_id else valid_topics, ensure_ascii=False),
        )
        if plugin and hasattr(plugin, "db_service") and hasattr(plugin.db_service, "save_event_async"):
            try:
                await plugin.db_service.save_event_async(event)
                await self._observe(session_id, "legacy_event_write_success", summary=event.event_id)
            except Exception as exc:
                await self._observe(session_id, "legacy_event_write_failed", level="warning", reason="legacy_event_write_failed", summary=str(exc))

        if hasattr(self.engine, "record_cognitive_feedback"):
            try:
                feedback_parts = [str(summary or "")[:220]]
                if valid_topics:
                    feedback_parts.append("topics: " + ", ".join(valid_topics[:6]))
                if reflection:
                    feedback_parts.append("reflection: " + str(reflection)[:180])
                await self.engine.record_cognitive_feedback(
                    session_id=str(session_id),
                    source="memory_summary",
                    summary=" | ".join(part for part in feedback_parts if part.strip()),
                    guidance="Use this consolidated memory only when it is directly relevant; do not force older topics into the current reply.",
                    tags=valid_topics[:8],
                    importance=min(0.8, max(0.4, float(importance or 0.4))),
                )
            except Exception as exc:
                logger.warning(f"[SessionMemorySummarizer] cognitive feedback write degraded: {exc}")
                await self._observe(session_id, "cognitive_feedback_degraded", level="warning", reason="cognitive_feedback_degraded", summary=str(exc))
        return True

    @staticmethod
    def _default_subject_id(session_id: str) -> str:
        clean = str(session_id or "").strip()
        marker = ":FriendMessage:"
        if marker in clean:
            return clean.rsplit(marker, 1)[-1].strip()
        return ""

    @staticmethod
    def _history_digest(chat_history_text: str, messages: List[Dict] | None = None) -> str:
        parts: list[str] = []
        for item in messages or []:
            parts.append(
                "|".join(
                    (
                        str(item.get("message_id") or ""),
                        str(item.get("sender_id") or ""),
                        str(item.get("timestamp") or ""),
                        str(item.get("content") or "").strip(),
                    )
                )
            )
        payload = "\n".join(parts).strip() or "\n".join(
            line.strip() for line in str(chat_history_text or "").splitlines() if line.strip()
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _watermark_key(session_id: str) -> str:
        digest = hashlib.sha256(str(session_id or "").encode("utf-8")).hexdigest()[:24]
        return f"summary_watermark:{digest}"

    async def _get_summary_watermark(self, session_id: str) -> int:
        store = getattr(self.engine, "v2_store", None)
        getter = getattr(store, "get_meta", None)
        if not callable(getter):
            return 0
        try:
            return max(0, int(await getter(self._watermark_key(session_id), "0") or 0))
        except (TypeError, ValueError):
            return 0

    async def _set_summary_watermark(self, session_id: str, log_id: int) -> None:
        store = getattr(self.engine, "v2_store", None)
        setter = getattr(store, "set_meta", None)
        if callable(setter):
            await setter(self._watermark_key(session_id), str(max(0, int(log_id or 0))))

    @staticmethod
    def _build_topic_messages(chat_history_text: str) -> List[Dict]:
        messages = []
        for index, raw_line in enumerate(str(chat_history_text or "").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>[^:]+):\s*(?P<content>.*)$", line)
            if match:
                sender = match.group("sender").strip()
                content = match.group("content").strip()
            else:
                sender = "unknown"
                content = line
            if content:
                messages.append({"sender": sender, "content": content, "timestamp": float(index)})
        return messages

    async def _observe(
        self,
        chat_id: str,
        stage: str,
        *,
        level: str = "info",
        reason: str = "",
        summary: str = "",
        memory_id: str = "",
        payload: dict | None = None,
    ) -> None:
        observer = getattr(self.engine, "memory_observer", None)
        if observer is None or not hasattr(observer, "record"):
            return
        try:
            await observer.record(
                chat_id=str(chat_id or ""),
                component="session_summarizer",
                stage=stage,
                level=level,
                memory_id=memory_id,
                reason=reason,
                summary=summary,
                payload=payload or {},
            )
        except Exception:
            return


def uuid4_short() -> str:
    import uuid

    return uuid.uuid4().hex[:8]


__all__ = ["SessionMemorySummarizer"]
