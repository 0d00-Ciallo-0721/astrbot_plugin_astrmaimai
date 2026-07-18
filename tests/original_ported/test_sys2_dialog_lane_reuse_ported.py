import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs
from tests.helpers.executor_stubs import install_executor_stubs
from tests.helpers.planner_stubs import install_planner_stubs


class _FakeGateway:
    def __init__(self):
        self.calls = []
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            reply=SimpleNamespace(fallback_text="fallback"),
            infra=SimpleNamespace(api_timeout=15),
        )

    def get_agent_models(self):
        return ["model-a"]

    async def chat_in_lane_result(self, **kwargs):
        self.calls.append(("chat", kwargs))
        return SimpleNamespace(text="lane-text-reply")

    async def tool_chat_in_lane_result(self, **kwargs):
        self.calls.append(("tool", kwargs))
        return SimpleNamespace(text="lane-tool-reply")


class _FakeReplyEngine:
    def __init__(self):
        self.replies = []

    async def handle_reply(self, event, text, chat_id):
        self.replies.append((chat_id, text))


class _FakeEvent:
    def __init__(self, *, group=True):
        self.unified_msg_origin = "default:GroupMessage:group-1" if group else "default:FriendMessage:user-1"
        self.message_str = "hello"
        self.message_obj = None
        self._group = group
        self._extra = {
            "astrmai_prefix_hash": "hash-1",
            "astrmai_turn_thread_id": "sender:user-1",
        }

    def get_self_id(self):
        return "bot-1"

    def get_group_id(self):
        return "group-1" if self._group else ""

    def get_sender_id(self):
        return "user-1"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class Sys2DialogLaneReusePortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.execution.executor", None)
        self.executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        self.executor_mod = importlib.reload(self.executor_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_text_mode_uses_dialog_lane_gateway(self):
        gateway = _FakeGateway()
        reply_engine = _FakeReplyEngine()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_engine,
            evolution_manager=SimpleNamespace(),
            config=gateway.config,
        )

        async def _run():
            return await executor.execute(_FakeEvent(), "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "lane-text-reply")
        self.assertEqual(len(gateway.calls), 1)
        mode, kwargs = gateway.calls[0]
        self.assertEqual(mode, "chat")
        self.assertEqual(kwargs["lane_key"].task_family, "dialog")
        self.assertEqual(kwargs["base_origin"], "default:GroupMessage:group-1@@thread:sender:user-1")
        self.assertEqual(kwargs["lane_key"].scope_id, "default:GroupMessage:group-1#sender:user-1")

    def test_private_text_mode_keeps_chat_scoped_dialog_lane(self):
        gateway = _FakeGateway()
        reply_engine = _FakeReplyEngine()
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=gateway,
            reply_engine=reply_engine,
            evolution_manager=SimpleNamespace(),
            config=gateway.config,
        )

        async def _run():
            return await executor.execute(_FakeEvent(group=False), "prompt", "system")

        result = asyncio.run(_run())

        self.assertEqual(result, "lane-text-reply")
        _mode, kwargs = gateway.calls[0]
        self.assertEqual(kwargs["base_origin"], "default:FriendMessage:user-1")
        self.assertEqual(kwargs["lane_key"].scope_id, "default:FriendMessage:user-1")


if __name__ == "__main__":
    unittest.main()
