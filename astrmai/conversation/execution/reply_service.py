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
from ...infrastructure.runtime.turn_call_ledger import observe_stage, record_reply_stats
from ...multimodal import MEMES_DIR, send_meme
from ...state.relationship.affection_router import AffectionRouter
from ..contracts.focus_context import FreshnessState, ReplyMode
from ..contracts.reply_artifact import OutboundPolicy, VisibleReplyArtifact
from ..contracts.committed_reply import CommittedBotTurn
from .text_segmenter import TextSegmenter


from .reply_artifact_builder import ReplyArtifactMixin
from .reply_freshness import ReplyFreshnessMixin
from .reply_post_send import ReplyPostSendMixin
from .reply_commit_service import ReplyCommitService
from .qq_action_dispatcher import QQActionDispatcher
from .tts_bridge import TTSBridge


class ReplyService(ReplyFreshnessMixin, ReplyArtifactMixin, ReplyPostSendMixin):
    """Handles visible reply sanitizing, sending, and post-send emotional settlement."""

    def __init__(
        self,
        state_engine,
        mood_manager,
        config=None,
        runtime_coordinator=None,
        memory_engine=None,
        dialogue_store=None,
        evolution_manager=None,
        reply_commit_service=None,
        post_reply_feedback_coordinator=None,
    ):
        self.state_engine = state_engine
        self.mood_manager = mood_manager
        self.config = config if config else state_engine.config
        self.runtime_coordinator = runtime_coordinator
        self.memory_engine = memory_engine
        self.dialogue_store = dialogue_store
        self.evolution_manager = evolution_manager
        self.reply_commit_service = (
            reply_commit_service
            if reply_commit_service is not None
            else ReplyCommitService()
        )
        self.post_reply_feedback_coordinator = post_reply_feedback_coordinator
        self.group_reread_observer = None
        self.qq_action_dispatcher = QQActionDispatcher(
            config=self.config,
            runtime_coordinator=runtime_coordinator,
        )
        self.tts_bridge = TTSBridge(config=self.config)

        self.segmentation_threshold = self.config.reply.segment_min_len
        self.no_segment_limit = self.config.reply.no_segment_max_len
        self.meme_probability = self.config.reply.meme_probability
        self.segmenter = TextSegmenter(
            min_length=self.segmentation_threshold,
            max_length=self.no_segment_limit,
        )

    def refresh_config(self, config) -> None:
        self.config = config
        self.segmentation_threshold = self.config.reply.segment_min_len
        self.no_segment_limit = self.config.reply.no_segment_max_len
        self.meme_probability = self.config.reply.meme_probability
        self.segmenter = TextSegmenter(
            min_length=self.segmentation_threshold,
            max_length=self.no_segment_limit,
        )
        self.qq_action_dispatcher.refresh_config(config)
        self.tts_bridge.refresh_config(config)

    def bind_post_reply_feedback_coordinator(self, coordinator) -> None:
        self.post_reply_feedback_coordinator = coordinator

    def bind_group_reread_observer(self, observer) -> None:
        self.group_reread_observer = observer

    async def _record_group_reread_seeds(self, event, chat_id: str, receipt) -> None:
        observer = self.group_reread_observer
        if (
            observer is None
            or not getattr(event, "get_group_id", lambda: "")()
            or event.get_extra("astrmai_group_reread_dispatched", False)
            or event.get_extra("astrmai_reread_request", None)
        ):
            return
        bot_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        message_ids = list(getattr(receipt, "outbound_message_ids", ()) or ())
        for index, segment in enumerate(getattr(receipt, "sent_segments", ()) or ()):
            event_id = message_ids[index] if index < len(message_ids) else ""
            try:
                await observer.record_outbound_text_seed(
                    chat_id,
                    segment,
                    bot_id=bot_id,
                    event_id=event_id,
                )
            except Exception:
                logger.debug("[ReplyService] group reread seed degraded", exc_info=True)

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
            return VisibleReplyArtifact("", [], "", blocked_reason="empty_reply")

        with observe_stage(event, "reply.prepare") as prepare_stage:
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
            prepare_stage["reply_chars"] = len(artifact.visible_text or "")
            prepare_stage["segment_count"] = len(artifact.segments or [])
            prepare_stage["blocked"] = bool(artifact.blocked)
        record_reply_stats(
            event,
            segment_count=len(artifact.segments or []),
            segment_lengths=[len(segment or "") for segment in artifact.segments or []],
            total_chars=len(artifact.visible_text or ""),
            strategy=str(artifact.metadata.get("segment_strategy", "") or ""),
            send_status=str(artifact.metadata.get("send_status", "") or ""),
            reply_completed=bool(artifact.blocked or freshness_state == FreshnessState.EXPIRED),
            metadata=artifact.metadata,
        )
        if artifact.blocked:
            event.set_extra("astrmai_reply_delivery_status", "cancelled")
            event.set_extra(
                "astrmai_reply_delivery_failure_reason",
                str(artifact.blocked_reason or "blocked"),
            )
            logger.debug(f"[{chat_id}] trace={event.get_extra('astrmai_trace_id', '')} reply blocked: {artifact.blocked_reason}")
            await self._settle_no_send_affection(
                event,
                chat_id,
                skipped_reason=str(artifact.blocked_reason or "blocked"),
                anchor_event=anchor_event,
            )
            return artifact
        if freshness_state == FreshnessState.EXPIRED:
            event.set_extra("astrmai_reply_delivery_status", "stale")
            event.set_extra(
                "astrmai_reply_delivery_failure_reason",
                str(stale_reason or "reply_age_exceeded"),
            )
            logger.info(
                f"[ReplyService] skipped stale reply for {chat_id}: {stale_reason} | preview={preview_text(artifact.visible_text, 80)!r}"
            )
            await self._settle_no_send_affection(
                event,
                chat_id,
                skipped_reason="stale",
                anchor_event=anchor_event,
            )
            return artifact

        sender_name = event.get_sender_name() or "群友"
        rich_text = event.get_extra("astrmai_rich_text", event.message_str)
        formatted_user_text = f"{sender_name}: {rich_text}"
        reply_plan = self._build_reply_plan(event, chat_id, artifact)
        at_targets = self._merge_wait_targets(event, pending_actions)
        with observe_stage(
            event,
            "reply.send",
            metadata={"planned_segment_count": len(artifact.segments or [])},
        ) as send_stage:
            sent = await self._send_segments(event, chat_id, artifact, at_targets)
            send_stage["sent"] = bool(sent)
            send_stage["sent_segment_count"] = int(artifact.metadata.get("sent_segment_count", 0) or 0)
        if not sent:
            event.set_extra("astrmai_reply_delivery_status", "failed")
            event.set_extra(
                "astrmai_reply_delivery_failure_reason",
                str(
                    artifact.metadata.get("send_failure_reason", "")
                    or "send_failed"
                ),
            )
            event.set_extra(
                "astrmai_reply_sent_segment_count",
                int(artifact.metadata.get("sent_segment_count", 0) or 0),
            )
            record_reply_stats(
                event,
                segment_count=len(artifact.segments or []),
                segment_lengths=[len(segment or "") for segment in artifact.segments or []],
                total_chars=len(artifact.visible_text or ""),
                strategy=str(artifact.metadata.get("segment_strategy", "") or ""),
                send_status=str(artifact.metadata.get("send_status", "failed") or "failed"),
                sent_segment_count=int(artifact.metadata.get("sent_segment_count", 0) or 0),
                reply_completed=True,
                metadata=artifact.metadata,
            )
            artifact.blocked_reason = artifact.blocked_reason or "send_failed"
            artifact.metadata.setdefault("send_status", "failed")
            await self._settle_no_send_affection(
                event,
                chat_id,
                skipped_reason="send_failed",
                anchor_event=anchor_event,
            )
            return artifact
        record_reply_stats(
            event,
            segment_count=len(artifact.segments or []),
            segment_lengths=[len(segment or "") for segment in artifact.segments or []],
            total_chars=len(artifact.visible_text or ""),
            strategy=str(artifact.metadata.get("segment_strategy", "") or ""),
            send_status=str(artifact.metadata.get("send_status", "sent") or "sent"),
            sent_segment_count=int(artifact.metadata.get("sent_segment_count", len(artifact.segments or [])) or 0),
            reply_completed=True,
            metadata=artifact.metadata,
        )
        try:
            await self.qq_action_dispatcher.commit(
                event,
                chat_id,
                send_key=str(event.get_extra("astrmai_reply_send_key", "") or ""),
            )
        except Exception as exc:
            logger.warning(f"[ReplyService] optional QQ action commit degraded: {exc}")
        send_receipt = self._build_send_receipt(artifact)
        event.set_extra("astrmai_reply_delivery_status", send_receipt.status.value)
        event.set_extra(
            "astrmai_reply_sent_segment_count",
            len(send_receipt.sent_segments),
        )
        event.set_extra(
            "astrmai_reply_outbound_message_ids",
            list(send_receipt.outbound_message_ids),
        )
        committed_turn = CommittedBotTurn.from_plan(
            reply_plan,
            send_receipt,
        )
        await self._record_group_reread_seeds(event, chat_id, send_receipt)
        artifact.metadata["reply_commit_id"] = committed_turn.commit_id
        artifact.metadata["reply_commit_status"] = committed_turn.send_status.value
        artifact.metadata["partial_send"] = committed_turn.partial_send
        with observe_stage(
            event,
            "reply.commit",
            metadata={"commit_id": committed_turn.commit_id},
        ) as commit_stage:
            commit_result = await self._commit_visible_reply(
                event,
                committed_turn,
                user_text=formatted_user_text,
            )
            commit_stage["consumer_count"] = len(commit_result.consumer_status)
            commit_stage["repair_scheduled"] = bool(commit_result.repair_scheduled)
        event.set_extra("astrmai_reply_commit_id", committed_turn.commit_id)
        record_committed_reply = getattr(
            self.state_engine,
            "record_committed_bot_reply",
            None,
        )
        if callable(record_committed_reply):
            try:
                await record_committed_reply(
                    chat_id,
                    committed_at=committed_turn.sent_at,
                    is_proactive=bool(
                        event.get_extra("astrmai_is_proactive_event", False)
                    ),
                    commit_id=committed_turn.commit_id,
                )
            except Exception as exc:
                logger.warning(
                    f"[ReplyService] bot reply watermark persistence degraded for {chat_id}: {exc}"
                )
        artifact.metadata["commit_consumer_status"] = dict(
            commit_result.consumer_status
        )
        artifact.metadata["commit_repair_scheduled"] = bool(
            commit_result.repair_scheduled
        )

        await self._settle_post_send(
            event,
            chat_id,
            bypassed_tag=(
                bypassed_tag
                or event.get_extra("astrmai_bypass_mood_analysis", None)
            ),
            window_events=window_events,
            anchor_event=anchor_event,
            reply_text=artifact.visible_text,
        )
        artifact.metadata.setdefault("send_status", "sent")
        return artifact


__all__ = ["ReplyService"]
