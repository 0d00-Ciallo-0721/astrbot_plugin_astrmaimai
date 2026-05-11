from __future__ import annotations

import asyncio
import re
from typing import Any, List, Sequence

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

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

from ...infrastructure.compat.legacy_compat import emit_legacy_reply_runtime_extras
from ...infrastructure.gateway.output_guard import is_sendable_segment, sanitize_visible_reply_text
from ..contracts.focus_context import FreshnessState, ReplyMode
from ..contracts.reply_artifact import OutboundPolicy, VisibleReplyArtifact


def _component_instance(cls: Any, value: Any, attr_name: str, **kwargs: Any):
    try:
        return cls(**kwargs) if kwargs else cls(value)
    except TypeError:
        instance = cls()
        setattr(instance, attr_name, value)
        for key, item in kwargs.items():
            setattr(instance, key, item)
        return instance


def _plain_component(text: str):
    return _component_instance(Comp.Plain, text, "text")


def _at_component(uid: Any):
    return _component_instance(Comp.At, uid, "qq", qq=uid)


class ReplyArtifactMixin:
    def _bot_speaker_names(self) -> List[str]:
        names: List[str] = ["Bot"]
        nicknames = getattr(getattr(self.config, "system1", None), "nicknames", [])
        if isinstance(nicknames, list):
            names.extend(str(name).strip() for name in nicknames if str(name).strip())
        return list(dict.fromkeys(names))

    def _clean_reply_content(self, text: str) -> str:
        if not text:
            return ""
        fallback_text = getattr(self.config.reply, "fallback_text", "（陷入了短暂的沉默...）")
        cleaned = sanitize_visible_reply_text(text, fallback_text=fallback_text, speaker_names=self._bot_speaker_names())
        if cleaned != str(text).strip():
            logger.warning("[ReplyService] sanitized reply text before sending")
        return cleaned

    def _segment_reply_content(self, text: str, reply_mode: ReplyMode, policy: OutboundPolicy) -> List[str]:
        del reply_mode
        if policy.segment_strategy == "single":
            cleaned = re.sub(r"^\n+|\n+$", "", text.strip())
            return [cleaned] if cleaned else []
        if len(text) > self.no_segment_limit:
            cleaned = re.sub(r"^\n+|\n+$", "", text.strip())
            return [cleaned] if cleaned else []
        segments = self.segmenter.segment(text)
        if policy.segment_strategy == "gentle_two_step":
            return segments[:2]
        return segments

    def _build_visible_reply_artifact(
        self,
        text: str,
        *,
        reply_mode: ReplyMode = ReplyMode.CASUAL_FOLLOWUP,
        freshness_state: FreshnessState = FreshnessState.FRESH,
        stale_reason: str = "",
    ) -> VisibleReplyArtifact:
        policy = self._build_outbound_policy(reply_mode, freshness_state, stale_reason)
        if not policy.should_send:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason=policy.blocked_reason or "outbound_blocked",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        clean_text = self._clean_reply_content(text)
        if freshness_state == FreshnessState.STALE_BUT_SALVAGEABLE and policy.late_rewrite_allowed:
            clean_text = self._rewrite_late_reply(reply_mode, clean_text)
        if not clean_text:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason="empty_or_blocked_reply",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        segments = [
            segment
            for segment in self._segment_reply_content(clean_text, reply_mode, policy)
            if is_sendable_segment(segment)
        ]
        if not segments:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason="no_sendable_segments",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        visible_text = "\n".join(segments).strip()
        return VisibleReplyArtifact(
            visible_text=visible_text,
            segments=segments,
            persistable_text=visible_text,
            metadata={
                "segment_count": len(segments),
                "reply_mode": reply_mode.value,
                "freshness_state": freshness_state.value,
                "segment_strategy": policy.segment_strategy,
            },
        )

    def _merge_wait_targets(self, event: AstrMessageEvent, pending_actions: Sequence[dict[str, Any]] | None) -> list[str]:
        actions = list(pending_actions or event.get_extra("astrmai_pending_actions", []) or [])
        at_targets = [str(action.get("target_id")) for action in actions if action.get("action") == "at" and str(action.get("target_id", "")).strip()]
        if not at_targets:
            return list(event.get_extra("astrmai_wait_targets", []) or [])
        existing = [str(target) for target in (event.get_extra("astrmai_wait_targets", []) or []) if str(target).strip()]
        merged = existing[:]
        for target_id in at_targets:
            if target_id not in merged:
                merged.append(target_id)
        target_names = [
            str(action.get("target_name"))
            for action in actions
            if action.get("action") == "at" and action.get("target_name")
        ]
        emit_legacy_reply_runtime_extras(
            event,
            wait_targets=merged,
            wait_target_name=target_names[0] if target_names else "",
        )
        return merged

    async def _send_segments(self, event: AstrMessageEvent, chat_id: str, artifact: VisibleReplyArtifact, at_targets: Sequence[str]) -> bool:
        del chat_id
        from astrbot.api.event import MessageChain

        emit_legacy_reply_runtime_extras(event, artifact=artifact, is_self_reply=True)
        context = getattr(self.state_engine.gateway, "context", None)
        if not context:
            logger.error("[ReplyService] gateway context missing, unable to send message")
            return False

        for index, seg in enumerate(artifact.segments):
            freshness_state, stale_reason = await self._check_reply_freshness(event, event.unified_msg_origin)
            if freshness_state == FreshnessState.EXPIRED:
                logger.info(
                    f"[ReplyService] stopped stale segmented reply for {event.unified_msg_origin}: {stale_reason} | segment_index={index}"
                )
                break

            chain = MessageChain()
            if index == 0 and at_targets:
                for target_id in at_targets:
                    uid: Any = int(target_id) if str(target_id).isdigit() else target_id
                    chain.chain.append(_at_component(uid))
                chain.chain.append(_plain_component(" "))
            chain.chain.append(_plain_component(seg))
            await context.send_message(event.unified_msg_origin, chain)
            artifact.sent = True
            if not event.get_extra("astrmai_reply_sent", False):
                emit_legacy_reply_runtime_extras(event, artifact=artifact, reply_sent=True)

            if index < len(artifact.segments) - 1:
                base_factor = getattr(self.config.reply, "typing_speed_factor", 0.1)
                delay = min(2.0, max(0.5, len(seg) * base_factor))
                await asyncio.sleep(delay)
        return artifact.sent
