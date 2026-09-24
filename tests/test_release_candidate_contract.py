from __future__ import annotations

import compileall
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_release_candidate import (
    EXCLUDED_SOURCE_RULES,
    REQUIRED_ARCHITECTURE_FILES,
    build_release_candidate,
    manifest_path_for,
    validate_release_candidate,
    validate_release_manifest,
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
            self.assertFalse(
                any(path.read_bytes().startswith(b"\xef\xbb\xbf") for path in output.rglob("*.py")),
                "release Python sources must use UTF-8 without BOM",
            )
            self.assertTrue((output / "CHANGELOG.md").is_file())
            self.assertIn('astrbot_version: ">=4.26.4,<5"', (output / "metadata.yaml").read_text(encoding="utf-8"))
            schema = json.loads((output / "_conf_schema.json").read_text(encoding="utf-8"))
            self.assertIsInstance(schema, dict)
            for relative in REQUIRED_ARCHITECTURE_FILES:
                self.assertTrue(
                    (output / Path(relative)).is_file(),
                    f"missing architecture runtime file: {relative}",
                )
            excluded = {
                "scripts/audit_learning_data.py",
                "astrmai/webui/backend/server.py",
                "astrmai/webui/backend/routes.py",
                "astrmai/webui/backend/routes/__init__.py",
                "astrmai/conversation/loop/scheduler_benchmark.py",
                "astrmai/conversation/replay/__init__.py",
                "astrmai/conversation/replay/context_architecture_harness.py",
                "astrmai/infrastructure/persistence/architecture_migration_audit.py",
                "astrmai/infrastructure/runtime/business_kpis.py",
                "astrmai/infrastructure/runtime/context_economy_benchmark.py",
                "astrmai/learning/evaluation/replay_runner.py",
            }
            for relative in excluded:
                self.assertFalse((output / relative).exists(), relative)

            retained_page_backend = {
                "astrmai/webui/backend/adapters/plugin_api.py",
                "astrmai/webui/backend/db.py",
                "astrmai/webui/backend/paths.py",
                "astrmai/webui/backend/repositories.py",
                "astrmai/webui/backend/schemas.py",
                "astrmai/webui/backend/services/admin_ui_service.py",
                "astrmai/webui/backend/services/memory_ui_service.py",
                "astrmai/webui/backend/services/review_ui_service.py",
                "astrmai/webui/backend/services/user_ui_service.py",
            }
            for relative in retained_page_backend:
                self.assertTrue((output / relative).is_file(), relative)
            self.assertTrue(
                (output / "astrmai/infrastructure/runtime/context_economy_benchmark_store.py").is_file()
            )

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

            page_code = (
                "import importlib,sys;"
                f"sys.path.insert(0,{str(output.parent)!r});"
                "m=importlib.import_module('astrmai_release_candidate.astrmai.webui.plugin_pages');"
                "registered=[];"
                "C=type('C',(),{'register_web_api':lambda self,*args:registered.append(args)});"
                "m.register_astrmai_admin_pages(C(),type('F',(),{'runtime':None})());"
                "assert registered;"
                "assert all(args[2][0] in {'GET','POST'} for args in registered)"
            )
            page_result = subprocess.run(
                [sys.executable, "-c", page_code],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(page_result.returncode, 0, page_result.stderr or page_result.stdout)

    def test_release_manifest_is_complete_and_reproducible(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            baseline = root / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {
                        "candidate_files": [
                            {"path": "main.py", "sha256": "0" * 64, "size": 1},
                            {"path": "obsolete.py", "sha256": "1" * 64, "size": 1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            first = root / "first"
            second = root / "second"
            build_release_candidate(first, baseline_manifest=baseline)
            build_release_candidate(second, baseline_manifest=baseline)

            first_manifest_path = manifest_path_for(first)
            second_manifest_path = manifest_path_for(second)
            first_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
            second_manifest = json.loads(second_manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(first_manifest["schema"], "astrmai.release-candidate-manifest")
            self.assertEqual(first_manifest["version"], 1)
            self.assertEqual(validate_release_manifest(first, first_manifest_path), [])
            self.assertEqual(validate_release_manifest(second, second_manifest_path), [])
            self.assertEqual(
                hashlib.sha256(first_manifest_path.read_bytes()).hexdigest(),
                hashlib.sha256(second_manifest_path.read_bytes()).hexdigest(),
            )
            self.assertIn("obsolete.py", first_manifest["comparison"]["removed"])
            self.assertIn("main.py", first_manifest["comparison"]["retained"])
            self.assertFalse(first_manifest["unknown_files"])
            self.assertEqual(
                {item["classification"] for item in first_manifest["files"]},
                {"runtime", "page", "resource"},
            )
            excluded_paths = {item["path"] for item in first_manifest["excluded_files"]}
            for rule in EXCLUDED_SOURCE_RULES:
                self.assertTrue(
                    any(
                        path == rule.path or (rule.prefix and path.startswith(rule.path))
                        for path in excluded_paths
                    ),
                    rule.path,
                )


if __name__ == "__main__":
    unittest.main()
