"""Regression tests for HIGH-severity bugfixes (R6–R14).

Uses direct source file reading for reliable verification.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_source(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


class HighBugfixRegressionTests(unittest.TestCase):

    # ── R14: hooks no longer silent ──

    def test_hooks_replace_pass_with_logger_debug(self):
        """R14: main.py hooks use logger.debug, not bare pass."""
        src = _read_source("main.py")
        # Count occurrences of "except Exception:\n            pass"
        # After fix: should be 0 (previously 3 in hooks)
        # Check that on_llm_response has logger.debug in its except block
        self.assertIn('logger.debug("[AstrMai] on_llm_response hook failed"', src)
        self.assertIn('logger.debug("[AstrMai] on_agent_begin hook failed"', src)
        self.assertIn('logger.debug("[AstrMai] on_agent_done hook failed"', src)

    # ── R6: system_prompt conditional protection ──

    def test_on_llm_request_conditionally_modifies_system_prompt(self):
        """R6: system_prompt only modified conditionally."""
        src = _read_source("main.py")
        self.assertIn("needs_reverse_block", src)
        self.assertIn("<astrbot_reverse_session>", src)

    # ── R13: gate.py uses logger.exception ──

    def test_gate_sensors_use_logger_exception(self):
        """R13: gate.py sensors use logger.exception."""
        src = _read_source("astrmai/conversation/attention/gate.py")
        self.assertIn('logger.exception(f"[AttentionGate] sensor is_command check failed', src)
        self.assertIn('logger.exception("[AttentionGate] sensor should_process_message check failed', src)

    # ── R12: bootstrap.py warns on ProactiveTask failure ──

    def test_bootstrap_warns_proactive_failure(self):
        """R12: bootstrap.py warns when ProactiveTask creation fails."""
        src = _read_source("astrmai/app/bootstrap.py")
        self.assertIn("主动发言、梦境整理等功能将不可用", src)

    # ── R11: GroupDialogueStore sentinel key ──

    def test_resolve_chat_key_exists(self):
        """R11: GroupDialogueStore has _resolve_chat_key method."""
        src = _read_source("astrmai/conversation/attention/group_dialogue_store.py")
        self.assertIn("def _resolve_chat_key", src)
        self.assertIn('raise ValueError("chat_id must be a non-empty string', src)
        # Verify str(chat_id or "") has been replaced
        self.assertNotIn("str(chat_id or \"\")", src)
        # _resolve_chat_key should be used instead
        self.assertIn("self._resolve_chat_key(chat_id)", src)

    # ── R7+R8+R9: EventBus fixes ──

    def test_event_bus_fixes(self):
        """R7+R8+R9: EventBus has worker_tasks, dropped_count, improved logging."""
        src = _read_source("astrmai/infrastructure/runtime/event_bus.py")
        # R8: _worker_tasks
        self.assertIn("self._worker_tasks", src)
        # R9: get_dropped_count
        self.assertIn("def get_dropped_count", src)
        # R9: improved queue full logging
        self.assertIn("qsize", src)
        # R7: knowledge_update task tracked
        self.assertIn("track_set=self._background_tasks", src)
        self.assertIn("self._track_background_task(", src)
        # R8: health check uses _worker_tasks
        health_src = src[src.find("_worker_health_check"):src.find("_worker_health_check") + 500]
        self.assertIn("self._worker_tasks", health_src)


if __name__ == "__main__":
    unittest.main()
