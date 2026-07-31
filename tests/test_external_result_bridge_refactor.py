import asyncio
import importlib
import types
import unittest


class _Plain:
    def __init__(self, text):
        self.text = text


class _Image:
    pass


class _FakeAttentionGate:
    def __init__(self):
        self.calls = []

    async def inject_external_event(self, chat_id, event):
        self.calls.append((chat_id, event))


class _FakeEvolution:
    def __init__(self):
        self.calls = []

    async def process_bot_reply(self, chat_id, bot_id, payload):
        self.calls.append((chat_id, bot_id, payload))


class _FakeEvent:
    def __init__(self):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._extra = {}
        self.message_obj = types.SimpleNamespace(message=[], self_id="bot-1")
        self._result = types.SimpleNamespace(chain=[_Plain("任务完成"), _Image()])

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_result(self):
        return self._result

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"

    def get_self_id(self):
        return "bot-1"


class _FakeHostBridge:
    def is_ghost_sentinel(self, text):
        return text == "[[ghost]]"

    def should_intercept_error(self, text, enabled=True):
        return enabled and "Traceback" in text


class _FakePrivateEvent(_FakeEvent):
    def __init__(self):
        super().__init__()
        self.unified_msg_origin = "default:FriendMessage:user-1"

    def get_group_id(self):
        return None


class RefactoredExternalResultBridgeTests(unittest.TestCase):
    def test_bridge_injects_untrusted_external_event_without_recording_as_astrmai_reply(self):
        import sys

        message_components_mod = types.ModuleType("astrbot.api.message_components")
        message_components_mod.Plain = _Plain
        message_components_mod.Image = _Image
        sys.modules["astrbot.api.message_components"] = message_components_mod

        bridge_mod = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")
        bridge_mod = importlib.reload(bridge_mod)
        runtime = type(
            "Runtime",
            (),
            {
                "attention_gate": _FakeAttentionGate(),
                "evolution": _FakeEvolution(),
            },
        )()
        event = _FakeEvent()

        asyncio.run(bridge_mod.bridge_external_plugin_result(runtime, event))

        self.assertEqual(len(runtime.attention_gate.calls), 1)
        self.assertEqual(
            runtime.attention_gate.calls[0][1]["extra"]["astrmai_loop_source"],
            "external_result_bridge",
        )
        self.assertEqual(
            runtime.attention_gate.calls[0][1]["extra"]["astrmai_event_provenance"],
            "external_plugin",
        )
        self.assertFalse(
            runtime.attention_gate.calls[0][1]["extra"]["astrmai_is_committed_astrmai_reply"]
        )
        self.assertEqual(runtime.evolution.calls, [])


    def test_trusted_external_result_bypasses_self_filter_and_preserves_scope(self):
        bridge_mod = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")
        runtime = types.SimpleNamespace(
            attention_gate=_FakeAttentionGate(),
            evolution=_FakeEvolution(),
            host_bridge=_FakeHostBridge(),
            config=types.SimpleNamespace(
                global_settings=types.SimpleNamespace(
                    external_result_sources=["trusted_plugin"],
                    enable_error_interception=True,
                )
            ),
        )
        event = _FakeEvent()
        event._extra.update(
            {
                "astrmai_is_self_reply": True,
                "astrmai_loop_source": "trusted_plugin",
            }
        )

        asyncio.run(bridge_mod.bridge_external_plugin_result(runtime, event))

        self.assertEqual(len(runtime.attention_gate.calls), 1)
        chat_id, payload = runtime.attention_gate.calls[0]
        self.assertEqual(chat_id, event.unified_msg_origin)
        self.assertEqual(payload["unified_msg_origin"], event.unified_msg_origin)
        self.assertEqual(payload["group_id"], "group-1")
        self.assertEqual(payload["sender_id"], "bot-1")
        self.assertEqual(payload["self_id"], "bot-1")
        self.assertEqual(payload["extra"]["astrmai_origin_sender_id"], "user-1")

    def test_hidden_error_result_is_not_injected_or_recorded(self):
        bridge_mod = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")
        runtime = types.SimpleNamespace(
            attention_gate=_FakeAttentionGate(),
            evolution=_FakeEvolution(),
            host_bridge=_FakeHostBridge(),
            config=types.SimpleNamespace(
                global_settings=types.SimpleNamespace(
                    external_result_sources=["astrbot_builtin"],
                    enable_error_interception=True,
                )
            ),
        )
        event = _FakeEvent()
        event._result = types.SimpleNamespace(chain=[_Plain("Traceback: boom")])

        asyncio.run(bridge_mod.bridge_external_plugin_result(runtime, event))

        self.assertEqual(runtime.attention_gate.calls, [])
        self.assertEqual(runtime.evolution.calls, [])

    def test_private_external_result_keeps_private_origin_without_group_scope(self):
        bridge_mod = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")
        runtime = types.SimpleNamespace(
            attention_gate=_FakeAttentionGate(),
            evolution=_FakeEvolution(),
        )
        event = _FakePrivateEvent()

        asyncio.run(bridge_mod.bridge_external_plugin_result(runtime, event))

        chat_id, payload = runtime.attention_gate.calls[0]
        self.assertEqual(chat_id, "default:FriendMessage:user-1")
        self.assertEqual(payload["unified_msg_origin"], chat_id)
        self.assertEqual(payload["group_id"], "")
        self.assertEqual(payload["sender_id"], "bot-1")


if __name__ == "__main__":
    unittest.main()
