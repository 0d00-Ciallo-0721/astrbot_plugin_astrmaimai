import asyncio
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


class RefactoredExternalResultBridgeTests(unittest.TestCase):
    def test_bridge_injects_attention_event_and_records_bot_reply(self):
        import sys

        message_components_mod = types.ModuleType("astrbot.api.message_components")
        message_components_mod.Plain = _Plain
        message_components_mod.Image = _Image
        sys.modules["astrbot.api.message_components"] = message_components_mod

        bridge_mod = __import__(
            "astrmai.conversation.ingress.external_result_bridge",
            fromlist=["bridge_external_plugin_result"],
        )
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
            runtime.evolution.calls,
            [("default:GroupMessage:group-1", "bot-1", "(内置插件执行结果): 任务完成[图片]")],
        )


if __name__ == "__main__":
    unittest.main()
