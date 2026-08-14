from __future__ import annotations

import re
import json
from collections import Counter
from typing import Any, Iterable


class LearningMessageView:
    """Read-only message view with sanitized learning content."""

    def __init__(
        self,
        source: Any,
        content: str,
        source_kind: str = "human_text",
        *,
        context_content: str = "",
        evidence_eligible: bool = True,
    ):
        self._source = source
        self.content = content
        self.learning_source_kind = source_kind
        self.learning_context_content = context_content or content
        self.learning_evidence_eligible = bool(evidence_eligible)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)


class LearningInputPolicy:
    """Shared provenance and content gate for all automatic learning pipelines."""

    _BOT_ROLES = {"assistant", "bot", "self", "system", "tool"}
    _BOT_PROVENANCE = {"bot_echo", "external_plugin", "proactive", "synthetic", "replay"}
    _SELF_IDS = {"SELF", "BOT", "SELF_BOT"}
    _TRANSPORT_MARKERS = (
        "mqqapi://",
        "%7b%22version%22",
        "qqbot.ugcimg.cn",
        "[cq:",
        "(系统指令执行结果)",
        "astrbot - after_message_sent",
    )
    _CARD_MARKERS = (
        "复制打开抖音",
        "复制打开快手",
        "小程序",
        "聊天记录",
    )
    _FORTUNE_MARKERS = ("少女祈祷中", "今日运势", "财运", "事业运", "桃花运", "仅供娱乐")

    def __init__(self) -> None:
        self.last_report: dict[str, Any] = {}

    @staticmethod
    def _value(message: Any, name: str, default: Any = "") -> Any:
        if isinstance(message, dict):
            return message.get(name, default)
        return getattr(message, name, default)

    @classmethod
    def _reject_reason(cls, message: Any, text: str) -> str:
        sender_id = str(cls._value(message, "sender_id", "") or "").strip().upper()
        sender_name = str(cls._value(message, "sender_name", "") or "").strip().upper()
        role = str(cls._value(message, "role", "") or "").strip().lower()
        provenance = str(cls._value(message, "provenance", "") or "").strip().lower()
        chat_kind = str(cls._value(message, "chat_kind", "") or "").strip().lower()
        message_kind = str(cls._value(message, "message_kind", "") or "").strip().lower()
        lowered = text.lower()

        if bool(cls._value(message, "is_bot", False)) or sender_id in cls._SELF_IDS or sender_name == "SELF":
            return "bot_message"
        if role in cls._BOT_ROLES:
            return "non_human_role"
        if provenance in cls._BOT_PROVENANCE:
            return "non_human_provenance"
        if bool(cls._value(message, "recalled", False)):
            return "recalled_message"
        if chat_kind in {"private", "friend", "friendmessage"}:
            return "non_group_scope"
        if any(marker in lowered for marker in cls._TRANSPORT_MARKERS):
            return "transport_payload"
        if text.startswith(("/", "!")):
            return "command"
        if sum(marker in text for marker in cls._FORTUNE_MARKERS) >= 2:
            return "plugin_output"
        if any(marker in text for marker in cls._CARD_MARKERS) and "http" in lowered:
            return "markdown_card"
        if message_kind in {"image", "interaction", "system"} and not cls._meaningful_text(text):
            return "non_text_event"
        return ""

    @staticmethod
    def _meaningful_text(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or ""))
        compact = re.sub(r"\[[^\]]*(?:图片|image|表情|pic)[^\]]*\]", "", compact, flags=re.IGNORECASE)
        return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", compact))

    @classmethod
    def _sanitize(cls, text: str) -> str:
        value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        # Quoted material is context, not evidence for the current speaker's habit.
        value = "\n".join(line for line in value.split("\n") if not line.lstrip().startswith(">"))
        value = re.sub(r"<[^>]+>", " ", value)
        value = re.sub(r"\[(?:CQ|Image|At|Reply)[^\]]*\]", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"\[[^\]]+\]\(mqqapi://[^)]+\)", " ", value, flags=re.IGNORECASE)
        value = re.sub(r"https?://\S+", " ", value, flags=re.IGNORECASE)
        return " ".join(value.split())

    @classmethod
    def _sanitize_evidence(cls, text: str) -> str:
        value = cls._sanitize(text)
        value = re.sub(
            r"\[(?:图片转述|表情包转述|图片|表情包|image|pic)[：:][^\]]*\]",
            " ",
            value,
            flags=re.IGNORECASE,
        )
        value = re.sub(r"\[(?:图片|表情包|image|pic)\]", " ", value, flags=re.IGNORECASE)
        return " ".join(value.split())

    @classmethod
    def _image_ref_count(cls, message: Any) -> int:
        raw = cls._value(message, "image_refs", [])
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = [raw] if raw.strip() and raw.strip() != "[]" else []
        return len(raw or []) if isinstance(raw, (list, tuple, set)) else 0

    def normalize(self, messages: Iterable[Any] | None) -> list[LearningMessageView]:
        accepted: list[LearningMessageView] = []
        rejected = Counter()
        total = 0
        for message in messages or []:
            total += 1
            if message is None:
                rejected["empty_message"] += 1
                continue
            raw_text = str(self._value(message, "content", "") or "").strip()
            context_text = str(self._value(message, "learning_context_content", "") or raw_text).strip()
            image_count = self._image_ref_count(message)
            has_visual_context = bool(image_count or re.search(r"\[(?:图片转述|表情包转述|图片|表情包)", context_text))
            if not raw_text and not has_visual_context:
                rejected["empty_content"] += 1
                continue
            reason = self._reject_reason(message, raw_text)
            if reason and not (reason in {"non_text_event", "empty_content"} and has_visual_context):
                rejected[reason] += 1
                continue
            cleaned = self._sanitize_evidence(raw_text)
            evidence_eligible = bool(cleaned and self._meaningful_text(cleaned))
            if not evidence_eligible and not has_visual_context:
                rejected["empty_after_sanitize"] += 1
                continue
            context_cleaned = self._sanitize(context_text) or ("[图片]" if has_visual_context else cleaned)
            source_kind = "human_text"
            if has_visual_context:
                source_kind = "human_text_with_image" if evidence_eligible else (
                    "image_transcription" if "图片转述" in context_cleaned or "表情包转述" in context_cleaned else "image_placeholder"
                )
            accepted.append(
                LearningMessageView(
                    message,
                    cleaned,
                    source_kind,
                    context_content=context_cleaned,
                    evidence_eligible=evidence_eligible,
                )
            )
        self.last_report = {
            "input_messages": total,
            "accepted_messages": len(accepted),
            "rejected_messages": total - len(accepted),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "evidence_eligible_messages": sum(item.learning_evidence_eligible for item in accepted),
            "context_only_messages": sum(not item.learning_evidence_eligible for item in accepted),
            "source_kinds": dict(Counter(item.learning_source_kind for item in accepted)),
        }
        return accepted


__all__ = ["LearningInputPolicy", "LearningMessageView"]
