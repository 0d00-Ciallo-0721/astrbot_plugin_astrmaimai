import asyncio
import threading
import time
from time import monotonic
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


@dataclass
class GroupReplyWaitState:
    chat_id: str
    target_user_id: str
    thread_id: str = ""
    target_name: str = ""
    reason: str = ""
    source_user_id: str = ""
    thread_signature: str = ""
    reply_mode: str = ""
    root_event_identity: str = ""
    outbound_message_ids: List[str] = field(default_factory=list)
    turn_generation: int = 0
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    remaining_messages: int = 5


class GroupReplyWaitManager:
    """Manage short-lived group reply follow-up waits."""

    DEFAULT_TIMEOUT_SEC = 30.0
    DEFAULT_MESSAGE_BUDGET = 5
    DEFAULT_MAX_ACTIVE_WAITS_PER_CHAT = 5

    def __init__(
        self,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        message_budget: int = DEFAULT_MESSAGE_BUDGET,
        *,
        threaded_enabled: bool = False,
        max_active_waits_per_chat: int = DEFAULT_MAX_ACTIVE_WAITS_PER_CHAT,
    ):
        self.timeout_sec = float(timeout_sec)
        self.message_budget = int(message_budget)
        self.threaded_enabled = bool(threaded_enabled)
        self.max_active_waits_per_chat = max(1, int(max_active_waits_per_chat or 1))
        self._states: Dict[str, Dict[str, GroupReplyWaitState]] = {}
        self._timeout_tasks: Dict[str, asyncio.Task] = {}
        self._states_lock = threading.RLock()

    def _thread_id_from_event(self, event: AstrMessageEvent) -> str:
        if not self.threaded_enabled:
            return str(event.unified_msg_origin)
        turn_thread_id = str(event.get_extra("astrmai_turn_thread_id", "") or "").strip()
        if turn_thread_id:
            return turn_thread_id
        thread_signature = str(event.get_extra("astrmai_thread_signature", "") or "").strip()
        if thread_signature:
            return thread_signature
        return str(event.unified_msg_origin)

    def refresh_config(self, config) -> None:
        conversation = getattr(config, "conversation", None)
        enabled = bool(getattr(conversation, "group_thread_wait_enabled", True))
        if enabled == self.threaded_enabled:
            return
        with self._states_lock:
            self.threaded_enabled = enabled
            self._states.clear()
            tasks = list(self._timeout_tasks.values())
            self._timeout_tasks.clear()
        for task in tasks:
            if task and not task.done():
                task.cancel()

    @staticmethod
    def _timeout_key(chat_id: str, thread_id: str = "") -> str:
        normalized_chat_id = str(chat_id)
        normalized_thread_id = str(thread_id or chat_id)
        return f"{normalized_chat_id}\x1f{normalized_thread_id}"

    def _get_state_bucket_locked(self, chat_id: str, create: bool = False) -> Dict[str, GroupReplyWaitState]:
        key = str(chat_id)
        raw_bucket = self._states.get(key)
        if isinstance(raw_bucket, dict):
            return raw_bucket
        if raw_bucket is not None:
            migrated = {key: raw_bucket}
            self._states[key] = migrated
            return migrated
        if not create:
            return {}
        bucket: Dict[str, GroupReplyWaitState] = {}
        self._states[key] = bucket
        return bucket

    def _pop_timeout_task_locked(self, chat_id: str, thread_id: str = "") -> Optional[asyncio.Task]:
        if thread_id:
            task = self._timeout_tasks.pop(self._timeout_key(chat_id, thread_id), None)
            if task is not None:
                return task
        return self._timeout_tasks.pop(str(chat_id), None)

    def _cancel_timeout_task(self, chat_id: str) -> None:
        with self._states_lock:
            tasks = [
                task
                for key, task in list(self._timeout_tasks.items())
                if key == str(chat_id) or key.startswith(f"{chat_id}\x1f")
            ]
            for key in list(self._timeout_tasks):
                if key == str(chat_id) or key.startswith(f"{chat_id}\x1f"):
                    self._timeout_tasks.pop(key, None)
        for task in tasks:
            if task and not task.done():
                task.cancel()

    @staticmethod
    def _build_wait_info_snapshot(state: GroupReplyWaitState, now: float) -> dict:
        return {
            "chat_id": state.chat_id,
            "thread_id": state.thread_id,
            "target_user_id": state.target_user_id,
            "target_name": state.target_name,
            "reason": state.reason,
            "thread_signature": state.thread_signature,
            "reply_mode": state.reply_mode,
            "remaining_messages": state.remaining_messages,
            "remaining_seconds": max(0.0, state.expires_at - now),
        }

    def _arm_timeout_task(self, chat_id: str, thread_id: str, expected_state: GroupReplyWaitState) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        timeout_key = self._timeout_key(chat_id, thread_id)

        async def _expire_later():
            expired = False
            try:
                await asyncio.sleep(self.timeout_sec)
                with self._states_lock:
                    bucket = self._get_state_bucket_locked(chat_id)
                    state = bucket.get(thread_id)
                    if state is expected_state:
                        bucket.pop(thread_id, None)
                        if not bucket:
                            self._states.pop(chat_id, None)
                        task = asyncio.current_task()
                        if self._timeout_tasks.get(timeout_key) is task:
                            self._timeout_tasks.pop(timeout_key, None)
                        expired = True
                if expired:
                    logger.info(f"[GroupWait] wait expired by timeout for chat={chat_id}, thread={thread_id}")
            except asyncio.CancelledError:
                return
            finally:
                with self._states_lock:
                    task = asyncio.current_task()
                    if self._timeout_tasks.get(timeout_key) is task:
                        self._timeout_tasks.pop(timeout_key, None)

        task = loop.create_task(_expire_later())
        with self._states_lock:
            bucket = self._get_state_bucket_locked(chat_id)
            if bucket.get(thread_id) is expected_state:
                self._timeout_tasks[timeout_key] = task
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

    @staticmethod
    def _reply_to_message_id(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_chain = getattr(message_obj, "message", None) or []
        for component in message_chain:
            if type(component).__name__.lower() != "reply" and str(
                getattr(component, "type", "")
            ).lstrip("_").lower() != "reply":
                continue
            for attr_name in ("message_id", "id", "reply_id", "target_id"):
                value = str(getattr(component, attr_name, "") or "").strip()
                if value:
                    return value
        return ""

    def _is_strong_resume(
        self,
        event: AstrMessageEvent,
        state: GroupReplyWaitState,
        *,
        reply_id_matched: bool,
    ) -> bool:
        if not self.threaded_enabled:
            if state.thread_signature:
                return self._looks_like_thread_resume(event)
            return True
        if reply_id_matched:
            return True
        incoming_signature = str(event.get_extra("astrmai_thread_signature", "") or "").strip()
        if incoming_signature and state.thread_signature and incoming_signature == state.thread_signature:
            return True
        return self._looks_like_thread_resume(event)

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

        thread_id = self._thread_id_from_event(event)
        state = GroupReplyWaitState(
            chat_id=chat_id,
            target_user_id=target_user_id,
            thread_id=thread_id,
            target_name=target_name,
            reason=reason,
            source_user_id=str(event.get_sender_id() or ""),
            thread_signature=str(event.get_extra("astrmai_thread_signature", "") or ""),
            reply_mode=str(event.get_extra("astrmai_reply_mode", "") or ""),
            root_event_identity=str(event.get_extra("astrmai_focus_thread_root_reason", "") or ""),
            outbound_message_ids=list(
                dict.fromkeys(
                    str(item)
                    for item in (event.get_extra("astrmai_reply_outbound_message_ids", []) or [])
                    if str(item)
                )
            ),
            turn_generation=int(event.get_extra("astrmai_turn_generation", 0) or 0),
            expires_at=monotonic() + self.timeout_sec,
            remaining_messages=self.message_budget,
        )
        tasks_to_cancel = []
        with self._states_lock:
            bucket = self._get_state_bucket_locked(chat_id, create=True)
            old_task = self._pop_timeout_task_locked(chat_id, thread_id)
            if old_task is not None:
                tasks_to_cancel.append(old_task)
            if (
                self.threaded_enabled
                and thread_id not in bucket
                and len(bucket) >= self.max_active_waits_per_chat
            ):
                evicted_thread_id, _ = min(
                    bucket.items(),
                    key=lambda item: float(getattr(item[1], "created_at", 0.0) or 0.0),
                )
                bucket.pop(evicted_thread_id, None)
                evicted_task = self._pop_timeout_task_locked(chat_id, evicted_thread_id)
                if evicted_task is not None:
                    tasks_to_cancel.append(evicted_task)
            bucket[thread_id] = state
        for task in tasks_to_cancel:
            if task and not task.done():
                task.cancel()
        logger.info(
            f"[GroupWait] armed wait for chat={chat_id}, thread={thread_id}, target={target_user_id}, reason={reason}, budget={self.message_budget}, timeout={self.timeout_sec}s"
        )
        self._arm_timeout_task(chat_id, thread_id, state)
        event.set_extra("astrmai_group_wait", {
            "chat_id": chat_id,
            "thread_id": thread_id,
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
        incoming_thread_id = self._thread_id_from_event(event)
        incoming_thread_signature = str(event.get_extra("astrmai_thread_signature", "") or "").strip()
        incoming_turn_thread_id = str(event.get_extra("astrmai_turn_thread_id", "") or "").strip()
        has_explicit_thread = bool(incoming_thread_signature or incoming_turn_thread_id)
        reply_to_message_id = self._reply_to_message_id(event)
        timeout_task = None
        action = "NONE"
        resume_payload = None
        log_target = ""
        now = monotonic()
        sender_id = str(event.get_sender_id() or "")
        with self._states_lock:
            bucket = self._get_state_bucket_locked(chat_id)
            state = bucket.get(incoming_thread_id)
            reply_id_matched = False
            if state is None and self.threaded_enabled and reply_to_message_id:
                for candidate_thread_id, candidate_state in bucket.items():
                    if reply_to_message_id in candidate_state.outbound_message_ids:
                        incoming_thread_id = candidate_thread_id
                        state = candidate_state
                        reply_id_matched = True
                        break
            unique_target_matched = False
            if state is None and self.threaded_enabled and not has_explicit_thread and sender_id:
                target_matches = [
                    (candidate_thread_id, candidate_state)
                    for candidate_thread_id, candidate_state in bucket.items()
                    if candidate_state.target_user_id == sender_id
                ]
                if len(target_matches) == 1:
                    incoming_thread_id, state = target_matches[0]
                    unique_target_matched = True
            if (
                state is None
                and not self.threaded_enabled
                and not has_explicit_thread
                and len(bucket) == 1
            ):
                incoming_thread_id, state = next(iter(bucket.items()))
            if not state:
                return "NONE"

            if now >= state.expires_at:
                bucket.pop(incoming_thread_id, None)
                if not bucket:
                    self._states.pop(chat_id, None)
                timeout_task = self._pop_timeout_task_locked(chat_id, incoming_thread_id)
                action = "EXPIRED_TIMEOUT"
            elif sender_id and sender_id == state.target_user_id:
                if not (unique_target_matched or has_explicit_thread) and not self._is_strong_resume(
                    event,
                    state,
                    reply_id_matched=reply_id_matched,
                ):
                    state.remaining_messages -= 1
                    if state.remaining_messages <= 0:
                        bucket.pop(incoming_thread_id, None)
                        if not bucket:
                            self._states.pop(chat_id, None)
                        timeout_task = self._pop_timeout_task_locked(chat_id, incoming_thread_id)
                        action = "EXPIRED_BUDGET"
                    else:
                        action = "OBSERVED_TARGET"
                        log_target = state.target_name or state.target_user_id
                else:
                    bucket.pop(incoming_thread_id, None)
                    if not bucket:
                        self._states.pop(chat_id, None)
                    timeout_task = self._pop_timeout_task_locked(chat_id, incoming_thread_id)
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
                    bucket.pop(incoming_thread_id, None)
                    if not bucket:
                        self._states.pop(chat_id, None)
                    timeout_task = self._pop_timeout_task_locked(chat_id, incoming_thread_id)
                    action = "EXPIRED_BUDGET"
                else:
                    action = "OBSERVED"

        if timeout_task and not timeout_task.done():
            timeout_task.cancel()

        if action == "EXPIRED_TIMEOUT":
            logger.info(f"[GroupWait] wait expired by timeout for chat={chat_id}, thread={incoming_thread_id}")
            return "EXPIRED"
        if action == "EXPIRED_BUDGET":
            logger.info(f"[GroupWait] wait expired after message budget for chat={chat_id}, thread={incoming_thread_id}")
            return "EXPIRED"
        if action == "OBSERVED_TARGET":
            logger.info(
                f"[GroupWait] observed target activity but kept waiting for same thread in chat={chat_id}, "
                f"thread={incoming_thread_id}, target={log_target}"
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
                f"thread={incoming_thread_id}, target={resume_payload['target_label']}"
            )
            return "RESUME"

        return action

    def cancel_wait(self, chat_id: str, reason: str = "", thread_id: str = "") -> bool:
        with self._states_lock:
            bucket = self._get_state_bucket_locked(chat_id)
            if thread_id:
                states = [bucket.pop(str(thread_id), None)]
                if not bucket:
                    self._states.pop(str(chat_id), None)
                timeout_tasks = [self._pop_timeout_task_locked(chat_id, str(thread_id))]
            else:
                states = list(bucket.values())
                self._states.pop(str(chat_id), None)
                timeout_tasks = [
                    task
                    for key, task in list(self._timeout_tasks.items())
                    if key == str(chat_id) or key.startswith(f"{chat_id}\x1f")
                ]
                for key in list(self._timeout_tasks):
                    if key == str(chat_id) or key.startswith(f"{chat_id}\x1f"):
                        self._timeout_tasks.pop(key, None)
        for timeout_task in timeout_tasks:
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
        states = [state for state in states if state is not None]
        if not states:
            return False
        for state in states:
            logger.info(
                f"[GroupWait] cancelled wait for chat={chat_id}, target={state.target_user_id}, reason={reason or 'unspecified'}"
            )
        return True

    def get_wait_info(self, chat_id: str, thread_id: str = "") -> Optional[dict]:
        with self._states_lock:
            bucket = self._get_state_bucket_locked(chat_id)
            if thread_id:
                state = bucket.get(str(thread_id))
            elif bucket:
                state = max(bucket.values(), key=lambda item: float(getattr(item, "created_at", 0.0) or 0.0))
            else:
                state = None
            if not state:
                return None
            return self._build_wait_info_snapshot(state, monotonic())

    def list_waits(self, chat_id: str) -> list[dict]:
        with self._states_lock:
            bucket = self._get_state_bucket_locked(chat_id)
            now = monotonic()
            states = sorted(
                bucket.values(),
                key=lambda item: float(getattr(item, "created_at", 0.0) or 0.0),
            )
            return [self._build_wait_info_snapshot(state, now) for state in states]
