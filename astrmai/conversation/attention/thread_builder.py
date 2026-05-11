from __future__ import annotations

import hashlib
from typing import Optional

from ..contracts.focus_context import FocusThreadContext, FreshnessState, ReplyFreshnessBudget, ReplyMode, VisionBundle


def resolve_thread_root(gate, focus_candidate, normalized_events):
    if focus_candidate.reply_target_sender_id or focus_candidate.reply_target_sender_name:
        for previous in reversed(normalized_events[:focus_candidate.index]):
            if focus_candidate.reply_target_sender_id and previous.sender_id == focus_candidate.reply_target_sender_id:
                return previous, "explicit_reply_target"
            if focus_candidate.reply_target_sender_name and previous.sender_name == focus_candidate.reply_target_sender_name:
                return previous, "explicit_reply_target"
        return None, "explicit_reply_target"

    same_speaker_window = int(getattr(getattr(gate.config, "attention", None), "thread_same_speaker_followup_sec", 8) or 8)
    if focus_candidate.is_near_context_query:
        return None, "recent_assistant_turn"

    for previous in reversed(normalized_events[:focus_candidate.index]):
        if previous.is_self:
            continue
        if previous.sender_id != focus_candidate.sender_id:
            break
        if focus_candidate.timestamp and previous.timestamp and (focus_candidate.timestamp - previous.timestamp) > same_speaker_window:
            break
        return previous, "same_sender_chain"
    return focus_candidate, "self_root"


def _score_thread_relation(gate, candidate, focus_candidate, root_candidate: Optional[object]) -> int:
    if candidate.event is focus_candidate.event:
        return 10_000
    if root_candidate and candidate.event is root_candidate.event:
        return 9_000

    score = 0
    shared_tokens = set()
    if candidate.token_set:
        shared_tokens |= candidate.token_set & focus_candidate.token_set
        if root_candidate:
            shared_tokens |= candidate.token_set & root_candidate.token_set

    if root_candidate and candidate.reply_target_sender_id:
        if candidate.reply_target_sender_id == root_candidate.sender_id:
            score += 100
        elif focus_candidate.reply_target_sender_id and candidate.reply_target_sender_id == focus_candidate.reply_target_sender_id:
            score += 85
    if candidate.sender_id == focus_candidate.sender_id and candidate.index < focus_candidate.index:
        same_speaker_window = int(getattr(getattr(gate.config, "attention", None), "thread_same_speaker_followup_sec", 8) or 8)
        if not candidate.timestamp or not focus_candidate.timestamp or (focus_candidate.timestamp - candidate.timestamp) <= same_speaker_window:
            score += 40
    if candidate.image_urls and focus_candidate.image_urls:
        if candidate.sender_id == focus_candidate.sender_id:
            score += 35
        if candidate.has_direct_vision and focus_candidate.has_direct_vision:
            score += 25
    if shared_tokens:
        score += 25
    if candidate.is_near_context_query and abs(candidate.index - focus_candidate.index) <= 1:
        score += 25
    if abs(candidate.index - focus_candidate.index) <= 1:
        score += 15
    return score


def _question_like(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    question_keywords = ("?", "？", "为什么", "怎么", "啥", "什么", "吗", "是不是", "能不能", "可不可以")
    return any(keyword in normalized for keyword in question_keywords)


def _emotion_like(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    emotional_keywords = (
        "难受", "焦虑", "害怕", "不舒服", "委屈", "难过", "崩溃", "emo", "痛", "累", "好困", "想哭",
        "呜", "呜呜", "抱抱", "安慰", "救救", "不想活", "烦", "烦死", "想死",
    )
    return any(keyword in normalized for keyword in emotional_keywords)


def _infer_reply_mode(focus_candidate, root_candidate, normalized_events) -> ReplyMode:
    del normalized_events
    interaction_kind = str(focus_candidate.event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
    text = focus_candidate.rich_text or focus_candidate.text
    if interaction_kind:
        return ReplyMode.PLAYFUL_INTERACTION
    if focus_candidate.image_urls:
        if focus_candidate.has_direct_vision or focus_candidate.is_at_bot or focus_candidate.is_reply_to_bot:
            return ReplyMode.IMAGE_REACTION
        return ReplyMode.AMBIENT_IGNORE
    if _emotion_like(text):
        return ReplyMode.EMOTIONAL_SUPPORT
    if _question_like(text):
        return ReplyMode.DIRECT_QUESTION
    if focus_candidate.is_direct_wakeup or focus_candidate.is_at_bot or focus_candidate.is_reply_to_bot:
        return ReplyMode.CASUAL_FOLLOWUP
    if root_candidate and root_candidate.event is not focus_candidate.event:
        return ReplyMode.CASUAL_FOLLOWUP
    return ReplyMode.CASUAL_FOLLOWUP


def _derive_social_state(reply_mode: ReplyMode) -> str:
    mapping = {
        ReplyMode.PLAYFUL_INTERACTION: "playful_present",
        ReplyMode.EMOTIONAL_SUPPORT: "gentle_support",
        ReplyMode.DIRECT_QUESTION: "direct_answering",
        ReplyMode.CASUAL_FOLLOWUP: "casual_presence",
        ReplyMode.IMAGE_REACTION: "light_visual_reaction",
        ReplyMode.LATE_RECONNECT: "late_reconnect",
        ReplyMode.AMBIENT_IGNORE: "ambient_background",
    }
    return mapping.get(reply_mode, "casual_presence")


def _build_thread_signature(focus_candidate, root_candidate, reply_mode: ReplyMode) -> str:
    root = root_candidate or focus_candidate
    basis = "|".join(
        [
            str(reply_mode.value),
            str(root.sender_id or ""),
            str(root.reply_target_sender_id or ""),
            str(focus_candidate.sender_id or ""),
            hashlib.md5(str(root.rich_text or root.text or "").encode("utf-8")).hexdigest()[:10],
        ]
    )
    return basis


def build_focus_thread(gate, focus_candidate, root_candidate, normalized_events):
    core_events: list = []
    related_events: list = []
    ambient_events: list = []
    attention_config = getattr(gate.config, "attention", None)
    thread_enabled = bool(getattr(attention_config, "focus_thread_enabled", True))
    core_limit = int(getattr(attention_config, "focus_thread_core_max_messages", 4) or 4)
    related_limit = int(getattr(attention_config, "focus_thread_related_max_messages", 3) or 3)
    ambient_limit = int(getattr(attention_config, "ambient_background_max_messages", 2) or 2)

    def _append_unique(container: list, event, limit: int | None = None):
        if event in container:
            return
        if limit is not None and len(container) >= limit:
            return
        container.append(event)

    _append_unique(core_events, focus_candidate.event, core_limit)
    if root_candidate and root_candidate.event is not focus_candidate.event:
        _append_unique(core_events, root_candidate.event, core_limit)
    reply_mode = _infer_reply_mode(focus_candidate, root_candidate, normalized_events)
    social_state = _derive_social_state(reply_mode)
    thread_signature = _build_thread_signature(focus_candidate, root_candidate, reply_mode)
    freshness_budget = ReplyFreshnessBudget(
        state=FreshnessState.FRESH,
        created_at=float(focus_candidate.timestamp or 0.0),
    )

    if not thread_enabled:
        for candidate in normalized_events:
            if candidate.event is focus_candidate.event:
                continue
            _append_unique(ambient_events, candidate.event, ambient_limit)
        return FocusThreadContext(
            focus_event=focus_candidate.event,
            root_event=root_candidate.event if root_candidate else None,
            core_events=core_events,
            related_events=related_events,
            ambient_events=ambient_events,
            focus_reason="",
            root_reason="",
            focus_message_text="",
            focus_sender_id=focus_candidate.sender_id,
            focus_sender_name=focus_candidate.sender_name,
            reply_mode=reply_mode,
            social_state=social_state,
            thread_signature=thread_signature,
            freshness_budget=freshness_budget,
            vision_bundle=VisionBundle(
                image_urls=focus_candidate.image_urls[:],
                direct_image_urls=focus_candidate.image_urls[:] if focus_candidate.has_direct_vision else [],
                is_direct_request=focus_candidate.has_direct_vision,
                is_image_only=focus_candidate.is_image_only,
                source="focus_thread",
            ),
        )

    scored_candidates = []
    for candidate in normalized_events:
        if candidate.event in core_events:
            continue
        relation_score = _score_thread_relation(gate, candidate, focus_candidate, root_candidate)
        scored_candidates.append((relation_score, candidate))

    for relation_score, candidate in sorted(scored_candidates, key=lambda item: (item[0], item[1].index), reverse=True):
        if relation_score >= 70:
            _append_unique(core_events, candidate.event, core_limit)
        elif relation_score >= 35:
            _append_unique(related_events, candidate.event, related_limit)
        elif relation_score >= 0:
            _append_unique(ambient_events, candidate.event, ambient_limit)

    for candidate in normalized_events:
        if candidate.event in core_events or candidate.event in related_events or candidate.event in ambient_events:
            continue
        _append_unique(ambient_events, candidate.event, ambient_limit)

    core_events.sort(key=lambda event: next(item.index for item in normalized_events if item.event is event))
    related_events.sort(key=lambda event: next(item.index for item in normalized_events if item.event is event))
    ambient_events.sort(key=lambda event: next(item.index for item in normalized_events if item.event is event))

    return FocusThreadContext(
        focus_event=focus_candidate.event,
        root_event=root_candidate.event if root_candidate else None,
        core_events=core_events,
        related_events=related_events,
        ambient_events=ambient_events,
        focus_reason="",
        root_reason="",
        focus_message_text="",
        focus_sender_id=focus_candidate.sender_id,
        focus_sender_name=focus_candidate.sender_name,
        reply_mode=reply_mode,
        social_state=social_state,
        thread_signature=thread_signature,
        freshness_budget=freshness_budget,
        vision_bundle=VisionBundle(
            image_urls=focus_candidate.image_urls[:],
            direct_image_urls=focus_candidate.image_urls[:] if focus_candidate.has_direct_vision else [],
            is_direct_request=focus_candidate.has_direct_vision,
            is_image_only=focus_candidate.is_image_only,
            source="focus_thread",
        ),
    )


__all__ = [
    "build_focus_thread",
    "resolve_thread_root",
]
