from __future__ import annotations

import asyncio
import time

from astrbot.api import logger


class FollowupManager:
    def __init__(self, runtime):
        self.runtime = runtime
        self._private_wait_tasks: dict[str, asyncio.Task] = {}

    def _resolve_followup_cooldown_seconds(self) -> float:
        attention = getattr(getattr(self.runtime, "config", None), "attention", None)
        configured = float(getattr(attention, "thread_same_speaker_followup_sec", 0.0) or 0.0)
        if configured > 0:
            return configured
        kernel = getattr(self.runtime, "chat_loop_kernel", None)
        return float(getattr(kernel, "DEFAULT_FOLLOWUP_COOLDOWN_SEC", 8.0) or 8.0)

    async def _mark_followup_cooldown(self, chat_id: str) -> None:
        kernel = getattr(self.runtime, "chat_loop_kernel", None)
        if kernel is None:
            return
        cooldown_seconds = self._resolve_followup_cooldown_seconds()
        if cooldown_seconds <= 0:
            return
        await kernel.set_cooldown(
            chat_id,
            "followup",
            time.time() + cooldown_seconds,
            reason="followup_dispatch",
        )

    async def sync_wait_targets(self, chat_id: str, main_event) -> None:
        if not self.runtime.runtime_coordinator:
            return
        targets = list(main_event.get_extra("astrmai_wait_targets", []) or [])
        target_name = str(main_event.get_extra("astrmai_wait_target_name", "") or "")
        await self.runtime.runtime_coordinator.update_wait_targets(chat_id, targets, target_name)
        if getattr(self.runtime, "chat_loop_kernel", None) is not None:
            await self.runtime.chat_loop_kernel.sync_runtime_wait_targets(chat_id, targets, target_name)

    async def _await_private_followup_reply(self, chat_id: str, sender_id: str) -> None:
        current_task = asyncio.current_task()
        try:
            has_reply = await self.runtime.private_chat_manager.wait_for_new_message(sender_id, chat_id=chat_id)
            if has_reply:
                return
            logger.info(f"[{chat_id}] private followup wait timed out")
            if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                await self.runtime.chat_loop_kernel.expire_wait(chat_id, "private_wait_timeout")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(f"[{chat_id}] private followup wait task failed: {exc}")
        finally:
            if self._private_wait_tasks.get(chat_id) is current_task:
                self._private_wait_tasks.pop(chat_id, None)

    def _schedule_private_followup_wait(self, chat_id: str, sender_id: str) -> None:
        existing = self._private_wait_tasks.get(chat_id)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(self._await_private_followup_reply(chat_id, sender_id))
        self._private_wait_tasks[chat_id] = task

    async def finalize_after_reply(self, chat_id: str, main_event, reply_sent: bool) -> None:
        if not reply_sent:
            return

        wait_targets = list(main_event.get_extra("astrmai_wait_targets", []) or [])
        if wait_targets:
            await self._mark_followup_cooldown(chat_id)

        is_private = bool(main_event.get_extra("is_private_chat", False))
        if is_private and self.runtime.private_chat_manager:
            sender_id = str(main_event.get_sender_id())
            if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                await self.runtime.chat_loop_kernel.arm_private_wait(
                    chat_id,
                    {
                        "user_id": sender_id,
                        "target_ids": [sender_id],
                        "target_name": str(main_event.get_sender_name() or ""),
                        "timeout": float(getattr(self.runtime.private_chat_manager, "timeout_sec", 0.0) or 0.0),
                        "reason": "private_followup_wait",
                    },
                )
            await self._mark_followup_cooldown(chat_id)
            self._schedule_private_followup_wait(chat_id, sender_id)
            return

        if main_event.get_group_id() and self.runtime.group_reply_wait_manager:
            if self.runtime.group_reply_wait_manager.register_from_reply_event(main_event):
                if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                    payload = self.runtime.group_reply_wait_manager.get_wait_info(chat_id)
                    if payload:
                        await self.runtime.chat_loop_kernel.arm_group_wait(chat_id, payload)
                await self._mark_followup_cooldown(chat_id)


__all__ = ["FollowupManager"]
