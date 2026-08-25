from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, List, Set

from astrbot.api.event import AstrMessageEvent

from ..contracts.conversation_event import ConversationEvent
from ..runtime.architecture_rollout import (
    ArchitectureTimer,
    record_architecture_observation,
    rollout_enabled,
)


@dataclass
class SessionContext:
    """In-memory per-chat attention accumulation context."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accumulation_pool: List[Any] = field(default_factory=list)
    attention_window: List[Any] = field(default_factory=list)
    attention_window_ts: List[float] = field(default_factory=list)
    is_evaluating: bool = False
    # OPT-07/RT-05: 私聊图片屏障的 per-burst 截止时间——合并循环多次迭代共享，
    # 防止每次 prepare_batch 重新起算总额（单图烧穿整轮预算的三缺口之一）
    vision_burst_deadline: float = 0.0
    last_active_time: float = field(default_factory=time.time)
    last_message_hash: str = ""
    repeat_count: int = 0
    last_active_user_time: float = 0.0
    last_window_open_ts: float = 0.0
    pending_vision_images: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_vision_mentions: dict[str, dict[str, Any]] = field(default_factory=dict)
    vision_pair_signal: asyncio.Event = field(default_factory=asyncio.Event)
    # Worker identity is tied to the live SessionContext.  A cancelled worker
    # must never revive a replaced/cleared chat session.
    worker_generation: int = 0
    worker_token: int = 0
    worker_task: Any = None
    closed: bool = False
    overflow_count: int = 0
    dropped_event_count: int = 0
    oldest_pending_at: float = 0.0


@dataclass
class NormalizedEvent:
    event: AstrMessageEvent
    sender_id: str
    sender_name: str
    text: str
    rich_text: str
    timestamp: float
    is_self: bool
    is_reply_to_bot: bool
    is_at_bot: bool
    is_direct_wakeup: bool
    is_near_context_query: bool
    reply_target_sender_id: str = ''
    reply_target_sender_name: str = ''
    image_urls: List[str] = field(default_factory=list)
    has_direct_vision: bool = False
    is_image_only: bool = False
    vision_state: str = 'none'
    image_placeholder_count: int = 0
    user_asked_about_image: bool = False
    token_set: Set[str] = field(default_factory=set)
    index: int = 0
    canonical_event: ConversationEvent | None = None


def _topic_epoch(event: AstrMessageEvent) -> int:
    history_policy = event.get_extra('astrmai_dialog_history_policy', None)
    if history_policy is None:
        return 0
    value = (
        history_policy.get('topic_epoch', 0)
        if isinstance(history_policy, dict)
        else getattr(history_policy, 'topic_epoch', 0)
    )
    return max(0, int(value or 0))


def build_normalized_events(gate, events, self_id: str) -> list[NormalizedEvent]:
    normalized_events: list[NormalizedEvent] = []
    for index, event in enumerate(events):
        timer = ArchitectureTimer()
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or '??/??'
        rich_text = str(event.get_extra('astrmai_rich_text', event.message_str) or '')
        text = str(event.message_str or rich_text or '')
        direct_refs = list(event.get_extra('direct_image_refs', event.get_extra('direct_vision_urls', [])) or [])
        extracted_refs = list(event.get_extra('extracted_image_refs', event.get_extra('extracted_image_urls', [])) or [])
        image_urls = list(dict.fromkeys(direct_refs + extracted_refs))
        raw_image_count = int(event.get_extra('astrmai_image_raw_component_count', 0) or 0)
        vision_state = str(event.get_extra('astrmai_vision_state', 'none') or 'none')
        token_set = gate._tokenize_text(rich_text or text)
        reply_target_sender_id, reply_target_sender_name = gate._extract_reply_target(event)
        is_at_bot = gate._is_at_bot_event(event, self_id)
        is_reply_to_bot = gate._is_reply_to_bot_event(event, self_id)
        is_direct_wakeup = gate._is_direct_wakeup_event(event, self_id)
        canonical_event = ConversationEvent.from_astr_event(
            event,
            self_id=self_id,
            rich_text=rich_text,
            image_refs=extracted_refs,
            direct_image_refs=direct_refs,
            reply_target_actor_id=reply_target_sender_id,
            reply_target_actor_name=reply_target_sender_name,
            is_at_bot=is_at_bot,
            is_reply_to_bot=is_reply_to_bot,
            is_direct_wakeup=is_direct_wakeup,
            topic_epoch=_topic_epoch(event),
            provenance=str(
                event.get_extra("astrmai_event_provenance", "original") or "original"
            ),
        )
        event.set_extra('astrmai_conversation_event', canonical_event)
        event.set_extra('astrmai_conversation_event_schema_version', canonical_event.schema_version)
        event.set_extra('astrmai_conversation_event_id', canonical_event.event_id)
        event.set_extra('astrmai_conversation_event_id_source', canonical_event.event_id_source)
        canonical_read_enabled = rollout_enabled(
            getattr(gate, "config", None),
            "canonical_read_enabled",
            True,
        )
        record_architecture_observation(
            event,
            "canonical_event",
            {
                "schema_version": canonical_event.schema_version,
                "event_id": canonical_event.event_id,
                "event_id_source": canonical_event.event_id_source,
                "actor_id": canonical_event.actor_id,
                "read_enabled": canonical_read_enabled,
                "legacy_actor_match": canonical_event.actor_id == sender_id,
                "elapsed_ms": timer.elapsed_ms,
            },
        )

        normalized_events.append(
            NormalizedEvent(
                event=event,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                rich_text=rich_text,
                timestamp=float(event.get_extra('astrmai_timestamp', getattr(event, 'timestamp', 0.0)) or 0.0),
                is_self=sender_id == str(self_id),
                is_reply_to_bot=is_reply_to_bot,
                is_at_bot=is_at_bot,
                is_direct_wakeup=is_direct_wakeup,
                is_near_context_query=gate._is_near_context_query_text(text or rich_text),
                reply_target_sender_id=reply_target_sender_id,
                reply_target_sender_name=reply_target_sender_name,
                image_urls=image_urls,
                has_direct_vision=bool(direct_refs),
                is_image_only=bool((image_urls or raw_image_count) and not token_set),
                vision_state=vision_state,
                image_placeholder_count=int(event.get_extra('astrmai_image_placeholder_count', 0) or 0),
                user_asked_about_image=bool(event.get_extra('astrmai_user_asked_about_image', False)),
                token_set=token_set,
                index=index,
                canonical_event=canonical_event if canonical_read_enabled else None,
            )
        )
    return normalized_events


__all__ = [
    'NormalizedEvent',
    'SessionContext',
    'build_normalized_events',
]
