import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _FakeConversationManager
from tests.original_ported.helpers import _install_astrbot_stubs


class _FakeResponse:
    def __init__(self, text="ok"):
        self.completion_text = text
        self.usage = SimpleNamespace(input=8, input_cached=4, output=3)


class _FakeProvider:
    def __init__(self, provider_type, api_base, model, extra_config=None):
        self.provider_config = {"type": provider_type, "api_base": api_base, "model": model}
        if extra_config:
            self.provider_config.update(extra_config)
        self._model = model

    def meta(self):
        return SimpleNamespace(type=self.provider_config["type"])

    def get_model(self):
        return self._model


class _FakeContext:
    def __init__(self, provider_map=None):
        self.calls = []
        self.provider_map = provider_map or {}

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse()

    def get_provider_by_id(self, provider_id):
        return self.provider_map.get(provider_id)


class ReverseSessionMarkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        for mod_name in [
            "astrmai.infrastructure.runtime.reverse_session",
            "astrmai.infrastructure.runtime.lane_manager",
            "astrmai.infrastructure.gateway.model_gateway",
        ]:
            sys.modules.pop(mod_name, None)
        self.reverse_mod = importlib.import_module("astrmai.infrastructure.runtime.reverse_session")
        self.reverse_mod = importlib.reload(self.reverse_mod)
        self.lane_mod = importlib.import_module("astrmai.infrastructure.runtime.lane_manager")
        self.lane_mod = importlib.reload(self.lane_mod)
        self.gateway_mod = importlib.import_module("astrmai.infrastructure.gateway.model_gateway")
        self.gateway_mod = importlib.reload(self.gateway_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_append_marker_is_idempotent(self):
        marker_once = self.reverse_mod.append_reverse_session_block(
            "system prompt",
            "lane-1",
            session_scope="chat:1",
            parent_session_id="parent-1",
            session_kind="sys2:dialog",
            source="astrmai",
        )
        marker_twice = self.reverse_mod.append_reverse_session_block(
            marker_once,
            "lane-1",
            session_scope="chat:1",
            parent_session_id="parent-1",
            session_kind="sys2:dialog",
            source="astrmai",
        )
        self.assertEqual(marker_once, marker_twice)
        parsed = self.reverse_mod.parse_reverse_session_block(marker_twice)
        self.assertEqual(parsed["session_id"], "lane-1")
        self.assertEqual(parsed["parent_session_id"], "parent-1")
        self.assertEqual(parsed["source"], "astrmai")

    def test_provider_detection_only_matches_explicit_reverse_markers(self):
        local_reverse = _FakeProvider(
            "openai_chat_completion",
            "http://127.0.0.1:8000/v1",
            "gemini-2.5-pro",
            extra_config={"reverse_plugin": "astrbot_plugin_gemini_reverse"},
        )
        local_unmarked = _FakeProvider(
            "openai_chat_completion",
            "http://127.0.0.1:8000/v1",
            "gemini-2.5-pro",
        )
        builtin_gemini = _FakeProvider(
            "google_gemini",
            "https://generativelanguage.googleapis.com",
            "gemini-2.5-flash",
        )
        self.assertTrue(self.reverse_mod.provider_is_gemini_reverse(local_reverse))
        self.assertFalse(self.reverse_mod.provider_is_gemini_reverse(local_unmarked))
        self.assertFalse(self.reverse_mod.provider_is_gemini_reverse(builtin_gemini))

    def test_maybe_attach_marker_only_for_gemini_reverse_provider(self):
        reverse_provider = _FakeProvider(
            "openai_chat_completion",
            "http://127.0.0.1:8000/v1",
            "gemini-2.5-pro",
            extra_config={"reverse_provider": "gemini_web"},
        )
        regular_provider = _FakeProvider(
            "openai_chat_completion",
            "https://api.openai.com/v1",
            "gpt-5",
        )

        reverse_prompt = self.reverse_mod.maybe_attach_reverse_session_block(
            "stable prompt",
            provider=reverse_provider,
            session_id="default:GroupMessage:group-1@@astrmai:sys2:dialog:v1",
            session_scope="default:GroupMessage:group-1",
            parent_session_id="default:GroupMessage:group-1",
            session_kind="sys2:dialog",
            source="astrmai",
        )
        regular_prompt = self.reverse_mod.maybe_attach_reverse_session_block(
            "stable prompt",
            provider=regular_provider,
            session_id="default:GroupMessage:group-1@@astrmai:sys2:dialog:v1",
            session_scope="default:GroupMessage:group-1",
            parent_session_id="default:GroupMessage:group-1",
            session_kind="sys2:dialog",
            source="astrmai",
        )

        self.assertIn("<astrbot_reverse_session>", reverse_prompt)
        self.assertIn("session_id=default:GroupMessage:group-1@@astrmai:sys2:dialog:v1", reverse_prompt)
        self.assertNotIn("<astrbot_reverse_session>", regular_prompt)


if __name__ == "__main__":
    unittest.main()
