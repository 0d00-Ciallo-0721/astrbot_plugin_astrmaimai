import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


@dataclass
class GroupReplyWaitState:
    chat_id: str
    target_user_id: str
    target_name: str = ""
    reason: str = ""
    source_user_id: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    remaining_messages: int = 5


class GroupReplyWaitManager:
    """Manage short-lived group reply follow-up waits."""

    DEFAULT_TIMEOUT_SEC = 30.0
    DEFAULT_MESSAGE_BUDGET = 5

    def __init__(self, timeout_sec: float = DEFAULT_TIMEOUT_SEC, message_budget: int = DEFAULT_MESSAGE_BUDGET):
        self.timeout_sec = float(timeout_sec)
        self.message_budget = int(message_budget)
        self._states: Dict[str, GroupReplyWaitState] = {}
        self._timeout_tasks: Dict[str, asyncio.Task] = {}

    def _cancel_timeout_task(self, chat_id: str) -> None:
        task = self._timeout_tasks.pop(str(chat_id), None)
        if task and not task.done():
            task.cancel()

    def _arm_timeout_task(self, chat_id: str) -> None:
        self._cancel_timeout_task(chat_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _expire_later():
            try:
                await asyncio.sleep(self.timeout_sec)
                state = self._states.pop(chat_id, None)
                if state:
                    logger.info(f"[GroupWait] wait expired by timeout for chat={chat_id}")
            except asyncio.CancelledError:
                return
            finally:
                self._timeout_tasks.pop(chat_id, None)

        self._timeout_tasks[chat_id] = loop.create_task(_expire_later())

    def register_from_reply_event(self, event: AstrMessageEvent) -> bool:
        if not event.get_group_id():
            return False

        chat_id = str(event.unified_msg_origin)
        target_user_id = ""
        target_name = ""
        reason = ""

        wait_targets = event.get_extra("astrmai_wait_targets", []) or []
        if wait_targets:
            target_user_id = str(wait_targets[0])
            target_name = str(event.get_extra("astrmai_wait_target_name", "") or "")
            reason = "bot_at_target"
        elif event.get_extra("astrmai_group_direct_wakeup", False):
            target_user_id = str(event.get_sender_id() or "")
            target_name = str(event.get_sender_name() or "")
            reason = "direct_wakeup_reply"

        if not target_user_id:
            return False

        self._states[chat_id] = GroupReplyWaitState(
            chat_id=chat_id,
            target_user_id=target_user_id,
            target_name=target_name,
            reason=reason,
            source_user_id=str(event.get_sender_id() or ""),
            expires_at=time.time() + self.timeout_sec,
            remaining_messages=self.message_budget,
        )
        logger.info(
            f"[GroupWait] armed wait for chat={chat_id}, target={target_user_id}, reason={reason}, budget={self.message_budget}, timeout={self.timeout_sec}s"
        )
        self._arm_timeout_task(chat_id)
        return True

    def handle_incoming_message(self, event: AstrMessageEvent) -> str:
        if not event.get_group_id():
            return "NONE"

        chat_id = str(event.unified_msg_origin)
        state = self._states.get(chat_id)
        if not state:
            return "NONE"

        now = time.time()
        if now >= state.expires_at:
            self._states.pop(chat_id, None)
            self._cancel_timeout_task(chat_id)
            logger.info(f"[GroupWait] wait expired by timeout for chat={chat_id}")
            return "EXPIRED"

        sender_id = str(event.get_sender_id() or "")
        if sender_id and sender_id == state.target_user_id:
            self._states.pop(chat_id, None)
            self._cancel_timeout_task(chat_id)
            target_label = state.target_name or state.target_user_id
            event.set_extra("astrmai_force_engage", True)
            event.set_extra("astrmai_group_wait_resume", True)
            event.set_extra("astrmai_group_wait_target_id", state.target_user_id)
            event.set_extra("astrmai_group_wait_target_name", state.target_name)
            event.set_extra("astrmai_wait_resume_thought", f"{target_label}接上了你刚才的话题，立刻自然地继续回应。")
            logger.info(f"[GroupWait] target matched and resumed main flow for chat={chat_id}, target={target_label}")
            return "RESUME"

        state.remaining_messages -= 1
        if state.remaining_messages <= 0:
            self._states.pop(chat_id, None)
            self._cancel_timeout_task(chat_id)
            logger.info(f"[GroupWait] wait expired after message budget for chat={chat_id}")
            return "EXPIRED"

        return "OBSERVED"

    def cancel_wait(self, chat_id: str, reason: str = "") -> bool:
        state = self._states.pop(str(chat_id), None)
        self._cancel_timeout_task(chat_id)
        if not state:
            return False
        logger.info(
            f"[GroupWait] cancelled wait for chat={chat_id}, target={state.target_user_id}, reason={reason or 'unspecified'}"
        )
        return True

    def get_wait_info(self, chat_id: str) -> Optional[dict]:
        state = self._states.get(str(chat_id))
        if not state:
            return None
        return {
            "chat_id": state.chat_id,
            "target_user_id": state.target_user_id,
            "target_name": state.target_name,
            "reason": state.reason,
            "remaining_messages": state.remaining_messages,
            "remaining_seconds": max(0.0, state.expires_at - time.time()),
        }
