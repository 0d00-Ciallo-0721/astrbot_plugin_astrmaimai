import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict


@dataclass
class RecorderWindow:
    timestamps: Deque[float] = field(default_factory=deque)
    last_trigger_time: float = 0.0


class MessageRecorder:
    """
    按时间窗聚合学习触发器。
    只负责“什么时候该学”，消息事实仍然落在 MessageLog。
    """

    def __init__(
        self,
        window_seconds: int = 60,
        min_messages: int = 30,
        cooldown_seconds: int = 45,
    ):
        self.window_seconds = max(int(window_seconds or 60), 10)
        self.min_messages = max(int(min_messages or 30), 2)
        self.cooldown_seconds = max(int(cooldown_seconds or 45), 5)
        self._windows: Dict[str, RecorderWindow] = {}

    def _get_window(self, scope_id: str) -> RecorderWindow:
        if scope_id not in self._windows:
            self._windows[scope_id] = RecorderWindow()
        return self._windows[scope_id]

    def record(self, scope_id: str, timestamp: float | None = None) -> bool:
        if not scope_id:
            return False

        now = float(timestamp or time.time())
        window = self._get_window(scope_id)
        window.timestamps.append(now)
        cutoff = now - self.window_seconds
        while window.timestamps and window.timestamps[0] < cutoff:
            window.timestamps.popleft()

        if now - window.last_trigger_time < self.cooldown_seconds:
            return False

        if len(window.timestamps) < self.min_messages:
            return False

        window.last_trigger_time = now
        return True

    def clear(self, scope_id: str) -> None:
        self._windows.pop(scope_id, None)
