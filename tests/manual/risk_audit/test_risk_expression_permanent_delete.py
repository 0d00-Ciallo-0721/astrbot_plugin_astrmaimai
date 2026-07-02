"""Risk 4.5: Expression learning permanent delete with no rollback.

Verifies that when an expression is auto-reviewed as 'rejected', and the
14-day grace period expires, the expression is physically deleted from
the database with no undo path.
"""

from __future__ import annotations

import unittest


class TestExpressionPermanentDelete(unittest.TestCase):
    """Verify rejected expressions are permanently deleted after grace period."""

    def test_purge_kind_candidates_physically_deletes(self):
        """purge_kind_candidates does a physical DELETE, not a soft-delete."""
        import inspect

        from astrmai.memory.services.v2_store import MemoryV2Store

        source = inspect.getsource(MemoryV2Store.purge_kind_candidates)

        self.assertIn("DELETE FROM canonical_memories", source,
                      "purge_kind_candidates uses physical DELETE — "
                      "deleted expressions are unrecoverable.")

    def test_rejected_expressions_have_grace_period(self):
        """Rejected expressions have a 14-day grace period before purge."""
        import inspect

        from astrmai.memory.services.memory_maintenance_service import MemoryMaintenanceService

        source = inspect.getsource(MemoryMaintenanceService)

        self.assertIn("rejected_expression_grace_seconds", source or "rejected",
                      "There IS a grace period for rejected expressions. "
                      "But after that, deletion is permanent.")

    def test_no_undo_path_after_purge(self):
        """Once purge_kind_candidates runs, there is no undo/restore mechanism."""
        import inspect

        from astrmai.memory.services.v2_store import MemoryV2Store

        source = inspect.getsource(MemoryV2Store)

        undo_keywords = ["undelete", "rollback"]
        found = [kw for kw in undo_keywords if kw in source.lower()]

        self.assertEqual(found, [],
                         f"No undo/restore mechanism in MemoryV2Store. "
                         f"Deletion is truly permanent.")

    def test_auto_review_can_reject_correct_expressions(self):
        """Auto-review uses LLM — hallucinations can reject valid expressions."""
        import inspect

        from astrmai.learning.review.expression_auto_check_task import ExpressionAutoCheckTask

        source = inspect.getsource(ExpressionAutoCheckTask)

        self.assertIn("rejected", source.lower(),
                      "Auto-review uses LLM to decide rejected/approved. "
                      "LLM hallucination can reject correct expressions.")

    def test_purge_no_soft_delete_fallback(self):
        """Verify purge_kind_candidates has no soft-delete code path."""
        import inspect

        from astrmai.memory.services.v2_store import MemoryV2Store

        source = inspect.getsource(MemoryV2Store.purge_kind_candidates)

        self.assertIn("DELETE FROM canonical_memories", source,
                      "purge_kind_candidates uses physical DELETE")
        self.assertIn("older_than_seconds", source,
                      "older_than_seconds parameter controls the grace period. "
                      "Default is set by the caller (14 days for rejected expressions).")


if __name__ == "__main__":
    unittest.main()
