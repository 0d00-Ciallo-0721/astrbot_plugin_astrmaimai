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

    FLEX_CONTEXT_BUDGET_CHARS = 1600
    WARM_CONTEXT_MIN_CHARS = 180
    RECENT_CONTEXT_MIN_CHARS = 280
    MEMORY_CONTEXT_MIN_CHARS = 120
    MEMORY_PREVIEW_TARGET_CHARS = 180
    SOFT_BACKGROUND_BUDGET_CHARS = 420
    SOFT_BACKGROUND_MIN_SECTION_CHARS = 80
    SOFT_BACKGROUND_FAST_MODE_BUDGET = 0
    SOFT_BACKGROUND_NEAR_CONTEXT_BUDGET = 0
    RUNTIME_GUIDANCE_MAX_CHARS = 360
    SOFT_BACKGROUND_PRIORITY_ORDER = (
        "cold_summary",
        "stable_state",
        "stable_behavior_rules",
        "stable_private_chat",
        "stable_expression",
        "stable_slang",
        "stable_jargon",
    )
    SOFT_BACKGROUND_TRIM_ORDER = (
        "stable_jargon",
        "stable_slang",
        "stable_expression",
        "stable_private_chat",
        "stable_behavior_rules",
        "stable_state",
        "cold_summary",
    )

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

    @staticmethod
    def _truncate_soft_background_text(text: str, budget_chars: int) -> str:
        cleaned = str(text or "").strip()
        budget = max(0, int(budget_chars or 0))
        if not cleaned or budget <= 0:
            return ""
        if len(cleaned) <= budget:
            return cleaned
        if budget <= 3:
            return cleaned[:budget]
        return cleaned[: budget - 3].rstrip() + "..."

    @staticmethod
    def _truncate_from_tail(text: str, budget_chars: int) -> str:
        cleaned = str(text or "").strip()
        budget = max(0, int(budget_chars or 0))
        if not cleaned or budget <= 0:
            return ""
        if len(cleaned) <= budget:
            return cleaned
        if budget <= 3:
            return cleaned[-budget:]
        return "..." + cleaned[-(budget - 3) :].lstrip()

    @staticmethod
    def _truncate_memory_text(text: str, budget_chars: int) -> str:
        cleaned = " ".join(str(text or "").split())
        budget = max(0, int(budget_chars or 0))
        if not cleaned or budget <= 0:
            return ""
        if len(cleaned) <= budget:
            return cleaned
        if budget <= 3:
            return cleaned[:budget]
        return cleaned[: budget - 3].rstrip() + "..."

    @classmethod
    def _render_runtime_guidance_cluster(
        cls,
        *,
        cognitive_drive_block: str,
        situational_context_block: str,
        planner_runtime_instruction_block: str,
    ) -> str:
        parts: list[str] = []
        if cognitive_drive_block:
            parts.append(f"---???????---\n{cognitive_drive_block}")
        if situational_context_block:
            parts.append(f"---褰撳墠鐘舵€佷笌绾︽潫---\n{situational_context_block}")
        if planner_runtime_instruction_block:
            parts.append(f"---鏈疆涓婁笅鏂囪В閲?--\n{planner_runtime_instruction_block}")
        if not parts:
            return ""
        return cls._truncate_soft_background_text("\n\n".join(parts), cls.RUNTIME_GUIDANCE_MAX_CHARS)

    def _resolve_soft_background_budget(
        self,
        *,
        is_fast_mode: bool,
        near_context_priority: bool,
    ) -> int:
        if is_fast_mode:
            return self.SOFT_BACKGROUND_FAST_MODE_BUDGET
        if near_context_priority:
            return self.SOFT_BACKGROUND_NEAR_CONTEXT_BUDGET
        return self.SOFT_BACKGROUND_BUDGET_CHARS

    def _render_soft_background_sections(
        self,
        prompt_envelope: PromptEnvelope,
        *,
        is_fast_mode: bool,
        near_context_priority: bool,
    ) -> tuple[str, dict[str, object]]:
        budget = self._resolve_soft_background_budget(
            is_fast_mode=is_fast_mode,
            near_context_priority=near_context_priority,
        )
        if budget <= 0:
            reason = "fast_mode" if is_fast_mode else "near_context_priority" if near_context_priority else "budget_zero"
            return "", {
                "budget_chars": budget,
                "trimmed_sections": [],
                "rendered_chars": 0,
                "skipped_reason": reason,
            }

        raw_sections = dict(getattr(prompt_envelope, "soft_background_sections", {}) or {})
        ordered_sections: list[tuple[str, str]] = []
        for key in self.SOFT_BACKGROUND_PRIORITY_ORDER:
            value = str(raw_sections.get(key, "") or "").strip()
            if value:
                ordered_sections.append((key, value))
        if not ordered_sections:
            fallback_text = str(getattr(prompt_envelope, "soft_background_block", "") or "").strip()
            if not fallback_text:
                return "", {
                    "budget_chars": budget,
                    "trimmed_sections": [],
                    "rendered_chars": 0,
                    "skipped_reason": "empty",
                }
            ordered_sections.append(("soft_background", fallback_text))

        kept: dict[str, str] = {key: value for key, value in ordered_sections}
        trimmed_sections: list[str] = []

        def _render_text() -> str:
            preferred = [
                kept[key]
                for key in self.SOFT_BACKGROUND_PRIORITY_ORDER
                if key in kept and kept[key]
            ]
            if preferred:
                return "\n\n".join(preferred)
            return "\n\n".join(
                value for key, value in ordered_sections if key in kept and kept[key]
            )

        rendered = _render_text()
        for key in self.SOFT_BACKGROUND_TRIM_ORDER:
            if len(rendered) <= budget:
                break
            value = kept.get(key, "")
            if not value:
                continue
            if key != "cold_summary":
                kept.pop(key, None)
                trimmed_sections.append(key)
                rendered = _render_text()
                continue
            kept[key] = self._truncate_soft_background_text(
                value,
                max(self.SOFT_BACKGROUND_MIN_SECTION_CHARS, budget),
            )
            trimmed_sections.append(f"{key}:truncated")
            rendered = _render_text()

        if len(rendered) > budget:
            last_key = next(
                (key for key in reversed(self.SOFT_BACKGROUND_PRIORITY_ORDER) if key in kept and kept[key]),
                "",
            )
            if not last_key:
                last_key = next((key for key, value in reversed(ordered_sections) if key in kept and kept[key]), "")
            if last_key:
                current_value = kept.get(last_key, "")
                other_parts = [
                    kept[key]
                    for key in self.SOFT_BACKGROUND_PRIORITY_ORDER
                    if key != last_key and key in kept and kept[key]
                ]
                other_text = "\n\n".join(other_parts)
                separator_len = 2 if other_text and current_value else 0
                remaining_budget = max(
                    self.SOFT_BACKGROUND_MIN_SECTION_CHARS,
                    budget - len(other_text) - separator_len,
                )
                kept[last_key] = self._truncate_soft_background_text(current_value, remaining_budget)
                trimmed_sections.append(f"{last_key}:truncated")
                rendered = _render_text()

        rendered = self._truncate_soft_background_text(rendered, budget)
        return rendered, {
            "budget_chars": budget,
            "trimmed_sections": trimmed_sections,
            "rendered_chars": len(rendered),
            "skipped_reason": "" if rendered else "trimmed_to_empty",
        }

    def _render_memory_block_for_budget(
        self,
        *,
        proactive_recall: str,
        injection: str,
    ) -> tuple[str, dict[str, object]]:
        proactive_text = str(proactive_recall or "").strip()
        injection_text = str(injection or "").strip()
        memory_parts = [part for part in (proactive_text, injection_text) if part]
        rendered = "\n".join(memory_parts).strip()
        return rendered, {
            "rendered_chars": len(rendered),
            "trimmed_sections": [],
            "proactive_recall": proactive_text,
            "injection": injection_text,
            "sections": {
                key: value
                for key, value in (
                    ("proactive_recall", proactive_text),
                    ("memory_injection", injection_text),
                )
                if value
            },
        }

    def _render_warm_context_for_budget(self, prompt_envelope: PromptEnvelope) -> tuple[str, dict[str, object]]:
        warm_summary = str(getattr(prompt_envelope, "warm_zone_summary", "") or "").strip()
        warm_quotes = str(getattr(prompt_envelope, "warm_zone_quotes", "") or "").strip()
        warm_transcript = str(getattr(prompt_envelope, "warm_zone_transcript", "") or "").strip()
        if warm_summary or warm_quotes:
            rendered = "\n".join(part for part in (warm_summary, warm_quotes) if part).strip()
        else:
            rendered = warm_transcript
        return rendered, {
            "rendered_chars": len(rendered),
            "trimmed_sections": [],
            "warm_summary": warm_summary,
            "warm_quotes": warm_quotes,
        }

    def _apply_flexible_context_budget(
        self,
        *,
        focus_text: str,
        direct_text: str,
        warm_text: str,
        warm_meta: dict[str, object],
        recent_text: str,
        memory_text: str,
        memory_meta: dict[str, object],
        soft_background_text: str,
    ) -> dict[str, object]:
        trimmed_sections: list[str] = []
        budget = self.FLEX_CONTEXT_BUDGET_CHARS
        warm_rendered = str(warm_text or "").strip()
        recent_rendered = str(recent_text or "").strip()
        memory_rendered = str(memory_text or "").strip()
        soft_background_rendered = str(soft_background_text or "").strip()

        def _combined_length() -> int:
            return sum(
                len(part)
                for part in (warm_rendered, recent_rendered, memory_rendered, soft_background_rendered)
                if part
            )

        if soft_background_rendered and _combined_length() > budget:
            soft_background_rendered = ""
            trimmed_sections.append("soft_background")

        if warm_rendered and _combined_length() > budget:
            warm_summary = str((warm_meta or {}).get("warm_summary", "") or "").strip()
            warm_quotes = str((warm_meta or {}).get("warm_quotes", "") or "").strip()
            if warm_quotes:
                warm_quotes = ""
                trimmed_sections.append("warm_quotes")
            warm_rendered = "\n".join(part for part in (warm_summary, warm_quotes) if part).strip() or warm_rendered
            if len(warm_rendered) > self.WARM_CONTEXT_MIN_CHARS:
                remaining_budget = max(
                    self.WARM_CONTEXT_MIN_CHARS,
                    budget - sum(
                        len(part)
                        for part in (recent_rendered, memory_rendered, soft_background_rendered)
                        if part
                    ),
                )
                if len(warm_rendered) > remaining_budget:
                    warm_rendered = self._truncate_soft_background_text(
                        warm_rendered,
                        max(self.WARM_CONTEXT_MIN_CHARS, remaining_budget),
                    )
                    trimmed_sections.append("warm_summary:truncated")

        if memory_rendered and _combined_length() > budget:
            proactive_text = str((memory_meta or {}).get("proactive_recall", "") or "").strip()
            injection_text = str((memory_meta or {}).get("injection", "") or "").strip()
            proactive_preview = self._memory_preview(proactive_text, self.MEMORY_PREVIEW_TARGET_CHARS) if proactive_text else ""
            injection_preview = self._memory_preview(injection_text, self.MEMORY_PREVIEW_TARGET_CHARS) if injection_text else ""
            memory_rendered = "\n".join(part for part in (proactive_preview, injection_preview) if part).strip()
            trimmed_sections.append("memory:preview")
            if memory_rendered and _combined_length() > budget and len(memory_rendered) > self.MEMORY_CONTEXT_MIN_CHARS:
                remaining_budget = max(
                    self.MEMORY_CONTEXT_MIN_CHARS,
                    budget - sum(
                        len(part)
                        for part in (warm_rendered, recent_rendered, soft_background_rendered)
                        if part
                    ),
                )
                if len(memory_rendered) > remaining_budget:
                    memory_rendered = self._truncate_memory_text(
                        memory_rendered,
                        max(self.MEMORY_CONTEXT_MIN_CHARS, remaining_budget),
                    )
                    trimmed_sections.append("memory:truncated")

        if recent_rendered and _combined_length() > budget:
            remaining_budget = max(
                self.RECENT_CONTEXT_MIN_CHARS,
                budget - sum(
                    len(part)
                    for part in (warm_rendered, memory_rendered, soft_background_rendered)
                    if part
                ),
            )
            if len(recent_rendered) > remaining_budget:
                recent_rendered = self._truncate_from_tail(
                    recent_rendered,
                    max(self.RECENT_CONTEXT_MIN_CHARS, remaining_budget),
                )
                trimmed_sections.append("recent:tail_truncated")

        return {
            "budget_chars": budget,
            "trimmed_sections": trimmed_sections,
            "protected_sections": [
                key
                for key, value in (
                    ("focus_message", str(focus_text or "").strip()),
                    ("direct_context", str(direct_text or "").strip()),
                )
                if value
            ],
            "warm_text": warm_rendered,
            "recent_text": recent_rendered,
            "memory_text": memory_rendered,
            "soft_background_text": soft_background_rendered,
            "warm_rendered_chars": len(warm_rendered),
            "recent_rendered_chars": len(recent_rendered),
            "memory_rendered_chars": len(memory_rendered),
        }

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
    def _should_include_time_anchor(
        *,
        event: AstrMessageEvent,
        prompt_text: str,
        focus_message_text: str,
        direct_context_text: str,
    ) -> bool:
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            return True
        turn_context = get_turn_context(event)
        if turn_context is not None and int(getattr(turn_context.continuity, "post_compaction_recovery_rounds", 0) or 0) > 0:
            return True
        query_text = "\n".join(
            part
            for part in (
                str(prompt_text or "").strip(),
                str(focus_message_text or "").strip(),
                str(direct_context_text or "").strip(),
                str(getattr(event, "message_str", "") or "").strip(),
                str(event.get_extra("astrmai_proactive_guidance", "") or "").strip(),
                str(event.get_extra("astrmai_wait_resume_thought", "") or "").strip(),
                str(event.get_extra("astrmai_stale_recovery_reason", "") or "").strip(),
                str(event.get_extra("astrmai_resume_reason", "") or "").strip(),
            )
            if part
        )
        lowered = query_text.lower()
        relative_tokens = (
            "刚才",
            "之前",
            "多久",
            "今天",
            "明天",
            "昨天",
            "待会",
            "一会",
            "稍后",
            "remember",
            "earlier",
            "just now",
            "today",
            "tomorrow",
            "yesterday",
            "how long",
        )
        if any(token in query_text or token in lowered for token in relative_tokens):
            return True
        schedule_tokens = (
            "提醒",
            "日程",
            "定时",
            "安排",
            "闹钟",
            "schedule",
            "remind",
            "alarm",
            "calendar",
        )
        if any(token in query_text or token in lowered for token in schedule_tokens):
            return True
        return any(
            str(event.get_extra(key, "") or "").strip()
            for key in (
                "astrmai_wait_resume_thought",
                "astrmai_stale_recovery_reason",
                "astrmai_resume_reason",
            )
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
        injection_service = getattr(self.memory_engine, "injection_service", None) if self.memory_engine else None
        if injection_service is None and self.memory_engine:
            retrieval_service = getattr(self.memory_engine, "retrieval_service", None)
            if retrieval_service:
                from ...memory.services.memory_injection_service import MemoryInjectionService

                injection_service = MemoryInjectionService(retrieval_service, config=self.config)
        if not injection_service or not hasattr(injection_service, "build_bundle"):
            decision.skip_reason = "memory_injection_service_unavailable"
            return decision, ""

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
        recent_transcript_source = getattr(prompt_envelope, "recent_transcript_source", "").strip()
        recent_transcript_reason = getattr(prompt_envelope, "recent_transcript_reason", "").strip()
        warm_zone_transcript = getattr(prompt_envelope, "warm_zone_transcript", "").strip()
        warm_zone_transcript_source = getattr(prompt_envelope, "warm_zone_transcript_source", "").strip()
        warm_zone_summary = getattr(prompt_envelope, "warm_zone_summary", "").strip()
        warm_zone_quotes = getattr(prompt_envelope, "warm_zone_quotes", "").strip()
        warm_context_for_dedup = "\n".join(part for part in (warm_zone_summary, warm_zone_quotes) if part).strip()
        if not warm_context_for_dedup:
            warm_context_for_dedup = warm_zone_transcript
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
        cognitive_drive_block = str(getattr(prompt_envelope, "cognitive_drive_block", "") or "").strip()
        situational_context_block = str(getattr(prompt_envelope, "situational_context_block", "") or "").strip()
        planner_runtime_instruction_block = str(getattr(prompt_envelope, "planner_runtime_instruction_block", "") or "").strip()
        soft_background_block, soft_background_meta = self._render_soft_background_sections(
            prompt_envelope,
            is_fast_mode=is_fast_mode,
            near_context_priority=near_context_priority,
        )
        prompt_envelope.soft_background_block = soft_background_block
        prompt_envelope.soft_background_budget_chars = int(soft_background_meta.get("budget_chars", 0) or 0)
        prompt_envelope.soft_background_trimmed_sections = list(soft_background_meta.get("trimmed_sections", []) or [])
        prompt_envelope.soft_background_rendered_chars = int(soft_background_meta.get("rendered_chars", 0) or 0)
        prompt_envelope.soft_background_skipped_reason = str(soft_background_meta.get("skipped_reason", "") or "")
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
                warm_context_for_dedup,
                focus_message_text,
                direct_context_text,
                related_context_text,
                background_window_text,
            ],
            min_dedup_length=6,
        )
        warm_zone_transcript = self._deduplicate_transcript(
            warm_zone_transcript,
            [focus_message_text, direct_context_text, related_context_text, background_window_text, recent_transcript],
            min_dedup_length=6,
        )
        warm_text, warm_meta = self._render_warm_context_for_budget(prompt_envelope)
        warm_text = self._deduplicate_transcript(
            warm_text,
            [focus_message_text, direct_context_text, related_context_text, background_window_text, recent_transcript],
            min_dedup_length=6,
        )
        memory_text, memory_meta = self._render_memory_block_for_budget(
            proactive_recall=effective_proactive_recall,
            injection=injection,
        )
        flex_budget_meta = self._apply_flexible_context_budget(
            focus_text=focus_message_text,
            direct_text=direct_context_text,
            warm_text=warm_text,
            warm_meta=warm_meta,
            recent_text=recent_transcript,
            memory_text=memory_text,
            memory_meta=memory_meta,
            soft_background_text=soft_background_block,
        )
        warm_zone_transcript = str(flex_budget_meta.get("warm_text", "") or "").strip()
        recent_transcript = str(flex_budget_meta.get("recent_text", "") or "").strip()
        memory_text = str(flex_budget_meta.get("memory_text", "") or "").strip()
        soft_background_block = str(flex_budget_meta.get("soft_background_text", "") or "").strip()
        effective_proactive_recall = memory_text
        injection = ""
        prompt_envelope.memory_block = memory_text
        prompt_envelope.background_memory_block = memory_text
        prompt_envelope.background_memory_sections = dict(memory_meta.get("sections", {}) or {})
        prompt_envelope.background_memory_budget_chars = int(flex_budget_meta.get("budget_chars", 0) or 0)
        prompt_envelope.background_memory_trimmed_sections = [
            item
            for item in list(flex_budget_meta.get("trimmed_sections", []) or [])
            if str(item).startswith("memory:")
        ]
        prompt_envelope.background_memory_rendered_chars = int(flex_budget_meta.get("memory_rendered_chars", 0) or 0)
        prompt_envelope.background_memory_skipped_reason = "" if memory_text else str(memory_decision.skip_reason or "")
        prompt_envelope.soft_background_block = soft_background_block
        prompt_envelope.soft_background_rendered_chars = len(soft_background_block)
        prompt_envelope.flex_context_budget_chars = int(flex_budget_meta.get("budget_chars", 0) or 0)
        prompt_envelope.flex_context_trimmed_sections = list(flex_budget_meta.get("trimmed_sections", []) or [])
        prompt_envelope.flex_context_protected_sections = list(flex_budget_meta.get("protected_sections", []) or [])
        prompt_envelope.warm_context_rendered_chars = int(flex_budget_meta.get("warm_rendered_chars", 0) or 0)
        prompt_envelope.recent_context_rendered_chars = int(flex_budget_meta.get("recent_rendered_chars", 0) or 0)
        prompt_envelope.memory_context_rendered_chars = int(flex_budget_meta.get("memory_rendered_chars", 0) or 0)

        final_system_prompt = await self._resolve_visual_memory(system_prompt)

        sections = []
        if self._should_include_time_anchor(
            event=event,
            prompt_text=prompt,
            focus_message_text=focus_message_text,
            direct_context_text=direct_context_text,
        ):
            sections.append(self._build_time_anchor())
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            proactive_guidance = str(event.get_extra("astrmai_proactive_guidance", "") or "").strip()
            if proactive_guidance:
                sections.append(
                    "---主动开口参考（仅供内心判断，不要复述）---\n"
                    "这是一次内部开口候选。只有在自然、合适、不打扰时才说一句短回复；"
                    "如果不自然，可以保持沉默。不要提到系统机制或这段指引。\n"
                    + proactive_guidance[:500]
                )
        if focus_message_text:
            sections.append(f"---眼前正在对我说的---\n{await self._resolve_visual_memory(focus_message_text)}")
        if direct_context_text:
            sections.append(f"---前因---\n{await self._resolve_visual_memory(direct_context_text)}")
        if related_context_text:
            sections.append(f"---补充---\n{await self._resolve_visual_memory(related_context_text)}")
        if recent_transcript:
            recent_title = "---对话记录---"
            if recent_transcript_source:
                recent_title = f"---对话记录（来源：{recent_transcript_source}）---"
            if recent_transcript_reason:
                recent_title = f"{recent_title}\n[reason: {recent_transcript_reason}]"
            sections.append(f"{recent_title}\n{await self._resolve_visual_memory(recent_transcript)}")
        if warm_zone_transcript:
            warm_title = "---近期对话脉络---"
            if warm_zone_transcript_source:
                warm_title = f"---近期对话脉络（来源：{warm_zone_transcript_source}）---"
            sections.append(f"{warm_title}\n{await self._resolve_visual_memory(warm_zone_transcript)}")
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
        if soft_background_block:
            sections.append(
                "---背景理解（仅作背景，不要主动续写旧话题，不要覆盖当前用户当前问题）---\n"
                + await self._resolve_visual_memory(soft_background_block)
            )
        runtime_guidance_cluster = self._render_runtime_guidance_cluster(
            cognitive_drive_block=await self._resolve_visual_memory(cognitive_drive_block),
            situational_context_block=await self._resolve_visual_memory(situational_context_block),
            planner_runtime_instruction_block=await self._resolve_visual_memory(planner_runtime_instruction_block),
        )
        if runtime_guidance_cluster:
            sections.append(runtime_guidance_cluster)

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
                f"warm_transcript={warm_zone_transcript[:160]!r} "
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
