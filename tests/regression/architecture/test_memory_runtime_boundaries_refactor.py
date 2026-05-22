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
    def test_summarizer_module_is_not_primary_runtime_implementation_host(self):
        summarizer = (ASTRMAI_ROOT / "memory" / "services" / "summarizer.py").read_text(encoding="utf-8")
        self.assertIn("compatibility facade", summarizer.lower())
        self.assertIn("SessionMemorySummarizer", summarizer)
        self.assertNotIn("class MemoryTurnPipeline", summarizer)

    def test_reply_post_send_no_longer_calls_legacy_summarizer_ingest(self):
        reply_post_send = (ASTRMAI_ROOT / "conversation" / "execution" / "reply_post_send.py").read_text(encoding="utf-8")
        self.assertNotIn("summarizer.ingest_committed_turn", reply_post_send)
        self.assertIn("memory_pipeline", reply_post_send)
        self.assertIn("instant_gate", reply_post_send)
        self.assertIn("publish_turn_committed", reply_post_send)

    def test_proactive_memory_maintenance_no_longer_calls_legacy_summarizer(self):
        proactive_task = (ASTRMAI_ROOT / "proactive" / "proactive_task.py").read_text(encoding="utf-8")
        self.assertNotIn("summarizer.run_once_for_session", proactive_task)
        self.assertIn("memory_pipeline", proactive_task)
        self.assertIn("run_maintenance_for_session", proactive_task)

    def test_runtime_code_does_not_read_memory_engine_summarizer_alias(self):
        offenders = []
        for path in _iter_python_files(ASTRMAI_ROOT):
            if path == ENGINE_PATH:
                continue
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if "memory_engine.summarizer" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "Production runtime should not read memory_engine.summarizer alias")

    def test_memory_turn_pipeline_is_the_only_runtime_async_bridge(self):
        reply_post_send = (ASTRMAI_ROOT / "conversation" / "execution" / "reply_post_send.py").read_text(encoding="utf-8")
        event_bus = (ASTRMAI_ROOT / "infrastructure" / "runtime" / "event_bus.py").read_text(encoding="utf-8")
        pipeline = (ASTRMAI_ROOT / "memory" / "services" / "memory_turn_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("TOPIC_MEMORY_TURN_COMMITTED", event_bus)
        self.assertIn("memory.turn_committed", event_bus)
        self.assertIn("publish_turn_committed", reply_post_send)
        self.assertIn('subscribe(self.event_bus.TOPIC_MEMORY_TURN_COMMITTED', pipeline)

    def test_memory_engine_runtime_wiring_exposes_new_components(self):
        engine_text = ENGINE_PATH.read_text(encoding="utf-8")
        self.assertIn("self.instant_gate = None", engine_text)
        self.assertIn("self.memory_pipeline = None", engine_text)
        self.assertIn("self.session_summarizer = None", engine_text)
        self.assertIn("self.instant_gate = InstantMemoryGate", engine_text)
        self.assertIn("self.memory_pipeline = MemoryTurnPipeline", engine_text)
        self.assertIn("self.session_summarizer = SessionMemorySummarizer", engine_text)
        self.assertNotIn("self.summarizer = self.memory_pipeline", engine_text)

    def test_tests_do_not_heavily_depend_on_compat_summarizer_module(self):
        offenders = []
        allowed = {
            ROOT / "tests" / "test_memory_refactor.py",
            ROOT / "tests" / "test_reply_service_refactor.py",
            ROOT / "tests" / "unit" / "memory" / "test_memory_contracts_migrated.py",
            ROOT / "tests" / "regression" / "architecture" / "test_memory_runtime_boundaries_refactor.py",
        }
        for path in (ROOT / "tests").rglob("*.py"):
            if path in allowed:
                continue
            text = path.read_text(encoding="utf-8-sig", errors="ignore")
            if "astrmai.memory.services.summarizer" in text or "ChatHistorySummarizer" in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders,
            [],
            "Only compat smoke tests should import the summarizer compat module",
        )

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
