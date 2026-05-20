import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

from astrbot.api import logger


@dataclass
class PrivateSession:
    user_id: str
    new_message_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_message_time: float = field(default_factory=time.time)
    is_bot_waiting: bool = False
    pending_messages: list = field(default_factory=list)
    turn_count: int = 0


class PrivateChatManager:
    DEFAULT_TIMEOUT_SEC = 300.0
    MAX_SESSIONS = 100

    def __init__(self, config=None):
        self.config = config
        self._sessions: Dict[str, PrivateSession] = {}
        self._chat_to_user: Dict[str, str] = {}
        self._cleanup_lock = asyncio.Lock()

        if config and hasattr(config, "private_chat"):
            self.timeout_sec = config.private_chat.wait_timeout_sec
        else:
            self.timeout_sec = self.DEFAULT_TIMEOUT_SEC

    async def signal_new_message(self, user_id: str, message_str: str = "", chat_id: str = ""):
        session = self._get_or_create_session(user_id)
        self._bind_chat_session(chat_id, user_id)
        session.last_message_time = time.time()
        session.turn_count += 1

        if message_str:
            session.pending_messages.append(message_str)

        if session.is_bot_waiting:
            session.new_message_event.set()
            logger.debug(f"[PrivateChat] message interrupted waiting session for {user_id}")

    async def wait_for_new_message(
        self,
        user_id: str,
        timeout: Optional[float] = None,
        chat_id: str = "",
    ) -> bool:
        session = self._get_or_create_session(user_id)
        self._bind_chat_session(chat_id, user_id)
        wait_timeout = timeout if timeout is not None else self.timeout_sec

        session.new_message_event.clear()
        session.is_bot_waiting = True
        logger.debug(f"[PrivateChat] waiting for {user_id} reply with timeout={wait_timeout}s")

        try:
            await asyncio.wait_for(session.new_message_event.wait(), timeout=wait_timeout)
            logger.debug(f"[PrivateChat] {user_id} replied before timeout")
            return True
        except asyncio.TimeoutError:
            logger.info(f"[PrivateChat] {user_id} timed out after {wait_timeout}s")
            return False
        finally:
            session.is_bot_waiting = False

    def get_pending_messages(self, user_id: str) -> list:
        session = self._sessions.get(user_id)
        if not session:
            return []
        msgs = list(session.pending_messages)
        session.pending_messages.clear()
        return msgs

    def get_session_info(self, user_id: str) -> Optional[dict]:
        session = self._sessions.get(user_id)
        if not session:
            return None
        return {
            "user_id": user_id,
            "turn_count": session.turn_count,
            "is_bot_waiting": session.is_bot_waiting,
            "last_message_time": session.last_message_time,
            "silence_sec": time.time() - session.last_message_time,
        }

    def get_session_info_by_chat_id(self, chat_id: str) -> Optional[dict]:
        user_id = self._resolve_user_id_from_chat_id(chat_id)
        if not user_id:
            return None
        return self.get_session_info(user_id)

    def close_session(self, user_id: str):
        if user_id in self._sessions:
            session = self._sessions.pop(user_id)
            session.new_message_event.set()
            stale_chat_ids = [chat_id for chat_id, mapped_user_id in self._chat_to_user.items() if mapped_user_id == user_id]
            for chat_id in stale_chat_ids:
                self._chat_to_user.pop(chat_id, None)
            logger.debug(f"[PrivateChat] closed session for {user_id}")

    async def cleanup_stale_sessions(self, max_silence_min: float = 30.0):
        async with self._cleanup_lock:
            now = time.time()
            stale = []
            for uid, session in self._sessions.items():
                silence_min = (now - session.last_message_time) / 60.0
                if silence_min > max_silence_min and not session.is_bot_waiting:
                    stale.append(uid)
            for uid in stale:
                self.close_session(uid)
            if stale:
                logger.debug(f"[PrivateChat] cleaned {len(stale)} stale sessions")

    def _get_or_create_session(self, user_id: str) -> PrivateSession:
        if user_id not in self._sessions:
            if len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(self._sessions.items(), key=lambda x: x[1].last_message_time)
                self.close_session(oldest[0])
            self._sessions[user_id] = PrivateSession(user_id=user_id)
        return self._sessions[user_id]

    def _bind_chat_session(self, chat_id: str, user_id: str) -> None:
        chat_id = str(chat_id or "").strip()
        user_id = str(user_id or "").strip()
        if not chat_id or not user_id:
            return
        self._chat_to_user[chat_id] = user_id

    def _resolve_user_id_from_chat_id(self, chat_id: str) -> str:
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return ""
        mapped_user_id = str(self._chat_to_user.get(chat_id, "") or "").strip()
        if mapped_user_id:
            return mapped_user_id
        if ":" in chat_id:
            return str(chat_id.rsplit(":", 1)[-1] or "").strip()
        return ""


__all__ = ["PrivateChatManager", "PrivateSession"]
