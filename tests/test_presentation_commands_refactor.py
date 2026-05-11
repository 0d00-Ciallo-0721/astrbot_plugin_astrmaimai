from pathlib import Path
import importlib
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class PresentationCommandsRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_work_command_request_parses_direct_work_message(self):
        dto_mod = importlib.import_module(
            "astrmai.presentation.dto.command_models"
        )
        request = dto_mod.WorkCommandRequest.from_message("/work 帮我整理今天的待办")
        self.assertEqual(request.task_query, "帮我整理今天的待办")
        self.assertFalse(request.is_empty)

    def test_main_uses_presentation_command_handlers(self):
        path = Path(__file__).resolve().parents[1] / "main.py"
        content = path.read_text(encoding="utf-8")
        self.assertIn("from .astrmai.presentation.commands import handle_mai_help, handle_work_mode", content)
        self.assertIn("async for result in handle_mai_help(self.facade, event):", content)
        self.assertIn("async for result in handle_work_mode(self.facade, event):", content)


if __name__ == "__main__":
    unittest.main()
