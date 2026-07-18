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
    def _is_followup_question_like(message_text: str) -> bool:
        normalized = str(message_text or "").strip().lower()
        if not normalized:
            return False
        followup_tokens = (
            "should",
            "can",
            "could",
            "why",
            "how",
            "still",
            "continue",
            "exact mainline",
            "without drifting",
            "right after compaction",
            "keep the compaction mainline",
            "delay compaction",
            "reply chain",
            "follow the exact mainline",
            "截图",
            "图片",
        )
        return any(token in normalized for token in followup_tokens)

    @staticmethod
    def _looks_like_vision_mainline_query(message_text: str) -> bool:
        normalized = str(message_text or "").strip().lower()
        if not normalized:
            return False
        return any(token in normalized for token in ("screenshot", "image", "picture", "photo", "截图", "图片"))

    @staticmethod
    def _has_recent_tail_context(recent_transcript: str) -> bool:
        lines = [line.strip() for line in str(recent_transcript or "").splitlines() if line.strip()]
        return len(lines) >= 2

    @staticmethod
    def _keyword_tokens(text: str) -> set[str]:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return set()
        for token in ",.!?;:()[]{}<>\"'`~@#%^&*-_=+/\\|\t\r\n":
            normalized = normalized.replace(token, " ")
        return {
            part
            for part in normalized.split(" ")
            if len(part) >= 4
        }

    @classmethod
    def _recent_tail_stays_on_followup_chain(cls, focus_message_text: str, recent_transcript: str) -> bool:
        lines = [line.strip() for line in str(recent_transcript or "").splitlines() if line.strip()]
        if len(lines) < 2:
            return False
        tail_lines = lines[-2:]
        focus_tokens = cls._keyword_tokens(focus_message_text)
        if not focus_tokens:
            return False
        tail_token_hits = 0
        for line in tail_lines:
            if cls._keyword_tokens(line) & focus_tokens:
                tail_token_hits += 1
        if tail_token_hits >= 2:
            return True
        if tail_token_hits == 1:
            joined_tail = " ".join(tail_lines).lower()
            return any(token in joined_tail for token in ("astrmai", "mai", "bot", "reply", "回覆", "回复"))
        return False

    @staticmethod
    def _warm_mentions_visual_context(warm_bundle) -> bool:
        summary = str(getattr(warm_bundle, "summary_text", "") or "").lower()
        quotes = str(getattr(warm_bundle, "quote_text", "") or "").lower()
        combined = f"{summary}\n{quotes}"
        return any(token in combined for token in ("screenshot", "image", "picture", "photo", "截图", "图片", "图文"))

    @staticmethod
    def _render_event_line(message_event: AstrMessageEvent) -> str:
        return MessageRenderer.render_event(message_event)

    @staticmethod
    def _is_group_event(message_event: AstrMessageEvent) -> bool:
        try:
            if str(message_event.get_group_id() or "").strip():
                return True
        except Exception:
            pass
        return "GroupMessage" in str(getattr(message_event, "unified_msg_origin", "") or "")

    @staticmethod
    def _safe_event_sender_id(message_event: AstrMessageEvent) -> str:
        try:
            return str(message_event.get_sender_id() or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _safe_event_sender_name(message_event: AstrMessageEvent) -> str:
        try:
            return str(message_event.get_sender_name() or "").strip()
        except Exception:
            return ""

    @classmethod
    def _build_current_speaker_block(
        cls,
        focus_event: AstrMessageEvent,
        focus_context: FocusThreadContext,
        *,
        is_lightweight_event: bool,
    ) -> str:
        sender_id = str(getattr(focus_context, "focus_sender_id", "") or "").strip() or cls._safe_event_sender_id(focus_event)
        sender_name = str(getattr(focus_context, "focus_sender_name", "") or "").strip() or cls._safe_event_sender_name(focus_event)
        chat_id = str(getattr(focus_event, "unified_msg_origin", "") or "").strip()
        if not sender_id and not sender_name:
            return ""

        vision_bundle = getattr(focus_context, "vision_bundle", None)
        has_image = bool(
            list(getattr(vision_bundle, "image_urls", []) or [])
            or list(getattr(vision_bundle, "direct_image_urls", []) or [])
        )
        interaction_kind = str(focus_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        message_kind = "interaction" if interaction_kind else "image" if has_image else "text"
        lines = [
            "本轮正在回应的对象只看这一位：",
            f"- QQ: {sender_id or 'unknown'}",
            f"- 昵称: {sender_name or '群友'}",
            f"- 会话: {chat_id or 'unknown'}",
            f"- 消息类型: {message_kind}",
            "历史里的其他发言人只是背景，不能当作当前用户，也不要把他们的名字、身份或关系套到当前用户身上。",
        ]
        if cls._is_group_event(focus_event):
            lines.append("这是群聊；如果前文出现过其他群友，本轮仍只把眼前这条消息归因给上述 QQ。")
        if is_lightweight_event or has_image or interaction_kind:
            lines.append("这是弱文本/图片/互动输入；除非当前图片或当前消息明确给出，不要从旧对话里补出其他人的名字。")
        return "\n".join(lines)

    @staticmethod
    def _is_lightweight_event(message_event: AstrMessageEvent, focus_context: FocusThreadContext) -> bool:
        interaction_kind = str(message_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        if message_event.get_extra("is_virtual_poke", False) or interaction_kind in {"poke", "peer_poke"}:
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
        recent_transcript_source: str,
        recent_transcript_reason: str,
        warm_zone_transcript: str,
        warm_zone_transcript_source: str,
        warm_zone_summary: str,
        warm_zone_quotes: str,
        warm_topics_preview: str,
        warm_zone_has_latest_assistant: bool,
        warm_zone_quote_event_ids: list[str],
        last_assistant_reply: str,
        current_speaker_block: str,
        near_context_priority: bool,
    ) -> PromptEnvelope:
        return PromptEnvelope(
            raw_user_text=focus_message_text,
            recent_transcript=recent_transcript,
            recent_transcript_source=recent_transcript_source,
            recent_transcript_reason=recent_transcript_reason,
            warm_zone_transcript=warm_zone_transcript,
            warm_zone_transcript_source=warm_zone_transcript_source,
            warm_zone_summary=warm_zone_summary,
            warm_zone_quotes=warm_zone_quotes,
            warm_topics_preview=warm_topics_preview,
            warm_zone_has_latest_assistant=warm_zone_has_latest_assistant,
            warm_zone_quote_event_ids=list(warm_zone_quote_event_ids or []),
            last_assistant_reply=last_assistant_reply,
            current_speaker_block=current_speaker_block,
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

    async def _get_warm_context_bundle(self, chat_id: str):
        context_engine = getattr(self, "context_engine", None)
        context_db = getattr(context_engine, "db", None)
        store = (
            getattr(self, "dialogue_store", None)
            or getattr(context_db, "dialogue_store", None)
            or getattr(self.gateway, "dialogue_store", None)
            or getattr(getattr(self.gateway, "db_service", None), "dialogue_store", None)
        )
        if not store:
            return None
        try:
            return await store.get_warm_context_bundle(chat_id, max_age_seconds=300.0, max_tokens=1200)
        except Exception as exc:
            logger.debug(f"[{chat_id}] warm transcript load failed: {exc}")
            return None

    async def _get_compaction_trace_status(self, chat_id: str, focus_context: FocusThreadContext | None):
        compaction = getattr(self, "context_compaction", None)
        if compaction is None or not hasattr(compaction, "get_trace_status"):
            return {}
        try:
            return await compaction.get_trace_status(chat_id, focus_context=focus_context)
        except Exception as exc:
            logger.debug(f"[{chat_id}] compaction trace load failed: {exc}")
            return {}

    def _should_include_recent_transcript(
        self,
        focus_message_text: str,
        warm_bundle,
        recent_transcript: str,
        *,
        post_compaction_recovery_rounds: int = 0,
    ) -> tuple[bool, str]:
        if not str(recent_transcript or "").strip():
            return False, "empty"
        if warm_bundle is None:
            return True, "warm_empty"
        warm_summary = str(getattr(warm_bundle, "summary_text", "") or "").strip()
        warm_quotes = str(getattr(warm_bundle, "quote_text", "") or "").strip()
        if not warm_summary and not warm_quotes:
            return True, "warm_empty"
        if post_compaction_recovery_rounds > 0:
            return True, "post_compaction_recovery"
        if not bool(getattr(warm_bundle, "has_latest_assistant", False)) and self._is_near_context_query(focus_message_text):
            return True, "missing_latest_assistant"
        if (
            self._looks_like_vision_mainline_query(focus_message_text)
            and self._warm_mentions_visual_context(warm_bundle)
            and self._has_recent_tail_context(recent_transcript)
        ):
            return True, "vision_mainline_recent"
        if (
            bool(getattr(warm_bundle, "has_latest_assistant", False))
            and self._is_followup_question_like(focus_message_text)
            and self._has_recent_tail_context(recent_transcript)
            and self._recent_tail_stays_on_followup_chain(focus_message_text, recent_transcript)
        ):
            return True, "tail_followup_recent"
        return False, "warm_sufficient"

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
        warm_bundle = None if is_lightweight_event else await self._get_warm_context_bundle(chat_id)
        warm_zone_summary = "" if warm_bundle is None else str(getattr(warm_bundle, "summary_text", "") or "").strip()
        warm_zone_quotes = "" if warm_bundle is None else str(getattr(warm_bundle, "quote_text", "") or "").strip()
        warm_topics_preview = "" if warm_bundle is None else str(getattr(warm_bundle, "topic_preview", "") or "").strip()
        warm_zone_quote_event_ids = [] if warm_bundle is None else list(getattr(warm_bundle, "quote_event_ids", []) or [])
        warm_zone_has_latest_assistant = bool(getattr(warm_bundle, "has_latest_assistant", False)) if warm_bundle is not None else False
        warm_zone_transcript = "\n".join(part for part in (warm_zone_summary, warm_zone_quotes) if part).strip()
        recent_transcript = "" if is_lightweight_event else await self._get_recent_dialogue_transcript(chat_id)
        compaction_trace_status = {} if is_lightweight_event else await self._get_compaction_trace_status(chat_id, focus_context)
        post_compaction_recovery_rounds = int(compaction_trace_status.get("post_compaction_recovery_rounds", 0) or 0)
        include_recent, recent_transcript_reason = self._should_include_recent_transcript(
            focus_message_text or event.message_str or raw_window_text,
            warm_bundle,
            recent_transcript,
            post_compaction_recovery_rounds=post_compaction_recovery_rounds,
        )
        if not include_recent:
            recent_transcript = ""
        last_assistant_reply = ""
        transcript_source = warm_zone_transcript or recent_transcript
        warm_zone_transcript_source = "store" if warm_zone_transcript else "empty"
        recent_transcript_source = "lane_fallback" if recent_transcript else "empty"
        if transcript_source:
            transcript_lines = [line.strip() for line in transcript_source.splitlines() if line.strip()]
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
        current_speaker_block = self._build_current_speaker_block(
            focus_event,
            focus_context,
            is_lightweight_event=is_lightweight_event,
        )

        prompt_envelope = self._build_prompt_envelope(
            focus_context=focus_context,
            focus_message_text=focus_message_text,
            direct_context_text=direct_context_text,
            related_context_text=related_context_text,
            ambient_background_text=ambient_background_text,
            recent_transcript=recent_transcript,
            recent_transcript_source=recent_transcript_source,
            recent_transcript_reason=recent_transcript_reason,
            warm_zone_transcript=warm_zone_transcript,
            warm_zone_transcript_source=warm_zone_transcript_source,
            warm_zone_summary=warm_zone_summary,
            warm_zone_quotes=warm_zone_quotes,
            warm_topics_preview=warm_topics_preview,
            warm_zone_has_latest_assistant=warm_zone_has_latest_assistant,
            warm_zone_quote_event_ids=warm_zone_quote_event_ids,
            last_assistant_reply=last_assistant_reply,
            current_speaker_block=current_speaker_block,
            near_context_priority=near_context_priority,
        )
        if is_lightweight_event:
            prompt_envelope.guidance_lines.append("这是轻互动，只回应当前动作，不要接旧话题或复述历史。")
        if current_speaker_block and (is_lightweight_event or near_context_priority):
            prompt_envelope.guidance_lines.append("本轮先遵守当前发言人边界；不要把近期脉络中的其他人名当作当前用户。")
        interaction_kind = str(focus_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        if interaction_kind == "poke":
            poke_hint = str(focus_event.get_extra("astrmai_poke_reply_hint", "") or "").strip()
            poke_intent = str(focus_event.get_extra("astrmai_poke_intent", "") or "").strip()
            poke_streak = int(focus_event.get_extra("astrmai_poke_streak_count", 0) or 0)
            if poke_hint:
                prompt_envelope.guidance_lines.append(f"戳一戳互动策略：{poke_hint}")
            if poke_intent:
                prompt_envelope.guidance_lines.append(
                    f"戳一戳语境：intent={poke_intent}, streak={poke_streak}。回复保持短促、鲜活，不要超过两句话。"
                )
        elif interaction_kind == "peer_poke":
            actor = str(focus_event.get_extra("astrmai_interaction_actor_display_name", "") or "").strip()
            target = str(focus_event.get_extra("astrmai_interaction_target_display_name", "") or "").strip()
            join_allowed = bool(focus_event.get_extra("astrmai_peer_poke_join_allowed", False))
            poke_hint = str(focus_event.get_extra("astrmai_poke_reply_hint", "") or "").strip()
            prompt_envelope.guidance_lines.append(
                f"群友互戳语境：{actor or '有人'} 戳了 {target or '另一位群友'}。这不是你被戳，你只是旁观者。"
            )
            prompt_envelope.guidance_lines.append(
                "如果回应，只能轻轻围观或吐槽这次群友互动；禁止说“谁戳我/谁戳妃爱/我被戳了”。"
            )
            if join_allowed:
                prompt_envelope.guidance_lines.append("若语境很自然，可以调用戳一戳工具加入互动，但只能戳发起者或被戳者。")
            else:
                prompt_envelope.guidance_lines.append("本轮不适合加入互戳，只能选择忽略或短句旁观。")
            if poke_hint:
                prompt_envelope.guidance_lines.append(f"群友互戳互动策略：{poke_hint}")
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
                f"warm_transcript={preview_text(warm_zone_transcript, 160)!r} "
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
