import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ASTRMAI_ROOT = ROOT / "astrmai"
SKIPPED_PARTS = {"__pycache__", "venv", ".venv", "site-packages"}
PROJECTOR_PATH = ASTRMAI_ROOT / "memory" / "services" / "memory_index_projector.py"
ENGINE_PATH = ASTRMAI_ROOT / "memory" / "services" / "memory_engine.py"


def _iter_python_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in SKIPPED_PARTS for part in path.parts):
            continue
        yield path


def _call_target_expr(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ""


class MemoryRuntimeBoundariesRefactorTests(unittest.TestCase):
    def test_runtime_code_does_not_call_memory_engine_recall_directly(self):
        offenders = []
        for path in _iter_python_files(ASTRMAI_ROOT):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "recall":
                    continue
                target = _call_target_expr(node.func.value)
                if "memory_engine" in target:
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "Production runtime should go through retrieval/tool services instead of memory_engine.recall()")

    def test_runtime_code_does_not_call_persona_recall_wrappers_directly(self):
        offenders = []
        for path in _iter_python_files(ASTRMAI_ROOT):
            if path == ENGINE_PATH:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"recall_persona_lore", "query_persona_lore"}:
                    continue
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [], "Production runtime should not route through recall_persona_lore/query_persona_lore wrappers")

    def test_prompt_refiner_does_not_drive_react_or_recall_fallback(self):
        prompt_refiner = (ASTRMAI_ROOT / "conversation" / "planning" / "prompt_refiner.py").read_text(encoding="utf-8")
        self.assertNotIn("react_retriever.retrieve(", prompt_refiner)
        self.assertNotIn("memory_engine.recall(", prompt_refiner)

    def test_legacy_document_projection_writes_stay_inside_projector(self):
        offenders = []
        for path in _iter_python_files(ASTRMAI_ROOT):
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if "retriever.add_memory(" in text and path != PROJECTOR_PATH:
                offenders.append(str(path.relative_to(ROOT)))
            if path != PROJECTOR_PATH and any(token in text for token in ("DELETE FROM documents", "UPDATE documents", "INSERT INTO documents")):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "Legacy documents writes should be limited to MemoryIndexProjector")


if __name__ == "__main__":
    unittest.main()
