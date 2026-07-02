"""Regression tests for MEDIUM-severity bugfixes (R15–R21)."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_source(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


class MediumBugfixRegressionTests(unittest.TestCase):

    def test_r15_safe_create_task_uses_create_task(self):
        """R15: safe_create_task uses asyncio.create_task, not ensure_future."""
        src = _read_source("astrmai/shared/helpers/plugin_helpers.py")
        self.assertIn("asyncio.create_task", src)
        self.assertNotIn("asyncio.ensure_future", src)
        self.assertIn("hasattr(t, 'get_name')", src)

    def test_r16_projection_failure_logged(self):
        """R16: index projection failure is logged."""
        src = _read_source("astrmai/memory/services/memory_write_service.py")
        self.assertIn("index projection failed for", src)

    def test_r19_shutdown_flush_not_silent(self):
        """R19: shutdown flush no longer uses bare pass."""
        src = _read_source("astrmai/app/lifecycle.py")
        self.assertIn("shutdown flush failed", src)
        # The old bare 'pass' after except Exception: should be gone in that context
        self.assertNotIn("# ponytail: secondary flush must not crash the shutdown", src)

    def test_r21_compute_hot_score_guards_access_count(self):
        """R21: compute_hot_score uses max(0.0, ...) for access_count."""
        src = _read_source("astrmai/memory/services/memory_scoring.py")
        self.assertIn("max(0.0, float(candidate.access_count or 0))", src)

    def test_r17_session_lock_eviction_checks_locked(self):
        """R17: session lock eviction checks .locked() before popping."""
        src = _read_source("astrmai/memory/services/v2_store.py")
        # Should have the locked() check
        self.assertIn(".locked()", src)

    def test_r18_handoff_registry_no_one_shot_cache(self):
        """R18: HandoffRegistry no longer uses one-shot _loaded cache for returns."""
        src = _read_source("astrmai/workmode/tools/handoff_registry.py")
        # Should NOT have the old one-shot pattern
        self.assertNotIn("if self._loaded:\n            return list(self._dynamic_agents)", src)
        self.assertIn("existing_names", src)
        self.assertIn("# ponytail: re-scan every call", src)

    def test_r20_user_profile_touctou_re_read(self):
        """R20: observe_user_activity re-reads profile under lock."""
        src = _read_source("astrmai/state/user_profile_service.py")
        self.assertIn("TOCTOU (R20)", src)
        self.assertIn("await self._get_profile_inner(user_id)", src)


if __name__ == "__main__":
    unittest.main()
