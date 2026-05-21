from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any, List, Sequence

from astrbot.api import logger

try:
    import astrbot.api.message_components as Comp
except ImportError:  # pragma: no cover
    class _CompatAt:
        def __init__(self, qq=None):
            self.qq = qq

    class _CompatPlain:
        def __init__(self, text=""):
            self.text = text

    class _CompatComp:
        At = _CompatAt
        Plain = _CompatPlain

    Comp = _CompatComp()

from astrbot.api.event import AstrMessageEvent

from ...infrastructure.compat.legacy_compat import emit_legacy_reply_runtime_extras
from ...infrastructure.gateway.output_guard import is_sendable_segment, sanitize_visible_reply_text
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...multimodal import MEMES_DIR, send_meme
from ...state.relationship.affection_router import AffectionRouter
from ..contracts.focus_context import FreshnessState, ReplyMode
from ..contracts.reply_artifact import OutboundPolicy, VisibleReplyArtifact
from .text_segmenter import TextSegmenter


from .reply_artifact_builder import ReplyArtifactMixin
from .reply_freshness import ReplyFreshnessMixin
from .reply_post_send import ReplyPostSendMixin


class ReplyService(ReplyFreshnessMixin, ReplyArtifactMixin, ReplyPostSendMixin):
    """Handles visible reply sanitizing, sending, and post-send emotional settlement."""

    def __init__(self, state_engine, mood_manager, config=None, runtime_coordinator=None, memory_engine=None):
        self.state_engine = state_engine
        self.mood_manager = mood_manager
        self.config = config if config else state_engine.config
        self.runtime_coordinator = runtime_coordinator
        self.memory_engine = memory_engine

        self.segmentation_threshold = self.config.reply.segment_min_len
        self.no_segment_limit = self.config.reply.no_segment_max_len
        self.meme_probability = self.config.reply.meme_probability
        self.segmenter = TextSegmenter(
            min_length=self.segmentation_threshold,
            max_length=self.no_segment_limit,
        )

    async def handle_reply(
        self,
        event: AstrMessageEvent,
        raw_text: str,
        chat_id: str,
        bypassed_tag: str = None,
        window_events: list = None,
        anchor_event: AstrMessageEvent = None,
        pending_actions: list = None,
    ):
        debug_trace(event, "execution.reply.enter", chat_id=chat_id, raw_preview=preview_text(str(raw_text or ""), 120))
        if not raw_text:
            return

        reply_mode = self._resolve_reply_mode(event)
        freshness_state, stale_reason = await self._check_reply_freshness(event, chat_id)
        artifact = self._build_visible_reply_artifact(
            raw_text,
            event=event,
            reply_mode=reply_mode,
            freshness_state=freshness_state,
            stale_reason=stale_reason,
            is_proactive=bool(event.get_extra("astrmai_is_proactive_event", False)),
        )
        if artifact.blocked:
            logger.debug(f"[{chat_id}] trace={event.get_extra('astrmai_trace_id', '')} reply blocked: {artifact.blocked_reason}")
            await self._settle_no_send_affection(
                event,
                chat_id,
                skipped_reason=str(artifact.blocked_reason or "blocked"),
                anchor_event=anchor_event,
            )
            return
        if freshness_state == FreshnessState.EXPIRED:
            logger.info(
                f"[ReplyService] skipped stale reply for {chat_id}: {stale_reason} | preview={preview_text(artifact.visible_text, 80)!r}"
            )
            await self._settle_no_send_affection(
                event,
                chat_id,
                skipped_reason="stale",
                anchor_event=anchor_event,
            )
            return

        sender_name = event.get_sender_name() or "群友"
        rich_text = event.get_extra("astrmai_rich_text", event.message_str)
        formatted_user_text = f"{sender_name}: {rich_text}"
        await self._sync_native_history_mirror(
            event=event,
            chat_id=chat_id,
            user_text=formatted_user_text,
            assistant_text=artifact.persistable_text,
        )

        at_targets = self._merge_wait_targets(event, pending_actions)
        if not await self._send_segments(event, chat_id, artifact, at_targets):
            await self._settle_no_send_affection(
                event,
                chat_id,
                skipped_reason="send_failed",
                anchor_event=anchor_event,
            )
            return
        await self._ingest_memory_turn(
            event,
            chat_id,
            formatted_user_text,
            artifact.persistable_text,
        )

        await self._settle_post_send(
            event,
            chat_id,
            bypassed_tag=bypassed_tag or event.get_extra("astrmai_bypass_mood_analysis", None),
            window_events=window_events,
            anchor_event=anchor_event,
        )


__all__ = ["ReplyService"]
