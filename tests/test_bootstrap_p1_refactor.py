import pathlib
import unittest


class BootstrapP1RefactorTests(unittest.TestCase):
    def test_bootstrap_no_longer_imports_legacy_big_classes(self):
        bootstrap_path = pathlib.Path(__file__).resolve().parents[1] / "astrmai" / "app" / "bootstrap.py"
        content = bootstrap_path.read_text(encoding="utf-8")
        self.assertNotIn("from astrmai.Heart.state_engine import StateEngine", content)
        self.assertNotIn("from astrmai.memory.engine import MemoryEngine", content)
        self.assertNotIn("from astrmai.evolution.processor import EvolutionManager", content)
        self.assertIn("from ..state import", content)
        self.assertIn("from ..memory import", content)
        self.assertIn("from ..learning import", content)


if __name__ == "__main__":
    unittest.main()
