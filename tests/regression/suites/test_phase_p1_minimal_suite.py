import unittest


P1_MODULES = [
    "tests.regression.state.test_state_engine_mood_migrated",
    "tests.unit.memory.test_memory_contracts_migrated",
    "tests.unit.learning.test_message_recorder_migrated",
    "tests.integration.runtime.test_runtime_contracts_migrated",
    "tests.regression.review.test_review_service_migrated",
    "tests.regression.memory.test_react_retriever_traces_migrated",
    "tests.regression.conversation.test_dialog_continuity_regression_migrated",
    "tests.regression.conversation.test_dialog_focus_thread_continuity_regression_migrated",
    "tests.regression.persistence.test_persistence_regressions_migrated",
    "tests.regression.proactive.test_dream_maintenance_migrated",
    "tests.regression.multimodal.test_vision_bundle_binding_migrated",
]


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromNames(P1_MODULES))
    return suite
