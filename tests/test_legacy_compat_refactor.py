import importlib
import unittest

from astrmai.conversation.contracts.focus_context import FocusThreadContext
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope


class _FakeEvent:
    def __init__(self):
        self._extra = {}

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class LegacyCompatRefactorTests(unittest.TestCase):
    def test_focus_and_prompt_legacy_extras_use_local_contracts(self):
        compat_mod = importlib.import_module(
            "astrmai.infrastructure.compat.legacy_compat"
        )
        event = _FakeEvent()
        focus_context = FocusThreadContext(focus_event="focus-event", focus_reason="direct_wakeup")
        prompt_envelope = PromptEnvelope(
            raw_user_text="hello",
            focus_message_text="hello",
            direct_context_text="focus line",
        )

        compat_mod.emit_legacy_focus_thread_extras(event, focus_context)
        compat_mod.emit_legacy_prompt_envelope_extras(event, prompt_envelope)

        self.assertIs(event.get_extra("astrmai_focus_thread_context"), focus_context)
        self.assertIs(event.get_extra("astrmai_prompt_envelope"), prompt_envelope)
        rebuilt = compat_mod.read_legacy_prompt_envelope(event, prompt="fallback")
        self.assertEqual(rebuilt.raw_user_text, "hello")
        self.assertEqual(rebuilt.focus_message_text, "hello")
        self.assertEqual(rebuilt.direct_context_text, "focus line")


if __name__ == "__main__":
    unittest.main()
