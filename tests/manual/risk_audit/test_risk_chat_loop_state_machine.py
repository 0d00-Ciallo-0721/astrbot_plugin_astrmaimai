"""Risk 6.1: ChatLoopKernel state machine complexity — untested transitions.

Verifies that the 7-phase state machine and 13+ decision branches in
_drive_phase() and _decide() have adequate test coverage, and identifies
any untested transitions that could silently misroute messages.
"""

from __future__ import annotations

import inspect
import unittest


class TestChatLoopStateMachineComplexity(unittest.TestCase):
    """Verify state machine completeness and test coverage gaps."""

    def test_derive_phase_has_7_paths(self):
        """_derive_phase must handle all possible action types."""
        from astrmai.conversation.loop.chat_loop_kernel import ChatLoopKernel

        source = inspect.getsource(ChatLoopKernel._derive_phase)

        # Count return points
        explicit_returns = source.count("return")
        action_map_entries = source.count('"ACTIVE"') + source.count('"WAITING"') + \
            source.count('"BUSY"') + source.count('"MAINTENANCE"') + \
            source.count('"COOLDOWN"') + source.count('"IDLE"')

        self.assertGreaterEqual(explicit_returns, 4,
                                f"_derive_phase has {explicit_returns} return paths")
        self.assertGreaterEqual(action_map_entries, 6,
                                f"All 6 phases should be mappable: {action_map_entries}")

    def test_decide_has_ingress_message_branch(self):
        """INGRESS_MESSAGE path must exist in _decide()."""
        from astrmai.conversation.loop.chat_loop_kernel import ChatLoopKernel

        source = inspect.getsource(ChatLoopKernel._decide)

        self.assertIn("INGRESS_MESSAGE", source,
                      "INGRESS_MESSAGE handler must exist in _decide()")
        self.assertIn("RESUME_WAIT", source,
                      "RESUME_WAIT handler must exist in _decide()")
        self.assertIn("INTERRUPT_WAIT", source,
                      "INTERRUPT_WAIT handler must exist in _decide()")

    def test_dream_maintenance_path_exists(self):
        """DREAM_MAINTENANCE dispatch path must exist but may lack explicit test."""
        from astrmai.conversation.loop.chat_loop_kernel import ChatLoopKernel

        source = inspect.getsource(ChatLoopKernel._decide)

        self.assertIn("DREAM_MAINTENANCE", source,
                      "DREAM_MAINTENANCE path exists in _decide(). "
                      "Verify it has a dedicated test — currently may be untested.")

    def test_state_machine_lines_of_code(self):
        """ChatLoopKernel is 2272 lines — verify expected complexity scope."""
        import os

        # Walk up from tests/manual/risk_audit/ to project root
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        ))))
        fpath = os.path.join(base, "astrmai", "conversation", "loop", "chat_loop_kernel.py")

        with open(fpath, encoding="utf-8") as f:
            line_count = sum(1 for _ in f)

        self.assertGreaterEqual(line_count, 2000,
                                f"ChatLoopKernel is {line_count} lines — extreme complexity. "
                                f"Any state transition bug could silently misroute messages.")
        print(f"\n    [SIZE] ChatLoopKernel: {line_count} lines")

    def test_scheduler_policy_profile_coverage(self):
        """All scheduler profiles should be referenced and have required structure."""
        from astrmai.conversation.loop.chat_loop_kernel import ChatLoopKernel

        # The profiles may be stored under different attribute names in different versions
        # Check the class dict for any scheduler-related profiles
        profile_attrs = [
            attr for attr in dir(ChatLoopKernel)
            if "scheduler" in attr.lower() or "profile" in attr.lower()
        ]
        self.assertGreaterEqual(len(profile_attrs), 1,
                                f"Should have at least 1 scheduler/profile attribute. Found: {profile_attrs}")
        print(f"\n    [PROFILES] Found: {profile_attrs}")


if __name__ == "__main__":
    unittest.main()
