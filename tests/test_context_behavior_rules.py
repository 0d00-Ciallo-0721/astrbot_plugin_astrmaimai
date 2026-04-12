import importlib
import sys
import tempfile
import types
import unittest

from tests.test_persistence_regressions import _install_astrbot_stubs


class ContextBehaviorRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        database_mod = types.ModuleType("astrmai.infra.database")
        database_mod.DatabaseService = type("DatabaseService", (), {})
        sys.modules["astrmai.infra.database"] = database_mod
        datamodels_mod = types.ModuleType("astrmai.infra.datamodels")
        datamodels_mod.ChatState = type("ChatState", (), {})
        datamodels_mod.UserProfile = type("UserProfile", (), {})
        datamodels_mod.VisualMemory = type("VisualMemory", (), {})
        sys.modules["astrmai.infra.datamodels"] = datamodels_mod
        persona_mod = types.ModuleType("astrmai.Brain.persona_summarizer")
        persona_mod.PersonaSummarizer = type("PersonaSummarizer", (), {})
        sys.modules["astrmai.Brain.persona_summarizer"] = persona_mod
        sys.modules.pop("astrmai.Brain.context_engine", None)
        sys.modules.pop("astrmai.infra.runtime_contracts", None)
        self.context_mod = importlib.import_module("astrmai.Brain.context_engine")
        self.context_mod = importlib.reload(self.context_mod)
        self.contracts_mod = importlib.import_module("astrmai.infra.runtime_contracts")
        self.contracts_mod = importlib.reload(self.contracts_mod)

    def tearDown(self):
        for name in (
            "astrmai.Brain.context_engine",
            "astrmai.infra.runtime_contracts",
            "astrmai.infra.database",
            "astrmai.infra.datamodels",
            "astrmai.Brain.persona_summarizer",
        ):
            sys.modules.pop(name, None)
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_behavior_rules_change_with_reply_mode_and_freshness(self):
        engine = self.context_mod.ContextEngine.__new__(self.context_mod.ContextEngine)
        envelope = self.context_mod.PromptEnvelope(
            reply_mode=self.context_mod.PromptEnvelope.__dataclass_fields__["reply_mode"].default.__class__.EMOTIONAL_SUPPORT,
            freshness_state=self.context_mod.PromptEnvelope.__dataclass_fields__["freshness_state"].default.__class__.STALE_BUT_SALVAGEABLE,
        )

        block = engine._build_behavior_rule_block(envelope)

        self.assertIn("优先接住最近一句", block)
        self.assertIn("先安抚", block)
        self.assertIn("偏晚", block)


if __name__ == "__main__":
    unittest.main()
