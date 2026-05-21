import importlib
import sys
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _DummyPolicy:
    pass


class GatewayPolicyRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.infrastructure.gateway.gateway_policy", None)
        sys.modules.pop("astrmai.infrastructure.runtime.runtime_contracts", None)
        policy_mod = importlib.import_module("astrmai.infrastructure.gateway.gateway_policy")
        contracts_mod = importlib.import_module("astrmai.infrastructure.runtime.runtime_contracts")
        self.policy_mod = importlib.reload(policy_mod)
        self.contracts_mod = importlib.reload(contracts_mod)
        self.policy = type("PolicyHarness", (self.policy_mod.GatewayPolicyMixin,), {})()

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_classify_failure_kind_covers_new_output_categories(self):
        self.assertEqual(
            self.policy._classify_failure_kind("unsafe_or_empty_text"),
            self.contracts_mod.FailureKind.UNSAFE_OR_EMPTY_TEXT,
        )
        self.assertEqual(
            self.policy._classify_failure_kind("prompt_scaffold_text"),
            self.contracts_mod.FailureKind.PROMPT_SCAFFOLD_TEXT,
        )
        self.assertEqual(
            self.policy._classify_failure_kind("tool_protocol_text"),
            self.contracts_mod.FailureKind.TOOL_PROTOCOL_TEXT,
        )


if __name__ == "__main__":
    unittest.main()
