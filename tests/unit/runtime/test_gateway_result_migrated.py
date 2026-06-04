from types import SimpleNamespace
import unittest

from astrmai.infrastructure.gateway.gateway_result import GatewayResultMixin


class _GatewayResultHarness(GatewayResultMixin):
    pass


class GatewayResultMigratedTests(unittest.TestCase):
    def setUp(self):
        self.mixin = _GatewayResultHarness()

    def test_enrich_cache_debug_meta_does_not_infer_prefix_stable_from_hash_presence(self):
        meta = self.mixin._enrich_cache_debug_meta(
            {},
            workload_policy=SimpleNamespace(
                stable_prefix_hash="abc123",
                cache_affinity_enabled=True,
            ),
            usage={"cached_usage_supported": True},
        )

        self.assertNotIn("prefix_stable", meta)
        self.assertTrue(meta["cache_affinity_enabled"])
        self.assertTrue(meta["cached_usage_supported"])

    def test_build_cache_observation_requires_explicit_prefix_stable_signal(self):
        observation = self.mixin._build_cache_observation(
            {"input_tokens": 100, "input_cached": 0, "output_tokens": 10},
            {
                "request_session_id": "sess-1",
                "cache_affinity_enabled": True,
            },
        )

        self.assertEqual(
            observation["cache_ready_reasons"],
            ["session_reuse", "cache_affinity_enabled"],
        )
        self.assertNotIn("semantic_system_hash_stable", observation["cache_ready_reasons"])

    def test_build_cache_observation_keeps_explicit_prefix_stable_signal(self):
        observation = self.mixin._build_cache_observation(
            {"input_tokens": 100, "input_cached": 0, "output_tokens": 10},
            {
                "prefix_stable": True,
                "provider_visible_hash_stable": True,
            },
        )

        self.assertEqual(
            observation["cache_ready_reasons"],
            ["semantic_system_hash_stable", "provider_visible_hash_stable"],
        )


__all__ = ["GatewayResultMigratedTests"]
