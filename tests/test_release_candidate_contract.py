from __future__ import annotations

import compileall
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_release_candidate import (
    RELEASE_SCRIPTS,
    REQUIRED_ARCHITECTURE_FILES,
    build_release_candidate,
    validate_release_candidate,
)


class ReleaseCandidateContractTests(unittest.TestCase):
    def test_release_candidate_contains_only_runtime_files_and_imports(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            output = Path(temp_dir) / "astrmai_release_candidate"
            build_release_candidate(output)

            self.assertEqual(validate_release_candidate(output), [])
            self.assertFalse((output / "tests").exists())
            self.assertFalse((output / ".agent").exists())
            self.assertFalse((output / "astrmai" / "webui" / "venv").exists())
            self.assertFalse((output / "astrmai" / "webui" / "data").exists())
            self.assertFalse(any(output.rglob("*.db")))
            self.assertFalse(any(output.rglob("*.pyc")))
            self.assertTrue((output / "CHANGELOG.md").is_file())
            self.assertIn('astrbot_version: ">=4.26.4,<5"', (output / "metadata.yaml").read_text(encoding="utf-8"))
            schema = json.loads((output / "_conf_schema.json").read_text(encoding="utf-8"))
            self.assertIsInstance(schema, dict)
            for relative in REQUIRED_ARCHITECTURE_FILES:
                self.assertTrue(
                    (output / Path(relative)).is_file(),
                    f"missing architecture runtime file: {relative}",
                )
            for name in RELEASE_SCRIPTS:
                self.assertTrue((output / "scripts" / name).is_file())

            self.assertTrue(compileall.compile_dir(output, quiet=1))

            code = (
                "import importlib,sys;"
                f"sys.path.insert(0,{str(output.parent)!r});"
                "m=importlib.import_module('astrmai_release_candidate.main');"
                "assert hasattr(m,'AstrMaiPlugin')"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
