import json
import re
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from ...infrastructure.compat.legacy_compat import read_legacy_prompt_envelope
from ..contracts.prompt_envelope import PromptEnvelope, ReplyMode
from ..contracts.turn_context import MemoryInjectionDecision, ensure_turn_context, get_turn_context


class PromptRefiner:
    """轻量 PromptRefiner，只保留本轮注入，不再脚本化整段历史。"""

    def __init__(self, memory_engine, db_service=None, config=None, react_retriever=None):
        self.memory_engine = memory_engine
        self.db_service = db_service
        self.config = config
        self.react_retriever = react_retriever

    @staticmethod
    def _format_memory_block(memory_text: str) -> str:
        cleaned = str(memory_text or "").strip()
        if not cleaned:
            return ""
        return (
            "（内心浮现的印象，仅供我自己判断当下；原文不要逐字出现在回复里）\n"
            f"{cleaned}\n"
            "（印象结束——如果这些内容有帮助，我只会消化后用自己的话自然带过，绝不照搬原文）\n"
        )

    @staticmethod
    def _memory_preview(memory_text: str, limit: int = 160) -> str:
        cleaned = " ".join(str(memory_text or "").split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)] + "..."

    def _memory_policy_for_event(self, event: AstrMessageEvent) -> str:
        turn_context = get_turn_context(event)
        if turn_context is not None and str(turn_context.cognitive.memory_policy or "").strip():
            return str(turn_context.cognitive.memory_policy or "").strip()
        policy = event.get_extra("astrmai_cognitive_memory_policy", "light")
        return str(policy or "light").strip() or "light"

    @staticmethod
    def _think_level_for_event(event: AstrMessageEvent) -> int | None:
        value = event.get_extra("astrmai_think_level", None) if hasattr(event, "get_extra") else None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _has_memory_intent(text: str) -> bool:
        lowered = str(text or "").lower()
        keywords = {
            "记得",
            "刚才",
            "之前",
            "上次",
            "你还记得",
            "你记得",
            "remember",
            "last time",
            "earlier",
            "before",
        }
        return any(keyword in str(text or "") or keyword in lowered for keyword in keywords)

    @staticmethod
    def _is_deep_policy(policy: str) -> bool:
        return str(policy or "").strip().lower() == "deep"

    def _allow_react_retrieval(self, *, think_level: int | None, decision: MemoryInjectionDecision, current_query: str) -> bool:
        if think_level is None:
            return True
        if think_level >= 3:
            return True
        return think_level == 2 and self._is_deep_policy(decision.policy) and self._has_memory_intent(current_query)

    @staticmethod
    def _allow_proactive_recall_for_budget(
        *,
        think_level: int | None,
        lightweight_event: bool,
        disable_rag: bool,
        is_fast_mode: bool,
        near_context_priority: bool,
    ) -> bool:
        if lightweight_event or disable_rag or is_fast_mode or near_context_priority:
            return False
        if think_level is not None and think_level <= 0:
            return False
        return True

    @staticmethod
    def _build_time_anchor() -> str:
        now = datetime.now().astimezone()
        offset = now.strftime("%z")
        if offset:
            offset = f"{offset[:3]}:{offset[3:]}"
        absolute_time = f"{now.strftime('%Y-%m-%d %H:%M:%S')} {offset}".strip()
        return (
            f"现在是 {absolute_time}。\n"
            "除非对方主动提起刚才、之前、记得这些旧线索，我不把更早的对话当成眼前正在发生的事。"
        )

    @staticmethod
    def _normalize_dedup_key(line: str) -> str:
        normalized = str(line or "").strip().replace("：", ":")
        normalized = re.sub(r"\s+", " ", normalized)
        return normalized.rstrip("。.!！?？~～")

    @classmethod
    def _deduplicate_transcript(
        cls,
        transcript: str,
        higher_priority_texts: list[str],
        *,
        min_dedup_length: int = 6,
    ) -> str:
        if not transcript:
            return ""
        high_priority_keys: set[str] = set()
        for text in higher_priority_texts:
            for line in str(text or "").splitlines():
                key = cls._normalize_dedup_key(line)
                if key and len(key) > min_dedup_length:
                    high_priority_keys.add(key)
        if not high_priority_keys:
            return transcript

        kept_lines: list[str] = []
        for line in transcript.splitlines():
            key = cls._normalize_dedup_key(line)
            if not key or len(key) <= min_dedup_length or key not in high_priority_keys:
                kept_lines.append(line)
        return "\n".join(kept_lines).strip()

    async def _decide_memory_injection(
        self,
        event: AstrMessageEvent,
        prompt: str,
        prompt_envelope: PromptEnvelope | None = None,
        disable_rag: bool = False,
        is_fast_mode: bool = False,
        retrieve_keys: list[str] | None = None,
    ) -> tuple[MemoryInjectionDecision, str]:
        injection_service = getattr(self.memory_engine, "injection_service", None)
        if injection_service and hasattr(injection_service, "build_bundle"):
            bundle = await injection_service.build_bundle(
                event=event,
                prompt=prompt,
                prompt_envelope=prompt_envelope,
                disable_rag=disable_rag,
                is_fast_mode=is_fast_mode,
                retrieve_keys=retrieve_keys,
            )
            turn_context = ensure_turn_context(event)
            return turn_context.memory, str(getattr(bundle, "rendered_prompt_block", "") or "")

        retrieve_keys = retrieve_keys or []
        decision = MemoryInjectionDecision(
            policy=self._memory_policy_for_event(event),
            retrieve_keys=list(retrieve_keys),
        )
        think_level = self._think_level_for_event(event)
        if event.get_extra("astrmai_lightweight_event", False):
            decision.skip_reason = "lightweight_event"
            return decision, ""
        if isinstance(prompt_envelope, PromptEnvelope):
            if prompt_envelope.near_context_priority:
                decision.skip_reason = "near_context_priority"
                return decision, ""
            current_query = str(
                prompt_envelope.raw_user_text
                or prompt_envelope.focus_message_text
                or prompt_envelope.direct_context_text
                or prompt
                or event.message_str
                or ""
            ).strip()
        else:
            if event.get_extra("astrmai_near_context_priority", False):
                decision.skip_reason = "near_context_priority"
                return decision, ""
            current_query = str(
                prompt
                or event.get_extra("astrmai_focus_message_text", "")
                or event.get_extra("astrmai_raw_user_text", "")
                or event.message_str
                or ""
            ).strip()
        prompt_text = str(prompt or "").strip()
        if prompt_text and prompt_text not in current_query:
            current_query = f"{current_query}\n{prompt_text}" if current_query else prompt_text
        if not current_query:
            decision.skip_reason = "empty_query"
            return decision, ""
        if think_level is not None and think_level <= 0:
            decision.policy = "none"
            decision.skip_reason = "think_level_0"
            return decision, ""
        if think_level == 1 and not self._has_memory_intent(current_query):
            decision.skip_reason = "think_level_1_no_memory_intent"
            return decision, ""
        if disable_rag or is_fast_mode:
            decision.skip_reason = "disable_rag_injection" if disable_rag else "fast_mode"
            return decision, ""

        chat_id = event.unified_msg_origin
        react_result = ""
        enable_react = True
        if self.config and hasattr(self.config, "memory"):
            enable_react = self.config.memory.enable_react_agent
        if not self._allow_react_retrieval(
            think_level=think_level,
            decision=decision,
            current_query=current_query,
        ):
            enable_react = False

        if self.react_retriever and enable_react:
            try:
                react_result = await self.react_retriever.retrieve(
                    query=current_query,
                    chat_id=chat_id,
                    chat_context=prompt,
                    sender_name=event.get_sender_name() or "",
                    retrieve_keys=retrieve_keys,
                )
            except Exception as exc:
                logger.debug(f"[PromptRefiner] ReAct retrieve failed, trying plain recall: {exc}")

        if react_result:
            decision.source = "react"
            decision.injected = True
            decision.summary_preview = self._memory_preview(react_result)
            return decision, self._format_memory_block(react_result)

        if not self.memory_engine:
            decision.skip_reason = "memory_engine_unavailable"
            return decision, ""

        memory_text = await self.memory_engine.recall(current_query, session_id=chat_id)
        if memory_text and "什么也没想起来" not in memory_text:
            decision.source = "fallback_recall"
            decision.injected = True
            decision.summary_preview = self._memory_preview(memory_text)
            return decision, self._format_memory_block(memory_text)
        decision.skip_reason = "no_result"
        return decision, ""

    async def _build_memory_injection(
        self,
        event: AstrMessageEvent,
        prompt: str,
        prompt_envelope: PromptEnvelope | None = None,
        disable_rag: bool = False,
        is_fast_mode: bool = False,
        retrieve_keys: list[str] | None = None,
    ) -> str:
        _decision, rendered = await self._decide_memory_injection(
            event=event,
            prompt=prompt,
            prompt_envelope=prompt_envelope,
            disable_rag=disable_rag,
            is_fast_mode=is_fast_mode,
            retrieve_keys=retrieve_keys,
        )
        return rendered

    @staticmethod
    def _build_guidance_section(
        prompt_envelope: PromptEnvelope,
        *,
        style_variant: str,
        is_fast_mode: bool,
    ) -> str:
        lines: list[str] = []
        for line in list(prompt_envelope.guidance_lines or []):
            cleaned = str(line or "").strip()
            if cleaned and cleaned not in lines:
                lines.append(cleaned)

        reply_mode = prompt_envelope.reply_mode
        if not is_fast_mode and reply_mode == ReplyMode.EMOTIONAL_SUPPORT:
            lines.append("先安抚情绪，再自然往下接。")
        elif not is_fast_mode and reply_mode == ReplyMode.IMAGE_REACTION:
            lines.append("先短促接住画面反应，不要说太满。")
        elif not is_fast_mode and reply_mode == ReplyMode.PLAYFUL_INTERACTION:
            lines.append("保持轻松俏皮，像当场接梗一样回应。")

        if not is_fast_mode:
            lines.append("优先回应当前线索；历史只用于理解关系和避免重复，不要把旧话题续写下去。")

        rendered_lines = [f"- {line}" for line in lines if line]
        style_text = str(style_variant or "").strip()
        if style_text:
            rendered_lines.append(f"本轮风格：{style_text}")
        return "\n".join(rendered_lines).strip()

    async def _resolve_visual_memory(self, text: str) -> str:
        if not isinstance(text, str):
            return text

        picids = set(re.findall(r"\[picid:([a-fA-F0-9]{32})\]", text))
        if not picids or not self.db_service:
            return text

        for picid in picids:
            resolved_text = "[一张尚未看清的图片]"
            try:
                with self.db_service.get_session() as session:
                    from ...infrastructure.persistence import VisualMemory

                    mem = session.get(VisualMemory, picid)
                    if mem and mem.description:
                        try:
                            tags = json.loads(mem.emotion_tags)
                            tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                        except Exception:
                            tags_str = ""
                        if mem.type == "emoji":
                            resolved_text = (
                                f"[发了一个表情包，画面是：{mem.description}，传达了：{tags_str}]"
                                if tags_str
                                else f"[发了一个表情包，画面是：{mem.description}]"
                            )
                        else:
                            resolved_text = f"[发了一张图片，画面是：{mem.description}]"
            except Exception as exc:
                logger.debug(f"[PromptRefiner] visual memory resolve failed {picid}: {exc}")

            text = text.replace(f"[picid:{picid}]", resolved_text)

        return text

    async def refine_prompt(
        self,
        event: AstrMessageEvent,
        system_prompt: str,
        prompt: str = "",
        context=None,
        *,
        prompt_envelope: PromptEnvelope | None = None,
        style_variant: str = "",
        proactive_recall: str = "",
    ) -> tuple[str, str]:
        disable_rag = False
        if hasattr(context, "get"):
            disable_rag = context.get("disable_rag_injection")
        elif hasattr(context, "shared_dict"):
            disable_rag = context.shared_dict.get("disable_rag_injection", False)

        retrieve_keys = event.get_extra("retrieve_keys", [])
        if not isinstance(prompt_envelope, PromptEnvelope):
            prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        if not isinstance(prompt_envelope, PromptEnvelope):
            prompt_envelope = read_legacy_prompt_envelope(event, prompt=prompt)

        recent_transcript = prompt_envelope.recent_transcript.strip()
        raw_user_text = (prompt_envelope.raw_user_text or prompt).strip()
        focus_message_text = (prompt_envelope.focus_message_text or raw_user_text or prompt).strip()
        direct_context_text = prompt_envelope.direct_context_text.strip()
        related_context_text = prompt_envelope.related_context_text.strip()
        background_window_text = prompt_envelope.ambient_background_text.strip()
        focus_reason = prompt_envelope.focus_reason.strip()
        focus_thread_reason = (prompt_envelope.focus_thread_reason or focus_reason).strip()
        near_context_priority = bool(prompt_envelope.near_context_priority)
        use_lane_history = bool(event.get_extra("astrmai_use_lane_history", False))
        is_fast_mode = "CORE_ONLY" in retrieve_keys

        memory_decision, injection = await self._decide_memory_injection(
            event=event,
            prompt=prompt,
            prompt_envelope=prompt_envelope if isinstance(prompt_envelope, PromptEnvelope) else None,
            disable_rag=disable_rag,
            is_fast_mode=is_fast_mode,
            retrieve_keys=retrieve_keys,
        )
        lightweight_event = bool(event.get_extra("astrmai_lightweight_event", False))
        allow_proactive_recall = self._allow_proactive_recall_for_budget(
            think_level=self._think_level_for_event(event),
            lightweight_event=lightweight_event,
            disable_rag=bool(disable_rag),
            is_fast_mode=bool(is_fast_mode),
            near_context_priority=near_context_priority,
        )
        effective_proactive_recall = proactive_recall if allow_proactive_recall else ""
        if effective_proactive_recall:
            memory_decision.source = (
                f"proactive_recall+{memory_decision.source}"
                if memory_decision.source
                else "proactive_recall"
            )
            memory_decision.injected = True
            memory_decision.skip_reason = ""
            proactive_preview = self._memory_preview(effective_proactive_recall)
            memory_decision.summary_preview = (
                f"{memory_decision.summary_preview} | {proactive_preview}"
                if memory_decision.summary_preview
                else proactive_preview
            )
        ensure_turn_context(event).memory = memory_decision

        if background_window_text and near_context_priority:
            background_lines = [line for line in background_window_text.splitlines() if line.strip()]
            background_window_text = "\n".join(background_lines[-1:])

        recent_transcript = self._deduplicate_transcript(
            recent_transcript,
            [
                focus_message_text,
                direct_context_text,
                related_context_text,
                background_window_text,
            ],
            min_dedup_length=6,
        )

        final_system_prompt = await self._resolve_visual_memory(system_prompt)

        sections = [self._build_time_anchor()]
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            proactive_guidance = str(event.get_extra("astrmai_proactive_guidance", "") or "").strip()
            if proactive_guidance:
                sections.append(
                    "---主动开口参考（仅供内心判断，不要复述）---\n"
                    "这是一次内部开口候选。只有在自然、合适、不打扰时才说一句短回复；"
                    "如果不自然，可以保持沉默。不要提到系统机制或这段指引。\n"
                    + proactive_guidance[:500]
                )
        if recent_transcript:
            sections.append(f"---对话记录---\n{await self._resolve_visual_memory(recent_transcript)}")
        if focus_message_text:
            sections.append(f"---眼前正在对我说的---\n{await self._resolve_visual_memory(focus_message_text)}")
        if direct_context_text:
            sections.append(f"---前因---\n{await self._resolve_visual_memory(direct_context_text)}")
        if related_context_text:
            sections.append(f"---补充---\n{await self._resolve_visual_memory(related_context_text)}")
        if background_window_text:
            sections.append(f"---旁边在聊的---\n{await self._resolve_visual_memory(background_window_text)}")

        memory_parts = []
        if effective_proactive_recall:
            memory_parts.append(await self._resolve_visual_memory(effective_proactive_recall))
        if injection:
            memory_parts.append(await self._resolve_visual_memory(injection))
        if memory_parts:
            sections.append(
                "---记忆闪回（仅供内心参考，不要出现在回复正文中）---\n"
                + "\n".join(part for part in memory_parts if part).strip()
            )

        guidance_section = self._build_guidance_section(
            prompt_envelope,
            style_variant=style_variant,
            is_fast_mode=is_fast_mode,
        )
        if guidance_section:
            sections.append(f"---本轮指引---\n{guidance_section}")

        final_prompt = "\n\n".join(section for section in sections if section).strip()

        if getattr(getattr(self.config, "global_settings", None), "debug_mode", False):
            logger.debug(
                f"[{event.unified_msg_origin}] PromptRefiner preview "
                f"raw_user_text={raw_user_text[:120]!r} "
                f"focus_message={focus_message_text[:160]!r} "
                f"direct_context={direct_context_text[:120]!r} "
                f"related_context={related_context_text[:120]!r} "
                f"background={background_window_text[:120]!r} "
                f"recent_transcript={recent_transcript[:160]!r} "
                f"focus_reason={focus_reason!r} "
                f"focus_thread_reason={focus_thread_reason!r} "
                f"near_context_priority={near_context_priority}"
            )

        logger.info(
            f"[{event.unified_msg_origin}] PromptRefiner ready "
            f"(lane_history={use_lane_history}, inject_memory={'yes' if (injection or effective_proactive_recall) else 'no'}, "
            f"near_context_priority={near_context_priority}, recent_transcript={'yes' if recent_transcript else 'no'})"
        )
        return final_system_prompt, final_prompt
