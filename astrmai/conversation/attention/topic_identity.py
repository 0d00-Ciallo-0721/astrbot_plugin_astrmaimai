from __future__ import annotations

import hashlib
import re
from typing import Any

from ..contracts.attention_topic import AttentionTopicIdentity
from ..contracts.dialog_history_policy import DialogHistoryPolicy


_AMBIGUOUS_SHORT_TEXTS = {
    "?",
    "？",
    "嗯",
    "嗯嗯",
    "好",
    "好的",
    "行",
    "可以",
    "对",
    "不对",
    "继续",
    "为什么",
    "啥意思",
    "什么意思",
    "然后呢",
    "还有呢",
}


def _event_text(event: Any) -> str:
    if event is None:
        return ""
    rich_text = (
        event.get_extra("astrmai_rich_text", "")
        if hasattr(event, "get_extra")
        else ""
    )
    return " ".join(str(rich_text or getattr(event, "message_str", "") or "").split())


def _event_id(event: Any) -> str:
    if event is None:
        return ""
    canonical = (
        event.get_extra("astrmai_conversation_event", None)
        if hasattr(event, "get_extra")
        else None
    )
    return str(
        getattr(canonical, "event_id", "")
        or getattr(getattr(event, "message_obj", None), "message_id", "")
        or ""
    ).strip()


def _reply_root_id(event: Any, focus_thread: Any) -> str:
    canonical = (
        event.get_extra("astrmai_conversation_event", None)
        if event is not None and hasattr(event, "get_extra")
        else None
    )
    explicit = str(
        getattr(canonical, "reply_target_event_id", "")
        or getattr(canonical, "quote_event_id", "")
        or ""
    ).strip()
    if explicit:
        return explicit
    root_event = getattr(focus_thread, "root_event", None)
    if root_event is not None and root_event is not event:
        return _event_id(root_event)
    return ""


def _normalized_fingerprint(text: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "").lower())
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _stable_key(*parts: str) -> str:
    material = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _topic_ngrams(text: str, *, size: int = 3) -> set[str]:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(text or "").lower())
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}


def attention_topic_anchors_match(left: str, right: str) -> bool:
    """Conservatively match short-lived cache anchors, independent of history continuity."""
    left_normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(left or "").lower())
    right_normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", str(right or "").lower())
    if not left_normalized or not right_normalized:
        return False
    if left_normalized == right_normalized:
        return True
    shorter, longer = sorted((left_normalized, right_normalized), key=len)
    if len(shorter) >= 6 and shorter in longer:
        return True
    left_ngrams = _topic_ngrams(left_normalized)
    right_ngrams = _topic_ngrams(right_normalized)
    if not left_ngrams or not right_ngrams:
        return False
    similarity = len(left_ngrams & right_ngrams) / len(left_ngrams | right_ngrams)
    return similarity >= 0.6


def resolve_attention_topic_identity(
    *,
    history_policy: DialogHistoryPolicy,
    focus_event: Any,
    focus_thread: Any,
) -> AttentionTopicIdentity:
    epoch = max(0, int(history_policy.topic_epoch or 0))
    evidence = tuple(str(item) for item in history_policy.continuity_evidence if str(item))
    text = _event_text(focus_event)
    if epoch <= 0:
        return AttentionTopicIdentity(
            source="history_topic_unknown",
            evidence=evidence,
            anchor_text=text,
        )

    root_event_id = _reply_root_id(focus_event, focus_thread)
    if root_event_id:
        return AttentionTopicIdentity(
            history_topic_epoch=epoch,
            attention_topic_key=_stable_key(str(epoch), "root", root_event_id),
            source="causal_root",
            confidence=1.0,
            evidence=evidence,
            anchor_text=text,
            root_event_id=root_event_id,
        )

    if "topic_similarity" in evidence:
        fingerprint = _normalized_fingerprint(text)
        if not fingerprint:
            return AttentionTopicIdentity(
                history_topic_epoch=epoch,
                source="topic_similarity_without_anchor",
                confidence=0.0,
                evidence=evidence,
                anchor_text=text,
            )
        return AttentionTopicIdentity(
            history_topic_epoch=epoch,
            attention_topic_key=_stable_key(str(epoch), "topic", fingerprint),
            source="topic_similarity",
            confidence=0.9,
            evidence=evidence,
            anchor_text=text,
        )

    if "short_followup" in evidence and text.strip().lower() not in _AMBIGUOUS_SHORT_TEXTS:
        fingerprint = _normalized_fingerprint(text)
        return AttentionTopicIdentity(
            history_topic_epoch=epoch,
            attention_topic_key=_stable_key(str(epoch), "followup", fingerprint),
            source="short_followup",
            confidence=0.75,
            evidence=evidence,
            anchor_text=text,
        )

    fingerprint = _normalized_fingerprint(text)
    if not fingerprint or text.strip().lower() in _AMBIGUOUS_SHORT_TEXTS:
        return AttentionTopicIdentity(
            history_topic_epoch=epoch,
            source="ambiguous_short_text",
            confidence=0.0,
            evidence=evidence,
            anchor_text=text,
        )
    return AttentionTopicIdentity(
        history_topic_epoch=epoch,
        attention_topic_key=_stable_key(str(epoch), "text", fingerprint),
        source="text_fingerprint",
        confidence=0.5,
        evidence=evidence,
        anchor_text=text,
    )


__all__ = ["attention_topic_anchors_match", "resolve_attention_topic_identity"]
