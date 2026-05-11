import unittest
from types import SimpleNamespace

from astrmai.infrastructure.runtime.reverse_session import (
    REVERSE_SESSION_TAG,
    append_reverse_session_block,
    maybe_attach_reverse_session_block,
    parse_reverse_session_block,
    provider_is_gemini_reverse,
    render_reverse_session_block,
    strip_reverse_session_block,
)


class ReverseSessionRefactorTests(unittest.TestCase):
    def test_render_parse_strip_and_append_roundtrip(self):
        block = render_reverse_session_block(
            "session-1",
            session_scope="chat",
            parent_session_id="parent-1",
            session_kind="dialog",
            source="unit",
        )
        self.assertIn(REVERSE_SESSION_TAG, block)
        parsed = parse_reverse_session_block(block)
        self.assertEqual(parsed["session_id"], "session-1")
        self.assertEqual(parsed["session_scope"], "chat")
        prompt = append_reverse_session_block("base", "session-1", source="unit")
        self.assertEqual(strip_reverse_session_block(prompt), "base")

    def test_provider_detection_and_maybe_attach(self):
        provider = SimpleNamespace(
            meta=lambda: SimpleNamespace(type="openai_chat_completion"),
            provider_config={"reverse_provider": "gemini_reverse"},
        )
        self.assertTrue(provider_is_gemini_reverse(provider))
        prompt = maybe_attach_reverse_session_block("base", provider, session_id="session-2")
        self.assertIn("session_id=session-2", prompt)
        normal_provider = SimpleNamespace(
            meta=lambda: SimpleNamespace(type="openai_chat_completion"),
            provider_config={},
        )
        self.assertEqual(maybe_attach_reverse_session_block("base", normal_provider, session_id="x"), "base")


if __name__ == "__main__":
    unittest.main()