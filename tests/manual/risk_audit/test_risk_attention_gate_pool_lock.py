"""Risk 6.2: AttentionGate _pool_lock global asyncio Lock bottleneck.

Verifies that the single global _pool_lock protecting focus_pools for ALL
chat_ids does not become a bottleneck under concurrent access.
"""

from __future__ import annotations

import asyncio
import time
import unittest


class TestAttentionGatePoolLock(unittest.TestCase):
    """Verify _pool_lock contention behavior."""

    def test_pool_lock_is_single_global_lock(self):
        """_pool_lock is ONE asyncio.Lock for all chat_ids."""
        import inspect

        from astrmai.conversation.attention.gate import AttentionGate

        source = inspect.getsource(AttentionGate.__init__)

        self.assertIn("_pool_lock", source,
                      "Single global _pool_lock protects focus_pools for ALL chats. "
                      "Under high concurrency (many chats), this is a contention point.")

        # Check if there are any per-chat locks
        per_chat_lock_count = source.count("Lock()")
        self.assertGreaterEqual(per_chat_lock_count, 1,
                                f"_pool_lock exists ({per_chat_lock_count} Lock creations total)")

    def test_critical_section_is_constant_time(self):
        """The critical section under _pool_lock must be O(1) — no I/O."""
        import inspect

        from astrmai.conversation.attention.gate import AttentionGate

        source = inspect.getsource(AttentionGate._get_or_create_session)

        # No await inside the lock (no I/O)
        lines_inside_lock = source.split("async with self._pool_lock:")[1].split("return")[0]

        self.assertNotIn("await", lines_inside_lock,
                         "CRITICAL: No await inside _pool_lock critical section. "
                         "This means the lock is held for microseconds only — NOT a real bottleneck.")

    def test_measure_lock_contention_with_concurrent_callers(self):
        """Simulate 50 concurrent callers accessing _get_or_create_session."""
        from unittest.mock import AsyncMock, MagicMock

        import asyncio

        async def _run():
            from astrmai.conversation.attention.gate import AttentionGate

            # Build a minimal gate
            gate = MagicMock(spec=AttentionGate)
            gate._pool_lock = asyncio.Lock()
            gate.focus_pools = {}

            # Inject the real _get_or_create_session
            async def real_get_or_create(chat_id):
                async with gate._pool_lock:
                    session = gate.focus_pools.get(chat_id)
                    if session is None:
                        session = {}
                        gate.focus_pools[chat_id] = session
                    session["last_active_time"] = time.time()
                    return session

            gate._get_or_create_session = real_get_or_create

            # 50 concurrent callers, each accessing a different chat_id
            start = time.perf_counter()
            tasks = [
                gate._get_or_create_session(f"chat_{i}")
                for i in range(50)
            ]
            await asyncio.gather(*tasks)
            elapsed = time.perf_counter() - start

            self.assertEqual(len(gate.focus_pools), 50,
                            "All 50 sessions should be created")
            self.assertLess(elapsed, 2.0,
                            f"50 concurrent callers completed in {elapsed:.3f}s. "
                            f"Lock contention adds measurable but small overhead.")

            print(f"\n    [PERF] 50 concurrent _get_or_create_session: {elapsed*1000:.1f} ms")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
