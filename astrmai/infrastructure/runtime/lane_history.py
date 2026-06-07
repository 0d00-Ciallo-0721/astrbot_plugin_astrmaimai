from __future__ import annotations

import json
import re
import time
from typing import Any, List, Optional

from astrbot.api import logger

from ...conversation.planning.message_renderer import MessageRenderer
from ..gateway.output_guard import is_safe_visible_text, sanitize_visible_reply_text


from .lane_transcript import LaneTranscriptMixin


class LaneHistoryMixin(LaneTranscriptMixin):
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
        for message in history:
            role = str(message.get("role", "assistant")).strip() or "assistant"
            content = self._stringify_content(message.get("content", ""))
            if not content:
                continue
            content = re.sub(r"\s+", " ", content)
            summary_lines.append(f"{role}: {content[:120]}")
            if len(summary_lines) >= 8:
                break
        if not summary_lines:
            return "较早对话摘要：暂无可用内容。"
        return "较早对话摘要：\n" + "\n".join(summary_lines)

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

    def _sanitize_dialog_message(self, message: dict) -> Optional[dict]:
        role = str(message.get("role", "")).strip()
        content = message.get("content", "")
        timestamp = self._message_timestamp(message)
        if role != "user":
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
            return turn
        cleaned = self._extract_dialogue_from_meta_prompt(content)
        if cleaned and not is_safe_visible_text(cleaned):
            return None
        if not cleaned:
            return None
        turn = {"role": role, "content": cleaned}
        if timestamp > 0:
            turn["timestamp"] = timestamp
        return turn

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

    def build_history_turn(self, role: str, content: Any) -> Optional[dict]:
        normalized_role = str(role or "").strip()
        if normalized_role == "assistant":
            sanitized = sanitize_visible_reply_text(
                self._stringify_content(content),
                fallback_text="",
                speaker_names=self._bot_speaker_names(
                    getattr(getattr(self, "settings", None), "nicknames", []) if getattr(self, "settings", None) else []
                ),
            )
            if not sanitized:
                return None
            return {"role": normalized_role, "content": sanitized, "timestamp": time.time()}
        if normalized_role == "user":
            sanitized = self._extract_dialogue_from_meta_prompt(content)
            if not sanitized or not is_safe_visible_text(sanitized):
                return None
            return {"role": normalized_role, "content": sanitized, "timestamp": time.time()}
        sanitized = self._stringify_content(content)
        if not sanitized:
            return None
        return {"role": normalized_role, "content": sanitized, "timestamp": time.time()}

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
