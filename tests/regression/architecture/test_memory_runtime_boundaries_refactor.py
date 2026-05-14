import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ASTRMAI_ROOT = ROOT / "astrmai"
SKIPPED_PARTS = {"__pycache__", "venv", ".venv", "site-packages"}
PROJECTOR_PATH = ASTRMAI_ROOT / "memory" / "services" / "memory_index_projector.py"
ENGINE_PATH = ASTRMAI_ROOT / "memory" / "services" / "memory_engine.py"
LEGACY_JARGON_WHITELIST = {
    ASTRMAI_ROOT / "infrastructure" / "persistence" / "database_jargon.py",
    ASTRMAI_ROOT / "infrastructure" / "persistence" / "repositories" / "memory_repository.py",
    ASTRMAI_ROOT / "memory" / "services" / "memory_engine.py",
}
LEGACY_EXPRESSION_WHITELIST = {
    ASTRMAI_ROOT / "infrastructure" / "persistence" / "database_review.py",
    ASTRMAI_ROOT / "infrastructure" / "persistence" / "repositories" / "review_repository.py",
    ASTRMAI_ROOT / "memory" / "services" / "memory_engine.py",
    ASTRMAI_ROOT / "memory" / "services" / "memory_migration_service.py",
}


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

    def test_runtime_code_does_not_depend_on_legacy_jargon_adapters(self):
        offenders = []
        blocked_tokens = (
            "get_jargon(",
            "get_jargons(",
            "search_jargons(",
            "load_jargon_list(",
            "save_jargon_async(",
        )
        for path in _iter_python_files(ASTRMAI_ROOT):
            if path in LEGACY_JARGON_WHITELIST:
                continue
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if any(token in text for token in blocked_tokens):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "Production runtime should use canonical jargon retrieval/write services instead of legacy jargon adapters",
        )

    def test_runtime_jargon_auto_injection_is_owned_by_memory_injection_service(self):
        planning_loader = (ASTRMAI_ROOT / "conversation" / "planning" / "planning_input_loader.py").read_text(encoding="utf-8")
        planner_side_inputs = (ASTRMAI_ROOT / "conversation" / "planning" / "planner_side_inputs.py").read_text(encoding="utf-8")
        self.assertNotIn('layers=["jargon"]', planning_loader)
        self.assertNotIn('intent="jargon"', planning_loader)
        self.assertNotIn("astrmai_jargon_injection_trace", planning_loader)
        self.assertNotIn('layers=["jargon"]', planner_side_inputs)
        self.assertNotIn('intent="jargon"', planner_side_inputs)

    def test_runtime_code_does_not_depend_on_legacy_expression_pattern_adapters(self):
        offenders = []
        blocked_tokens = (
            "get_patterns(",
            "save_pattern(",
            "save_pattern_async(",
            "adjust_pattern_weight(",
            "adjust_pattern_weight_async(",
        )
        for path in _iter_python_files(ASTRMAI_ROOT):
            if path in LEGACY_EXPRESSION_WHITELIST:
                continue
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if any(token in text for token in blocked_tokens):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "Production runtime should use canonical expression_pattern services instead of legacy ExpressionPattern adapters",
        )

    def test_webui_review_and_stats_do_not_write_or_count_legacy_expression_patterns(self):
        review_ui = (ASTRMAI_ROOT / "webui" / "backend" / "services" / "review_ui_service.py").read_text(encoding="utf-8")
        dashboard = (ASTRMAI_ROOT / "webui" / "backend" / "services" / "dashboard_service.py").read_text(encoding="utf-8")
        admin_ui = (ASTRMAI_ROOT / "webui" / "backend" / "services" / "admin_ui_service.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO ExpressionPattern", review_ui)
        self.assertNotIn("UPDATE ExpressionPattern", review_ui)
        self.assertNotIn("DELETE FROM ExpressionPattern", review_ui)
        self.assertNotIn("SELECT COUNT(*) FROM ExpressionPattern", dashboard)
        self.assertNotIn("SELECT COUNT(*) FROM ExpressionPattern", admin_ui)


if __name__ == "__main__":
    unittest.main()
