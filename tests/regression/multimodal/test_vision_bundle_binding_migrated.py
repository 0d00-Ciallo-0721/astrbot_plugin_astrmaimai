import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.helpers import install_astrbot_stubs


def _install_executor_deps():
    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    tool_mod.ToolSet = type("ToolSet", (), {"__init__": lambda self, tools: setattr(self, "tools", tools)})
    sys.modules["astrbot.core.agent.tool"] = tool_mod

    reply_mod = types.ModuleType("astrmai.Brain.reply_engine")
    reply_mod.ReplyEngine = type("ReplyEngine", (), {})
    sys.modules["astrmai.Brain.reply_engine"] = reply_mod

    lane_mod = types.ModuleType("astrmai.infra.lane_manager")
    lane_mod.LaneKey = type("LaneKey", (), {})
    lane_mod.LaneManager = type("LaneManager", (), {})
    sys.modules["astrmai.infra.lane_manager"] = lane_mod


class _FakeEvent:
    def __init__(self, focus_context):
        self.message_str = ""
        self._extra = {"astrmai_focus_thread_context": focus_context, "direct_vision_urls": ["legacy.jpg"]}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)


class VisionBundleBindingMigratedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        _install_executor_deps()
        sys.modules.pop("astrmai.infrastructure.runtime.runtime_contracts", None)
        sys.modules.pop("astrmai.conversation.execution.executor", None)
        self.contracts_mod = importlib.import_module("astrmai.infrastructure.runtime.runtime_contracts")
        self.contracts_mod = importlib.reload(self.contracts_mod)
        self.executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        self.executor_mod = importlib.reload(self.executor_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_executor_prefers_focus_thread_vision_bundle(self):
        focus_context = self.contracts_mod.FocusThreadContext(
            focus_event=object(),
            vision_bundle=self.contracts_mod.VisionBundle(
                image_urls=["thread-a.jpg", "thread-b.jpg"],
                direct_image_urls=["thread-a.jpg"],
                is_direct_request=True,
                source="focus_thread",
            ),
        )
        event = _FakeEvent(focus_context)
        executor = self.executor_mod.ConcurrentExecutor(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(), get_agent_models=lambda: []),
            reply_engine=SimpleNamespace(),
            evolution_manager=SimpleNamespace(),
            config=SimpleNamespace(),
        )
        bundle = executor._build_vision_bundle(event, ["extra.jpg"])
        self.assertEqual(bundle.direct_image_urls, ["thread-a.jpg", "extra.jpg"])
        self.assertEqual(bundle.image_urls, ["thread-a.jpg", "thread-b.jpg", "extra.jpg"])
        self.assertEqual(bundle.source, "focus_thread")


__all__ = ["VisionBundleBindingMigratedTests"]
