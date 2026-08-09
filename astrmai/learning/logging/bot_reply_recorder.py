from __future__ import annotations


DEFAULT_FALLBACK_TEXT = "（陷入了短暂的沉默...）"


class BotReplyRecorder:
    ERROR_KEYWORDS = ["Exception", "failed", "Traceback", "请求失败", "APITimeoutError", "All chat models fail"]

    def __init__(self, db_service, fallback_text: str = DEFAULT_FALLBACK_TEXT):
        self.db = db_service
        self.fallback_text = fallback_text

    def should_skip(self, reply_text: str) -> bool:
        text = (reply_text or "").strip()
        if not text or text == self.fallback_text:
            return True
        return any(keyword in text for keyword in self.ERROR_KEYWORDS)

    async def record(self, chat_id: str, bot_id: str, reply_text: str) -> bool:
        if self.should_skip(reply_text):
            return False
        chat_id = str(chat_id or "")
        normalized_chat_id = chat_id.lower()
        conversation_event = {
            "chat_kind": "group" if "groupmessage" in normalized_chat_id else "private",
            "role": "assistant",
            "message_kind": "text",
            "is_bot": True,
            "provenance": "bot_echo",
        }
        if hasattr(self.db, "add_message_log_async"):
            await self.db.add_message_log_async(
                group_id=chat_id,
                sender_id=str(bot_id),
                sender_name="SELF",
                content=reply_text,
                conversation_event=conversation_event,
            )
            return True
        self.db.add_message_log(
            group_id=chat_id,
            sender_id=str(bot_id),
            sender_name="SELF",
            content=reply_text,
            conversation_event=conversation_event,
        )
        return True


async def record_bot_reply(runtime, chat_id: str, bot_id: str, reply_text: str, prefix: str = "") -> bool:
    payload = f"{prefix}{reply_text}" if prefix else reply_text
    config = getattr(runtime, "config", None)
    reply_config = getattr(config, "reply", None)
    fallback_text = getattr(reply_config, "fallback_text", DEFAULT_FALLBACK_TEXT)

    db_service = getattr(runtime, "db_service", None)
    if db_service is None:
        evolution = getattr(runtime, "evolution", None)
        if evolution and hasattr(evolution, "process_bot_reply"):
            await evolution.process_bot_reply(chat_id, bot_id, payload)
            return True
        return False

    recorder = BotReplyRecorder(db_service, fallback_text=fallback_text)
    return await recorder.record(chat_id, bot_id, payload)
