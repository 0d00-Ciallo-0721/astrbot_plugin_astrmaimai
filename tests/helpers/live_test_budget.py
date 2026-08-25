from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class LiveBudgetExceeded(RuntimeError):
    pass


@dataclass
class LiveCallBudget:
    max_calls: int = 40
    max_concurrency: int = 1
    timeout_sec: float = 90.0
    _calls_started: int = 0
    _calls_completed: int = 0
    _calls_failed: int = 0
    _durations: list[float] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.max_calls = max(1, int(self.max_calls))
        self.max_concurrency = max(1, int(self.max_concurrency))
        self.timeout_sec = max(0.1, float(self.timeout_sec))
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def run(self, label: str, operation: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            if self._calls_started >= self.max_calls:
                raise LiveBudgetExceeded(f"live call budget exhausted: {label}")
            self._calls_started += 1
        started = time.perf_counter()
        async with self._semaphore:
            try:
                result = await asyncio.wait_for(operation(), timeout=self.timeout_sec)
            except BaseException:
                async with self._lock:
                    self._calls_failed += 1
                raise
            else:
                async with self._lock:
                    self._calls_completed += 1
                    self._durations.append(time.perf_counter() - started)
                return result

    def summary(self) -> dict[str, Any]:
        durations = sorted(self._durations)

        def percentile(ratio: float) -> float:
            if not durations:
                return 0.0
            index = min(len(durations) - 1, round((len(durations) - 1) * ratio))
            return round(durations[index], 3)

        return {
            "max_calls": self.max_calls,
            "max_concurrency": self.max_concurrency,
            "timeout_sec": self.timeout_sec,
            "calls_started": self._calls_started,
            "calls_completed": self._calls_completed,
            "calls_failed": self._calls_failed,
            "p50_sec": percentile(0.50),
            "p95_sec": percentile(0.95),
            "max_sec": round(max(durations), 3) if durations else 0.0,
        }


__all__ = ["LiveBudgetExceeded", "LiveCallBudget"]
