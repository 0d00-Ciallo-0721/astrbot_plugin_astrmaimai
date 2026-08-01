import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ASTRMAI_ROOT = ROOT / "astrmai"
TEST_ROOT = ROOT / "tests"
SKIPPED_SCAN_PARTS = {"venv", ".venv", "site-packages", "artifacts", ".agent", ".claude"}


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


def _collect_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def _is_namespace_or_child(module: str, namespace: str) -> bool:
    return module == namespace or module.startswith(f"{namespace}.")


class ImportBoundariesRefactorTests(unittest.TestCase):
    def test_project_files_do_not_embed_local_absolute_paths(self):
        forbidden_fragments = (
            "C:" + "\\Users",
            "C:" + "/Users",
            "Desktop" + "\\mai",
            "Desktop" + "/mai",
        )
        scanned_suffixes = {".py", ".json", ".yaml", ".yml", ".md"}
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in scanned_suffixes:
                continue
            if "__pycache__" in path.parts or any(part in SKIPPED_SCAN_PARTS for part in path.parts):
                continue
            if path.name == "memory.md":
                continue
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if any(fragment in text for fragment in forbidden_fragments):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "Project files should use relative paths or env-configured roots")

    def test_presentation_does_not_reach_into_persistence_internals(self):
        forbidden_prefix = "astrmai.infrastructure.persistence"
        for path in _iter_python_files(ASTRMAI_ROOT / "presentation"):
            imports = _collect_imports(path)
            with self.subTest(path=path):
                offenders = [module for module in imports if module.startswith(forbidden_prefix)]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path} should depend on facades/contracts instead of persistence internals",
                )

    def test_webui_routes_do_not_import_domain_internals(self):
        route_root = ASTRMAI_ROOT / "webui" / "backend" / "routes"
        forbidden_prefixes = (
            "astrmai.conversation",
            "astrmai.state",
            "astrmai.memory",
            "astrmai.learning",
        )
        for path in _iter_python_files(route_root):
            imports = _collect_imports(path)
            with self.subTest(path=path):
                offenders = [module for module in imports if module.startswith(forbidden_prefixes)]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path} should go through webui services/adapters instead of domain internals",
                )

    def test_top_level_refactor_tests_no_longer_use_root_test_helpers(self):
        for path in _iter_python_files(TEST_ROOT):
            relative = path.relative_to(TEST_ROOT)
            if relative.parts[0] in {"unit", "integration", "regression"}:
                continue
            imports = _collect_imports(path)
            with self.subTest(path=path):
                offenders = [module for module in imports if module.startswith("tests.test_")]
                self.assertEqual(
                    offenders,
                    [],
                    f"{path} should use local helpers/fixtures instead of root tests.test_* helpers",
                )

    def test_migrated_tests_do_not_import_old_runtime_namespaces(self):
        forbidden_prefixes = (
            "astrmai.Brain",
            "astrmai.Heart",
            "astrmai.infra",
            "astrmai.evolution",
            "astrmai.work",
        )
        allowed_parts = {"helpers"}
        for path in _iter_python_files(TEST_ROOT):
            relative = path.relative_to(TEST_ROOT)
            if relative.parts and relative.parts[0] in allowed_parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            offenders = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    offenders.extend(
                        alias.name
                        for alias in node.names
                        if any(_is_namespace_or_child(alias.name, prefix) for prefix in forbidden_prefixes)
                    )
                elif (
                    isinstance(node, ast.ImportFrom)
                    and node.module
                    and any(_is_namespace_or_child(node.module, prefix) for prefix in forbidden_prefixes)
                ):
                    offenders.append(node.module)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and any(_is_namespace_or_child(node.args[0].value, prefix) for prefix in forbidden_prefixes)
                ):
                    offenders.append(node.args[0].value)
            with self.subTest(path=path):
                self.assertEqual(
                    offenders,
                    [],
                    f"{path} should import refactor modules instead of old runtime namespaces",
                )

