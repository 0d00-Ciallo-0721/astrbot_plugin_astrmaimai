from __future__ import annotations

import asyncio

from .models import ChatLoopState


class ChatLoopStateStore:
    def __init__(self) -> None:
        self._states: dict[str, ChatLoopState] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, chat_id: str) -> ChatLoopState:
        async with self._lock:
            state = self._states.get(chat_id)
            if state is None:
                state = ChatLoopState(chat_id=chat_id)
                self._states[chat_id] = state
            return state

    async def save(self, state: ChatLoopState) -> None:
        async with self._lock:
            self._states[state.chat_id] = state

    async def get(self, chat_id: str) -> ChatLoopState | None:
        async with self._lock:
            return self._states.get(chat_id)

    async def count(self) -> int:
        async with self._lock:
            return len(self._states)

    def count_sync(self) -> int:
        return len(self._states)

    async def snapshot(self) -> dict[str, ChatLoopState]:
        async with self._lock:
            return dict(self._states)
