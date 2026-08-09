from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .history_utils import build_friend_umo, extract_text_history


@dataclass(slots=True)
class ConversationHistoryRecord:
    source: str
    chat_type: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: float = 0.0
    message_id: str = ""
    is_current_user: bool = False


class ConversationHistoryService:
    """Single read-only gateway for host and NapCat conversation history."""

    def __init__(self, host_context: Any, config: Any = None) -> None:
        self.host_context = host_context
        self.config = config

    def refresh_config(self, config: Any) -> None:
        self.config = config

    def _limits(self, *, count: int, max_chars: int | None = None) -> tuple[int, int]:
        conversation = getattr(self.config, "conversation", None)
        configured_count = int(getattr(conversation, "history_lookup_max_messages", 20) or 20)
        configured_chars = int(getattr(conversation, "history_lookup_max_chars", 4000) or 4000)
        return (
            max(1, min(int(count or configured_count), configured_count, 50)),
            max(200, min(int(max_chars or configured_chars), configured_chars, 12000)),
        )

    def _enabled_for(self, chat_type: str) -> bool:
        conversation = getattr(self.config, "conversation", None)
        if not bool(getattr(conversation, "history_lookup_enabled", True)):
            return False
        field_name = "history_lookup_group_enabled" if chat_type == "group" else "history_lookup_private_enabled"
        return bool(getattr(conversation, field_name, True))

    async def read_host_private_history(
        self,
        *,
        current_umo: str,
        sender_id: str,
        count: int = 12,
        max_chars: int | None = None,
    ) -> list[ConversationHistoryRecord]:
        if not self._enabled_for("private"):
            return []
        manager = getattr(self.host_context, "conversation_manager", None)
        if manager is None or not sender_id:
            return []
        limit, char_limit = self._limits(count=count, max_chars=max_chars)
        umo = build_friend_umo(current_umo, sender_id)
        try:
            conversation_id = await manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return []
            conversation = await manager.get_conversation(umo, conversation_id)
            history = extract_text_history(list(getattr(conversation, "history", []) or []))
        except Exception as exc:
            logger.debug(f"[ConversationHistory] host private history degraded: {exc}")
            return []
        records: list[ConversationHistoryRecord] = []
        consumed = 0
        for item in reversed(history[-limit:]):
            text = str(item.get("content") or "")
            if consumed + len(text) > char_limit and records:
                break
            consumed += len(text)
            records.append(
                ConversationHistoryRecord(
                    source="astrbot_conversation",
                    chat_type="private",
                    sender_id=sender_id if item.get("role") == "user" else "bot",
                    sender_name="用户" if item.get("role") == "user" else "Bot",
                    text=text,
                    is_current_user=item.get("role") == "user",
                )
            )
        return list(reversed(records))

    async def read_napcat_history(
        self,
        *,
        event: Any,
        chat_type: str,
        target_id: str,
        count: int = 20,
        max_chars: int | None = None,
    ) -> list[ConversationHistoryRecord]:
        normalized_chat_type = "group" if str(chat_type or "").lower() == "group" else "private"
        if not self._enabled_for(normalized_chat_type):
            return []
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None or not target_id:
            return []
        limit, char_limit = self._limits(count=count, max_chars=max_chars)
        action = "get_group_msg_history" if normalized_chat_type == "group" else "get_friend_msg_history"
        key = "group_id" if normalized_chat_type == "group" else "user_id"
        try:
            api_id: Any = int(target_id) if str(target_id).isdigit() else target_id
            payload = await api.call_action(action, **{key: api_id, "message_seq": 0, "count": limit})
        except Exception as exc:
            logger.debug(f"[ConversationHistory] NapCat {normalized_chat_type} history degraded: {exc}")
            return []
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        messages = []
        if isinstance(data, dict):
            for candidate_key in ("messages", "message_list", "items"):
                if isinstance(data.get(candidate_key), list):
                    messages = data[candidate_key]
                    break
        elif isinstance(data, list):
            messages = data
        current_user = str(getattr(event, "get_sender_id", lambda: "")() or "")
        records: list[ConversationHistoryRecord] = []
        consumed = 0
        for message in reversed(messages[-limit:]):
            if not isinstance(message, dict):
                continue
            sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
            sender_id = str(message.get("user_id") or sender.get("user_id") or "")
            sender_name = str(sender.get("card") or sender.get("nickname") or sender_id or "未知")
            raw = message.get("raw_message") or message.get("message") or message.get("message_str") or ""
            text = self._message_text(raw)
            if not text:
                continue
            if consumed + len(text) > char_limit and records:
                break
            consumed += len(text)
            records.append(
                ConversationHistoryRecord(
                    source="napcat_history",
                    chat_type=normalized_chat_type,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                    timestamp=float(message.get("time") or message.get("timestamp") or 0.0),
                    message_id=str(message.get("message_id") or ""),
                    is_current_user=bool(sender_id and sender_id == current_user),
                )
            )
        return list(reversed(records))

    @staticmethod
    def _message_text(raw: Any) -> str:
        if isinstance(raw, str):
            return " ".join(raw.split())
        if not isinstance(raw, list):
            return " ".join(str(raw or "").split())
        parts: list[str] = []
        for segment in raw:
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if segment_type == "text":
                parts.append(str(data.get("text") or ""))
            elif segment_type in {"image", "face", "mface"}:
                parts.append("[图片]" if segment_type == "image" else "[表情]")
        return " ".join(" ".join(parts).split())

    @staticmethod
    def render(records: list[ConversationHistoryRecord], *, heading: str) -> str:
        if not records:
            return f"{heading}：没有读取到近期消息。"
        lines = [f"{heading}：共 {len(records)} 条。"]
        for index, record in enumerate(records, start=1):
            identity = f"{record.sender_name}({record.sender_id})" if record.sender_id else record.sender_name
            message_id = f" message_id={record.message_id}" if record.message_id else ""
            lines.append(f"{index}. [{record.chat_type}/{record.source}] {identity}: {record.text}{message_id}")
        return "\n".join(lines)
