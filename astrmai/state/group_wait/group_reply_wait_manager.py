import asyncio
import threading
import time
from time import monotonic
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
    thread_signature: str = ""
    reply_mode: str = ""
    root_event_identity: str = ""
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
        self._states_lock = threading.RLock()

    def _pop_timeout_task_locked(self, chat_id: str) -> Optional[asyncio.Task]:
        return self._timeout_tasks.pop(str(chat_id), None)

    def _cancel_timeout_task(self, chat_id: str) -> None:
        with self._states_lock:
            task = self._pop_timeout_task_locked(chat_id)
        if task and not task.done():
            task.cancel()

    @staticmethod
    def _build_wait_info_snapshot(state: GroupReplyWaitState, now: float) -> dict:
        return {
            "chat_id": state.chat_id,
            "target_user_id": state.target_user_id,
            "target_name": state.target_name,
            "reason": state.reason,
            "thread_signature": state.thread_signature,
            "reply_mode": state.reply_mode,
            "remaining_messages": state.remaining_messages,
            "remaining_seconds": max(0.0, state.expires_at - now),
        }

    def _arm_timeout_task(self, chat_id: str, expected_state: GroupReplyWaitState) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _expire_later():
            expired = False
            try:
                await asyncio.sleep(self.timeout_sec)
                with self._states_lock:
                    state = self._states.get(chat_id)
                    if state is expected_state:
                        self._states.pop(chat_id, None)
                        task = asyncio.current_task()
                        if self._timeout_tasks.get(chat_id) is task:
                            self._timeout_tasks.pop(chat_id, None)
                        expired = True
                if expired:
                    logger.info(f"[GroupWait] wait expired by timeout for chat={chat_id}")
            except asyncio.CancelledError:
                return
            finally:
                with self._states_lock:
                    task = asyncio.current_task()
                    if self._timeout_tasks.get(chat_id) is task:
                        self._timeout_tasks.pop(chat_id, None)

        task = loop.create_task(_expire_later())
        with self._states_lock:
            if self._states.get(chat_id) is expected_state:
                self._timeout_tasks[chat_id] = task
            else:
                task.cancel()

    @staticmethod
    def _looks_like_thread_resume(event: AstrMessageEvent) -> bool:
        message_obj = getattr(event, "message_obj", None)
        message_chain = getattr(message_obj, "message", None)
        if message_chain:
            for component in message_chain:
                component_name = type(component).__name__
                if component_name in {"Reply", "At"}:
                    return True
        raw_text = str(getattr(event, "message_str", "") or "").strip()
        return raw_text.startswith("@")

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

        state = GroupReplyWaitState(
            chat_id=chat_id,
            target_user_id=target_user_id,
            target_name=target_name,
            reason=reason,
            source_user_id=str(event.get_sender_id() or ""),
            thread_signature=str(event.get_extra("astrmai_thread_signature", "") or ""),
            reply_mode=str(event.get_extra("astrmai_reply_mode", "") or ""),
            root_event_identity=str(event.get_extra("astrmai_focus_thread_root_reason", "") or ""),
            expires_at=monotonic() + self.timeout_sec,
            remaining_messages=self.message_budget,
        )
        with self._states_lock:
            old_task = self._pop_timeout_task_locked(chat_id)
            self._states[chat_id] = state
        if old_task and not old_task.done():
            old_task.cancel()
        logger.info(
            f"[GroupWait] armed wait for chat={chat_id}, target={target_user_id}, reason={reason}, budget={self.message_budget}, timeout={self.timeout_sec}s"
        )
        self._arm_timeout_task(chat_id, state)
        event.set_extra("astrmai_group_wait", {
            "chat_id": chat_id,
            "target_user_id": target_user_id,
            "target_name": target_name,
            "reason": reason,
            "expires_at": state.expires_at,
            "thread_signature": state.thread_signature,
        })
        return True

    def handle_incoming_message(self, event: AstrMessageEvent) -> str:
        if not event.get_group_id():
            return "NONE"

        chat_id = str(event.unified_msg_origin)
        timeout_task = None
        action = "NONE"
        resume_payload = None
        log_target = ""
        now = monotonic()
        sender_id = str(event.get_sender_id() or "")
        with self._states_lock:
            state = self._states.get(chat_id)
            if not state:
                return "NONE"

            if now >= state.expires_at:
                self._states.pop(chat_id, None)
                timeout_task = self._pop_timeout_task_locked(chat_id)
                action = "EXPIRED_TIMEOUT"
            elif sender_id and sender_id == state.target_user_id:
                if state.thread_signature and not self._looks_like_thread_resume(event):
                    state.remaining_messages -= 1
                    if state.remaining_messages <= 0:
                        self._states.pop(chat_id, None)
                        timeout_task = self._pop_timeout_task_locked(chat_id)
                        action = "EXPIRED_BUDGET"
                    else:
                        action = "OBSERVED_TARGET"
                        log_target = state.target_name or state.target_user_id
                else:
                    self._states.pop(chat_id, None)
                    timeout_task = self._pop_timeout_task_locked(chat_id)
                    target_label = state.target_name or state.target_user_id
                    resume_payload = {
                        "target_label": target_label,
                        "target_user_id": state.target_user_id,
                        "target_name": state.target_name,
                        "thread_signature": state.thread_signature,
                        "reply_mode": state.reply_mode,
                    }
                    action = "RESUME"
            else:
                state.remaining_messages -= 1
                if state.remaining_messages <= 0:
                    self._states.pop(chat_id, None)
                    timeout_task = self._pop_timeout_task_locked(chat_id)
                    action = "EXPIRED_BUDGET"
                else:
                    action = "OBSERVED"

        if timeout_task and not timeout_task.done():
            timeout_task.cancel()

        if action == "EXPIRED_TIMEOUT":
            logger.info(f"[GroupWait] wait expired by timeout for chat={chat_id}")
            return "EXPIRED"
        if action == "EXPIRED_BUDGET":
            logger.info(f"[GroupWait] wait expired after message budget for chat={chat_id}")
            return "EXPIRED"
        if action == "OBSERVED_TARGET":
            logger.info(
                f"[GroupWait] observed target activity but kept waiting for same thread in chat={chat_id}, "
                f"target={log_target}"
            )
            return "OBSERVED"
        if action == "RESUME" and resume_payload is not None:
            event.set_extra("astrmai_force_engage", True)
            event.set_extra("astrmai_group_wait_resume", True)
            event.set_extra("astrmai_group_wait_target_id", resume_payload["target_user_id"])
            event.set_extra("astrmai_group_wait_target_name", resume_payload["target_name"])
            if resume_payload["thread_signature"]:
                event.set_extra("astrmai_thread_signature", resume_payload["thread_signature"])
            if resume_payload["reply_mode"]:
                event.set_extra("astrmai_reply_mode", resume_payload["reply_mode"])
            event.set_extra(
                "astrmai_wait_resume_thought",
                f"{resume_payload['target_label']}接上了你刚才的话题，立刻自然地继续回应。",
            )
            logger.info(
                f"[GroupWait] target matched and resumed main flow for chat={chat_id}, "
                f"target={resume_payload['target_label']}"
            )
            return "RESUME"

        return action

    def cancel_wait(self, chat_id: str, reason: str = "") -> bool:
        with self._states_lock:
            state = self._states.pop(str(chat_id), None)
            timeout_task = self._pop_timeout_task_locked(chat_id)
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
        if not state:
            return False
        logger.info(
            f"[GroupWait] cancelled wait for chat={chat_id}, target={state.target_user_id}, reason={reason or 'unspecified'}"
        )
        return True

    def get_wait_info(self, chat_id: str) -> Optional[dict]:
        with self._states_lock:
            state = self._states.get(str(chat_id))
            if not state:
                return None
            return self._build_wait_info_snapshot(state, monotonic())
