from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ChatRuntimeState:
    sys2_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    executor_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    executor_pending: int = 0
    wait_targets: List[str] = field(default_factory=list)
    wait_target_name: str = ""


class ChatRuntimeCoordinator:
    def __init__(self) -> None:
        self._states: Dict[str, ChatRuntimeState] = {}
        self._lock = asyncio.Lock()

    async def _get_state(self, chat_id: str) -> ChatRuntimeState:
        async with self._lock:
            if chat_id not in self._states:
                self._states[chat_id] = ChatRuntimeState()
            return self._states[chat_id]

    async def get_sys2_lock(self, chat_id: str) -> asyncio.Lock:
        state = await self._get_state(chat_id)
        return state.sys2_lock

    async def try_acquire_executor(self, chat_id: str, max_pending: int = 2) -> Optional[asyncio.Lock]:
        async with self._lock:
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            if state.executor_pending >= max_pending:
                return None
            state.executor_pending += 1
            return state.executor_lock

    async def release_executor(self, chat_id: str) -> None:
        async with self._lock:
            state = self._states.get(chat_id)
            if not state:
                return
            state.executor_pending = max(0, state.executor_pending - 1)

    async def update_wait_targets(self, chat_id: str, targets: List[str], target_name: str = "") -> None:
        async with self._lock:
            state = self._states.setdefault(chat_id, ChatRuntimeState())
            state.wait_targets = list(dict.fromkeys([str(target) for target in targets if str(target)]))
            state.wait_target_name = target_name or ""

    async def get_wait_targets(self, chat_id: str) -> List[str]:
        state = await self._get_state(chat_id)
        return state.wait_targets[:]

    async def get_wait_target_name(self, chat_id: str) -> str:
        state = await self._get_state(chat_id)
        return state.wait_target_name
