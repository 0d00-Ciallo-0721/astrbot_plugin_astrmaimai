import importlib
import sys
import tempfile
import unittest

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeEvent:
    def __init__(self, message="hello", extras=None):
        self.message_str = message
        self._extras = dict(extras or {})

    def get_sender_name(self):
        return "Alice"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class PlannerPromptContextGuardsPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.planning.planner_prompt_context", None)
        self.prompt_context_mod = importlib.import_module("astrmai.conversation.planning.planner_prompt_context")
        self.prompt_context_mod = importlib.reload(self.prompt_context_mod)
        self.focus_mod = importlib.import_module("astrmai.conversation.contracts.focus_context")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_event_line_uses_speaker_content_layout(self):
        line = self.prompt_context_mod.PlannerPromptContextMixin._render_event_line(
            _FakeEvent("<hello>", extras={"astrmai_rich_text": "ATRI: hi <3"})
        )

        self.assertEqual(line, "Alice: ATRI: hi <3")
        self.assertNotIn("<message speaker=", line)

    def test_poke_event_is_lightweight(self):
        context = self.focus_mod.FocusThreadContext(focus_event=None)
        event = _FakeEvent("", extras={"is_virtual_poke": True, "astrmai_interaction_kind": "poke"})

        self.assertTrue(self.prompt_context_mod.PlannerPromptContextMixin._is_lightweight_event(event, context))


if __name__ == "__main__":
    unittest.main()
