from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
WEBUI_ROOT = ROOT / "astrmai" / "webui"
TESTS_ROOT = ROOT / "tests"


class DirectoryContractsRefactorTests(unittest.TestCase):
    def test_refactor_test_layout_has_expected_buckets(self):
        expected = [
            TESTS_ROOT / "unit",
            TESTS_ROOT / "integration",
            TESTS_ROOT / "regression",
            TESTS_ROOT / "fixtures",
            TESTS_ROOT / "helpers",
        ]
        for path in expected:
            self.assertTrue(path.exists(), str(path))

    def test_webui_shell_is_split_into_components_and_pages(self):
        self.assertTrue((WEBUI_ROOT / "frontend" / "components").exists())
        self.assertTrue((WEBUI_ROOT / "frontend" / "pages").exists())
        index_content = (WEBUI_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="page-slot-dashboard"', index_content)
        self.assertNotIn('id="page-dashboard" x-show=', index_content)

    def test_p2_99_acceptance_docs_exist(self):
        self.assertTrue((ROOT / "plan" / "P2_99_TEST_MIGRATION_MATRIX.md").exists())
        self.assertTrue((ROOT / "plan" / "P2_99_ACCEPTANCE_CHECKLIST.md").exists())


if __name__ == "__main__":
    unittest.main()
