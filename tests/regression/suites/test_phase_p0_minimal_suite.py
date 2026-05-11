import unittest


P0_MODULES = [
    "tests.unit.runtime.test_chat_runtime_coordinator_migrated",
    "tests.integration.gateway.test_gateway_context_passthrough_migrated",
    "tests.regression.attention.test_attention_focus_thread_selection_migrated",
    "tests.regression.reply.test_reply_engine_timeliness_migrated",
]


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromNames(P0_MODULES))
    return suite
