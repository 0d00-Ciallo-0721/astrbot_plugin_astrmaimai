from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
WEBUI_ROOT = ROOT / "astrmai" / "webui"
PLUGIN_PAGE_ROOT = ROOT / "pages" / "admin"
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

    def test_plugin_page_is_the_only_supported_management_entry(self):
        self.assertTrue((PLUGIN_PAGE_ROOT / "index.html").exists())
        self.assertTrue((PLUGIN_PAGE_ROOT / "app.js").exists())
        self.assertTrue((PLUGIN_PAGE_ROOT / "style.css").exists())
        server_content = (WEBUI_ROOT / "backend" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("frontend_dir", server_content)
        self.assertNotIn("StaticFiles(directory=frontend_dir", server_content)

    def test_p2_99_acceptance_docs_exist(self):
        self.assertTrue((ROOT / "plan" / "P2_99_TEST_MIGRATION_MATRIX.md").exists())
        self.assertTrue((ROOT / "plan" / "P2_99_ACCEPTANCE_CHECKLIST.md").exists())


if __name__ == "__main__":
    unittest.main()
