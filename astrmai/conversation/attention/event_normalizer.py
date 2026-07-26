from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, List, Set

from astrbot.api.event import AstrMessageEvent


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
    token_set: Set[str] = field(default_factory=set)
    index: int = 0


def build_normalized_events(gate, events, self_id: str) -> list[NormalizedEvent]:
    normalized_events: list[NormalizedEvent] = []
    for index, event in enumerate(events):
        sender_id = str(event.get_sender_id())
        sender_name = event.get_sender_name() or '??/??'
        rich_text = str(event.get_extra('astrmai_rich_text', event.message_str) or '')
        text = str(event.message_str or rich_text or '')
        direct_refs = list(event.get_extra('direct_image_refs', event.get_extra('direct_vision_urls', [])) or [])
        extracted_refs = list(event.get_extra('extracted_image_refs', event.get_extra('extracted_image_urls', [])) or [])
        image_urls = list(dict.fromkeys(direct_refs + extracted_refs))
        token_set = gate._tokenize_text(rich_text or text)
        reply_target_sender_id, reply_target_sender_name = gate._extract_reply_target(event)
        is_at_bot = gate._is_at_bot_event(event, self_id)

        normalized_events.append(
            NormalizedEvent(
                event=event,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                rich_text=rich_text,
                timestamp=float(event.get_extra('astrmai_timestamp', getattr(event, 'timestamp', 0.0)) or 0.0),
                is_self=sender_id == str(self_id),
                is_reply_to_bot=gate._is_reply_to_bot_event(event, self_id),
                is_at_bot=is_at_bot,
                is_direct_wakeup=gate._is_direct_wakeup_event(event, self_id),
                is_near_context_query=gate._is_near_context_query_text(text or rich_text),
                reply_target_sender_id=reply_target_sender_id,
                reply_target_sender_name=reply_target_sender_name,
                image_urls=image_urls,
                has_direct_vision=bool(direct_refs),
                is_image_only=bool(image_urls and not token_set),
                token_set=token_set,
                index=index,
            )
        )
    return normalized_events


__all__ = [
    'NormalizedEvent',
    'SessionContext',
    'build_normalized_events',
]
