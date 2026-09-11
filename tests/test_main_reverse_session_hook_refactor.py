import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _install_main_import_stubs():
    install_astrbot_stubs(tempfile.mkdtemp(prefix="astrmai-main-hook-"))

    class _Filter:
        class EventMessageType:
            ALL = "all"

        def _decorator(self, *args, **kwargs):
            def wrap(func):
                return func
            return wrap

        def on_astrbot_loaded(self, *args, **kwargs):
            return self._decorator(*args, **kwargs)

        def on_llm_request(self, *args, **kwargs):
            return self._decorator(*args, **kwargs)

        def command(self, *args, **kwargs):
            return self._decorator(*args, **kwargs)

        def on_decorating_result(self, *args, **kwargs):
            return self._decorator(*args, **kwargs)

        def event_message_type(self, *args, **kwargs):
            return self._decorator(*args, **kwargs)

    class MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(text)
            return self

    class FunctionTool:
        def __init__(self, *args, **kwargs):
            pass

        def __class_getitem__(cls, item):
            return cls

    class ToolSet:
        def __init__(self, tools=None):
            self.tools = tools or []

    class ToolExecResult:
        pass

    class ContextWrapper:
        def __class_getitem__(cls, item):
            return cls

    class AstrAgentContext:
        pass

    sys.modules["astrbot.api.event"].filter = _Filter()
    sys.modules["astrbot.api.event"].MessageChain = MessageChain

    def register(*args, **kwargs):
        def wrap(cls):
            return cls
        return wrap

    class Star:
        def __init__(self, context):
            self.context = context

    sys.modules["astrbot.api.star"].register = register
    sys.modules["astrbot.api.star"].Star = Star

    star_mod = types.ModuleType("astrbot.core.star")
    star_mod.__path__ = []
    cmd_mod = types.ModuleType("astrbot.core.star.command_management")
    cmd_mod.list_commands = lambda: []
    cmd_mod._collect_descriptors = lambda include_sub_commands=True: []
    run_context_mod = types.ModuleType("astrbot.core.agent.run_context")
    run_context_mod.ContextWrapper = ContextWrapper
    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    tool_mod.FunctionTool = FunctionTool
    tool_mod.ToolSet = ToolSet
    tool_mod.ToolExecResult = ToolExecResult
    astr_agent_context_mod = types.ModuleType("astrbot.core.astr_agent_context")
    astr_agent_context_mod.AstrAgentContext = AstrAgentContext
    sys.modules["astrbot.core.star"] = star_mod
    sys.modules["astrbot.core.star.command_management"] = cmd_mod
    sys.modules["astrbot.core.agent.run_context"] = run_context_mod
    sys.modules["astrbot.core.agent.tool"] = tool_mod
    sys.modules["astrbot.core.astr_agent_context"] = astr_agent_context_mod


class ReverseSessionMainHookTests(unittest.TestCase):
    def setUp(self):
        _install_main_import_stubs()
        sys.modules.pop("main", None)
        self.main_mod = importlib.import_module("main")

    def test_hook_injects_reverse_session_block_for_gemini_reverse_provider(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        provider = SimpleNamespace(
            meta=lambda: SimpleNamespace(type="openai_chat_completion"),
            provider_config={"reverse_provider": "gemini_reverse"},
        )
        plugin.context = SimpleNamespace(get_using_provider=lambda origin: provider)
        class _Event:
            def __init__(self):
                self.unified_msg_origin = "platform:FriendMessage:user-1"
                self._extra = {"astrmai_request_trace": {"gateway_system_hash": "prehook1111"}}

            def get_extra(self, key, default=None):
                return self._extra.get(key, default)

            def set_extra(self, key, value):
                self._extra[key] = value

        event = _Event()
        request = SimpleNamespace(system_prompt="base prompt", session_id="session-1")

        asyncio.run(plugin.inject_gemini_reverse_session(event, request))

        self.assertIn("base prompt", request.system_prompt)
        self.assertIn("astrbot_reverse_session", request.system_prompt)
        self.assertIn("session_id=session-1", request.system_prompt)
        self.assertIn("session_scope=platform:FriendMessage:user-1", request.system_prompt)
        trace = event.get_extra("astrmai_request_trace", {})
        self.assertEqual(trace["request_session_id"], "session-1")
        self.assertEqual(trace["post_hook_system_hash"], event.get_extra("astrmai_post_hook_system_hash"))
        self.assertEqual(trace["provider_visible_system_hash"], event.get_extra("astrmai_post_hook_system_hash"))

    def test_plugin_initialize_starts_runtime_after_hot_reload(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(
                activated=True,
                star_cls=plugin,
            ),
        )

        class _Facade:
            async def on_program_start(self, *, source: str = ""):
                calls.append("start")

        plugin.facade = _Facade()

        asyncio.run(plugin.initialize())

        self.assertEqual(calls, ["start"])

    def test_disabled_plugin_initialize_skips_start_and_terminates_active_runtime(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(activated=False),
        )

        class _Facade:
            runtime = SimpleNamespace(status=SimpleNamespace(is_running=True, lifecycle_started=True))

            async def on_program_start(self, *, source: str = ""):
                calls.append(("start", source))

            async def terminate(self):
                calls.append(("terminate", "disabled"))

        plugin.facade = _Facade()

        asyncio.run(plugin.initialize())

        self.assertEqual(calls, [("terminate", "disabled")])

    def test_disabled_loaded_hook_does_not_restart_runtime(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(activated=False),
        )

        class _Facade:
            runtime = SimpleNamespace(status=SimpleNamespace(is_running=False, lifecycle_started=False))

            async def on_program_start(self, *, source: str = ""):
                calls.append(("start", source))

        plugin.facade = _Facade()

        asyncio.run(plugin.on_program_start())

        self.assertEqual(calls, [])

    def test_unregistered_reload_hook_fails_closed(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: None,
        )

        class _Facade:
            runtime = SimpleNamespace(status=SimpleNamespace(is_running=False, lifecycle_started=False))

            async def on_program_start(self, *, source: str = ""):
                calls.append(source)

        plugin.facade = _Facade()

        asyncio.run(plugin.on_program_start())

        self.assertEqual(calls, [])

    def test_enabled_metadata_without_bound_instance_fails_closed(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(
                activated=True,
                star_cls=None,
            ),
        )

        class _Facade:
            async def on_program_start(self, *, source: str = ""):
                calls.append(source)

        plugin.facade = _Facade()

        asyncio.run(plugin.on_program_start())

        self.assertEqual(calls, [])

    def test_registry_lookup_error_falls_back_to_all_stars_for_current_instance(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: (_ for _ in ()).throw(RuntimeError("registry unavailable")),
            get_all_stars=lambda: [
                SimpleNamespace(
                    name="astrmai",
                    activated=True,
                    star_cls=plugin,
                )
            ],
        )

        class _Facade:
            async def on_program_start(self, *, source: str = ""):
                calls.append(source)

        plugin.facade = _Facade()

        asyncio.run(plugin.on_program_start())

        self.assertEqual(calls, ["astrbot_loaded"])

    def test_registry_enumeration_error_fails_closed(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_all_stars=lambda: (_ for _ in ()).throw(RuntimeError("enumeration unavailable")),
        )

        class _Facade:
            async def on_program_start(self, *, source: str = ""):
                calls.append(source)

        plugin.facade = _Facade()

        asyncio.run(plugin.on_program_start())

        self.assertEqual(calls, [])

    def test_disabled_runtime_without_terminate_method_does_not_start(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(activated=False),
        )

        class _Facade:
            async def on_program_start(self, *, source: str = ""):
                calls.append(source)

        plugin.facade = _Facade()

        asyncio.run(plugin.initialize())

        self.assertEqual(calls, [])

    def test_stale_instance_cannot_start_after_current_instance_reloads(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        calls = []
        current_plugin = object()
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(
                activated=True,
                star_cls=current_plugin,
            ),
        )

        class _Facade:
            runtime = SimpleNamespace(status=SimpleNamespace(is_running=False, lifecycle_started=False))

            async def on_program_start(self, *, source: str = ""):
                calls.append(source)

        plugin.facade = _Facade()

        asyncio.run(plugin.on_program_start())

        self.assertEqual(calls, [])

    def test_disabled_cleanup_failure_is_not_hidden(self):
        plugin = object.__new__(self.main_mod.AstrMaiPlugin)
        plugin.context = SimpleNamespace(
            get_registered_star=lambda name: SimpleNamespace(activated=False),
        )

        class _Facade:
            async def terminate(self):
                raise RuntimeError("cleanup failed")

        plugin.facade = _Facade()

        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            asyncio.run(plugin.initialize())


if __name__ == "__main__":
    unittest.main()
