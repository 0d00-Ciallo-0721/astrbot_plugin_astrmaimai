from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .topic_units import ColdSummaryStructure, TopicUnit


@dataclass(slots=True)
class DialogueSegment:
    timestamp: float
    event_id: str
    speaker_id: str
    speaker_name: str
    content: str
    role: str  # "user" | "assistant" | "interaction"
    message_kind: str  # "text" | "interaction" | "image" | "mixed"
    token_estimate: int
    is_bot: bool = False
    reply_target_sender_id: str = ""
    reply_target_sender_name: str = ""
    is_at_bot: bool = False
    is_reply_to_bot: bool = False
    has_direct_vision: bool = False
    is_image_only: bool = False


@dataclass(slots=True)
class DialogueThread:
    segments: list[DialogueSegment] = field(default_factory=list)
    cold_summary: str = ""
    cold_summary_structure: ColdSummaryStructure | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class WarmContextBundle:
    summary_text: str = ""
    quote_text: str = ""
    quote_event_ids: list[str] = field(default_factory=list)
    has_latest_assistant: bool = False
    """Whether the latest assistant message is present in the selected quotes.

    Recalculated by `_build_warm_quotes` after segment selection and
    deduplication — do not rely on stale values set before that point.
    """
    topic_preview: str = ""


class GroupDialogueStore:
    def __init__(self, *, hot_zone_ttl_seconds: float = 30.0, warm_zone_ttl_seconds: float = 300.0, warm_zone_max_tokens: int = 1200):
        self.hot_zone_ttl_seconds = float(hot_zone_ttl_seconds or 30.0)
        self.warm_zone_ttl_seconds = float(warm_zone_ttl_seconds or 300.0)
        self.warm_zone_max_tokens = int(warm_zone_max_tokens or 1200)
        self._threads: dict[str, DialogueThread] = {}
        self._lock = asyncio.Lock()

    def _get_thread(self, chat_id: str) -> DialogueThread:
        thread = self._threads.get(chat_id)
        if thread is None:
            thread = DialogueThread()
            self._threads[chat_id] = thread
        return thread

    async def clear_chat(self, chat_id: str) -> bool:
        async with self._lock:
            return self._threads.pop(self._resolve_chat_key(chat_id), None) is not None

    @staticmethod
    def _resolve_chat_key(chat_id) -> str:
        """Resolve chat_id to a dict key, rejecting None/empty values.
        
        ponytail: prevents legacy empty-string fallback coercion
        when chat_id is None, which would cause all None-chat_id calls to share
        the same DialogueThread.
        """
        key = (str(chat_id) if chat_id is not None else "").strip()
        if not key:
            raise ValueError("chat_id must be a non-empty string, got %r" % chat_id)
        return key

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        text = str(content or "").strip()
        if not text:
            return 0
        return max(1, len(text) // 2)

    async def append_segment(
        self,
        chat_id: str,
        *,
        event_id: str = "",
        speaker_id: str = "",
        speaker_name: str = "",
        content: str = "",
        role: str = "user",
        message_kind: str = "text",
        is_bot: bool = False,
        reply_target_sender_id: str = "",
        reply_target_sender_name: str = "",
        is_at_bot: bool = False,
        is_reply_to_bot: bool = False,
        has_direct_vision: bool = False,
        is_image_only: bool = False,
        timestamp: float | None = None,
    ) -> DialogueSegment:
        segment = DialogueSegment(
            timestamp=float(timestamp or time.time()),
            event_id=str(event_id or ""),
            speaker_id=str(speaker_id or ""),
            speaker_name=str(speaker_name or ""),
            content=str(content or "").strip(),
            role=str(role or "user"),
            message_kind=str(message_kind or "text"),
            token_estimate=self._estimate_tokens(content),
            is_bot=bool(is_bot),
            reply_target_sender_id=str(reply_target_sender_id or ""),
            reply_target_sender_name=str(reply_target_sender_name or ""),
            is_at_bot=bool(is_at_bot),
            is_reply_to_bot=bool(is_reply_to_bot),
            has_direct_vision=bool(has_direct_vision),
            is_image_only=bool(is_image_only),
        )
        async with self._lock:
            thread = self._get_thread(self._resolve_chat_key(chat_id))
            async with thread.lock:
                thread.segments.append(segment)
        return segment

    async def set_cold_summary(self, chat_id: str, summary: str) -> None:
        async with self._lock:
            thread = self._get_thread(self._resolve_chat_key(chat_id))
            async with thread.lock:
                thread.cold_summary = str(summary or "").strip()
                thread.cold_summary_structure = None

    async def get_cold_summary(self, chat_id: str) -> str:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return ""
            async with thread.lock:
                return thread.cold_summary

    async def set_cold_summary_structure(self, chat_id: str, structure: ColdSummaryStructure | None) -> None:
        async with self._lock:
            thread = self._get_thread(self._resolve_chat_key(chat_id))
            async with thread.lock:
                thread.cold_summary_structure = structure

    async def get_cold_summary_structure(self, chat_id: str) -> ColdSummaryStructure | None:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return None
            async with thread.lock:
                return thread.cold_summary_structure

    async def drain_old_segments(self, chat_id: str, *, keep_recent_segments: int = 12) -> list[DialogueSegment]:
        drained = await self.peek_old_segments(chat_id, keep_recent_segments=keep_recent_segments)
        if not drained:
            return []
        await self.commit_drain_old_segments(chat_id, keep_recent_segments=keep_recent_segments)
        return drained

    async def peek_old_segments(self, chat_id: str, *, keep_recent_segments: int = 12) -> list[DialogueSegment]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return []
            async with thread.lock:
                if keep_recent_segments <= 0:
                    return list(thread.segments)
                if len(thread.segments) <= keep_recent_segments:
                    return []
                return list(thread.segments[:-keep_recent_segments])

    async def commit_drain_old_segments(self, chat_id: str, *, keep_recent_segments: int = 12) -> list[DialogueSegment]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return []
            async with thread.lock:
                if keep_recent_segments <= 0:
                    drained = list(thread.segments)
                    thread.segments.clear()
                    return drained
                if len(thread.segments) <= keep_recent_segments:
                    return []
                drained = thread.segments[:-keep_recent_segments]
                thread.segments = thread.segments[-keep_recent_segments:]
                return list(drained)

    def _format_segment_line(self, segment: DialogueSegment) -> str:
        speaker = segment.speaker_name or segment.speaker_id or ("Bot" if segment.is_bot else "User")
        prefix_bits: list[str] = []
        if segment.is_reply_to_bot:
            target = segment.reply_target_sender_name or segment.reply_target_sender_id or "Bot"
            prefix_bits.append(f"回复 {target}")
        elif segment.reply_target_sender_name or segment.reply_target_sender_id:
            target = segment.reply_target_sender_name or segment.reply_target_sender_id
            prefix_bits.append(f"回复 {target}")
        if segment.is_at_bot:
            prefix_bits.append("@我")
        if segment.message_kind == "image" or segment.is_image_only or segment.has_direct_vision:
            prefix_bits.append("图片")
        if segment.message_kind == "mixed":
            prefix_bits.append("图文")
        prefix = f"({'，'.join(prefix_bits)}) " if prefix_bits else ""
        return f"{speaker} {prefix}: {segment.content}".replace("  ", " ").strip().replace(" :", ":")

    @staticmethod
    def _normalize_message_text(text: str) -> str:
        cleaned = str(text or "").strip()
        if not cleaned:
            return ""
        return re.sub(r"\s+", " ", cleaned)

    @staticmethod
    def _preview_text(text: str, limit: int = 36) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: max(0, limit - 3)] + "..."

    @classmethod
    def _standardized_length(cls, text: str) -> int:
        return len(cls._normalize_message_text(text))

    @classmethod
    def _message_load(cls, text: str) -> float:
        length = cls._standardized_length(text)
        if length <= 0:
            return 0.0
        if length <= 12:
            return 0.5
        if length <= 40:
            return 1.0
        if length <= 100:
            return 1.6
        if length <= 220:
            return 2.4
        return 3.2

    @classmethod
    def _segment_load(cls, segment: DialogueSegment) -> float:
        return cls._message_load(str(segment.content or ""))

    @classmethod
    def _segment_relevance_score(cls, segment: DialogueSegment) -> tuple[float, float]:
        score = 0.0
        if segment.is_at_bot:
            score += 6.0
        if segment.is_reply_to_bot:
            score += 5.0
        elif segment.reply_target_sender_id or segment.reply_target_sender_name:
            score += 3.0
        if segment.role == "assistant" or segment.is_bot:
            score += 3.5
        if segment.has_direct_vision or segment.is_image_only:
            score += 2.0
        if segment.message_kind == "mixed":
            score += 1.0
        return score, segment.timestamp

    @classmethod
    def _is_question_like(cls, text: str) -> bool:
        cleaned = cls._normalize_message_text(text)
        if not cleaned:
            return False
        lowered = cleaned.lower()
        return (
            "?" in cleaned
            or "？" in cleaned
            or any(
                token in lowered
                for token in ("why", "how", "what", "where", "when", "吗", "么", "为什么", "怎么", "咋", "是否", "要不要", "能不能")
            )
        )

    @classmethod
    def _is_decision_like(cls, text: str) -> bool:
        cleaned = cls._normalize_message_text(text)
        if not cleaned or cls._is_question_like(cleaned):
            return False
        lowered = cleaned.lower()
        return any(
            token in lowered
            for token in ("就这样", "那就", "可以", "行", "确定", "决定", "按这个", "收到", "ok", "deal", "没问题")
        )

    @classmethod
    def _detect_relation_state(cls, segments: list[DialogueSegment]) -> str:
        if any(segment.is_at_bot or segment.is_reply_to_bot for segment in segments):
            return "互动主线仍然直接连着我，大家在顺着同一个点继续接。"
        if any(segment.reply_target_sender_id or segment.reply_target_sender_name for segment in segments):
            return "最近几句明显沿着同一条回复链往下延伸。"
        return ""

    @classmethod
    def _detect_emotion_state(cls, segments: list[DialogueSegment]) -> str:
        combined = " ".join(cls._normalize_message_text(segment.content) for segment in segments if segment.content)
        lowered = combined.lower()
        if any(token in lowered for token in ("哈哈", "hh", "233", "笑死", "lol")):
            return "语气整体偏轻松，大家更像是在顺势接话。"
        if any(token in lowered for token in ("急", "烦", "生气", "无语", "离谱")):
            return "语气里带着一点紧张或不耐烦，这个点还没完全放松下来。"
        return ""

    def _extract_warm_topic_units(self, segments: list[DialogueSegment]) -> list[TopicUnit]:
        if not segments:
            return []
        recent = list(segments[-6:])
        direct_segments = [segment for segment in recent if segment.is_at_bot or segment.is_reply_to_bot]
        visual_segments = [
            segment
            for segment in recent
            if segment.has_direct_vision or segment.is_image_only or segment.message_kind in {"image", "mixed"}
        ]
        latest_assistant = next((segment for segment in reversed(recent) if segment.role == "assistant" or segment.is_bot), None)
        question_segments = [segment for segment in reversed(recent) if self._is_question_like(segment.content)]
        decision_segments = [segment for segment in reversed(recent) if self._is_decision_like(segment.content)]
        units: list[TopicUnit] = []

        if direct_segments:
            anchor = direct_segments[-1]
            units.append(
                TopicUnit(
                    slot="topic",
                    text=f"当前主线还在围绕我刚接住的那个点继续展开，焦点落在“{self._preview_text(anchor.content, 28)}”。",
                    event_ids=[segment.event_id for segment in direct_segments if segment.event_id],
                )
            )
        elif visual_segments:
            anchor = visual_segments[-1]
            units.append(
                TopicUnit(
                    slot="topic",
                    text=f"当前主要在围绕图片/图文线索继续往下聊，焦点落在“{self._preview_text(anchor.content, 28)}”。",
                    event_ids=[segment.event_id for segment in visual_segments if segment.event_id],
                )
            )
        else:
            anchor = recent[-1]
            units.append(
                TopicUnit(
                    slot="topic",
                    text=f"当前主要是在延续刚才的群聊话题，最近落点是“{self._preview_text(anchor.content, 28)}”。",
                    event_ids=[anchor.event_id] if anchor.event_id else [],
                )
            )

        if latest_assistant is not None:
            followups = [segment for segment in recent if segment.timestamp > latest_assistant.timestamp and self._is_question_like(segment.content)]
            if followups:
                units.append(
                    TopicUnit(
                        slot="event",
                        text="最近的推进是我刚给出一次回应，后面马上有人继续追问或补条件，这条链还没收口。",
                        event_ids=[latest_assistant.event_id, *[segment.event_id for segment in followups if segment.event_id]],
                    )
                )
            else:
                units.append(
                    TopicUnit(
                        slot="event",
                        text="最近的推进是我刚给过回应，群里现在是在顺着那个回应继续消化细节。",
                        event_ids=[latest_assistant.event_id] if latest_assistant.event_id else [],
                    )
                )
        else:
            units.append(
                TopicUnit(
                    slot="event",
                    text="最近的推进主要来自群友之间的补充和接话，还没有形成一次明确的 bot 往返闭环。",
                    event_ids=[segment.event_id for segment in recent[-2:] if segment.event_id],
                )
            )

        if decision_segments:
            anchor = decision_segments[0]
            units.append(
                TopicUnit(
                    slot="decision",
                    text=f"这段里已经出现了阶段性确认，方向更像是“{self._preview_text(anchor.content, 30)}”。",
                    event_ids=[anchor.event_id] if anchor.event_id else [],
                )
            )
        if question_segments:
            anchor = question_segments[0]
            units.append(
                TopicUnit(
                    slot="open_question",
                    text=f"现在还没收口的问题更像是“{self._preview_text(anchor.content, 30)}”，后面大概率还会继续接着问。",
                    event_ids=[anchor.event_id] if anchor.event_id else [],
                )
            )
        relation_text = self._detect_relation_state(recent)
        if relation_text:
            units.append(
                TopicUnit(
                    slot="relation_state",
                    text=relation_text,
                    event_ids=[segment.event_id for segment in recent if segment.event_id][:3],
                )
            )
        emotion_text = self._detect_emotion_state(recent)
        if emotion_text:
            units.append(
                TopicUnit(
                    slot="emotion_state",
                    text=emotion_text,
                    event_ids=[segment.event_id for segment in recent if segment.event_id][:3],
                )
            )
        if visual_segments:
            anchor = visual_segments[-1]
            units.append(
                TopicUnit(
                    slot="visual_context",
                    text=f"最近还有图片/图文线索在场，相关内容落在“{self._preview_text(anchor.content, 26)}”附近。",
                    event_ids=[segment.event_id for segment in visual_segments if segment.event_id],
                )
            )
        if question_segments and not decision_segments:
            units.append(
                TopicUnit(
                    slot="next_action",
                    text="接下来更像是在等我继续解释、确认条件，或者把刚才那个点真正答完。",
                    event_ids=[question_segments[0].event_id] if question_segments[0].event_id else [],
                )
            )
        return units

    def _select_warm_summary_segments(self, segments: list[DialogueSegment], *, limit: int = 6) -> list[DialogueSegment]:
        if not segments:
            return []
        scored: list[tuple[float, int, DialogueSegment]] = [
            (self._segment_relevance_score(segment)[0], index, segment)
            for index, segment in enumerate(segments)
        ]
        prioritized = [item for item in scored if item[0] > 0]
        if not prioritized:
            return list(segments[-limit:])
        selected = sorted(prioritized, key=lambda item: (item[0], item[1]), reverse=True)[:limit]
        return [segment for _, _, segment in sorted(selected, key=lambda item: item[1])]

    def _build_warm_summary(self, segments: list[DialogueSegment], *, max_tokens: int) -> str:
        if not segments or max_tokens <= 0:
            return ""
        direct = sum(1 for segment in segments if segment.is_at_bot or segment.is_reply_to_bot)
        reply_chain = sum(
            1
            for segment in segments
            if (segment.reply_target_sender_id or segment.reply_target_sender_name) and not segment.is_reply_to_bot
        )
        visuals = sum(
            1
            for segment in segments
            if segment.has_direct_vision or segment.is_image_only or segment.message_kind in {"image", "mixed"}
        )
        latest_speakers: list[str] = []
        for segment in reversed(segments):
            speaker = segment.speaker_name or segment.speaker_id or ("Bot" if segment.is_bot else "User")
            if speaker and speaker not in latest_speakers:
                latest_speakers.append(speaker)
            if len(latest_speakers) >= 3:
                break
        latest_speakers.reverse()
        lines: list[str] = []
        if latest_speakers:
            lines.append(f"最近主要是 {' / '.join(latest_speakers)} 在接着聊。")
        if direct:
            lines.append(f"其中有 {direct} 条是在直接问我或接着我的回复。")
        elif reply_chain:
            lines.append(f"其中有 {reply_chain} 条是在沿着回复链继续往下接。")
        if visuals:
            lines.append(f"最近还夹着 {visuals} 条图片或图文相关消息。")
        latest_assistant = next((segment for segment in reversed(segments) if segment.role == "assistant" or segment.is_bot), None)
        if latest_assistant is not None:
            speaker = latest_assistant.speaker_name or latest_assistant.speaker_id or "Bot"
            lines.append(f"{speaker} 刚刚也说过话，后面还有人在顺着接。")
        selected: list[str] = []
        used = 0
        for line in lines:
            estimate = self._estimate_tokens(line)
            if used + estimate > max_tokens:
                break
            selected.append(line)
            used += estimate
        return "\n".join(selected).strip()

    def _build_warm_summary_v2(self, segments: list[DialogueSegment], *, max_tokens: int) -> str:
        if not segments or max_tokens <= 0:
            return ""
        topic_units = self._extract_warm_topic_units(segments)
        if not topic_units:
            return ""
        selected: list[str] = []
        used = 0
        for unit in topic_units:
            line = str(unit.text or "").strip()
            if not line:
                continue
            estimate = max(1, self._estimate_tokens(line))
            if selected and used + estimate > max_tokens:
                break
            if not selected and estimate > max_tokens:
                selected.append(line)
                break
            selected.append(line)
            used += estimate
            if len(selected) >= 4:
                break
        return "\n".join(selected).strip()

    def _build_warm_quotes(self, segments: list[DialogueSegment], *, max_tokens: int) -> tuple[str, list[str], bool]:
        if not segments or max_tokens <= 0:
            return "", [], False
        candidate_pool = segments[-8:] if len(segments) > 8 else list(segments)
        ordered = [
            segment
            for _, segment in sorted(
                enumerate(candidate_pool),
                key=lambda pair: (self._segment_relevance_score(pair[1])[0], pair[0]),
                reverse=True,
            )
        ]
        selected: list[DialogueSegment] = []
        used = 0
        for segment in ordered:
            line = self._format_segment_line(segment)
            if not line:
                continue
            estimate = max(1, self._estimate_tokens(line))
            if used + estimate > max_tokens:
                continue
            selected.append(segment)
            used += estimate
            if len(selected) >= 3:
                break
        if not selected and candidate_pool:
            selected.append(candidate_pool[-1])
        latest_direct_user = next(
            (
                segment
                for segment in reversed(candidate_pool)
                if segment.role == "user"
                and not segment.is_bot
                and (segment.is_at_bot or segment.is_reply_to_bot)
            ),
            None,
        )
        if latest_direct_user is not None and latest_direct_user not in selected:
            line = self._format_segment_line(latest_direct_user)
            estimate = max(1, self._estimate_tokens(line))
            if used + estimate <= max_tokens or not selected:
                selected.append(latest_direct_user)
                used += estimate
        # 限制扫描窗口为最近 64 条，避免大群积压时 O(n) 全量扫描（D21）
        _scan_window = segments[-64:]
        latest_assistant = next((segment for segment in reversed(_scan_window) if segment.role == "assistant" or segment.is_bot), None)
        has_latest_assistant = bool(latest_assistant and latest_assistant in selected)
        if latest_assistant is not None and not has_latest_assistant:
            line = self._format_segment_line(latest_assistant)
            estimate = max(1, self._estimate_tokens(line))
            if used + estimate <= max_tokens or not selected:
                selected.append(latest_assistant)
                has_latest_assistant = True
        deduped: list[DialogueSegment] = []
        seen_keys: set[str] = set()
        for segment in sorted(selected, key=lambda item: item.timestamp):
            key = segment.event_id or f"{segment.timestamp}:{segment.speaker_id}:{segment.content}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(segment)
        lines = [self._format_segment_line(segment) for segment in deduped if self._format_segment_line(segment)]
        quote_event_ids = [segment.event_id for segment in deduped if segment.event_id]
        return "\n".join(lines).strip(), quote_event_ids, has_latest_assistant

    async def get_warm_context_bundle(
        self,
        chat_id: str,
        *,
        max_age_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> WarmContextBundle:
        chat_key = self._resolve_chat_key(chat_id)
        max_age_seconds = self.warm_zone_ttl_seconds if max_age_seconds is None else float(max_age_seconds)
        max_tokens = self.warm_zone_max_tokens if max_tokens is None else int(max_tokens)
        now = time.time()
        async with self._lock:
            thread = self._threads.get(chat_key)
            if not thread:
                return WarmContextBundle()
            async with thread.lock:
                candidates: list[DialogueSegment] = []
                for segment in thread.segments:
                    if max_age_seconds > 0 and now - segment.timestamp > max_age_seconds:
                        continue
                    if not self._format_segment_line(segment):
                        continue
                    candidates.append(segment)
                if not candidates:
                    return WarmContextBundle()
                summary_budget = max(1, int(max_tokens * 0.35)) if max_tokens > 0 else 0
                quote_budget = max(1, max_tokens - summary_budget) if max_tokens > 0 else 0
                summary_candidates = self._select_warm_summary_segments(candidates)
                warm_units = self._extract_warm_topic_units(summary_candidates)
                summary_text = self._build_warm_summary_v2(summary_candidates, max_tokens=summary_budget)
                quote_text, quote_event_ids, has_latest_assistant = self._build_warm_quotes(
                    candidates,
                    max_tokens=quote_budget if quote_budget > 0 else max_tokens,
                )
                return WarmContextBundle(
                    summary_text=summary_text,
                    quote_text=quote_text,
                    quote_event_ids=quote_event_ids,
                    has_latest_assistant=has_latest_assistant,
                    topic_preview=" | ".join(
                        f"{unit.slot}:{self._preview_text(unit.text, 24)}"
                        for unit in warm_units[:4]
                        if str(unit.text or "").strip()
                    ),
                )

    async def get_warm_transcript(self, chat_id: str, *, max_age_seconds: float | None = None, max_tokens: int | None = None) -> str:
        bundle = await self.get_warm_context_bundle(chat_id, max_age_seconds=max_age_seconds, max_tokens=max_tokens)
        return "\n".join(part for part in (bundle.summary_text, bundle.quote_text) if part).strip()

    async def snapshot_compaction_candidates(self, chat_id: str, *, keep_recent_segments: int = 16) -> dict[str, Any]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return {
                    "active_segments": 0,
                    "compressible_segments": 0,
                    "compressible_message_load": 0.0,
                    "compressible_long_message_count": 0,
                    "tail_event_id": "",
                    "recent_segments": [],
                }
            async with thread.lock:
                active = list(thread.segments)
                keep_recent_segments = max(0, int(keep_recent_segments))
                if keep_recent_segments <= 0:
                    old_zone = list(active)
                elif len(active) <= keep_recent_segments:
                    old_zone = []
                else:
                    old_zone = active[:-keep_recent_segments]
                return {
                    "active_segments": len(active),
                    "compressible_segments": len(old_zone),
                    "compressible_message_load": sum(self._segment_load(segment) for segment in old_zone),
                    "compressible_long_message_count": sum(
                        1 for segment in old_zone if self._standardized_length(str(segment.content or "")) > 100
                    ),
                    "tail_event_id": old_zone[-1].event_id if old_zone else "",
                    "compressible_zone_segments": list(old_zone),
                    "recent_segments": list(active[-max(keep_recent_segments, 4):]),
                }

    async def snapshot_counts(self, chat_id: str) -> dict[str, Any]:
        async with self._lock:
            thread = self._threads.get(self._resolve_chat_key(chat_id))
            if not thread:
                return {"segments": 0, "tokens": 0, "has_summary": False, "message_load": 0.0}
            async with thread.lock:
                return {
                    "segments": len(thread.segments),
                    "tokens": sum(segment.token_estimate for segment in thread.segments),
                    "has_summary": bool(thread.cold_summary),
                    "message_load": sum(self._segment_load(segment) for segment in thread.segments),
                }
