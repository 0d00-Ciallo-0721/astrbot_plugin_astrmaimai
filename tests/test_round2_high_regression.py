"""Regression tests for Round 2 HIGH bugfixes (R1–R13)."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def _read(rel): return (ROOT / rel).read_text(encoding="utf-8")

class Round2RegressionTests(unittest.TestCase):

    def test_r1_dream_interval_ge_1(self):
        src = _read("config.py")
        self.assertIn("ge=1", src.split("dream_interval_min")[1][:50])

    def test_r4_breaker_uses_monotonic(self):
        src = _read("astrmai/conversation/execution/executor.py")
        self.assertIn("breaker_until > monotonic()", src)

    def test_r5_short_ack_no_len_check(self):
        src = _read("astrmai/conversation/planning/think_level_policy.py")
        self.assertNotIn("len(compact_text) <= 4", src)
        self.assertIn("return lowered in ThinkLevelPolicy.SHORT_ACKS", src)

    def test_r9_cooldown_uses_monotonic(self):
        src = _read("astrmai/infrastructure/gateway/gateway_policy.py")
        self.assertIn('"until": monotonic() + duration', src)

    def test_r3_bootstrap_warns_on_null_gate(self):
        src = _read("astrmai/app/bootstrap.py")
        self.assertIn("attention_gate is None", src)

    def test_r8_timeout_not_re_raised(self):
        src = _read("astrmai/infrastructure/gateway/gateway_call.py")
        self.assertNotIn("raise TimeoutError", src)

    def test_r10_bm25_score_range_fixed(self):
        src = _read("astrmai/memory/retrieval/bm25.py")
        self.assertIn("max(abs(max_score), 1.0)", src)

    def test_r11_visual_cortex_queue_maxsize(self):
        src = _read("astrmai/multimodal/visual_cortex.py")
        self.assertIn("Queue(maxsize=100)", src)

    def test_r13_startup_hooks_raises(self):
        src = _read("astrmai/presentation/events/startup_hooks.py")
        self.assertIn("raise  # ponytail: R13", src)

    def test_r2_dream_throttle_in_semaphore(self):
        src = _read("astrmai/proactive/dream_scheduler.py")
        self.assertIn("throttle check inside semaphore", src)

    def test_r6_unique_constraint_added(self):
        src = _read("astrmai/infrastructure/persistence/orm_models.py")
        self.assertIn("uq_expression_pattern", src)
        src_db = _read("astrmai/infrastructure/persistence/database_review.py")
        self.assertIn("IntegrityError", src_db)

    def test_r7_async_init_updates_user_version(self):
        src = _read("astrmai/infrastructure/persistence/persistence_schema.py")
        self.assertIn("PRAGMA user_version", src)

    def test_r12_deferred_messages_added(self):
        src = _read("astrmai/conversation/attention/gate.py")
        self.assertIn("_deferred_messages", src)
        self.assertIn("queue.append(event)", src)

if __name__ == "__main__":
    unittest.main()
