from __future__ import annotations

import unittest

from astrmai.memory.services.vector_migration_decision import decide_vector_migration


class VectorMigrationDecisionTests(unittest.TestCase):
    def _manifest(self, **overrides):
        payload = {
            "embedding_models": ["embed-v2"],
            "provider_source_id": "provider-a",
            "api_base_fingerprint": "base-a",
            "dimension": 1024,
            "generation": 3,
        }
        payload.update(overrides)
        return payload

    def test_full_identity_reuses_active(self):
        result = decide_vector_migration(
            self._manifest(),
            expected_model="embed-v2",
            expected_provider_source="provider-a",
            expected_api_base_fingerprint="base-a",
            expected_dimension=1024,
            physical_readable=True,
            physical_dimension=1024,
            physical_ids={"1", "2"},
            expected_ids={"1", "2"},
        )
        self.assertEqual(result.action, "reuse_active")

    def test_empty_fingerprint_requires_rebuild(self):
        result = decide_vector_migration(
            self._manifest(api_base_fingerprint=""),
            expected_model="embed-v2",
            expected_provider_source="provider-a",
            expected_api_base_fingerprint="base-a",
            expected_dimension=1024,
            physical_readable=True,
            physical_dimension=1024,
        )
        self.assertEqual(result.action, "rebuild_new_generation")

    def test_provider_outage_uses_lexical_fallback(self):
        result = decide_vector_migration(
            self._manifest(api_base_fingerprint=""),
            expected_model="embed-v2",
            expected_provider_source="provider-a",
            expected_api_base_fingerprint="base-a",
            expected_dimension=1024,
            physical_readable=True,
            physical_dimension=1024,
            provider_available=False,
        )
        self.assertEqual(result.action, "lexical_fallback_pending_provider")

    def test_dimension_mismatch_is_blocked(self):
        result = decide_vector_migration(
            self._manifest(),
            expected_model="embed-v2",
            expected_provider_source="provider-a",
            expected_api_base_fingerprint="base-a",
            expected_dimension=1024,
            physical_readable=True,
            physical_dimension=4096,
        )
        self.assertEqual(result.action, "blocked_dimension_mismatch")

    def test_invalid_manifest_dimension_is_blocked(self):
        result = decide_vector_migration(
            self._manifest(dimension="unknown"),
            expected_model="embed-v2",
            expected_provider_source="provider-a",
            expected_api_base_fingerprint="base-a",
            expected_dimension=1024,
            physical_readable=True,
            physical_dimension=1024,
        )
        self.assertEqual(result.action, "blocked_identity_unknown")

    def test_id_set_mismatch_rebuilds_without_touching_old_index(self):
        result = decide_vector_migration(
            self._manifest(),
            expected_model="embed-v2",
            expected_provider_source="provider-a",
            expected_api_base_fingerprint="base-a",
            expected_dimension=1024,
            physical_readable=True,
            physical_dimension=1024,
            physical_ids={"1"},
            expected_ids={"1", "2"},
        )
        self.assertEqual(result.action, "rebuild_new_generation")


if __name__ == "__main__":
    unittest.main()
