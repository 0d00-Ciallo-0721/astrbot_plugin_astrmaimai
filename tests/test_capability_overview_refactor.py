from pathlib import Path
import unittest


class CapabilityOverviewRefactorTests(unittest.TestCase):
    def test_plugin_facade_exposes_stable_capability_overview_entrypoints(self):
        root = Path(__file__).resolve().parents[1]
        facade_path = root / "astrmai" / "app" / "plugin_facade.py"
        runtime_path = root / "astrmai" / "app" / "runtime_context.py"
        facade_content = facade_path.read_text(encoding="utf-8")
        runtime_content = runtime_path.read_text(encoding="utf-8")
        self.assertIn("def get_capability_overview_sync(self) -> dict:", facade_content)
        self.assertIn("async def get_capability_overview(self) -> dict:", facade_content)
        self.assertIn("return self.runtime.build_capability_overview_sync()", facade_content)
        self.assertIn("return await self.runtime.build_capability_overview()", facade_content)
        self.assertIn('diagnostics["capabilities"] = self.get_capability_overview_sync()', facade_content)
        self.assertIn("def build_capability_overview_sync(self) -> dict[str, Any]:", runtime_content)
        self.assertIn("async def build_capability_overview(self) -> dict[str, Any]:", runtime_content)
        self.assertIn('"multimodal"', runtime_content)
        self.assertIn('"proactive"', runtime_content)
        self.assertIn('"workmode"', runtime_content)
        self.assertIn('"dream_scheduler"', runtime_content)
        self.assertIn('"review_dispatcher"', runtime_content)


if __name__ == "__main__":
    unittest.main()
