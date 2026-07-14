from __future__ import annotations

import asyncio
import hashlib
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
from ...infrastructure.runtime.trace_runtime import debug_trace
from ..concurrency.controls import (
    record_conversation_concurrency_trace,
    resolve_conversation_concurrency_flags,
)
from ..contracts.focus_context import FreshnessState, ReplyMode
from ..contracts.reply_artifact import OutboundPolicy, VisibleReplyArtifact
from ..contracts.turn_identity import build_turn_send_key


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


def _hash_send_key(send_key: str) -> str:
    return hashlib.sha256(str(send_key or "").encode("utf-8", errors="ignore")).hexdigest()[:16]


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

    @staticmethod
    def _reply_stance(event: AstrMessageEvent | None) -> str:
        if event is None or not hasattr(event, "get_extra"):
            return ""
        return str(event.get_extra("astrmai_stance", "") or "").strip().lower()

    @staticmethod
    def _reply_social_intent(event: AstrMessageEvent | None) -> str:
        if event is None or not hasattr(event, "get_extra"):
            return ""
        return str(event.get_extra("astrmai_social_intent", "") or "").strip().lower()

    @staticmethod
    def _reply_sentence_chunks(text: str) -> List[str]:
        if not text:
            return []
        chunks = re.findall(r".*?(?:[。！？!?]+(?:[\"'”’」』]*)|$)", str(text or "").strip(), re.DOTALL)
        return [chunk.strip() for chunk in chunks if chunk and chunk.strip()]

    @staticmethod
    def _looks_like_extension_question(text: str) -> bool:
        stripped = str(text or "").strip()
        if not stripped:
            return False
        if stripped.endswith(("?", "？")):
            return True
        return any(
            marker in stripped.lower()
            for marker in (
                "要不要",
                "还要",
                "还想",
                "可以吗",
                "行吗",
                "好吗",
                "要吗",
                "want to",
                "do you want",
                "would you like",
                "can you",
            )
        )

    @staticmethod
    def _trim_text_with_cap(text: str, char_cap: int) -> str:
        normalized = str(text or "").strip()
        if len(normalized) <= char_cap:
            return normalized
        window = normalized[:char_cap]
        split_positions = [window.rfind(token) for token in ("。", "！", "？", ".", "!", "?", "，", ",", "；", ";")]
        best = max(split_positions)
        if best >= max(12, char_cap // 2):
            return window[: best + 1].strip()
        return window.rstrip(" ,，;；") + "..."

    @staticmethod
    def _stance_first_reply_profile(stance: str, social_intent: str) -> tuple[int, int]:
        intent = social_intent if social_intent in {"boundary", "observe", "answer", "comfort"} else "answer"
        profiles = {
            "guarded": {
                "boundary": (1, 28),
                "observe": (1, 32),
                "answer": (1, 38),
                "comfort": (2, 48),
            },
            "cool": {
                "boundary": (1, 34),
                "observe": (1, 40),
                "answer": (2, 60),
                "comfort": (2, 72),
            },
        }
        return profiles[stance][intent]

    def _apply_stance_first_reply_constraints(
        self,
        text: str,
        *,
        event: AstrMessageEvent | None = None,
    ) -> tuple[str, dict[str, Any]]:
        stance = self._reply_stance(event)
        if stance not in {"guarded", "cool"}:
            return text, {}

        social_intent = self._reply_social_intent(event)
        sentence_cap, char_cap = self._stance_first_reply_profile(stance, social_intent)
        original = str(text or "").strip()
        sentences = self._reply_sentence_chunks(original)
        reasons: list[str] = []

        if len(sentences) > 1 and self._looks_like_extension_question(sentences[-1]):
            sentences = sentences[:-1]
            reasons.append("trimmed_trailing_question")
        if len(sentences) > sentence_cap:
            sentences = sentences[:sentence_cap]
            reasons.append("capped_sentence_count")

        separator = "" if any(token in original for token in ("。", "！", "？")) else " "
        constrained = separator.join(sentences).strip() if sentences else original
        trimmed = self._trim_text_with_cap(constrained, char_cap)
        if trimmed != constrained:
            reasons.append("capped_character_count")
        constrained = trimmed
        constrained = re.sub(r"([!！~～])\1+", r"\1", constrained)
        constrained = re.sub(r"\s{2,}", " ", constrained).strip()

        if constrained == original:
            return original, {}
        return constrained, {
            "stance_clamp_applied": True,
            "stance_clamp_reason": ",".join(reasons) or "stance_first_reply_constraint",
            "stance_before_len": len(original),
            "stance_after_len": len(constrained),
            "stance": stance,
            "stance_social_intent": social_intent or "answer",
            "stance_sentence_cap": sentence_cap,
            "stance_char_cap": char_cap,
        }

    def _single_segment(self, text: str) -> List[str]:
        cleaned = re.sub(r"^\n+|\n+$", "", text.strip())
        return [cleaned] if cleaned else []

    def _join_segments_for_single_send(self, segments: Sequence[str]) -> str:
        joined = "\n".join(str(segment or "").strip() for segment in segments if str(segment or "").strip())
        return re.sub(r"^\n+|\n+$", "", joined.strip())

    def _cap_segments(self, segments: Sequence[str], limit: int) -> List[str]:
        cleaned = [str(segment or "").strip() for segment in segments if str(segment or "").strip()]
        if len(cleaned) <= limit:
            return cleaned
        head = cleaned[: limit - 1]
        tail = self._join_segments_for_single_send(cleaned[limit - 1 :])
        return [*head, tail] if tail else head

    def _segment_reply_content(
        self,
        text: str,
        reply_mode: ReplyMode,
        policy: OutboundPolicy,
        *,
        is_proactive: bool = False,
        force_segment: bool = False,
    ) -> tuple[List[str], str]:
        del reply_mode
        has_forced_paragraph = force_segment or "\n\n" in text
        if policy.segment_strategy == "single" and not has_forced_paragraph:
            return self._single_segment(text), "policy_single"
        if not has_forced_paragraph:
            if len(text) < self.segmentation_threshold:
                return self._single_segment(text), "below_segment_min"
            if len(text) >= self.no_segment_limit:
                return self._single_segment(text), "at_or_above_segment_limit"
        segments = self.segmenter.segment(text)
        if not segments:
            return [], "empty_after_segment"
        if is_proactive:
            return self._cap_segments(segments, 2), "proactive_limited"
        if policy.segment_strategy == "gentle_two_step":
            gentle_segments = self._cap_segments(segments, 2)
            if len(gentle_segments) == 2 and len(gentle_segments[1]) > max(48, self.no_segment_limit // 2):
                return self._single_segment(self._join_segments_for_single_send(gentle_segments)), "gentle_single_fallback"
            return gentle_segments, "gentle_two_step"
        return self._cap_segments(segments, 3), "natural_segmenter"

    def _build_visible_reply_artifact(
        self,
        text: str,
        *,
        event: AstrMessageEvent | None = None,
        reply_mode: ReplyMode = ReplyMode.CASUAL_FOLLOWUP,
        freshness_state: FreshnessState = FreshnessState.FRESH,
        stale_reason: str = "",
        is_proactive: bool = False,
    ) -> VisibleReplyArtifact:
        force_segment = "\n\n" in str(text or "")
        policy = self._build_outbound_policy(reply_mode, freshness_state, stale_reason)
        if not policy.should_send:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason=policy.blocked_reason or "outbound_blocked",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        if force_segment:
            clean_parts = [
                self._clean_reply_content(part)
                for part in re.split(r"\n{2,}", str(text or ""))
                if str(part or "").strip()
            ]
            clean_text = "\n\n".join(part for part in clean_parts if part)
        else:
            clean_text = self._clean_reply_content(text)
        if freshness_state == FreshnessState.STALE_BUT_SALVAGEABLE and policy.late_rewrite_allowed:
            clean_text = self._rewrite_late_reply(reply_mode, clean_text)
        clean_text, stance_metadata = self._apply_stance_first_reply_constraints(clean_text, event=event)
        if not clean_text:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason="empty_or_blocked_reply",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        is_proactive = bool(is_proactive or getattr(self, "_current_reply_is_proactive", False))
        raw_segments, segment_reason = self._segment_reply_content(
            clean_text,
            reply_mode,
            policy,
            is_proactive=is_proactive,
            force_segment=force_segment,
        )
        segments = [
            segment
            for segment in raw_segments
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
                "segment_reason": segment_reason,
                "delay_profile": "proactive" if is_proactive else policy.send_delay_profile,
                **stance_metadata,
            },
        )

    def _merge_wait_targets(self, event: AstrMessageEvent, pending_actions: Sequence[dict[str, Any]] | None) -> list[str]:
        actions = list(pending_actions or event.get_extra("astrmai_pending_actions", []) or [])
        existing = [str(target) for target in (event.get_extra("astrmai_wait_targets", []) or []) if str(target).strip()]
        at_targets = [str(action.get("target_id")) for action in actions if action.get("action") == "at" and str(action.get("target_id", "")).strip()]
        if not at_targets:
            return existing
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

    def _segment_send_delay(self, segment: str, profile: str) -> float:
        base_factor = float(getattr(self.config.reply, "typing_speed_factor", 0.1) or 0.0)
        if base_factor <= 0:
            return 0.0
        length_weight = min(90, max(8, len(str(segment or ""))))
        delay = 0.32 + (length_weight * base_factor * 0.16)
        if profile == "gentle":
            delay += 0.25
        elif profile == "fast":
            delay -= 0.15
        elif profile == "proactive":
            delay = min(delay, 0.9)
        return round(min(2.0, max(0.5, delay)), 2)

    async def _send_segments(self, event: AstrMessageEvent, chat_id: str, artifact: VisibleReplyArtifact, at_targets: Sequence[str]) -> bool:
        from astrbot.api.event import MessageChain

        emit_legacy_reply_runtime_extras(event, artifact=artifact, is_self_reply=True)
        context = getattr(self.state_engine.gateway, "context", None)
        if not context:
            logger.error("[ReplyService] gateway context missing, unable to send message")
            artifact.metadata["send_status"] = "failed"
            return False

        send_key = ""
        runtime_coordinator = getattr(self, "runtime_coordinator", None)
        turn = event.get_extra("astrmai_turn_identity", None)
        concurrency_flags = resolve_conversation_concurrency_flags(getattr(self, "config", None))
        response_kind = str(event.get_extra("astrmai_response_kind", "final") or "final").strip()
        if response_kind not in {"final", "follow_up"}:
            response_kind = "final"
        if (
            concurrency_flags.send_claim_enabled
            and turn is not None
            and runtime_coordinator is not None
            and hasattr(runtime_coordinator, "claim_send")
        ):
            try:
                send_key = build_turn_send_key(turn, response_kind)
                if not await runtime_coordinator.claim_send(chat_id, send_key):
                    debug_trace(
                        event,
                        "reply.duplicate_final_blocked" if response_kind == "final" else "reply.duplicate_follow_up_blocked",
                        chat_id=chat_id,
                        thread_id=getattr(turn, "thread_id", ""),
                        generation=getattr(turn, "generation", ""),
                        response_kind=response_kind,
                    )
                    record_conversation_concurrency_trace(
                        event,
                        "send_claim",
                        debug_enabled=concurrency_flags.debug_trace_enabled,
                        chat_id=chat_id,
                        thread_id=getattr(turn, "thread_id", ""),
                        generation=getattr(turn, "generation", ""),
                        claim_status="duplicate",
                        send_key_hash=_hash_send_key(send_key),
                    )
                    logger.info(f"[ReplyService] skipped duplicate {response_kind} send for {chat_id}: {send_key}")
                    artifact.metadata["send_status"] = "duplicate_blocked"
                    return False
                debug_trace(
                    event,
                    "reply.send_claimed",
                    chat_id=chat_id,
                    thread_id=getattr(turn, "thread_id", ""),
                    generation=getattr(turn, "generation", ""),
                )
                record_conversation_concurrency_trace(
                    event,
                    "send_claim",
                    debug_enabled=concurrency_flags.debug_trace_enabled,
                    chat_id=chat_id,
                    thread_id=getattr(turn, "thread_id", ""),
                    generation=getattr(turn, "generation", ""),
                    claim_status="claimed",
                    send_key_hash=_hash_send_key(send_key),
                )
            except Exception:
                logger.debug("[ReplyService] send claim degraded; preserving legacy send behavior", exc_info=True)
                send_key = ""

        outbound_message_ids: list[str] = []
        sent_segment_count = 0
        try:
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
                sent_result = await context.send_message(event.unified_msg_origin, chain)
                if sent_result is not None:
                    outbound_message_ids.append(str(sent_result))
                artifact.sent = True
                sent_segment_count += 1
                if not event.get_extra("astrmai_reply_sent", False):
                    emit_legacy_reply_runtime_extras(event, artifact=artifact, reply_sent=True)

                if index < len(artifact.segments) - 1:
                    delay = self._segment_send_delay(seg, str(artifact.metadata.get("delay_profile", "default") or "default"))
                    await asyncio.sleep(delay)
        except Exception as exc:
            if artifact.sent:
                artifact.metadata["send_status"] = "partial_sent"
                artifact.metadata["sent_segment_count"] = sent_segment_count
                logger.warning(
                    f"[ReplyService] segmented reply partially sent for {chat_id}: {exc}"
                )
            else:
                artifact.metadata["send_status"] = "failed"
            if (
                not artifact.sent
                and send_key
                and runtime_coordinator is not None
                and hasattr(runtime_coordinator, "mark_send_failed")
            ):
                try:
                    failure_reason = str(exc) or "send_exception"
                    await runtime_coordinator.mark_send_failed(chat_id, send_key, failure_reason)
                    debug_trace(event, "reply.send_failed", chat_id=chat_id, reason=failure_reason)
                    record_conversation_concurrency_trace(
                        event,
                        "send_claim",
                        debug_enabled=concurrency_flags.debug_trace_enabled,
                        chat_id=chat_id,
                        thread_id=getattr(turn, "thread_id", "") if turn is not None else "",
                        generation=getattr(turn, "generation", "") if turn is not None else "",
                        claim_status="failed",
                        send_key_hash=_hash_send_key(send_key),
                    )
                except Exception:
                    logger.debug("[ReplyService] send claim failure marking degraded", exc_info=True)
            if not artifact.sent:
                raise
        if artifact.sent and sent_segment_count < len(artifact.segments):
            artifact.metadata["send_status"] = "partial_sent"
            artifact.metadata["sent_segment_count"] = sent_segment_count
            sent_text = "\n".join(artifact.segments[:sent_segment_count]).strip()
            artifact.visible_text = sent_text
            artifact.persistable_text = sent_text
        if outbound_message_ids:
            event.set_extra("astrmai_reply_outbound_message_ids", outbound_message_ids[:])
        if send_key and runtime_coordinator is not None and hasattr(runtime_coordinator, "commit_send"):
            try:
                if artifact.sent:
                    await runtime_coordinator.commit_send(chat_id, send_key, outbound_message_ids)
                    debug_trace(event, "reply.send_committed", chat_id=chat_id, sent_count=len(outbound_message_ids))
                    record_conversation_concurrency_trace(
                        event,
                        "send_claim",
                        debug_enabled=concurrency_flags.debug_trace_enabled,
                        chat_id=chat_id,
                        thread_id=getattr(turn, "thread_id", "") if turn is not None else "",
                        generation=getattr(turn, "generation", "") if turn is not None else "",
                        claim_status="committed",
                        send_key_hash=_hash_send_key(send_key),
                    )
                elif hasattr(runtime_coordinator, "mark_send_failed"):
                    await runtime_coordinator.mark_send_failed(chat_id, send_key, "not_sent")
                    debug_trace(event, "reply.send_failed", chat_id=chat_id, reason="not_sent")
                    record_conversation_concurrency_trace(
                        event,
                        "send_claim",
                        debug_enabled=concurrency_flags.debug_trace_enabled,
                        chat_id=chat_id,
                        thread_id=getattr(turn, "thread_id", "") if turn is not None else "",
                        generation=getattr(turn, "generation", "") if turn is not None else "",
                        claim_status="failed",
                        send_key_hash=_hash_send_key(send_key),
                    )
            except Exception:
                logger.debug("[ReplyService] send claim commit degraded", exc_info=True)
        return artifact.sent
