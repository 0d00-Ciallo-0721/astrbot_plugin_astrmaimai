from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .topic_units import ColdSummaryStructure, TopicUnit


@dataclass(slots=True)
class DialogueSegment:
    timestamp: float
    event_id: str
    speaker_id: str
    speaker_name: str
    content: str
    role: str  # "user" | "assistant" | "interaction"
    message_kind: str  # "text" | "interaction" | "image" | "mixed"
    token_estimate: int
    is_bot: bool = False
    reply_target_sender_id: str = ""
    reply_target_sender_name: str = ""
    is_at_bot: bool = False
    is_reply_to_bot: bool = False
    has_direct_vision: bool = False
    is_image_only: bool = False
    # G3/ID-08: 被撤回的消息保留占位（speaker 与时序不变），内容替换为墓碑文案，
    # 避免 bot 在后续回复里原文复述用户已撤回的内容
    is_recalled: bool = False
    sequence: int = 0
    topic_epoch: int = 0
    causal_parent_event_id: str = ""
    provenance: str = "original"
    echo_of_event_id: str = ""
    outcome: str = ""
    source_event_ids: list[str] = field(default_factory=list)
    stance: str = ""
    social_event: str = ""


@dataclass(slots=True)
class DialogueThread:
    segments: list[DialogueSegment] = field(default_factory=list)
    cold_summary: str = ""
    cold_summary_structure: ColdSummaryStructure | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class WarmContextBundle:
    summary_text: str = ""
    quote_text: str = ""
    quote_event_ids: list[str] = field(default_factory=list)
    has_latest_assistant: bool = False
    """Whether the latest assistant message is present in the selected quotes.

    Recalculated by `_build_warm_quotes` after segment selection and
    deduplication — do not rely on stale values set before that point.
    """
    topic_preview: str = ""


@dataclass(slots=True)
class GroupSocialStateItem:
    state_id: str
    kind: str
    value: str
    owner_id: str
    owner_name: str
    created_at: float
    updated_at: float
    topic_epoch: int = 0
    status: str = "candidate"
    confidence: float = 0.5
    evidence_event_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PendingDirectItem:
    pending_id: str
    event_id: str
    speaker_id: str
    speaker_name: str
    content: str
    created_at: float
    updated_at: float
    topic_epoch: int = 0
    status: str = "pending"
    resolved_by_event_id: str = ""


@dataclass(slots=True)
class BotTurnRecord:
    turn_id: str
    timestamp: float
    target_sender_id: str
    target_sender_name: str
    source_event_ids: list[str]
    reply_text: str
    reply_hash: str
    stance: str = ""
    social_event: str = ""
    topic_epoch: int = 0


@dataclass(slots=True)
class GroupSocialIncident:
    incident_id: str
    kind: str
    actor_id: str
    actor_name: str
    target_id: str
    target_name: str
    created_at: float
    updated_at: float
    topic_epoch: int = 0
    status: str = "open"
    stance: str = ""
    evidence_event_ids: list[str] = field(default_factory=list)
    resolution_event_id: str = ""
    resolution_kind: str = ""


class GroupDialogueStore:
    # G4/PL-09: 快照 schema 版本——不兼容即整份弃用（不做半解析），避免旧结构
    # 恢复出畸形 segment 污染上下文
    SNAPSHOT_SCHEMA_VERSION = 2
    SNAPSHOT_FILENAME = "dialogue_store_state.json"
    SNAPSHOT_MAX_CHATS = 64
    SNAPSHOT_MAX_SEGMENTS_PER_CHAT = 40

    def __init__(
        self,
        *,
        hot_zone_ttl_seconds: float = 30.0,
        warm_zone_ttl_seconds: float = 300.0,
        warm_zone_max_tokens: int = 1200,
        snapshot_dir: Any = None,
    ):
        self.hot_zone_ttl_seconds = float(hot_zone_ttl_seconds or 30.0)
        self.warm_zone_ttl_seconds = float(warm_zone_ttl_seconds or 300.0)
        self.warm_zone_max_tokens = int(warm_zone_max_tokens or 1200)
        self.snapshot_dir = snapshot_dir
        self._threads: dict[str, DialogueThread] = {}
        self._social_states: dict[str, list[GroupSocialStateItem]] = {}
        self._pending_direct: dict[str, list[PendingDirectItem]] = {}
        self._bot_turns: dict[str, list[BotTurnRecord]] = {}
        self._social_incidents: dict[str, list[GroupSocialIncident]] = {}
        self._sequence_by_chat: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _get_thread(self, chat_id: str) -> DialogueThread:
        thread = self._threads.get(chat_id)
        if thread is None:
            thread = DialogueThread()
            self._threads[chat_id] = thread
        return thread

    async def clear_chat(self, chat_id: str) -> bool:
        async with self._lock:
            key = self._resolve_chat_key(chat_id)
            thread_removed = self._threads.pop(key, None) is not None
            social_removed = self._social_states.pop(key, None) is not None
            pending_removed = self._pending_direct.pop(key, None) is not None
            bot_turns_removed = self._bot_turns.pop(key, None) is not None
            incidents_removed = self._social_incidents.pop(key, None) is not None
            self._sequence_by_chat.pop(key, None)
            return (
                thread_removed
                or social_removed
                or pending_removed
                or bot_turns_removed
                or incidents_removed
            )

    @staticmethod
    def _resolve_chat_key(chat_id) -> str:
        """Resolve chat_id to a dict key, rejecting None/empty values.
        
        ponytail: prevents legacy empty-string fallback coercion
        when chat_id is None, which would cause all None-chat_id calls to share
        the same DialogueThread.
        """
        key = (str(chat_id) if chat_id is not None else "").strip()
        if not key:
            raise ValueError("chat_id must be a non-empty string, got %r" % chat_id)
        return key

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        text = str(content or "").strip()
        if not text:
            return 0
        return max(1, len(text) // 2)

    async def append_segment(
        self,
        chat_id: str,
        *,
        event_id: str = "",
        speaker_id: str = "",
        speaker_name: str = "",
        content: str = "",
        role: str = "user",
        message_kind: str = "text",
        is_bot: bool = False,
        reply_target_sender_id: str = "",
        reply_target_sender_name: str = "",
        is_at_bot: bool = False,
        is_reply_to_bot: bool = False,
        has_direct_vision: bool = False,
        is_image_only: bool = False,
        timestamp: float | None = None,
        topic_epoch: int = 0,
        causal_parent_event_id: str = "",
        provenance: str = "original",
        echo_of_event_id: str = "",
        outcome: str = "",
        source_event_ids: list[str] | None = None,
        stance: str = "",
        social_event: str = "",
        create_pending_direct: bool = False,
    ) -> DialogueSegment:
        chat_key = self._resolve_chat_key(chat_id)
        created_at = float(timestamp or time.time())
        segment = DialogueSegment(
            timestamp=created_at,
            event_id=str(event_id or ""),
            speaker_id=str(speaker_id or ""),
            speaker_name=str(speaker_name or ""),
            content=str(content or "").strip(),
            role=str(role or "user"),
            message_kind=str(message_kind or "text"),
            token_estimate=self._estimate_tokens(content),
            is_bot=bool(is_bot),
            reply_target_sender_id=str(reply_target_sender_id or ""),
            reply_target_sender_name=str(reply_target_sender_name or ""),
            is_at_bot=bool(is_at_bot),
            is_reply_to_bot=bool(is_reply_to_bot),
            has_direct_vision=bool(has_direct_vision),
            is_image_only=bool(is_image_only),
            topic_epoch=max(0, int(topic_epoch or 0)),
            causal_parent_event_id=str(causal_parent_event_id or ""),
            provenance=str(provenance or "original"),
            echo_of_event_id=str(echo_of_event_id or ""),
            outcome=str(outcome or ""),
            source_event_ids=[
                str(item).strip()
                for item in list(source_event_ids or [])
                if str(item).strip()
            ][-8:],
            stance=str(stance or ""),
            social_event=str(social_event or ""),
        )
        async with self._lock:
            thread = self._get_thread(chat_key)
            async with thread.lock:
                next_sequence = int(self._sequence_by_chat.get(chat_key, 0) or 0) + 1
                self._sequence_by_chat[chat_key] = next_sequence
                segment.sequence = next_sequence
                if not segment.is_bot and segment.provenance == "original":
                    echo = self._find_recent_bot_echo(thread.segments, segment.content, created_at)
                    if echo is not None:
                        segment.provenance = "bot_echo"
                        segment.echo_of_event_id = echo.event_id
                thread.segments.append(segment)
            if create_pending_direct and not segment.is_bot and segment.provenance != "bot_echo":
                pending_id = self._stable_id(
                    "pending",
                    chat_key,
                    segment.event_id or str(segment.sequence),
                )
                items = self._pending_direct.setdefault(chat_key, [])
                items.append(
                    PendingDirectItem(
                        pending_id=pending_id,
                        event_id=segment.event_id,
                        speaker_id=segment.speaker_id,
                        speaker_name=segment.speaker_name,
                        content=segment.content,
                        created_at=created_at,
                        updated_at=created_at,
                        topic_epoch=segment.topic_epoch,
                    )
                )
                self._pending_direct[chat_key] = items[-80:]
            if segment.is_bot:
                self._record_bot_turn_locked(chat_key, segment)
        return segment

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
        return f"{prefix}_" + hashlib.sha256(payload).hexdigest()[:20]

    @classmethod
    def _normalize_echo_text(cls, text: str) -> str:
        normalized = cls._normalize_message_text(text).lower()
        return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)

    @classmethod
    def _find_recent_bot_echo(
        cls,
        segments: list[DialogueSegment],
        content: str,
        timestamp: float,
    ) -> DialogueSegment | None:
        normalized = cls._normalize_echo_text(content)
        if len(normalized) < 8:
            return None
        for candidate in reversed(segments[-24:]):
            if not candidate.is_bot and candidate.role != "assistant":
                continue
            if timestamp - float(candidate.timestamp or 0.0) > 900.0:
                break
            if cls._normalize_echo_text(candidate.content) == normalized:
                return candidate
        return None

    def _record_bot_turn_locked(self, chat_key: str, segment: DialogueSegment) -> None:
        source_ids = list(segment.source_event_ids or [])
        if not source_ids and segment.causal_parent_event_id:
            source_ids = [segment.causal_parent_event_id]
        reply_hash = hashlib.sha256(segment.content.encode("utf-8")).hexdigest()[:20]
        turn = BotTurnRecord(
            turn_id=segment.event_id or self._stable_id(
                "bot_turn",
                chat_key,
                str(segment.sequence),
            ),
            timestamp=segment.timestamp,
            target_sender_id=segment.reply_target_sender_id,
            target_sender_name=segment.reply_target_sender_name,
            source_event_ids=source_ids[-8:],
            reply_text=segment.content,
            reply_hash=reply_hash,
            stance=segment.stance,
            social_event=segment.social_event,
            topic_epoch=segment.topic_epoch,
        )
        turns = self._bot_turns.setdefault(chat_key, [])
        turns.append(turn)
        self._bot_turns[chat_key] = turns[-80:]
        for pending in self._pending_direct.get(chat_key, []):
            source_match = bool(pending.event_id and pending.event_id in source_ids)
            target_match = bool(
                turn.target_sender_id
                and pending.speaker_id == turn.target_sender_id
                and pending.status == "pending"
            )
            if pending.status == "pending" and (source_match or target_match):
                pending.status = "answered"
                pending.resolved_by_event_id = turn.turn_id
                pending.updated_at = turn.timestamp

    async def get_actor_tail(
        self,
        chat_id: str,
        *,
        current_sender_id: str,
        ttl_seconds: float = 1200.0,
        max_items: int = 8,
        now: float | None = None,
    ) -> list[DialogueSegment]:
        key = self._resolve_chat_key(chat_id)
        sender_id = str(current_sender_id or "").strip()
        if not sender_id:
            return []
        timestamp = time.time() if now is None else float(now)
        async with self._lock:
            thread = self._threads.get(key)
            if thread is None:
                return []
            async with thread.lock:
                selected = [
                    segment
                    for segment in thread.segments
                    if segment.speaker_id == sender_id
                    and not segment.is_bot
                    and timestamp - float(segment.timestamp or 0.0) <= max(60.0, ttl_seconds)
                ]
        return selected[-max(1, int(max_items or 1)) :]

    async def get_pending_direct_items(
        self,
        chat_id: str,
        *,
        current_sender_id: str,
        ttl_seconds: float = 1200.0,
        include_answered: bool = False,
        now: float | None = None,
    ) -> list[PendingDirectItem]:
        key = self._resolve_chat_key(chat_id)
        sender_id = str(current_sender_id or "").strip()
        timestamp = time.time() if now is None else float(now)
        async with self._lock:
            items = self._pending_direct.get(key, [])
            ttl = max(60.0, ttl_seconds)
            for item in items:
                if (
                    item.status == "pending"
                    and timestamp - float(item.updated_at or 0.0) > ttl
                ):
                    item.status = "expired"
                    item.updated_at = timestamp
            items = list(items)
        return [
            item
            for item in items
            if item.speaker_id == sender_id
            and (
                item.status == "expired"
                or timestamp - float(item.updated_at or 0.0) <= max(60.0, ttl_seconds)
            )
            and (include_answered or item.status == "pending")
        ][-8:]

    async def set_pending_direct_status(
        self,
        chat_id: str,
        *,
        event_id: str,
        status: str,
        resolved_by_event_id: str = "",
        now: float | None = None,
    ) -> bool:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {
            "pending",
            "answered",
            "superseded",
            "withdrawn",
            "expired",
        }:
            raise ValueError(f"unsupported pending direct status: {status}")
        target_event_id = str(event_id or "").strip()
        if not target_event_id:
            return False
        key = self._resolve_chat_key(chat_id)
        timestamp = time.time() if now is None else float(now)
        async with self._lock:
            for item in self._pending_direct.get(key, []):
                if item.event_id != target_event_id:
                    continue
                item.status = normalized_status
                item.updated_at = timestamp
                item.resolved_by_event_id = str(resolved_by_event_id or "")
                return True
        return False

    async def supersede_pending_direct_for_actor(
        self,
        chat_id: str,
        *,
        actor_id: str,
        superseded_by_event_id: str,
        now: float | None = None,
    ) -> int:
        key = self._resolve_chat_key(chat_id)
        normalized_actor = str(actor_id or "").strip()
        replacement_event_id = str(superseded_by_event_id or "").strip()
        if not normalized_actor or not replacement_event_id:
            return 0
        timestamp = time.time() if now is None else float(now)
        updated = 0
        async with self._lock:
            for item in self._pending_direct.get(key, []):
                if item.speaker_id != normalized_actor or item.status != "pending":
                    continue
                if item.event_id == replacement_event_id:
                    continue
                item.status = "superseded"
                item.updated_at = timestamp
                item.resolved_by_event_id = replacement_event_id
                updated += 1
        return updated

    async def get_recent_bot_turns(
        self,
        chat_id: str,
        *,
        target_sender_id: str,
        ttl_seconds: float = 1200.0,
        max_items: int = 4,
        now: float | None = None,
    ) -> list[BotTurnRecord]:
        key = self._resolve_chat_key(chat_id)
        sender_id = str(target_sender_id or "").strip()
        timestamp = time.time() if now is None else float(now)
        async with self._lock:
            turns = list(self._bot_turns.get(key, []))
        selected = [
            turn
            for turn in turns
            if turn.target_sender_id == sender_id
            and timestamp - float(turn.timestamp or 0.0) <= max(60.0, ttl_seconds)
        ]
        return selected[-max(1, int(max_items or 1)) :]

    async def observe_social_incident(
        self,
        chat_id: str,
        *,
        kind: str,
        actor_id: str,
        actor_name: str = "",
        target_id: str = "",
        target_name: str = "",
        evidence_event_id: str = "",
        topic_epoch: int = 0,
        stance: str = "",
        now: float | None = None,
    ) -> GroupSocialIncident | None:
        key = self._resolve_chat_key(chat_id)
        normalized_kind = str(kind or "").strip().lower()
        normalized_actor = str(actor_id or "").strip()
        if not normalized_kind or not normalized_actor:
            return None
        timestamp = time.time() if now is None else float(now)
        evidence_id = str(evidence_event_id or "").strip()
        async with self._lock:
            incidents = self._social_incidents.setdefault(key, [])
            existing = next(
                (
                    item
                    for item in reversed(incidents)
                    if item.status == "open"
                    and item.kind == normalized_kind
                    and item.actor_id == normalized_actor
                    and item.target_id == str(target_id or "").strip()
                ),
                None,
            )
            if existing is not None:
                existing.updated_at = timestamp
                existing.actor_name = str(actor_name or existing.actor_name or "")
                existing.stance = str(stance or existing.stance or "")
                if evidence_id and evidence_id not in existing.evidence_event_ids:
                    existing.evidence_event_ids.append(evidence_id)
                    existing.evidence_event_ids = existing.evidence_event_ids[-8:]
                return existing
            incident = GroupSocialIncident(
                incident_id=self._stable_id(
                    "incident",
                    key,
                    normalized_kind,
                    normalized_actor,
                    str(target_id or ""),
                    evidence_id or str(timestamp),
                ),
                kind=normalized_kind,
                actor_id=normalized_actor,
                actor_name=str(actor_name or ""),
                target_id=str(target_id or ""),
                target_name=str(target_name or ""),
                created_at=timestamp,
                updated_at=timestamp,
                topic_epoch=max(0, int(topic_epoch or 0)),
                stance=str(stance or ""),
                evidence_event_ids=[evidence_id] if evidence_id else [],
            )
            incidents.append(incident)
            self._social_incidents[key] = incidents[-80:]
            return incident

    async def resolve_social_incidents(
        self,
        chat_id: str,
        *,
        actor_id: str,
        resolution_event_id: str,
        resolution_kind: str,
        now: float | None = None,
    ) -> int:
        key = self._resolve_chat_key(chat_id)
        normalized_actor = str(actor_id or "").strip()
        if not normalized_actor:
            return 0
        timestamp = time.time() if now is None else float(now)
        resolved = 0
        async with self._lock:
            for incident in self._social_incidents.get(key, []):
                if incident.status != "open" or incident.actor_id != normalized_actor:
                    continue
                incident.status = "resolved"
                incident.updated_at = timestamp
                incident.resolution_event_id = str(resolution_event_id or "")
                incident.resolution_kind = str(resolution_kind or "")
                resolved += 1
        return resolved

    async def get_social_incidents(
        self,
        chat_id: str,
        *,
        current_sender_id: str,
        ttl_seconds: float = 1800.0,
        include_resolved: bool = False,
        now: float | None = None,
    ) -> list[GroupSocialIncident]:
        key = self._resolve_chat_key(chat_id)
        sender_id = str(current_sender_id or "").strip()
        timestamp = time.time() if now is None else float(now)
        async with self._lock:
            incidents = list(self._social_incidents.get(key, []))
        return [
            item
            for item in incidents
            if item.actor_id == sender_id
            and timestamp - float(item.updated_at or 0.0) <= max(60.0, ttl_seconds)
            and (include_resolved or item.status == "open")
        ][-8:]

    async def count_recent_bot_echoes(
        self,
        chat_id: str,
        *,
        ttl_seconds: float = 1200.0,
        now: float | None = None,
    ) -> int:
        key = self._resolve_chat_key(chat_id)
        timestamp = time.time() if now is None else float(now)
        async with self._lock:
            thread = self._threads.get(key)
            if thread is None:
                return 0
            async with thread.lock:
                return sum(
                    1
                    for segment in thread.segments
                    if segment.provenance == "bot_echo"
                    and timestamp - float(segment.timestamp or 0.0) <= max(60.0, ttl_seconds)
                )

    RECALLED_PLACEHOLDER = "[已撤回]"

    async def mark_recalled(self, chat_id: str, event_id: str) -> bool:
        """G3/ID-08: 把已撤回消息的内容换成墓碑，保留 speaker 与时序。

        只改展示层内容——原始事件存储不动。返回是否命中（未命中说明该消息
        不在热区，属正常情况：可能早已被压缩进冷区或从未进入本 store）。
        """
        target_id = str(event_id or "").strip()
        if not target_id:
            return False
        async with self._lock:
            thread = self._get_thread(self._resolve_chat_key(chat_id))
            async with thread.lock:
                hit = False
                for segment in thread.segments:
                    if str(segment.event_id or "") != target_id or segment.is_recalled:
                        continue
                    segment.content = self.RECALLED_PLACEHOLDER
                    segment.is_recalled = True
                    segment.message_kind = "text"
                    segment.has_direct_vision = False
                    segment.is_image_only = False
                    segment.token_estimate = self._estimate_tokens(self.RECALLED_PLACEHOLDER)
                    hit = True
                if hit:
                    for pending in self._pending_direct.get(
                        self._resolve_chat_key(chat_id),
                        [],
                    ):
                        if pending.event_id == target_id and pending.status == "pending":
                            pending.status = "withdrawn"
                            pending.updated_at = time.time()
                return hit

    # ---- G4/PL-09 快照持久化 ---------------------------------------------
    # 背景：本 store 纯内存，AstrBot 面板改配置触发插件重载即丢全部群热/温区，
    # 表现为 bot "突然接不上话"。策略三要素：
    #   TTL      —— 只恢复 warm_zone_ttl_seconds 内的 segment（陈旧上下文宁可不要）
    #   版本门槛 —— schema 不匹配整份弃用并 WARN，不做半解析
    #   写入时机 —— terminate 钩子（对齐 dream_scheduler_state.json 先例）

    def snapshot_path(self) -> Path | None:
        if not self.snapshot_dir:
            return None
        try:
            return Path(self.snapshot_dir) / self.SNAPSHOT_FILENAME
        except Exception:
            return None

    def _serialize_segment(self, segment: DialogueSegment) -> dict:
        return {field.name: getattr(segment, field.name) for field in dataclass_fields(DialogueSegment)}

    def _deserialize_segment(self, payload: dict) -> DialogueSegment | None:
        valid_names = {field.name for field in dataclass_fields(DialogueSegment)}
        data = {key: value for key, value in dict(payload or {}).items() if key in valid_names}
        try:
            return DialogueSegment(**data)
        except Exception:
            return None

    @staticmethod
    def _serialize_social_state(item: GroupSocialStateItem) -> dict:
        return {field.name: getattr(item, field.name) for field in dataclass_fields(GroupSocialStateItem)}

    @staticmethod
    def _deserialize_social_state(payload: dict) -> GroupSocialStateItem | None:
        valid_names = {field.name for field in dataclass_fields(GroupSocialStateItem)}
        data = {key: value for key, value in dict(payload or {}).items() if key in valid_names}
        try:
            data["evidence_event_ids"] = [
                str(item).strip()
                for item in list(data.get("evidence_event_ids", []) or [])
                if str(item).strip()
            ][-8:]
            item = GroupSocialStateItem(**data)
            if not str(item.owner_id or "").strip() or not str(item.value or "").strip():
                return None
            if item.status not in {"candidate", "confirmed", "rejected"}:
                return None
            return item
        except Exception:
            return None

    @staticmethod
    def _serialize_dataclass(item: Any) -> dict:
        return {field.name: getattr(item, field.name) for field in dataclass_fields(type(item))}

    @staticmethod
    def _deserialize_pending_direct(payload: dict) -> PendingDirectItem | None:
        try:
            valid_names = {field.name for field in dataclass_fields(PendingDirectItem)}
            return PendingDirectItem(
                **{key: value for key, value in dict(payload or {}).items() if key in valid_names}
            )
        except Exception:
            return None

    @staticmethod
    def _deserialize_bot_turn(payload: dict) -> BotTurnRecord | None:
        try:
            valid_names = {field.name for field in dataclass_fields(BotTurnRecord)}
            data = {key: value for key, value in dict(payload or {}).items() if key in valid_names}
            data["source_event_ids"] = [
                str(item).strip()
                for item in list(data.get("source_event_ids", []) or [])
                if str(item).strip()
            ][-8:]
            return BotTurnRecord(**data)
        except Exception:
            return None

    @staticmethod
    def _deserialize_social_incident(payload: dict) -> GroupSocialIncident | None:
        try:
            valid_names = {field.name for field in dataclass_fields(GroupSocialIncident)}
            data = {key: value for key, value in dict(payload or {}).items() if key in valid_names}
            data["evidence_event_ids"] = [
                str(item).strip()
                for item in list(data.get("evidence_event_ids", []) or [])
                if str(item).strip()
            ][-8:]
            incident = GroupSocialIncident(**data)
            if incident.status not in {"open", "resolved", "expired"}:
                return None
            return incident
        except Exception:
            return None

    @staticmethod
    def _social_state_id(kind: str, value: str, owner_id: str) -> str:
        payload = "\x1f".join((str(kind), str(owner_id), str(value))).encode("utf-8")
        return "social_" + hashlib.sha256(payload).hexdigest()[:20]

    async def upsert_social_candidate(
        self,
        chat_id: str,
        *,
        kind: str,
        value: str,
        owner_id: str,
        owner_name: str = "",
        topic_epoch: int = 0,
        source_event_id: str = "",
        confidence: float = 0.5,
        now: float | None = None,
    ) -> GroupSocialStateItem | None:
        key = self._resolve_chat_key(chat_id)
        normalized_kind = str(kind or "").strip().lower()
        normalized_value = self._normalize_message_text(value)
        normalized_owner_id = str(owner_id or "").strip()
        if not normalized_kind or not normalized_value or not normalized_owner_id:
            return None
        timestamp = time.time() if now is None else float(now)
        state_id = self._social_state_id(normalized_kind, normalized_value, normalized_owner_id)
        evidence_id = str(source_event_id or "").strip()
        async with self._lock:
            items = self._social_states.setdefault(key, [])
            existing = next((item for item in items if item.state_id == state_id), None)
            if existing is not None:
                existing.updated_at = timestamp
                existing.owner_name = str(owner_name or existing.owner_name or "").strip()
                existing.topic_epoch = max(0, int(topic_epoch or existing.topic_epoch or 0))
                existing.confidence = max(
                    0.0,
                    min(1.0, max(float(existing.confidence or 0.0), float(confidence or 0.0))),
                )
                if evidence_id and evidence_id not in existing.evidence_event_ids:
                    existing.evidence_event_ids.append(evidence_id)
                    existing.evidence_event_ids = existing.evidence_event_ids[-8:]
                return existing
            item = GroupSocialStateItem(
                state_id=state_id,
                kind=normalized_kind,
                value=normalized_value,
                owner_id=normalized_owner_id,
                owner_name=str(owner_name or "").strip(),
                created_at=timestamp,
                updated_at=timestamp,
                topic_epoch=max(0, int(topic_epoch or 0)),
                confidence=max(0.0, min(1.0, float(confidence or 0.0))),
                evidence_event_ids=[evidence_id] if evidence_id else [],
            )
            items.append(item)
            self._social_states[key] = items[-80:]
            return item

    async def set_social_state_status(
        self,
        chat_id: str,
        state_id: str,
        status: str,
        *,
        now: float | None = None,
    ) -> bool:
        key = self._resolve_chat_key(chat_id)
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"candidate", "confirmed", "rejected"}:
            return False
        async with self._lock:
            for item in self._social_states.get(key, []):
                if item.state_id != str(state_id or "").strip():
                    continue
                item.status = normalized_status
                item.updated_at = time.time() if now is None else float(now)
                return True
        return False

    async def get_social_context_items(
        self,
        chat_id: str,
        *,
        current_sender_id: str,
        topic_epoch: int = 0,
        ttl_seconds: float = 86400.0,
        ownership_check_enabled: bool = True,
        now: float | None = None,
    ) -> list[GroupSocialStateItem]:
        key = self._resolve_chat_key(chat_id)
        timestamp = time.time() if now is None else float(now)
        sender_id = str(current_sender_id or "").strip()
        ttl = max(60.0, float(ttl_seconds or 86400.0))
        async with self._lock:
            items = list(self._social_states.get(key, []))
        selected: list[GroupSocialStateItem] = []
        for item in items:
            if item.status == "rejected" or timestamp - float(item.updated_at or 0.0) > ttl:
                continue
            if ownership_check_enabled:
                if not sender_id or item.owner_id != sender_id:
                    continue
            elif item.owner_id and sender_id and item.owner_id != sender_id:
                continue
            selected.append(item)
        selected.sort(
            key=lambda item: (
                item.status == "confirmed",
                item.topic_epoch == max(0, int(topic_epoch or 0)),
                item.updated_at,
            ),
            reverse=True,
        )
        return selected[:8]

    async def render_social_context(
        self,
        chat_id: str,
        *,
        current_sender_id: str,
        topic_epoch: int = 0,
        ttl_seconds: float = 86400.0,
        ownership_check_enabled: bool = True,
        now: float | None = None,
    ) -> str:
        items = await self.get_social_context_items(
            chat_id,
            current_sender_id=current_sender_id,
            topic_epoch=topic_epoch,
            ttl_seconds=ttl_seconds,
            ownership_check_enabled=ownership_check_enabled,
            now=now,
        )
        if not items:
            return ""
        lines = ["当前发言人的群内互动状态（已校验归属）："]
        for item in items:
            status = "已确认" if item.status == "confirmed" else "待确认"
            owner = item.owner_name or item.owner_id
            lines.append(f"- {item.kind}：{item.value}（归属：{owner}，{status}）")
        lines.append("这些状态只属于当前发言人；待确认内容只能作为互动线索，不能冒充已证实事实。")
        return "\n".join(lines)

    async def export_snapshot(self) -> dict:
        now = time.time()
        chats: dict[str, dict] = {}
        async with self._lock:
            threads = list(self._threads.items())
            social_states = {
                chat_id: [self._serialize_social_state(item) for item in items]
                for chat_id, items in self._social_states.items()
                if items
            }
            pending_direct = {
                chat_id: [self._serialize_dataclass(item) for item in items[-80:]]
                for chat_id, items in self._pending_direct.items()
                if items
            }
            bot_turns = {
                chat_id: [self._serialize_dataclass(item) for item in items[-80:]]
                for chat_id, items in self._bot_turns.items()
                if items
            }
            social_incidents = {
                chat_id: [self._serialize_dataclass(item) for item in items[-80:]]
                for chat_id, items in self._social_incidents.items()
                if items
            }
            sequence_by_chat = dict(self._sequence_by_chat)
        for chat_id, thread in threads[: self.SNAPSHOT_MAX_CHATS]:
            async with thread.lock:
                fresh = [
                    segment
                    for segment in thread.segments
                    if now - float(segment.timestamp or 0.0) <= self.warm_zone_ttl_seconds
                ][-self.SNAPSHOT_MAX_SEGMENTS_PER_CHAT :]
                if not fresh and not thread.cold_summary:
                    continue
                chats[chat_id] = {
                    "segments": [self._serialize_segment(segment) for segment in fresh],
                    "cold_summary": thread.cold_summary,
                }
        return {
            "schema_version": self.SNAPSHOT_SCHEMA_VERSION,
            "saved_at": now,
            "chats": chats,
            "social_states": social_states,
            "pending_direct": pending_direct,
            "bot_turns": bot_turns,
            "social_incidents": social_incidents,
            "sequence_by_chat": sequence_by_chat,
        }

    async def persist_snapshot(self) -> bool:
        path = self.snapshot_path()
        if path is None:
            return False
        payload = await self.export_snapshot()
        if not any(
            payload.get(key)
            for key in (
                "chats",
                "social_states",
                "pending_direct",
                "bot_turns",
                "social_incidents",
            )
        ):
            return False

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(path)

        try:
            await asyncio.to_thread(_write)
            logger.info(
                "[DialogueStore] persisted context snapshot for "
                f"{len(payload['chats'])} chats and {len(payload.get('social_states', {}))} social scopes"
            )
            return True
        except Exception as exc:
            logger.warning(f"[DialogueStore] failed to persist context snapshot: {exc}")
            return False

    async def restore_snapshot(self) -> int:
        path = self.snapshot_path()
        if path is None or not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[DialogueStore] context snapshot unreadable; ignored: {exc}")
            return 0
        version = int(payload.get("schema_version", 0) or 0)
        if version not in {1, self.SNAPSHOT_SCHEMA_VERSION}:
            logger.warning(
                f"[DialogueStore] context snapshot schema {version} is unsupported; "
                f"current={self.SNAPSHOT_SCHEMA_VERSION}; discarded"
            )
            return 0
        now = time.time()
        restored = 0
        restored_chat_ids: set[str] = set()
        for chat_id, chat_payload in dict(payload.get("chats", {}) or {}).items():
            # 用 _resolve_chat_key 统一口径（R11 禁止 str(x or "") 式空串强制）
            try:
                key = self._resolve_chat_key(chat_id)
            except ValueError:
                continue
            segments: list[DialogueSegment] = []
            for item in list((chat_payload or {}).get("segments", []) or []):
                segment = self._deserialize_segment(item)
                if segment is None:
                    continue
                # TTL 二次校验：快照落盘到恢复之间可能已经过期
                if now - float(segment.timestamp or 0.0) > self.warm_zone_ttl_seconds:
                    continue
                segments.append(segment)
            cold_summary = str((chat_payload or {}).get("cold_summary", "") or "")
            if not segments and not cold_summary:
                continue
            async with self._lock:
                thread = self._get_thread(key)
            async with thread.lock:
                thread.segments = segments + thread.segments
                if cold_summary and not thread.cold_summary:
                    thread.cold_summary = cold_summary
            restored += 1
            restored_chat_ids.add(key)
        restored_social = 0
        for chat_id, items_payload in dict(payload.get("social_states", {}) or {}).items():
            try:
                key = self._resolve_chat_key(chat_id)
            except ValueError:
                continue
            items: list[GroupSocialStateItem] = []
            for item_payload in list(items_payload or []):
                item = self._deserialize_social_state(item_payload)
                if item is None or item.status == "rejected":
                    continue
                items.append(item)
            if not items:
                continue
            async with self._lock:
                existing = {item.state_id: item for item in self._social_states.get(key, [])}
                for item in items:
                    existing.setdefault(item.state_id, item)
                self._social_states[key] = list(existing.values())[-80:]
            restored_social += 1
            restored_chat_ids.add(key)
        restored_causal = 0
        causal_specs = (
            ("pending_direct", self._pending_direct, self._deserialize_pending_direct),
            ("bot_turns", self._bot_turns, self._deserialize_bot_turn),
            ("social_incidents", self._social_incidents, self._deserialize_social_incident),
        )
        for payload_key, target, deserializer in causal_specs:
            for chat_id, items_payload in dict(payload.get(payload_key, {}) or {}).items():
                try:
                    key = self._resolve_chat_key(chat_id)
                except ValueError:
                    continue
                items = [
                    item
                    for item in (
                        deserializer(item_payload)
                        for item_payload in list(items_payload or [])
                    )
                    if item is not None
                ][-80:]
                if not items:
                    continue
                async with self._lock:
                    existing = {
                        getattr(item, "pending_id", "")
                        or getattr(item, "turn_id", "")
                        or getattr(item, "incident_id", ""): item
                        for item in target.get(key, [])
                    }
                    for item in items:
                        item_id = (
                            getattr(item, "pending_id", "")
                            or getattr(item, "turn_id", "")
                            or getattr(item, "incident_id", "")
                        )
                        existing.setdefault(item_id, item)
                    target[key] = list(existing.values())[-80:]
                restored_causal += 1
                restored_chat_ids.add(key)
        async with self._lock:
            for chat_id, sequence in dict(payload.get("sequence_by_chat", {}) or {}).items():
                try:
                    key = self._resolve_chat_key(chat_id)
                    self._sequence_by_chat[key] = max(
                        int(self._sequence_by_chat.get(key, 0) or 0),
                        int(sequence or 0),
                    )
                except (TypeError, ValueError):
                    continue
        if restored:
            logger.info(f"[DialogueStore] restored context snapshot for {restored} chats")
        if restored_social:
            logger.info(f"[DialogueStore] restored social state for {restored_social} chats")
        if restored_causal:
            logger.info(f"[DialogueStore] restored causal state for {restored_causal} scopes")
        return len(restored_chat_ids)

    async def set_cold_summary(self, chat_id: str, summary: str) -> None:
        async with self._lock:
            thread = self._get_thread(self._resolve_chat_key(chat_id))
            async with thread.lock:
                thread.cold_summary = str(summary or "").strip()
                thread.cold_summary_structure = None

    async def get_cold_summary(self, chat_id: str) -> str:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return ""
            async with thread.lock:
                return thread.cold_summary

    async def set_cold_summary_structure(self, chat_id: str, structure: ColdSummaryStructure | None) -> None:
        async with self._lock:
            thread = self._get_thread(self._resolve_chat_key(chat_id))
            async with thread.lock:
                thread.cold_summary_structure = structure

    async def get_cold_summary_structure(self, chat_id: str) -> ColdSummaryStructure | None:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return None
            async with thread.lock:
                return thread.cold_summary_structure

    async def drain_old_segments(self, chat_id: str, *, keep_recent_segments: int = 12) -> list[DialogueSegment]:
        drained = await self.peek_old_segments(chat_id, keep_recent_segments=keep_recent_segments)
        if not drained:
            return []
        await self.commit_drain_old_segments(chat_id, keep_recent_segments=keep_recent_segments)
        return drained

    async def peek_old_segments(self, chat_id: str, *, keep_recent_segments: int = 12) -> list[DialogueSegment]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return []
            async with thread.lock:
                if keep_recent_segments <= 0:
                    return list(thread.segments)
                if len(thread.segments) <= keep_recent_segments:
                    return []
                return list(thread.segments[:-keep_recent_segments])

    async def commit_drain_old_segments(self, chat_id: str, *, keep_recent_segments: int = 12) -> list[DialogueSegment]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return []
            async with thread.lock:
                if keep_recent_segments <= 0:
                    drained = list(thread.segments)
                    thread.segments.clear()
                    return drained
                if len(thread.segments) <= keep_recent_segments:
                    return []
                drained = thread.segments[:-keep_recent_segments]
                thread.segments = thread.segments[-keep_recent_segments:]
                return list(drained)

    def _format_segment_line(self, segment: DialogueSegment, *, include_identity: bool = False) -> str:
        speaker = segment.speaker_name or segment.speaker_id or ("Bot" if segment.is_bot else "User")
        if include_identity and segment.speaker_id and not segment.is_bot:
            speaker = f"{speaker}（QQ: {segment.speaker_id}）"
        prefix_bits: list[str] = []
        if segment.is_reply_to_bot:
            target = segment.reply_target_sender_name or segment.reply_target_sender_id or "Bot"
            prefix_bits.append(f"回复 {target}")
        elif segment.reply_target_sender_name or segment.reply_target_sender_id:
            target = segment.reply_target_sender_name or segment.reply_target_sender_id
            prefix_bits.append(f"回复 {target}")
        if segment.is_at_bot:
            prefix_bits.append("@我")
        if segment.message_kind == "image" or segment.is_image_only or segment.has_direct_vision:
            prefix_bits.append("图片")
        if segment.message_kind == "mixed":
            prefix_bits.append("图文")
        prefix = f"({'，'.join(prefix_bits)}) " if prefix_bits else ""
        return f"{speaker} {prefix}: {segment.content}".replace("  ", " ").strip().replace(" :", ":")

    @staticmethod
    def _normalize_message_text(text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        return re.sub(r"\s+", " ", cleaned)

    @staticmethod
    def _preview_text(text: str, limit: int = 36) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)] + "..."

    @classmethod
    def _standardized_length(cls, text: str) -> int:
        return len(cls._normalize_message_text(text))

    @classmethod
    def _message_load(cls, text: str) -> float:
        length = cls._standardized_length(text)
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

    @classmethod
    def _segment_load(cls, segment: DialogueSegment) -> float:
        return cls._message_load(str(segment.content or ""))

    @classmethod
    def _segment_relevance_score(cls, segment: DialogueSegment) -> tuple[float, float]:
        score = 0.0
        if segment.is_at_bot:
            score += 6.0
        if segment.is_reply_to_bot:
            score += 5.0
        elif segment.reply_target_sender_id or segment.reply_target_sender_name:
            score += 3.0
        if segment.role == "assistant" or segment.is_bot:
            score += 3.5
        if segment.has_direct_vision or segment.is_image_only:
            score += 2.0
        if segment.message_kind == "mixed":
            score += 1.0
        return score, segment.timestamp

    @classmethod
    def _is_question_like(cls, text: str) -> bool:
        cleaned = cls._normalize_message_text(text)
        if not cleaned:
            return False
        lowered = cleaned.lower()
        return (
            "?" in cleaned
            or "？" in cleaned
            or any(
                token in lowered
                for token in ("why", "how", "what", "where", "when", "吗", "么", "为什么", "怎么", "咋", "是否", "要不要", "能不能")
            )
        )

    @classmethod
    def _is_decision_like(cls, text: str) -> bool:
        cleaned = cls._normalize_message_text(text)
        if not cleaned or cls._is_question_like(cleaned):
            return False
        lowered = cleaned.lower()
        return any(
            token in lowered
            for token in ("就这样", "那就", "可以", "行", "确定", "决定", "按这个", "收到", "ok", "deal", "没问题")
        )

    @classmethod
    def _detect_relation_state(cls, segments: list[DialogueSegment]) -> str:
        if any(segment.is_at_bot or segment.is_reply_to_bot for segment in segments):
            return "互动主线仍然直接连着我，大家在顺着同一个点继续接。"
        if any(segment.reply_target_sender_id or segment.reply_target_sender_name for segment in segments):
            return "最近几句明显沿着同一条回复链往下延伸。"
        return ""

    @classmethod
    def _detect_emotion_state(cls, segments: list[DialogueSegment]) -> str:
        combined = " ".join(cls._normalize_message_text(segment.content) for segment in segments if segment.content)
        lowered = combined.lower()
        if any(token in lowered for token in ("哈哈", "hh", "233", "笑死", "lol")):
            return "语气整体偏轻松，大家更像是在顺势接话。"
        if any(token in lowered for token in ("急", "烦", "生气", "无语", "离谱")):
            return "语气里带着一点紧张或不耐烦，这个点还没完全放松下来。"
        return ""

    def _extract_warm_topic_units(self, segments: list[DialogueSegment]) -> list[TopicUnit]:
        if not segments:
            return []
        recent = list(segments[-6:])
        direct_segments = [segment for segment in recent if segment.is_at_bot or segment.is_reply_to_bot]
        visual_segments = [
            segment
            for segment in recent
            if segment.has_direct_vision or segment.is_image_only or segment.message_kind in {"image", "mixed"}
        ]
        latest_assistant = next((segment for segment in reversed(recent) if segment.role == "assistant" or segment.is_bot), None)
        question_segments = [segment for segment in reversed(recent) if self._is_question_like(segment.content)]
        decision_segments = [segment for segment in reversed(recent) if self._is_decision_like(segment.content)]
        units: list[TopicUnit] = []

        if direct_segments:
            anchor = direct_segments[-1]
            units.append(
                TopicUnit(
                    slot="topic",
                    text=f"当前主线还在围绕我刚接住的那个点继续展开，焦点落在“{self._preview_text(anchor.content, 28)}”。",
                    event_ids=[segment.event_id for segment in direct_segments if segment.event_id],
                )
            )
        elif visual_segments:
            anchor = visual_segments[-1]
            units.append(
                TopicUnit(
                    slot="topic",
                    text=f"当前主要在围绕图片/图文线索继续往下聊，焦点落在“{self._preview_text(anchor.content, 28)}”。",
                    event_ids=[segment.event_id for segment in visual_segments if segment.event_id],
                )
            )
        else:
            anchor = recent[-1]
            units.append(
                TopicUnit(
                    slot="topic",
                    # OPT-16/ID-04: 中性措辞——该模板同时服务私聊（1:1 对话被"群聊/
                    # 群友"话术误导，私聊 15/15 轮实证命中）
                    text=f"当前主要是在延续刚才的对话，最近落点是“{self._preview_text(anchor.content, 28)}”。",
                    event_ids=[anchor.event_id] if anchor.event_id else [],
                )
            )

        if latest_assistant is not None:
            followups = [segment for segment in recent if segment.timestamp > latest_assistant.timestamp and self._is_question_like(segment.content)]
            if followups:
                units.append(
                    TopicUnit(
                        slot="event",
                        text="最近的推进是我刚给出一次回应，后面马上有人继续追问或补条件，这条链还没收口。",
                        event_ids=[latest_assistant.event_id, *[segment.event_id for segment in followups if segment.event_id]],
                    )
                )
            else:
                units.append(
                    TopicUnit(
                        slot="event",
                        text="最近的推进是我刚给过回应，对话现在是在顺着那个回应继续消化细节。",
                        event_ids=[latest_assistant.event_id] if latest_assistant.event_id else [],
                    )
                )
        else:
            units.append(
                TopicUnit(
                    slot="event",
                    text="最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。",
                    event_ids=[segment.event_id for segment in recent[-2:] if segment.event_id],
                )
            )

        if decision_segments:
            anchor = decision_segments[0]
            units.append(
                TopicUnit(
                    slot="decision",
                    text=f"这段里已经出现了阶段性确认，方向更像是“{self._preview_text(anchor.content, 30)}”。",
                    event_ids=[anchor.event_id] if anchor.event_id else [],
                )
            )
        if question_segments:
            anchor = question_segments[0]
            units.append(
                TopicUnit(
                    slot="open_question",
                    text=f"现在还没收口的问题更像是“{self._preview_text(anchor.content, 30)}”，后面大概率还会继续接着问。",
                    event_ids=[anchor.event_id] if anchor.event_id else [],
                )
            )
        relation_text = self._detect_relation_state(recent)
        if relation_text:
            units.append(
                TopicUnit(
                    slot="relation_state",
                    text=relation_text,
                    event_ids=[segment.event_id for segment in recent if segment.event_id][:3],
                )
            )
        emotion_text = self._detect_emotion_state(recent)
        if emotion_text:
            units.append(
                TopicUnit(
                    slot="emotion_state",
                    text=emotion_text,
                    event_ids=[segment.event_id for segment in recent if segment.event_id][:3],
                )
            )
        if visual_segments:
            anchor = visual_segments[-1]
            units.append(
                TopicUnit(
                    slot="visual_context",
                    text=f"最近还有图片/图文线索在场，相关内容落在“{self._preview_text(anchor.content, 26)}”附近。",
                    event_ids=[segment.event_id for segment in visual_segments if segment.event_id],
                )
            )
        if question_segments and not decision_segments:
            units.append(
                TopicUnit(
                    slot="next_action",
                    text="接下来更像是在等我继续解释、确认条件，或者把刚才那个点真正答完。",
                    event_ids=[question_segments[0].event_id] if question_segments[0].event_id else [],
                )
            )
        return units

    def _select_warm_summary_segments(self, segments: list[DialogueSegment], *, limit: int = 6) -> list[DialogueSegment]:
        if not segments:
            return []
        scored: list[tuple[float, int, DialogueSegment]] = [
            (self._segment_relevance_score(segment)[0], index, segment)
            for index, segment in enumerate(segments)
        ]
        prioritized = [item for item in scored if item[0] > 0]
        if not prioritized:
            return list(segments[-limit:])
        selected = sorted(prioritized, key=lambda item: (item[0], item[1]), reverse=True)[:limit]
        return [segment for _, _, segment in sorted(selected, key=lambda item: item[1])]

    def _build_warm_summary(self, segments: list[DialogueSegment], *, max_tokens: int) -> str:
        if not segments or max_tokens <= 0:
            return ""
        direct = sum(1 for segment in segments if segment.is_at_bot or segment.is_reply_to_bot)
        reply_chain = sum(
            1
            for segment in segments
            if (segment.reply_target_sender_id or segment.reply_target_sender_name) and not segment.is_reply_to_bot
        )
        visuals = sum(
            1
            for segment in segments
            if segment.has_direct_vision or segment.is_image_only or segment.message_kind in {"image", "mixed"}
        )
        latest_speakers: list[str] = []
        for segment in reversed(segments):
            speaker = segment.speaker_name or segment.speaker_id or ("Bot" if segment.is_bot else "User")
            if speaker and speaker not in latest_speakers:
                latest_speakers.append(speaker)
            if len(latest_speakers) >= 3:
                break
        latest_speakers.reverse()
        lines: list[str] = []
        if latest_speakers:
            lines.append(f"最近主要是 {' / '.join(latest_speakers)} 在接着聊。")
        if direct:
            lines.append(f"其中有 {direct} 条是在直接问我或接着我的回复。")
        elif reply_chain:
            lines.append(f"其中有 {reply_chain} 条是在沿着回复链继续往下接。")
        if visuals:
            lines.append(f"最近还夹着 {visuals} 条图片或图文相关消息。")
        latest_assistant = next((segment for segment in reversed(segments) if segment.role == "assistant" or segment.is_bot), None)
        if latest_assistant is not None:
            speaker = latest_assistant.speaker_name or latest_assistant.speaker_id or "Bot"
            lines.append(f"{speaker} 刚刚也说过话，后面还有人在顺着接。")
        selected: list[str] = []
        used = 0
        for line in lines:
            estimate = self._estimate_tokens(line)
            if used + estimate > max_tokens:
                break
            selected.append(line)
            used += estimate
        return "\n".join(selected).strip()

    def _build_warm_summary_v2(self, segments: list[DialogueSegment], *, max_tokens: int) -> str:
        if not segments or max_tokens <= 0:
            return ""
        topic_units = self._extract_warm_topic_units(segments)
        if not topic_units:
            return ""
        selected: list[str] = []
        used = 0
        for unit in topic_units:
            line = str(unit.text or "").strip()
            if not line:
                continue
            estimate = max(1, self._estimate_tokens(line))
            if selected and used + estimate > max_tokens:
                break
            if not selected and estimate > max_tokens:
                selected.append(line)
                break
            selected.append(line)
            used += estimate
            if len(selected) >= 4:
                break
        return "\n".join(selected).strip()

    def _build_warm_quotes(
        self,
        segments: list[DialogueSegment],
        *,
        max_tokens: int,
        include_identity: bool = False,
    ) -> tuple[str, list[str], bool]:
        if not segments or max_tokens <= 0:
            return "", [], False
        candidate_pool = segments[-8:] if len(segments) > 8 else list(segments)
        ordered = [
            segment
            for _, segment in sorted(
                enumerate(candidate_pool),
                key=lambda pair: (self._segment_relevance_score(pair[1])[0], pair[0]),
                reverse=True,
            )
        ]
        selected: list[DialogueSegment] = []
        used = 0
        for segment in ordered:
            line = self._format_segment_line(segment, include_identity=include_identity)
            if not line:
                continue
            estimate = max(1, self._estimate_tokens(line))
            if used + estimate > max_tokens:
                continue
            selected.append(segment)
            used += estimate
            if len(selected) >= 3:
                break
        if not selected and candidate_pool:
            selected.append(candidate_pool[-1])
        latest_direct_user = next(
            (
                segment
                for segment in reversed(candidate_pool)
                if segment.role == "user"
                and not segment.is_bot
                and (segment.is_at_bot or segment.is_reply_to_bot)
            ),
            None,
        )
        if latest_direct_user is not None and latest_direct_user not in selected:
            line = self._format_segment_line(latest_direct_user, include_identity=include_identity)
            estimate = max(1, self._estimate_tokens(line))
            if used + estimate <= max_tokens or not selected:
                selected.append(latest_direct_user)
                used += estimate
        # 限制扫描窗口为最近 64 条，避免大群积压时 O(n) 全量扫描（D21）
        _scan_window = segments[-64:]
        latest_assistant = next((segment for segment in reversed(_scan_window) if segment.role == "assistant" or segment.is_bot), None)
        has_latest_assistant = bool(latest_assistant and latest_assistant in selected)
        if latest_assistant is not None and not has_latest_assistant:
            line = self._format_segment_line(latest_assistant, include_identity=include_identity)
            estimate = max(1, self._estimate_tokens(line))
            if used + estimate <= max_tokens or not selected:
                selected.append(latest_assistant)
                has_latest_assistant = True
        deduped: list[DialogueSegment] = []
        seen_keys: set[str] = set()
        for segment in sorted(selected, key=lambda item: item.timestamp):
            key = segment.event_id or f"{segment.timestamp}:{segment.speaker_id}:{segment.content}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(segment)
        lines = [
            self._format_segment_line(segment, include_identity=include_identity)
            for segment in deduped
            if self._format_segment_line(segment, include_identity=include_identity)
        ]
        quote_event_ids = [segment.event_id for segment in deduped if segment.event_id]
        return "\n".join(lines).strip(), quote_event_ids, has_latest_assistant

    async def get_warm_context_bundle(
        self,
        chat_id: str,
        *,
        max_age_seconds: float | None = None,
        max_tokens: int | None = None,
        include_identity: bool = False,
    ) -> WarmContextBundle:
        chat_key = self._resolve_chat_key(chat_id)
        max_age_seconds = self.warm_zone_ttl_seconds if max_age_seconds is None else float(max_age_seconds)
        max_tokens = self.warm_zone_max_tokens if max_tokens is None else int(max_tokens)
        now = time.time()
        async with self._lock:
            thread = self._threads.get(chat_key)
            if not thread:
                return WarmContextBundle()
            async with thread.lock:
                candidates: list[DialogueSegment] = []
                for segment in thread.segments:
                    if max_age_seconds > 0 and now - segment.timestamp > max_age_seconds:
                        continue
                    if not self._format_segment_line(segment):
                        continue
                    candidates.append(segment)
                if not candidates:
                    return WarmContextBundle()
                summary_budget = max(1, int(max_tokens * 0.35)) if max_tokens > 0 else 0
                quote_budget = max(1, max_tokens - summary_budget) if max_tokens > 0 else 0
                summary_candidates = self._select_warm_summary_segments(candidates)
                warm_units = self._extract_warm_topic_units(summary_candidates)
                summary_text = self._build_warm_summary_v2(summary_candidates, max_tokens=summary_budget)
                quote_text, quote_event_ids, has_latest_assistant = self._build_warm_quotes(
                    candidates,
                    max_tokens=quote_budget if quote_budget > 0 else max_tokens,
                    include_identity=include_identity,
                )
                return WarmContextBundle(
                    summary_text=summary_text,
                    quote_text=quote_text,
                    quote_event_ids=quote_event_ids,
                    has_latest_assistant=has_latest_assistant,
                    topic_preview=" | ".join(
                        f"{unit.slot}:{self._preview_text(unit.text, 24)}"
                        for unit in warm_units[:4]
                        if str(unit.text or "").strip()
                    ),
                )

    async def get_warm_transcript(self, chat_id: str, *, max_age_seconds: float | None = None, max_tokens: int | None = None) -> str:
        bundle = await self.get_warm_context_bundle(chat_id, max_age_seconds=max_age_seconds, max_tokens=max_tokens)
        return "\n".join(part for part in (bundle.summary_text, bundle.quote_text) if part).strip()

    async def snapshot_compaction_candidates(self, chat_id: str, *, keep_recent_segments: int = 16) -> dict[str, Any]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return {
                    "active_segments": 0,
                    "compressible_segments": 0,
                    "compressible_message_load": 0.0,
                    "compressible_long_message_count": 0,
                    "tail_event_id": "",
                    "recent_segments": [],
                }
            async with thread.lock:
                active = list(thread.segments)
                keep_recent_segments = max(0, int(keep_recent_segments))
                if keep_recent_segments <= 0:
                    old_zone = list(active)
                elif len(active) <= keep_recent_segments:
                    old_zone = []
                else:
                    old_zone = active[:-keep_recent_segments]
                return {
                    "active_segments": len(active),
                    "compressible_segments": len(old_zone),
                    "compressible_message_load": sum(self._segment_load(segment) for segment in old_zone),
                    "compressible_long_message_count": sum(
                        1 for segment in old_zone if self._standardized_length(str(segment.content or "")) > 100
                    ),
                    "tail_event_id": old_zone[-1].event_id if old_zone else "",
                    "compressible_zone_segments": list(old_zone),
                    "recent_segments": list(active[-max(keep_recent_segments, 4):]),
                }

    async def snapshot_counts(self, chat_id: str) -> dict[str, Any]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return {"segments": 0, "tokens": 0, "has_summary": False, "message_load": 0.0}
            async with thread.lock:
                return {
                    "segments": len(thread.segments),
                    "tokens": sum(segment.token_estimate for segment in thread.segments),
                    "has_summary": bool(thread.cold_summary),
                    "message_load": sum(self._segment_load(segment) for segment in thread.segments),
                }
