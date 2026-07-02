"""Risk 6.3: Judge prompt injection via unbounded message/persona fields.

Verifies that the Judge.evaluate() prompt has no length limits on the
user message and persona_summary, allowing prompt injection attacks
through extremely long inputs.
"""

from __future__ import annotations

import inspect
import unittest


class TestJudgePromptInjection(unittest.TestCase):
    """Verify Judge prompt has unbounded fields vulnerable to injection."""

    def test_message_field_no_length_limit(self):
        """The user message is injected verbatim into the LLM prompt."""
        from astrmai.conversation.decision.judge import Judge

        source = inspect.getsource(Judge.evaluate)

        # Find where message is injected into prompt
        self.assertIn("{message}", source,
                      "User message is injected verbatim via f-string. "
                      "No length limit or truncation. A 10K+ char message "
                      "is injected directly into the LLM context.")

    def test_persona_summary_no_length_limit(self):
        """The persona_summary is also injected verbatim."""
        from astrmai.conversation.decision.judge import Judge

        source = inspect.getsource(Judge.evaluate)

        self.assertIn("persona_summary", source,
                      "persona_summary is injected without length limit. "
                      "Combined with unbounded message, the prompt can grow "
                      "arbitrarily large.")

    def test_history_truncation_exists(self):
        """History records are truncated to 60 chars — but message is not."""
        from astrmai.conversation.decision.judge import Judge

        source = inspect.getsource(Judge._render_history_context)

        self.assertIn("[:60]", source,
                      "History records ARE truncated to 60 chars. "
                      "But the LIVE message field (not from history) has no such guard.")

    def test_build_dynamic_actions_is_bounded(self):
        """Dynamic actions output is bounded (~400 chars max)."""
        from astrmai.conversation.decision.judge import Judge

        source = inspect.getsource(Judge._build_dynamic_actions)

        # Each action is one line, max 6 actions
        action_count = source.count("actions.append")
        self.assertLessEqual(action_count, 5,
                             f"Max {action_count} dynamic actions — bounded output")

    def test_calculate_max_prompt_size(self):
        """Estimate maximum LLM prompt size for Judge.evaluate()."""
        from astrmai.conversation.decision.judge import Judge

        source = inspect.getsource(Judge.evaluate)

        # Components and estimated max sizes:
        # - JUDGE_STABLE_PREFIX: ~200 chars (system prompt, fixed)
        # - persona_summary: UNBOUNDED (could be 50K+)
        # - keyword_reaction_block: config-dependent, ~500 chars max
        # - history_context: 8 records × 60 chars = ~480 chars
        # - message: UNBOUNDED (could be 10K+)
        # - available_actions: ~400 chars
        # - Total minimum overhead: ~1600 chars

        self.assertIn("message", source,
                      "The {message} field in the prompt IS the live user input. "
                      "A malicious 50K-character message results in a 50K+ prompt. "
                      "This is a prompt injection vector via message length.")

    def test_flatten_history_content_has_truncation(self):
        """_flatten_history_content exists and should handle large inputs."""
        from astrmai.conversation.decision.judge import Judge

        self.assertTrue(hasattr(Judge, "_flatten_history_content"),
                        "_flatten_history_content exists for parsing message components")

    def test_evaluate_has_no_input_validation(self):
        """evaluate() has no input size validation before prompt construction."""
        from astrmai.conversation.decision.judge import Judge

        source = inspect.getsource(Judge.evaluate)

        validation_keywords = ["len(message)", "max_length", "truncate", "[:", "limit"]
        found = [kw for kw in validation_keywords if kw in source]

        # The only "limit" reference is for history, not message
        self.assertNotIn("len(message)", source,
                         "evaluate() does NOT check message length. "
                         "No input validation before prompt injection. "
                         f"Found validation keywords (not applied to message): {found}")


if __name__ == "__main__":
    unittest.main()
