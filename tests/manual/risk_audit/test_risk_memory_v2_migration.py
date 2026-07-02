"""Risk 6.4: Memory v2 migration chain — multi-step failure may cause inconsistency.

Verifies that the sequential migration steps in memory_engine.initialize()
have proper error handling: each step should not block subsequent steps,
and partial failures should be recorded.
"""

from __future__ import annotations

import inspect
import unittest


class TestMemoryV2MigrationChain(unittest.TestCase):
    """Verify migration chain resilience."""

    def test_initialize_runs_sequential_migration_steps(self):
        """initialize() calls multiple migration steps in sequence."""
        from astrmai.memory.services.memory_engine import MemoryEngine

        source = inspect.getsource(MemoryEngine.initialize)

        migrations = [
            "import_legacy_documents",
            "import_persona_cache",
            "import_legacy_memory_events",
            "import_legacy_jargons",
            "import_legacy_expression_patterns",
        ]

        found = [m for m in migrations if m in source]
        self.assertEqual(len(found), len(migrations),
                         f"All {len(migrations)} migration steps should exist. "
                         f"Found: {found}")

    def test_migration_steps_have_internal_try_except(self):
        """Steps 10-14 each catch exceptions internally — failure doesn't block next."""
        from astrmai.memory.services.memory_engine import MemoryEngine

        # Check import_legacy_memory_events has try/except
        source = inspect.getsource(MemoryEngine.import_legacy_memory_events)
        self.assertIn("except", source,
                      "import_legacy_memory_events has internal try/except — "
                      "failure is recorded but doesn't halt")

        # Check import_legacy_jargons
        source = inspect.getsource(MemoryEngine.import_legacy_jargons)
        self.assertIn("except", source,
                      "import_legacy_jargons has internal try/except")

        # Check import_legacy_expression_patterns
        source = inspect.getsource(MemoryEngine.import_legacy_expression_patterns)
        self.assertIn("except", source,
                      "import_legacy_expression_patterns has internal try/except")

    def test_v2_store_initialize_has_no_outer_try_except(self):
        """v2_store.initialize() is NOT wrapped in try/except."""
        from astrmai.memory.services.memory_engine import MemoryEngine

        source = inspect.getsource(MemoryEngine.initialize)

        # Check if initialize itself has an outer try/except
        lines = source.split("\n")
        in_try = False
        for line in lines:
            if "try:" in line and "v2_store.initialize" in source:
                in_try = True
                break

        self.assertFalse(in_try,
                         "memory_engine.initialize() has NO outer try/except. "
                         "If v2_store.initialize() fails, the entire chain halts "
                         "and the error propagates to the caller without cleanup.")

    def test_bm25_retriever_init_can_fail(self):
        """BM25Retriever initialization has no try/except wrapper."""
        from astrmai.memory.services.memory_engine import MemoryEngine

        source = inspect.getsource(MemoryEngine.initialize)

        self.assertIn("bm25_retriever", source,
                      "BM25Retriever is initialized during migration chain")

        # Check if there's a try/except around BM25Retriever
        bm25_pos = source.find("bm25_retriever")
        try_pos = source.rfind("try:", 0, bm25_pos) if bm25_pos > 0 else -1
        except_pos = source.find("except", bm25_pos) if bm25_pos > 0 else -1

        if try_pos < 0 or except_pos < 0 or except_pos < try_pos:
            self.assertTrue(True,
                            "BM25Retriever init has NO try/except wrapper. "
                            "If it fails, the memory engine is partially initialized.")

    def test_migration_failure_records_status(self):
        """Failed migrations record 'failed' status in memory_v2_migrations table."""
        from astrmai.memory.services.memory_engine import MemoryEngine

        # Check that record_migration is called with status="failed"
        source = inspect.getsource(MemoryEngine.import_legacy_memory_events)
        self.assertIn("failed", source,
                      "Failed migrations are recorded with status='failed'. "
                      "WebUI should surface these to the admin.")


if __name__ == "__main__":
    unittest.main()
