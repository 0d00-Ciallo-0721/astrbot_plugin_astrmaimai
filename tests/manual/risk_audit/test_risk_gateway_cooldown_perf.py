"""Risk 4.4: Model gateway cooldown O(n) performance degradation.

Verifies that _cleanup_model_cooldowns() scans the entire _model_cooldowns
dict on every LLM call, and with many expired entries this linear scan adds
measurable latency under the global semaphore.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock


class TestGatewayCooldownPerformance(unittest.TestCase):
    """Verify cooldown cleanup is O(n) and measure its impact."""

    def test_cleanup_scans_all_entries(self):
        """_cleanup_model_cooldowns iterates the entire dict every call."""
        import inspect

        from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin

        source = inspect.getsource(GatewayPolicyMixin._cleanup_model_cooldowns)

        self.assertIn("list(cooldowns.items())", source,
                      "_cleanup_model_cooldowns iterates all entries — O(n) per call.")

    def test_cooldown_cleanup_called_on_every_llm_request(self):
        """_cleanup_model_cooldowns is called from _filter_cooldown_attempt_queue."""
        import inspect

        from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin

        source = inspect.getsource(GatewayPolicyMixin._filter_cooldown_attempt_queue)

        self.assertIn("_cleanup_model_cooldowns", source,
                      "_cleanup_model_cooldowns() is called on every "
                      "_filter_cooldown_attempt_queue() — which runs on every LLM call.")

    def test_measure_cooldown_cleanup_latency(self):
        """Measure cleanup latency with 500 expired cooldown entries."""
        from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin

        class FakeGateway(GatewayPolicyMixin):
            pass

        gw = FakeGateway()
        now = time.time()

        gw._model_cooldowns = {}
        for i in range(500):
            gw._model_cooldowns[(f"pool_{i % 10}", f"model_{i}")] = {
                "until": now - 3600,
                "reason": "rate_limit",
            }

        start = time.perf_counter()
        for _ in range(100):
            gw._cleanup_model_cooldowns()
            # Re-populate to keep size constant
            for i in range(500):
                gw._model_cooldowns[(f"pool_{i % 10}", f"model_{i}")] = {
                    "until": now - 3600,
                    "reason": "rate_limit",
                }
        elapsed = time.perf_counter() - start
        avg_us = (elapsed / 100) * 1_000_000

        self.assertLess(avg_us, 100_000,
                        f"Cleanup of 500 expired entries averaged {avg_us:.1f} µs/call. "
                        f"Within acceptable range (<100ms).")

        print(f"\n    [PERF] _cleanup_model_cooldowns with 500 entries: {avg_us:.1f} µs/call")

    def test_semaphore_serializes_all_llm_calls(self):
        """The global semaphore means cleanup latency blocks all callers."""
        import asyncio

        from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway

        # _global_semaphore is set in __init__, not as a class-level attribute
        import inspect as _inspect
        src = _inspect.getsource(GlobalModelGateway.__init__)
        self.assertIn("_global_semaphore", src,
            "GlobalModelGateway uses _global_semaphore to serialize LLM calls. "
            "Any latency in cooldown cleanup blocks ALL concurrent callers."
        )


if __name__ == "__main__":
    unittest.main()
