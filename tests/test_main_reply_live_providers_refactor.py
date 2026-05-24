import asyncio
import importlib
import unittest
from unittest.mock import AsyncMock, patch


class MainReplyLiveProvidersTests(unittest.TestCase):
    def setUp(self):
        self.mod = importlib.import_module("tests.manual.main_reply_live_providers")
        self.mod = importlib.reload(self.mod)

    def test_live_completion_result_defaults(self):
        result = self.mod.LiveCompletionResult(text="ok")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage_input_cached, 0)
        self.assertEqual(result.request_session_id, "")

    def test_openai_client_requires_base_url(self):
        client = self.mod.OpenAICompatibleLiveClient(
            api_key="sk-test",
            model="gpt-test",
            timeout=5.0,
            base_url="",
        )

        async def _run():
            await client.__aenter__()
            try:
                with self.assertRaises(RuntimeError):
                    await client.complete("sys", "prompt", request_label="chat")
            finally:
                await client.__aexit__(None, None, None)

        asyncio.run(_run())

    def test_build_live_provider_client_dispatches_by_family(self):
        async def _run():
            moonshot = await self.mod.build_live_provider_client("kimi", api_key="sk-test", model="kimi", timeout=5.0)
            self.assertEqual(moonshot.provider_family, "kimi")
            anthropic = await self.mod.build_live_provider_client("anthropic", api_key="sk-test", model="claude", timeout=5.0)
            self.assertEqual(anthropic.provider_family, "anthropic")
            gemini = await self.mod.build_live_provider_client("gemini", api_key="sk-test", model="gemini", timeout=5.0)
            self.assertEqual(gemini.provider_family, "gemini")
            native_chat = await self.mod.build_live_provider_client(
                "native_chat",
                api_key="sk-test",
                model="gpt-test",
                timeout=5.0,
                base_url="https://example.com",
            )
            self.assertEqual(native_chat.provider_family, "native_chat")

        asyncio.run(_run())

    def test_anthropic_payload_maps_cached_usage(self):
        payload = {
            "model": "claude-3-5-sonnet",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 120,
                "cache_read_input_tokens": 80,
                "output_tokens": 12,
                "cache_creation_input_tokens": 40,
            },
        }
        result = self.mod.AnthropicLiveClient._build_result_from_payload(payload, "claude-3-5-sonnet")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage_input_tokens, 120)
        self.assertEqual(result.usage_input_cached, 80)
        self.assertEqual(result.usage_output_tokens, 12)
        self.assertTrue(result.cached_usage_supported)

    def test_gemini_payload_marks_cached_usage_unsupported_when_field_missing(self):
        payload = {
            "usageMetadata": {
                "promptTokenCount": 90,
                "candidatesTokenCount": 11,
                "totalTokenCount": 101,
            },
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        }
        result = self.mod.GeminiLiveClient._build_result_from_payload(payload, "gemini-1.5-pro")
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage_input_tokens, 90)
        self.assertEqual(result.usage_input_cached, 0)
        self.assertFalse(result.cached_usage_supported)

    def test_openai_payload_uses_prompt_tokens_details_cached_tokens_first(self):
        payload = {
            "model": "gpt-test",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 15,
                "total_tokens": 115,
                "prompt_tokens_details": {"cached_tokens": 60},
                "input_cached": 30,
                "cached_tokens": 20,
            },
        }
        result = self.mod.OpenAICompatibleLiveClient._build_result_from_payload(payload, "gpt-test", hint_payload={"prompt_cache_key": "abc"})
        self.assertEqual(result.text, "ok")
        self.assertEqual(result.usage_input_cached, 60)
        self.assertTrue(result.cached_usage_supported)

    def test_base_client_splits_multiple_api_keys(self):
        client = self.mod.GeminiLiveClient(api_key="key-a\nkey-b,key-c", model="gemini", timeout=5.0)
        self.assertEqual(client.api_key_pool, ["key-a", "key-b", "key-c"])
        self.assertEqual(client.api_key, "key-a")

    def test_base_client_session_uses_trust_env(self):
        client = self.mod.GeminiLiveClient(api_key="key-a", model="gemini", timeout=5.0)

        class DummySession:
            async def close(self):
                return None

        with patch.object(self.mod.aiohttp, "ClientSession", return_value=DummySession()) as mock_session:
            async def _run():
                await client.__aenter__()
                await client.__aexit__(None, None, None)

            asyncio.run(_run())
        self.assertTrue(mock_session.call_args.kwargs.get("trust_env"))

    def test_gemini_retries_with_next_api_key(self):
        client = self.mod.GeminiLiveClient(api_key="key-a,key-b", model="gemini", timeout=5.0)
        calls = []

        async def fake_post_json(url, *, headers=None, body=None):
            calls.append(url)
            if "key=key-a" in url:
                raise self.mod.LiveProviderRequestError("quota", status=429, retry_after=0, retryable=True)
            return {
                "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            }

        async def _run():
            await client.__aenter__()
            try:
                client._post_json = fake_post_json
                with patch.object(self.mod.asyncio, "sleep", new=AsyncMock()):
                    result = await client.complete("sys", "prompt", request_label="chat")
            finally:
                await client.__aexit__(None, None, None)
            return result

        result = asyncio.run(_run())
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(calls), 2)
        self.assertIn("key=key-b", calls[-1])

    def test_openai_client_retries_same_key_on_retryable_failure(self):
        client = self.mod.OpenAICompatibleLiveClient(
            api_key="sk-test",
            model="gpt-test",
            timeout=5.0,
            base_url="https://example.com",
        )
        calls = []

        async def fake_post_json(url, *, headers=None, body=None):
            calls.append((url, headers))
            if len(calls) == 1:
                raise self.mod.LiveProviderRequestError("temporary", status=503, retry_after=0, retryable=True)
            return {
                "model": "gpt-test",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            }

        async def _run():
            await client.__aenter__()
            try:
                client._post_json = fake_post_json
                with patch.object(self.mod.asyncio, "sleep", new=AsyncMock()):
                    result = await client.complete("sys", "prompt", request_label="chat")
            finally:
                await client.__aexit__(None, None, None)
            return result

        result = asyncio.run(_run())
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["Authorization"], "Bearer sk-test")


if __name__ == "__main__":
    unittest.main()
