from __future__ import annotations

from typing import List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...infrastructure.compat.legacy_compat import emit_legacy_prompt_envelope_extras, read_legacy_focus_thread_context
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import preview_text
from ..contracts.focus_context import FocusThreadContext, FreshnessState, ReplyMode
from ..contracts.prompt_envelope import PromptEnvelope
from .message_renderer import MessageRenderer


class PlannerPromptContextMixin:
    @staticmethod
    def _is_near_context_query(message_text: str) -> bool:
        if not isinstance(message_text, str):
            return False
        normalized = message_text.strip()
        if not normalized:
            return False
        trigger_phrases = [
            "为什么",
            "哪里",
            "什么意思",
            "你刚刚",
            "刚刚说",
            "这个",
            "那个",
            "上一个",
            "上一句",
            "不是这个",
            "为啥",
            "咋",
            "啥意思",
            "不可以",
        ]
        return any(phrase in normalized for phrase in trigger_phrases)

    @staticmethod
    def _render_event_line(message_event: AstrMessageEvent) -> str:
        return MessageRenderer.render_event(message_event)

    @staticmethod
    def _is_lightweight_event(message_event: AstrMessageEvent, focus_context: FocusThreadContext) -> bool:
        interaction_kind = str(message_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        if message_event.get_extra("is_virtual_poke", False) or interaction_kind in {"poke"}:
            return True
        text = str(message_event.get_extra("astrmai_rich_text", message_event.message_str) or "").strip()
        if text.startswith("(Interaction:") and text.endswith(")"):
            return True
        vision_bundle = getattr(focus_context, "vision_bundle", None)
        image_urls = list(getattr(vision_bundle, "image_urls", []) or []) if vision_bundle else []
        direct_urls = list(getattr(vision_bundle, "direct_image_urls", []) or []) if vision_bundle else []
        return not text and not image_urls and not direct_urls

    def _build_focus_message_text(self, focus_event: AstrMessageEvent, focus_context: FocusThreadContext) -> str:
        return focus_context.focus_message_text or self._render_event_line(focus_event)

    def _build_direct_context_text(
        self,
        root_event: Optional[AstrMessageEvent],
        focus_event: AstrMessageEvent,
        core_events: List[AstrMessageEvent],
    ) -> str:
        direct_context_lines = []
        if root_event and root_event is not focus_event:
            direct_context_lines.append(self._render_event_line(root_event))

        for candidate in core_events:
            if candidate is focus_event or candidate is root_event:
                continue
            direct_context_lines.append(self._render_event_line(candidate))
        return "\n".join(line for line in direct_context_lines if line)

    def _build_related_context_text(
        self,
        focus_event: AstrMessageEvent,
        related_events: List[AstrMessageEvent],
    ) -> str:
        related_lines = [self._render_event_line(candidate) for candidate in related_events if candidate is not focus_event]
        return "\n".join(line for line in related_lines if line)

    def _build_ambient_background_text(self, ambient_events: List[AstrMessageEvent]) -> str:
        return "\n".join(self._render_event_line(message_event) for message_event in ambient_events)

    def _coerce_focus_thread_context(self, event: AstrMessageEvent) -> FocusThreadContext:
        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        if isinstance(focus_context, FocusThreadContext):
            return focus_context
        return read_legacy_focus_thread_context(event, default_event=event)

    def _build_prompt_envelope(
        self,
        focus_context: FocusThreadContext,
        focus_message_text: str,
        direct_context_text: str,
        related_context_text: str,
        ambient_background_text: str,
        recent_transcript: str,
        last_assistant_reply: str,
        near_context_priority: bool,
    ) -> PromptEnvelope:
        return PromptEnvelope(
            raw_user_text=focus_message_text,
            recent_transcript=recent_transcript,
            last_assistant_reply=last_assistant_reply,
            focus_message_text=focus_message_text,
            direct_context_text=direct_context_text,
            related_context_text=related_context_text,
            ambient_background_text=ambient_background_text,
            focus_reason=focus_context.focus_reason,
            focus_thread_reason=focus_context.root_reason or focus_context.focus_reason,
            near_context_priority=near_context_priority,
            reply_mode=focus_context.reply_mode,
            social_state=focus_context.social_state,
            freshness_state=focus_context.freshness_budget.state or FreshnessState.FRESH,
            thread_signature=focus_context.thread_signature,
            guidance_lines=self._build_guidance_lines(focus_context.reply_mode),
        )

    @staticmethod
    def _build_guidance_lines(reply_mode: ReplyMode) -> List[str]:
        guidance_map = {
            ReplyMode.PLAYFUL_INTERACTION: ["先接住互动，再轻轻打趣，不要突然讲大道理。"],
            ReplyMode.EMOTIONAL_SUPPORT: ["先安抚情绪，再决定是否追问。", "语气保持温柔，不要抢着转移话题。"],
            ReplyMode.DIRECT_QUESTION: ["优先正面回答当前问题，不要绕远。"],
            ReplyMode.CASUAL_FOLLOWUP: ["顺着刚才的话自然接下去，不另起话题。"],
            ReplyMode.IMAGE_REACTION: ["先短促回应看到的画面感，不要铺陈过长。"],
            ReplyMode.LATE_RECONNECT: ["承认自己是接回当前时刻，保持简短。"],
            ReplyMode.AMBIENT_IGNORE: ["若没有明确需要，不要强行插话。"],
        }
        return guidance_map.get(reply_mode, ["顺着当前对话自然回应。"])

    async def _get_recent_dialogue_transcript(self, chat_id: str, max_age_seconds: float = 900.0) -> str:
        lane_manager = getattr(self.gateway, "lane_manager", None)
        if not lane_manager:
            return ""
        try:
            return await lane_manager.get_recent_transcript(
                lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
                base_origin=chat_id,
                max_turns=4,
                max_age_seconds=max_age_seconds,
            )
        except TypeError as exc:
            if "max_age_seconds" not in str(exc):
                logger.debug(f"[{chat_id}] recent transcript load failed: {exc}")
                return ""
            try:
                return await lane_manager.get_recent_transcript(
                    lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
                    base_origin=chat_id,
                    max_turns=4,
                )
            except Exception as fallback_exc:
                logger.debug(f"[{chat_id}] recent transcript legacy load failed: {fallback_exc}")
                return ""
        except Exception as exc:
            logger.debug(f"[{chat_id}] recent transcript load failed: {exc}")
            return ""

    def _set_disable_rag_injection(self, ctx, disabled: bool) -> None:
        if not ctx:
            return
        if hasattr(ctx, "set"):
            ctx.set("disable_rag_injection", bool(disabled))
        elif hasattr(ctx, "shared_dict"):
            ctx.shared_dict["disable_rag_injection"] = bool(disabled)

    async def _build_planning_context(self, event: AstrMessageEvent, event_messages: List[AstrMessageEvent], chat_id: str):
        focus_context = self._coerce_focus_thread_context(event)
        focus_event = focus_context.focus_event or event
        thread_core_events = list(focus_context.core_events or [])
        thread_related_events = list(focus_context.related_events or [])
        ambient_events = list(focus_context.ambient_events or [])
        thread_root_event = focus_context.root_event
        is_lightweight_event = self._is_lightweight_event(focus_event, focus_context)
        if is_lightweight_event:
            thread_related_events = []
            ambient_events = []
            thread_core_events = [candidate for candidate in thread_core_events if candidate is focus_event]
        context_events = [
            candidate
            for candidate in ambient_events + thread_related_events + thread_core_events
            if candidate is not focus_event
        ]
        context_events.append(focus_event)

        window_lines = [self._render_event_line(message_event) for message_event in context_events]
        focus_message_text = self._build_focus_message_text(focus_event, focus_context)
        direct_context_text = self._build_direct_context_text(
            root_event=thread_root_event,
            focus_event=focus_event,
            core_events=thread_core_events,
        )
        related_context_text = self._build_related_context_text(
            focus_event=focus_event,
            related_events=thread_related_events,
        )
        ambient_background_text = self._build_ambient_background_text(ambient_events)
        raw_window_text = focus_message_text
        recent_transcript = "" if is_lightweight_event else await self._get_recent_dialogue_transcript(chat_id)
        last_assistant_reply = ""
        if recent_transcript:
            transcript_lines = [line.strip() for line in recent_transcript.splitlines() if line.strip()]
            bot_name = "Bot"
            nicknames = getattr(getattr(self.gateway.config, "system1", None), "nicknames", [])
            if isinstance(nicknames, list) and nicknames:
                bot_name = str(nicknames[0]).strip() or bot_name
            for line in reversed(transcript_lines):
                if line.startswith(f"{bot_name}:") or line.startswith("Bot:"):
                    last_assistant_reply = line.split(":", 1)[1].strip()
                    break

        near_context_priority = self._is_near_context_query(
            focus_message_text or event.message_str or raw_window_text
        )
        if is_lightweight_event:
            near_context_priority = True

        prompt_envelope = self._build_prompt_envelope(
            focus_context=focus_context,
            focus_message_text=focus_message_text,
            direct_context_text=direct_context_text,
            related_context_text=related_context_text,
            ambient_background_text=ambient_background_text,
            recent_transcript=recent_transcript,
            last_assistant_reply=last_assistant_reply,
            near_context_priority=near_context_priority,
        )
        if is_lightweight_event:
            prompt_envelope.guidance_lines.append("这是轻互动，只回应当前动作，不要接旧话题或复述历史。")
        event.set_extra("astrmai_lightweight_event", is_lightweight_event)
        event.set_extra("astrmai_focus_thread_context", focus_context)
        emit_legacy_prompt_envelope_extras(event, prompt_envelope, use_lane_history=True)

        if getattr(getattr(self.gateway.config, "global_settings", None), "debug_mode", False):
            logger.debug(
                f"[{chat_id}] trace={event.get_extra('astrmai_trace_id', '')} "
                f"planner raw_user_text={preview_text(focus_message_text, 160)!r} "
                f"direct_context={preview_text(direct_context_text, 160)!r} "
                f"related_context={preview_text(related_context_text, 160)!r} "
                f"background={preview_text(ambient_background_text, 160)!r} "
                f"recent_transcript={preview_text(recent_transcript, 160)!r} "
                f"near_context_priority={near_context_priority}"
            )

        return {
            "focus_context": focus_context,
            "context_events": context_events,
            "window_lines": window_lines,
            "prompt_envelope": prompt_envelope,
            "near_context_priority": near_context_priority,
            "is_lightweight_event": is_lightweight_event,
        }
