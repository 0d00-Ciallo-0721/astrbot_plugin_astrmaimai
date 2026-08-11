from __future__ import annotations

import hashlib
import re
from typing import Optional

from ..contracts.focus_context import FocusThreadContext, FreshnessState, MediaCandidate, ReplyFreshnessBudget, ReplyMode, VisionBundle
from .turn_target_resolver import resolve_legacy_turn_target, resolve_turn_target
from ..runtime.architecture_rollout import (
    ArchitectureTimer,
    record_architecture_observation,
    rollout_enabled,
)


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
            hashlib.sha256(str(root.rich_text or root.text or "").encode("utf-8")).hexdigest()[:10],
        ]
    )
    return basis


_EXPLICIT_IMAGE_REFERENCE_RE = re.compile(
    r"(?:这|那|刚才|上面|前面|上一)[^\n]{0,5}(?:张|个)?(?:图|图片|表情包)|"
    r"(?:图|图片|表情包)(?:里|中|上|呢|是什么|怎么)|看(?:看|清|一下)?(?:这|那|刚才|上面)?(?:张|个)?(?:图|图片|表情包)"
)


def _event_message_id(event) -> str:
    message_obj = getattr(event, "message_obj", None)
    return str(
        getattr(message_obj, "message_id", "")
        or getattr(message_obj, "id", "")
        or ""
    ).strip()


def _collect_recent_media_candidates(
    gate,
    focus_candidate,
    root_candidate,
    normalized_events,
) -> list[MediaCandidate]:
    conversation = getattr(getattr(gate, "config", None), "conversation", None)
    if not bool(getattr(conversation, "autonomous_vision_tool_enabled", True)):
        return []
    window_sec = max(
        5.0,
        float(getattr(conversation, "recent_image_candidate_window_sec", 30.0) or 30.0),
    )
    max_count = max(
        1,
        min(4, int(getattr(conversation, "recent_image_candidate_max_count", 2) or 2)),
    )
    focus_text = str(focus_candidate.rich_text or focus_candidate.text or "")
    explicitly_references_image = bool(_EXPLICIT_IMAGE_REFERENCE_RE.search(focus_text))
    focus_ts = float(focus_candidate.timestamp or 0.0)
    collected: list[MediaCandidate] = []
    seen_ids: set[str] = set()

    for candidate in reversed(normalized_events):
        if not candidate.image_urls or candidate.index > focus_candidate.index:
            continue
        message_id = _event_message_id(candidate.event)
        if not message_id or message_id in seen_ids:
            continue
        age = max(0.0, focus_ts - float(candidate.timestamp or focus_ts))
        relation = ""
        if candidate.event is focus_candidate.event:
            relation = "current"
        elif root_candidate is not None and candidate.event is root_candidate.event:
            relation = "reply_target"
        elif age <= window_sec and candidate.sender_id == focus_candidate.sender_id:
            relation = "same_sender_recent"
        elif age <= window_sec and explicitly_references_image:
            relation = "explicit_recent_reference"
        if not relation:
            continue
        canonical = getattr(candidate, "canonical_event", None)
        collected.append(
            MediaCandidate(
                message_id=message_id,
                event_id=str(getattr(canonical, "event_id", "") or ""),
                sender_id=str(candidate.sender_id or ""),
                sender_name=str(candidate.sender_name or ""),
                age_seconds=age,
                relation=relation,
                image_count=max(1, len(candidate.image_urls)),
            )
        )
        seen_ids.add(message_id)
        if len(collected) >= max_count:
            break
    collected.reverse()
    return collected


def build_focus_thread(gate, focus_candidate, root_candidate, normalized_events):
    core_events: list = []
    related_events: list = []
    ambient_events: list = []
    event_order = {candidate.event: candidate.index for candidate in normalized_events}
    attention_config = getattr(gate.config, "attention", None)
    thread_enabled = bool(getattr(attention_config, "focus_thread_enabled", True))
    core_limit = int(getattr(attention_config, "focus_thread_core_max_messages", 4) or 4)
    related_limit = int(getattr(attention_config, "focus_thread_related_max_messages", 3) or 3)
    ambient_limit = int(getattr(attention_config, "ambient_background_max_messages", 2) or 2)
    pending_events = [
        candidate.event
        for candidate in normalized_events
        if bool(candidate.event.get_extra("astrmai_private_pending_context", False))
    ]
    if pending_events and bool(focus_candidate.event.get_extra("is_private_chat", False)):
        core_limit = max(core_limit, len(pending_events) + 1)

    def _append_unique(container: list, event, limit: int | None = None):
        if event in container:
            return
        if limit is not None and len(container) >= limit:
            return
        container.append(event)

    _append_unique(core_events, focus_candidate.event, core_limit)
    if root_candidate and root_candidate.event is not focus_candidate.event:
        _append_unique(core_events, root_candidate.event, core_limit)
    if pending_events and bool(focus_candidate.event.get_extra("is_private_chat", False)):
        for pending_event in pending_events:
            _append_unique(core_events, pending_event, core_limit)
    reply_mode = _infer_reply_mode(focus_candidate, root_candidate, normalized_events)
    social_state = _derive_social_state(reply_mode)
    thread_signature = _build_thread_signature(focus_candidate, root_candidate, reply_mode)
    freshness_budget = ReplyFreshnessBudget(
        state=FreshnessState.FRESH,
        created_at=float(focus_candidate.timestamp or 0.0),
    )
    focus_event = focus_candidate.event
    bot_id = str(
        getattr(focus_event, "get_self_id", lambda: "")()
        or getattr(gate, "self_id", "")
        or ""
    )
    target_timer = ArchitectureTimer()
    new_turn_target, new_actor_set = resolve_turn_target(
        focus_candidate,
        root_candidate,
        normalized_events,
        bot_id=bot_id,
    )
    legacy_turn_target, legacy_actor_set = resolve_legacy_turn_target(
        focus_candidate,
        root_candidate,
        normalized_events,
        bot_id=bot_id,
    )
    target_read_enabled = rollout_enabled(
        getattr(gate, "config", None),
        "turn_target_read_enabled",
        True,
    )
    turn_target = new_turn_target if target_read_enabled else legacy_turn_target
    actor_set = new_actor_set if target_read_enabled else legacy_actor_set
    record_architecture_observation(
        focus_event,
        "turn_target",
        {
            "read_enabled": target_read_enabled,
            "new": new_turn_target.as_dict(),
            "legacy": legacy_turn_target.as_dict(),
            "actor_match": (
                new_turn_target.target_actor_id == legacy_turn_target.target_actor_id
            ),
            "kind_match": new_turn_target.target_kind == legacy_turn_target.target_kind,
            "elapsed_ms": target_timer.elapsed_ms,
        },
    )
    compatibility_sender_id = turn_target.target_actor_id or focus_candidate.sender_id
    compatibility_sender_name = turn_target.target_actor_name or focus_candidate.sender_name
    recent_media_candidates = _collect_recent_media_candidates(
        gate,
        focus_candidate,
        root_candidate,
        normalized_events,
    )
    if recent_media_candidates:
        safe_candidates = [candidate.as_safe_dict() for candidate in recent_media_candidates]
        focus_event.set_extra("astrmai_recent_media_candidates", safe_candidates)
        existing_bound = focus_event.get_extra("astrmai_bound_message_ids", []) or []
        bound_ids = [str(item or "").strip() for item in existing_bound if str(item or "").strip()]
        for candidate in recent_media_candidates:
            if candidate.message_id not in bound_ids:
                bound_ids.append(candidate.message_id)
        focus_event.set_extra("astrmai_bound_message_ids", bound_ids)

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
            focus_sender_id=compatibility_sender_id,
            focus_sender_name=compatibility_sender_name,
            turn_target=turn_target,
            actor_set=actor_set,
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
            recent_media_candidates=recent_media_candidates,
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

    core_events.sort(key=lambda event: event_order.get(event, -1))
    related_events.sort(key=lambda event: event_order.get(event, -1))
    ambient_events.sort(key=lambda event: event_order.get(event, -1))

    return FocusThreadContext(
        focus_event=focus_candidate.event,
        root_event=root_candidate.event if root_candidate else None,
        core_events=core_events,
        related_events=related_events,
        ambient_events=ambient_events,
        focus_reason="",
        root_reason="",
        focus_message_text="",
        focus_sender_id=compatibility_sender_id,
        focus_sender_name=compatibility_sender_name,
        turn_target=turn_target,
        actor_set=actor_set,
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
        recent_media_candidates=recent_media_candidates,
    )


__all__ = [
    "build_focus_thread",
    "resolve_thread_root",
]
