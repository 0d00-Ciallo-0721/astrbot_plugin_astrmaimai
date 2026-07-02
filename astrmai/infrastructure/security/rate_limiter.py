"""Lightweight TokenBucket rate limiter for WebUI API and LLM call reuse.

No third-party dependencies.  Async-safe via asyncio.Lock.
"""

import asyncio
import time


class TokenBucket:
    """Token bucket rate limiter.

    Usage:
        limiter = TokenBucket(rate=1.0, capacity=5)
        if await limiter.consume():
            # allowed
    """

    def __init__(self, rate: float, capacity: int) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.rate)
        self.last_refill = now

    async def consume(self, tokens: int = 1) -> bool:
        """Consume tokens. Returns True if allowed, False if rate-limited."""
        if tokens <= 0:
            return True
        async with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    @property
    def available(self) -> float:
        """Current available tokens (non-blocking, approximate)."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        return min(float(self.capacity), self.tokens + elapsed * self.rate)


__all__ = ["TokenBucket"]
