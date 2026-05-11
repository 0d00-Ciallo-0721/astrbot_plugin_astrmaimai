from pathlib import Path
import json
import unittest


class DataResourceCompletenessTests(unittest.TestCase):
    def test_original_runtime_data_assets_are_present(self):
        project_root = Path(__file__).resolve().parents[1]
        required = [
            "data/cmd_config.json",
            "data/patterns/.gitkeep",
            "data/t2i_templates/base.html",
            "data/t2i_templates/astrbot_powershell.html",
        ]
        for rel_path in required:
            with self.subTest(rel_path=rel_path):
                ref_file = project_root / rel_path
                self.assertTrue(ref_file.exists(), rel_path)
                if rel_path != "data/patterns/.gitkeep":
                    self.assertGreater(ref_file.stat().st_size, 0)
        with (project_root / "data/cmd_config.json").open("r", encoding="utf-8-sig") as file:
            self.assertIsInstance(json.load(file), dict)


if __name__ == "__main__":
    unittest.main()
