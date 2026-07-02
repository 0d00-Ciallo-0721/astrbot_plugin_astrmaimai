from __future__ import annotations

import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...infrastructure.runtime.trace_runtime import preview_text
from ..contracts.focus_context import FreshnessState, ReplyMode
from ..contracts.reply_artifact import OutboundPolicy


class ReplyFreshnessMixin:
    def _reply_max_age_seconds(self) -> float:
        configured = float(getattr(getattr(self.config, "reply", None), "stale_reply_max_age_sec", 0.0) or 0.0)
        if configured > 0:
            return configured
        api_timeout = float(getattr(getattr(self.config, "infra", None), "api_timeout", 15.0) or 15.0)
        return max(30.0, min(90.0, api_timeout * 2.5))

    def _resolve_reply_mode(self, event: AstrMessageEvent) -> ReplyMode:
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        if prompt_envelope and getattr(prompt_envelope, "reply_mode", None):
            return prompt_envelope.reply_mode
        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        if focus_context and getattr(focus_context, "reply_mode", None):
            return focus_context.reply_mode
        raw_mode = str(event.get_extra("astrmai_reply_mode", ReplyMode.CASUAL_FOLLOWUP.value) or ReplyMode.CASUAL_FOLLOWUP.value)
        try:
            return ReplyMode(raw_mode)
        except ValueError:
            return ReplyMode.CASUAL_FOLLOWUP

    def _is_direct_engagement_event(self, event: AstrMessageEvent) -> bool:
        return bool(
            event.get_extra("astrmai_group_direct_wakeup", False)
            or event.get_extra("astrmai_force_engage", False)
            or event.get_extra("is_private_chat", False)
        )

    async def _allow_direct_reply_timeout(self, event: AstrMessageEvent, chat_id: str, event_ts: float) -> bool:
        if not self._is_direct_engagement_event(event):
            return False
        if not self.runtime_coordinator or not hasattr(self.runtime_coordinator, "get_latest_activity"):
            return False
        latest_ts, _, _, _ = await self.runtime_coordinator.get_latest_activity(chat_id)
        return not latest_ts or latest_ts <= event_ts

    async def _check_reply_freshness(self, event: AstrMessageEvent, chat_id: str) -> tuple[FreshnessState, str]:
        event_ts = float(event.get_extra("astrmai_timestamp", 0.0) or 0.0)
        if event_ts <= 0:
            return FreshnessState.FRESH, ""

        reply_age = max(0.0, time.time() - event_ts)  # ponytail: NTP guard
        max_age = self._reply_max_age_seconds()
        if reply_age > max_age:
            if await self._allow_direct_reply_timeout(event, chat_id, event_ts):
                logger.info(
                    f"[ReplyService] allowing overdue direct reply for {chat_id}: "
                    f"{reply_age:.1f}s>{max_age:.1f}s without newer activity"
                )
                return FreshnessState.FRESH, ""
            return FreshnessState.EXPIRED, f"reply_age_exceeded:{reply_age:.1f}s>{max_age:.1f}s"

        if not self.runtime_coordinator:
            return FreshnessState.FRESH, ""

        if not hasattr(self.runtime_coordinator, "evaluate_reply_freshness"):
            latest_ts, latest_sender_id, latest_sender_name, latest_preview = await self.runtime_coordinator.get_latest_activity(chat_id)
            if latest_ts and latest_ts > event_ts:
                actor = latest_sender_name or latest_sender_id or "unknown"
                return FreshnessState.EXPIRED, f"superseded_by_newer_activity:{actor}:{preview_text(latest_preview, 60)}"
            return FreshnessState.FRESH, ""

        thread_signature = str(event.get_extra("astrmai_thread_signature", "") or "")
        freshness_state, stale_reason = await self.runtime_coordinator.evaluate_reply_freshness(
            chat_id,
            event_ts,
            max_age_seconds=max_age,
            thread_signature=thread_signature,
        )
        if freshness_state != FreshnessState.FRESH and stale_reason.startswith("superseded_by_newer_activity"):
            latest_ts, latest_sender_id, latest_sender_name, latest_preview = await self.runtime_coordinator.get_latest_activity(chat_id)
            actor = latest_sender_name or latest_sender_id or "unknown"
            stale_reason = f"superseded_by_newer_activity:{actor}:{preview_text(latest_preview, 60)}"
        return freshness_state, stale_reason

    def _build_outbound_policy(self, reply_mode: ReplyMode, freshness_state: FreshnessState, stale_reason: str) -> OutboundPolicy:
        if freshness_state == FreshnessState.EXPIRED:
            return OutboundPolicy(
                should_send=False,
                freshness_state=freshness_state,
                blocked_reason=stale_reason or "expired",
            )
        if freshness_state == FreshnessState.STALE_BUT_SALVAGEABLE:
            return OutboundPolicy(
                should_send=True,
                freshness_state=freshness_state,
                length_class="short",
                segment_strategy="single",
                late_rewrite_allowed=True,
                send_delay_profile="fast",
            )
        strategy = "default"
        delay_profile = "default"
        if reply_mode in {ReplyMode.PLAYFUL_INTERACTION, ReplyMode.IMAGE_REACTION}:
            strategy = "single"
        elif reply_mode == ReplyMode.EMOTIONAL_SUPPORT:
            strategy = "gentle_two_step"
            delay_profile = "gentle"
        return OutboundPolicy(
            should_send=True,
            freshness_state=freshness_state,
            length_class="normal",
            segment_strategy=strategy,
            late_rewrite_allowed=False,
            send_delay_profile=delay_profile,
        )

    def _rewrite_late_reply(self, reply_mode: ReplyMode, clean_text: str) -> str:
        fallback_map = {
            ReplyMode.PLAYFUL_INTERACTION: "欸，我刚刚还想接你这句来着。",
            ReplyMode.EMOTIONAL_SUPPORT: "我还在这儿，先抱抱你。",
            ReplyMode.DIRECT_QUESTION: "我刚刚看到你这句了，让我接一下。",
            ReplyMode.CASUAL_FOLLOWUP: "我还记着你刚刚这句呢。",
            ReplyMode.IMAGE_REACTION: "刚刚那张我看到了。",
            ReplyMode.LATE_RECONNECT: "我这会儿才接上你刚刚那句。",
            ReplyMode.AMBIENT_IGNORE: "",
        }
        if not clean_text:
            return fallback_map.get(reply_mode, "")
        first_line = re.split(r"[\r\n。！？?!]", clean_text.strip(), maxsplit=1)[0].strip()
        if len(first_line) > 24:
            first_line = first_line[:24].rstrip("，。！？?!") + "..."
        fallback = fallback_map.get(reply_mode, "")
        return first_line or fallback
