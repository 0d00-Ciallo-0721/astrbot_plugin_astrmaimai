from __future__ import annotations

from astrbot.api import logger


class FollowupManager:
    def __init__(self, runtime):
        self.runtime = runtime

    async def sync_wait_targets(self, chat_id: str, main_event) -> None:
        if not self.runtime.runtime_coordinator:
            return
        await self.runtime.runtime_coordinator.update_wait_targets(
            chat_id,
            list(main_event.get_extra("astrmai_wait_targets", []) or []),
            str(main_event.get_extra("astrmai_wait_target_name", "") or ""),
        )

    async def finalize_after_reply(self, chat_id: str, main_event, reply_sent: bool) -> None:
        if not reply_sent:
            return

        is_private = bool(main_event.get_extra("is_private_chat", False))
        if is_private and self.runtime.private_chat_manager:
            sender_id = str(main_event.get_sender_id())
            has_reply = await self.runtime.private_chat_manager.wait_for_new_message(sender_id)
            if not has_reply:
                logger.info(f"[{chat_id}] 私聊用户长时间未回复，会话已自然休眠。")
            return

        if main_event.get_group_id() and self.runtime.group_reply_wait_manager:
            self.runtime.group_reply_wait_manager.register_from_reply_event(main_event)


__all__ = ["FollowupManager"]
