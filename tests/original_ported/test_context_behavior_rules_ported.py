import unittest

from astrmai.conversation.contracts.focus_context import FreshnessState, ReplyMode
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.planning.context_engine import ContextEngine


class ContextBehaviorRulesPortedTests(unittest.TestCase):
    def test_behavior_rules_change_with_reply_mode_and_freshness(self):
        engine = ContextEngine.__new__(ContextEngine)
        envelope = PromptEnvelope(
            reply_mode=ReplyMode.EMOTIONAL_SUPPORT,
            freshness_state=FreshnessState.STALE_BUT_SALVAGEABLE,
        )

        block = engine._build_behavior_rule_block(envelope)

        self.assertTrue(block.strip())
        self.assertGreaterEqual(block.count("- "), 3)
        self.assertNotEqual(block, engine._build_behavior_rule_block(PromptEnvelope()))


if __name__ == "__main__":
    unittest.main()
