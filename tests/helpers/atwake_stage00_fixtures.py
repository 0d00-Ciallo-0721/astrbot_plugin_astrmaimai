import asyncio
import importlib
import sys
import tempfile
import types
from contextlib import contextmanager
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from tests.helpers.executor_stubs import install_executor_stubs


class FakeGateway:
    def __init__(self, *, chat_responses=None, tool_responses=None, models=None):
        self.calls = []
        self.chat_responses = dict(chat_responses or {})
        self.tool_responses = dict(tool_responses or {})
        self.models = list(models or ["model-a"])
        self.config = SimpleNamespace(
            agent=SimpleNamespace(max_steps=5, timeout=10),
            infra=SimpleNamespace(api_timeout=15),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            reply=SimpleNamespace(fallback_text="fallback"),
            vision=SimpleNamespace(enable_vision=True, image_recognition_probability=1.0,
                                   use_native_main_reply_vision=False, native_main_reply_failure_cooldown_sec=180),
        )

    def get_agent_models(self):
        return list(self.models)

    async def tool_chat_in_lane_result(self, **kwargs):
        self.calls.append(("tool", kwargs))
        response = self.tool_responses.get(kwargs["models"][0], "[TERMINAL_YIELD]: tool-finished")
        if callable(response):
            response = response(kwargs)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(text=response)


class FakeReplyService:
    def __init__(self):
        self.calls = []

    async def handle_reply(self, event, text, chat_id):
        self.calls.append((chat_id, text))
        event.set_extra("astrmai_reply_sent", True)
        return SimpleNamespace(sent=True, blocked_reason="", persistable_text=text)


class FakeEvolution:
    async def process_bot_reply(self, chat_id, bot_id, reply_text):
        return None


class FakeEvent:
    def __init__(self, *, sender_id="", sender_name="", text="hello"):
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self.message_str = text
        self.message_obj = None
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = {"astrmai_prefix_hash": "hash-1"}

    def get_self_id(self):
        return "bot-1"

    def get_group_id(self):
        return "group-1"

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


@contextmanager
def executor_fixture():
    names = (
        "astrbot", "astrbot.api", "astrbot.api.star", "astrbot.api.event",
        "astrbot.api.message_components", "astrbot.api.all", "astrbot.core",
        "astrbot.core.star", "astrbot.core.star.command_management", "astrbot.core.db",
        "astrbot.core.db.vec_db", "astrbot.core.db.vec_db.faiss_impl",
        "astrbot.core.db.vec_db.faiss_impl.vec_db", "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path", "astrbot.core.agent", "astrbot.core.agent.message",
        "astrbot.core.agent.run_context", "astrbot.core.agent.tool", "astrbot.core.astr_agent_context",
        "astrmai.Brain.reply_engine", "astrmai.infra.lane_manager", "astrmai.workmode",
        "astrmai.conversation.execution.executor",
    )
    snapshot = {name: sys.modules.get(name) for name in names}
    present = {name for name in names if name in sys.modules}
    temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    try:
        install_astrbot_stubs(temp_dir.name)
        install_executor_stubs()
        api_all_mod = types.ModuleType("astrbot.api.all")
        api_all_mod.Context = type("Context", (), {})
        sys.modules["astrbot.api.all"] = api_all_mod
        sys.modules.pop("astrmai.conversation.execution.executor", None)
        executor_mod = importlib.import_module("astrmai.conversation.execution.executor")
        yield executor_mod
    finally:
        temp_dir.cleanup()
        for name in names:
            if name in present:
                sys.modules[name] = snapshot[name]
            else:
                sys.modules.pop(name, None)


__all__ = ["FakeGateway", "FakeReplyService", "FakeEvolution", "FakeEvent", "executor_fixture"]
