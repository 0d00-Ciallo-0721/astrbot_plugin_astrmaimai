import unittest


P2_MODULES = [
    "tests.regression.architecture.test_directory_contracts_refactor",
    "tests.regression.architecture.test_import_boundaries_refactor",
    "tests.regression.architecture.test_shared_test_support_refactor",
    "tests.test_presentation_commands_refactor",
    "tests.test_webui_backend_refactor",
    "tests.test_webui_frontend_shell_refactor",
]


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromNames(P2_MODULES))
    return suite
