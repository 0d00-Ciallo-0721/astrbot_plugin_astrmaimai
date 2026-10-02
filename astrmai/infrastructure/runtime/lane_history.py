from __future__ import annotations

import json
import re
import time
from typing import Any, List, Optional

from astrbot.api import logger

from ...conversation.planning.message_renderer import MessageRenderer
from ..gateway.output_guard import (
    looks_like_internal_event_envelope,
    looks_like_internal_media_context,
    sanitize_visible_reply_text,
)


from .lane_transcript import LaneTranscriptMixin


class LaneHistoryMixin(LaneTranscriptMixin):
    ROTATION_SUMMARY_PREFIX = "历史上下文摘要（轮换桥接，非真实回复）："
    ROTATION_PAIR_LIMIT = 3
    ROTATION_SUMMARY_LINE_LIMIT = 8
    ROTATION_SUMMARY_LINE_CHARS = 120

    @staticmethod
    def _bot_speaker_names(nicknames: list) -> List[str]:
        names: List[str] = ["Bot"]
        if isinstance(nicknames, list):
            names.extend(str(name).strip() for name in nicknames if str(name).strip())
        return list(dict.fromkeys(names))

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            fragments: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if text:
                        fragments.append(str(text))
            return " ".join(fragment for fragment in fragments if fragment).strip()
        if isinstance(content, dict):
            return str(content.get("text") or content.get("content") or "").strip()
        return str(content).strip()

    def _build_rolling_summary(self, history: List[dict]) -> str:
        summary_lines: List[str] = []
        summary_candidates = [
            message
            for message in history
            if not self._is_history_summary(message.get("content", ""))
        ]
        for message in summary_candidates[-self.ROTATION_SUMMARY_LINE_LIMIT :]:
            role = str(message.get("role", "assistant")).strip() or "assistant"
            content = self._stringify_content(message.get("content", ""))
            if not content:
                continue
            content = re.sub(r"\s+", " ", content)
            summary_lines.append(f"{role}: {content[:self.ROTATION_SUMMARY_LINE_CHARS]}")
            if len(summary_lines) >= self.ROTATION_SUMMARY_LINE_LIMIT:
                break
        if not summary_lines:
            return "较早对话摘要：暂无可用内容。"
        return "较早对话摘要：\n" + "\n".join(summary_lines)

    @classmethod
    def _is_history_summary(cls, content: Any) -> bool:
        normalized = str(content or "").strip()
        return normalized.startswith(("较早对话摘要：", cls.ROTATION_SUMMARY_PREFIX))

    @staticmethod
    def _history_identity(message: dict) -> str:
        for key in ("event_id", "message_id", "id"):
            value = str(message.get(key, "") or "").strip()
            if value:
                return f"{key}:{value}"
        role = str(message.get("role", "") or "").strip()
        content = str(message.get("content", "") or "").strip()
        timestamp = str(message.get("timestamp", message.get("created_at", "")) or "").strip()
        return f"{role}:{content}:{timestamp}"

    def _build_rotation_summary(self, history: List[dict]) -> str:
        candidates = [
            message
            for message in history
            if not self._is_history_summary(message.get("content", ""))
        ]
        lines: List[str] = []
        for message in candidates[-self.ROTATION_SUMMARY_LINE_LIMIT :]:
            role = str(message.get("role", "assistant")).strip() or "assistant"
            content = re.sub(r"\s+", " ", self._stringify_content(message.get("content", "")))
            if content:
                lines.append(f"{role}: {content[:self.ROTATION_SUMMARY_LINE_CHARS]}")
        if not lines:
            return ""
        return self.ROTATION_SUMMARY_PREFIX + "\n" + "\n".join(lines)

    def _build_rotation_seed(self, history: List[dict], lane_key: LaneKey) -> List[dict]:
        if not history:
            return []
        prepared = [dict(message) for message in history if isinstance(message, dict)]
        if (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
            prepared, _ = self._sanitize_dialog_history(prepared)

        pairs: List[tuple[int, dict, dict]] = []
        cursor = len(prepared) - 1
        while cursor > 0 and len(pairs) < self.ROTATION_PAIR_LIMIT:
            current = prepared[cursor]
            previous = prepared[cursor - 1]
            if current.get("role") == "assistant" and previous.get("role") == "user":
                pairs.append((cursor - 1, previous, current))
                cursor -= 2
                continue
            cursor -= 1
        if not pairs:
            return []

        pairs.reverse()
        recent: List[dict] = []
        seen: set[str] = set()
        for _index, user_turn, assistant_turn in pairs:
            for turn in (user_turn, assistant_turn):
                identity = self._history_identity(turn)
                if identity in seen:
                    continue
                seen.add(identity)
                recent.append(dict(turn))
        if not recent:
            return []

        earliest_pair_index = pairs[0][0]
        older_history = prepared[:earliest_pair_index]
        summary = self._build_rotation_summary(older_history)
        seed: List[dict] = []
        if summary:
            seed.append({"role": "assistant", "content": summary})
        seed.extend(recent)
        return seed

    def _extract_dialogue_from_meta_prompt(self, content: str) -> str:
        text = self._stringify_content(content)
        if not text:
            return ""
        patterns = [
            r"这是当前你看到的最新消息[:：]?\s*(.+?)(?:\n\n>>|\)$)",
            r"当前你看到的最新消息[:：]?\s*(.+?)(?:\n\n>>|\)$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.DOTALL)
            if match:
                extracted = match.group(1).strip()
                if extracted:
                    return extracted
        if any(marker in text for marker in ("导演旁白", "动作提示", "请仔细阅读设定和前面的剧本")):
            return ""
        return text

    def _sanitize_history_user_text(self, content: Any) -> str:
        cleaned = self._extract_dialogue_from_meta_prompt(content).replace("\ufeff", "").strip()
        if not cleaned:
            return ""
        if looks_like_internal_event_envelope(cleaned) or looks_like_internal_media_context(cleaned):
            return ""
        return cleaned

    def _sanitize_dialog_message(self, message: dict) -> Optional[dict]:
        role = str(message.get("role", "")).strip()
        content = message.get("content", "")
        timestamp = self._message_timestamp(message)
        if role != "user":
            raw_content = self._stringify_content(content)
            if self._is_history_summary(raw_content):
                turn = {"role": role, "content": raw_content}
                if timestamp > 0:
                    turn["timestamp"] = timestamp
                self._copy_history_identity(message, turn)
                return turn
            normalized = sanitize_visible_reply_text(
                self._stringify_content(content),
                fallback_text="",
                speaker_names=self._bot_speaker_names(
                    getattr(getattr(self, "settings", None), "nicknames", []) if getattr(self, "settings", None) else []
                ),
            )
            if not normalized:
                return None
            turn = {"role": role, "content": normalized}
            if timestamp > 0:
                turn["timestamp"] = timestamp
            self._copy_history_identity(message, turn)
            return turn
        cleaned = self._sanitize_history_user_text(content)
        if not cleaned:
            return None
        turn = {"role": role, "content": cleaned}
        if timestamp > 0:
            turn["timestamp"] = timestamp
        self._copy_history_identity(message, turn)
        return turn

    @staticmethod
    def _copy_history_identity(source: dict, target: dict) -> None:
        event_id = source.get("event_id") or source.get("message_id") or source.get("id")
        if isinstance(event_id, str) and event_id.strip():
            target["event_id"] = event_id.strip()[:96]

    @staticmethod
    def _looks_like_social_rendered_line(content: str) -> bool:
        normalized = str(content or "").strip()
        if not normalized:
            return False
        return (
            normalized.startswith("[")
            or normalized.startswith("<message ")
            or "说:" in normalized
            or "说：" in normalized
            or "发了一张" in normalized
            or "刚刚" in normalized
            or "戳了戳" in normalized
        )

    @staticmethod
    def _message_timestamp(message: dict) -> float:
        for key in ("timestamp", "_timestamp", "created_at"):
            try:
                value = float(message.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        return 0.0

    def build_history_turn(self, role: str, content: Any, event_id: str = "") -> Optional[dict]:
        normalized_role = str(role or "").strip()
        if normalized_role == "assistant":
            raw_content = self._stringify_content(content)
            if self._is_history_summary(raw_content):
                turn = {"role": normalized_role, "content": raw_content, "timestamp": time.time()}
                self._copy_history_identity({"event_id": event_id}, turn)
                return turn
            sanitized = sanitize_visible_reply_text(
                self._stringify_content(content),
                fallback_text="",
                speaker_names=self._bot_speaker_names(
                    getattr(getattr(self, "settings", None), "nicknames", []) if getattr(self, "settings", None) else []
                ),
            )
            if not sanitized:
                return None
            turn = {"role": normalized_role, "content": sanitized, "timestamp": time.time()}
            self._copy_history_identity({"event_id": event_id}, turn)
            return turn
        if normalized_role == "user":
            sanitized = self._sanitize_history_user_text(content)
            if not sanitized:
                return None
            turn = {"role": normalized_role, "content": sanitized, "timestamp": time.time()}
            self._copy_history_identity({"event_id": event_id}, turn)
            return turn
        sanitized = self._stringify_content(content)
        if not sanitized:
            return None
        turn = {"role": normalized_role, "content": sanitized, "timestamp": time.time()}
        self._copy_history_identity({"event_id": event_id}, turn)
        return turn

    @staticmethod
    def _render_social_transcript_turn(turn: SocialTranscriptTurn, bot_name: str) -> str:
        if turn.turn_type == "assistant":
            return MessageRenderer.render_bot_turn(turn.content[:180], turn.speaker_name or bot_name)
        if turn.content.startswith("[") or turn.content.startswith("<message "):
            return MessageRenderer.render_social_event(turn.content[:180])
        speaker = turn.speaker_name or "用户"
        if turn.target_name:
            return MessageRenderer.render_user_turn(f"对{turn.target_name}说: {turn.content[:180]}", speaker)
        return MessageRenderer.render_user_turn(turn.content[:180], speaker)

    def _sanitize_dialog_history(self, history: List[dict]) -> tuple[List[dict], bool]:
        sanitized: List[dict] = []
        changed = False
        for message in history:
            if not isinstance(message, dict):
                changed = True
                continue
            normalized = self._sanitize_dialog_message(message)
            if normalized is None:
                changed = True
                continue
            if normalized != message:
                changed = True
            sanitized.append(normalized)
        return sanitized, changed

    def _compact_history(self, normalized: List[dict], lane_key: LaneKey, policy: LanePolicy) -> List[dict]:
        if not normalized:
            return normalized

        if policy.store_mode == "summary_only":
            kept = normalized[-max(policy.max_raw_turns, 1):]
            if len(normalized) > len(kept):
                summary = {"role": "assistant", "content": self._build_rolling_summary(normalized[:-len(kept)])}
                return [summary, *kept][-(policy.max_raw_turns + 1):]
            return kept

        if (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
            max_messages = max(policy.max_raw_turns * 2, 4)
            if len(normalized) <= max_messages:
                return normalized[-max_messages:]
            keep_recent = min(max(policy.max_raw_turns, 4), len(normalized))
            recent_messages = normalized[-keep_recent:]
            older_messages = normalized[:-keep_recent]
            summary = {"role": "assistant", "content": self._build_rolling_summary(older_messages)}
            return [summary, *recent_messages]

        max_messages = max(policy.max_raw_turns, 1)
        if policy.store_mode == "full":
            max_messages *= 2
        return normalized[-max_messages:]

    def _normalize_history(self, history: List[dict], lane_key: LaneKey) -> List[dict]:
        policy = self.get_policy(lane_key)
        normalized: List[dict] = []
        for message in history:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", "")).strip()
            if role == "system":
                continue
            normalized.append(dict(message))
        if (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
            normalized, _ = self._sanitize_dialog_history(normalized)
        return self._compact_history(normalized, lane_key, policy)

    def _load_history(self, conversation: Any) -> List[dict]:
        if not conversation or not getattr(conversation, "history", None):
            return []
        raw_history = conversation.history
        if isinstance(raw_history, str):
            try:
                parsed = json.loads(raw_history)
            except json.JSONDecodeError:
                logger.warning("[LaneManager] Failed to parse lane history JSON; fallback to empty history.")
                return []
        else:
            parsed = raw_history
        if not isinstance(parsed, list):
            return []
        return [dict(item) for item in parsed if isinstance(item, dict)]
