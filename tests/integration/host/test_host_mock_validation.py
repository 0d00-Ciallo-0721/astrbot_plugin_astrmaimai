from __future__ import annotations

import asyncio
import base64
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from tests.helpers.astrbot_stubs import install_astrbot_stubs


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ASTRBOT_ROOT = Path(os.environ.get("ASTRBOT_ROOT", PROJECT_ROOT.parent / "AstrBot")).resolve()


def _purge_modules(prefixes: tuple[str, ...]) -> None:
    for name in list(sys.modules.keys()):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            sys.modules.pop(name, None)


def _install_extended_astrbot_stubs(data_dir: str) -> None:
    install_astrbot_stubs(data_dir)

    api_mod = sys.modules["astrbot.api"]
    event_mod = sys.modules["astrbot.api.event"]
    star_mod = sys.modules["astrbot.api.star"]
    comp_mod = sys.modules["astrbot.api.message_components"]

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

    class _At:
        def __init__(self, qq=""):
            self.qq = qq

    class _Reply:
        def __init__(self, sender_id="", sender_nickname=""):
            self.sender_id = sender_id
            self.sender_nickname = sender_nickname

    class _Poke:
        pass

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _MessageChain:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(text)
            return self

    def _register(*args, **kwargs):
        def wrap(cls):
            return cls

        return wrap

    class _Star:
        def __init__(self, context):
            self.context = context

    class _ToolSet:
        def __init__(self, tools=None):
            self.tools = list(tools or [])

        def get_light_tool_set(self):
            return self

    class _FunctionTool:
        def __class_getitem__(cls, item):
            return cls

    class _ToolExecResult:
        pass

    class _ContextWrapper:
        def __class_getitem__(cls, item):
            return cls

    class _AstrAgentContext:
        pass

    comp_mod.At = _At
    comp_mod.Reply = _Reply
    comp_mod.Poke = _Poke
    comp_mod.Plain = _Plain
    event_mod.MessageChain = _MessageChain
    event_mod.filter = _Filter()
    star_mod.register = _register
    star_mod.Star = _Star
    api_mod.AstrBotConfig = dict

    core_star_mod = types.ModuleType("astrbot.core.star")
    core_star_mod.__path__ = []
    command_management_mod = types.ModuleType("astrbot.core.star.command_management")
    command_management_mod._collect_descriptors = lambda include_sub_commands=True: []
    command_management_mod.list_commands = lambda: []

    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    tool_mod.ToolSet = _ToolSet
    tool_mod.FunctionTool = _FunctionTool
    tool_mod.ToolExecResult = _ToolExecResult

    run_context_mod = types.ModuleType("astrbot.core.agent.run_context")
    run_context_mod.ContextWrapper = _ContextWrapper

    agent_context_mod = types.ModuleType("astrbot.core.astr_agent_context")
    agent_context_mod.AstrAgentContext = _AstrAgentContext

    cron_tools_mod = types.ModuleType("astrbot.core.tools.cron_tools")
    cron_tools_mod.CREATE_CRON_JOB_TOOL = object()
    cron_tools_mod.DELETE_CRON_JOB_TOOL = object()
    cron_tools_mod.LIST_CRON_JOBS_TOOL = object()

    computer_python_mod = types.ModuleType("astrbot.core.computer.tools.python")
    computer_python_mod.LocalPythonTool = type("LocalPythonTool", (), {})

    computer_shell_mod = types.ModuleType("astrbot.core.computer.tools.shell")
    computer_shell_mod.ExecuteShellTool = type("ExecuteShellTool", (), {"__init__": lambda self, is_local=True: None})

    sys.modules["astrbot.core.star"] = core_star_mod
    sys.modules["astrbot.core.star.command_management"] = command_management_mod
    sys.modules["astrbot.core.agent.tool"] = tool_mod
    sys.modules["astrbot.core.agent.run_context"] = run_context_mod
    sys.modules["astrbot.core.astr_agent_context"] = agent_context_mod
    sys.modules["astrbot.core.tools.cron_tools"] = cron_tools_mod
    sys.modules["astrbot.core.computer.tools.python"] = computer_python_mod
    sys.modules["astrbot.core.computer.tools.shell"] = computer_shell_mod


async def _collect_asyncgen(gen) -> list:
    return [item async for item in gen]


class _StubMessageObj:
    def __init__(self, self_id: str, message=None):
        self.self_id = self_id
        self.message = list(message or [])


class _StubEvent:
    def __init__(
        self,
        *,
        umo: str,
        sender_id: str,
        sender_name: str,
        self_id: str = "bot-1",
        group_id: str = "",
        text: str = "",
        message=None,
        extras=None,
    ):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = _StubMessageObj(self_id=self_id, message=message or [])
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._group_id = group_id
        self._self_id = self_id
        self._extra = dict(extras or {})

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def plain_result(self, text):
        return {"type": "plain", "text": text}


class HostMockValidationTests(unittest.TestCase):
    def test_real_astrbot_host_can_register_refactor_plugin(self):
        if not (ASTRBOT_ROOT / "runtime_bootstrap.py").exists():
            self.skipTest("AstrBot host root not found; set ASTRBOT_ROOT or place AstrBot next to the plugin")
        with tempfile.TemporaryDirectory(prefix="astrmai-host-") as temp_root:
            env = os.environ.copy()
            env["ASTRBOT_ROOT"] = temp_root
            env["TESTING"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            script = textwrap.dedent(
                f"""
                import asyncio
                import os
                import shutil
                import sys
                from pathlib import Path

                temp_root = Path(r"{temp_root}")
                plugin_src = Path(r"{PROJECT_ROOT}")
                astrbot_root = Path(r"{ASTRBOT_ROOT}")
                plugin_dir_name = {PROJECT_ROOT.name!r}

                (temp_root / "data" / "plugins").mkdir(parents=True, exist_ok=True)
                (temp_root / "data" / "config").mkdir(parents=True, exist_ok=True)
                (temp_root / "data" / "temp").mkdir(parents=True, exist_ok=True)
                (temp_root / "data" / "plugin_data").mkdir(parents=True, exist_ok=True)
                (temp_root / "data" / "knowledge_base").mkdir(parents=True, exist_ok=True)
                shutil.copytree(plugin_src, temp_root / "data" / "plugins" / plugin_dir_name, dirs_exist_ok=True)

                sys.path.insert(0, str(temp_root))
                sys.path.insert(0, str(astrbot_root))

                import runtime_bootstrap
                runtime_bootstrap.initialize_runtime_bootstrap()
                from astrbot.core import LogBroker, db_helper
                from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

                async def main():
                    log_broker = LogBroker()
                    core = AstrBotCoreLifecycle(log_broker, db_helper)
                    try:
                        await core.initialize()
                        stars = list(core.plugin_manager.context.get_all_stars())
                        found = any(getattr(star, "root_dir_name", "") == plugin_dir_name for star in stars)
                        print(f"PLUGIN_FOUND={{found}}")
                    finally:
                        await core.stop()

                asyncio.run(main())
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(ASTRBOT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            output = result.stdout + "\n" + result.stderr
            self.assertEqual(result.returncode, 0, msg=output)
            self.assertIn("PLUGIN_FOUND=True", output)

    def test_message_entry_and_command_paths_work_with_mock_events(self):
        with tempfile.TemporaryDirectory(prefix="astrmai-entry-") as temp_dir:
            _install_extended_astrbot_stubs(temp_dir)
            _purge_modules(
                (
                    "astrmai.presentation.events.message_entry",
                    "astrmai.presentation.commands.mai_help",
                    "astrmai.presentation.commands.work_mode",
                )
            )

            from astrmai.presentation.events.message_entry import handle_global_message
            from astrmai.presentation.commands.mai_help import handle_mai_help
            from astrmai.presentation.commands.work_mode import handle_work_mode
            import astrbot.api.message_components as Comp

            calls: list[tuple[str, str]] = []

            class _LifecycleManager:
                def track_task(self, coro):
                    return asyncio.create_task(coro)

            class _Facade:
                async def update_user_stats(self, user_id: str):
                    calls.append(("update_user_stats", user_id))

                def is_framework_command(self, msg: str) -> bool:
                    return False

                def build_help_text(self) -> str:
                    return "mock-help"

                async def enter_sys3_direct(self, event):
                    yield event.plain_result("mock-work")

            async def _record_user_message(event):
                calls.append(("record_user_message", event.unified_msg_origin))

            async def _process_event(event):
                if not event.get_group_id():
                    calls.append(("attention", "private"))
                    return "PRIVATE_WAIT"
                if any(isinstance(component, Comp.At) for component in event.message_obj.message):
                    calls.append(("attention", "at"))
                    return "ENGAGED"
                if any(isinstance(component, Comp.Reply) for component in event.message_obj.message):
                    calls.append(("attention", "reply"))
                    return "ENGAGED"
                calls.append(("attention", "group"))
                return "BUFFERED"

            runtime = SimpleNamespace(
                config=SimpleNamespace(
                    global_settings=SimpleNamespace(
                        debug_mode=False,
                        whitelist_ids=["default:GroupMessage:group-1"],
                        admin_ids=[],
                        enable_private_chat=True,
                    ),
                    system1=SimpleNamespace(extra_command_list=[]),
                ),
                group_reply_wait_manager=SimpleNamespace(
                    handle_incoming_message=lambda event: "NONE",
                    cancel_wait=lambda *args, **kwargs: None,
                ),
                lifecycle=SimpleNamespace(manager=_LifecycleManager()),
                reflect_tracker=None,
                evolution=SimpleNamespace(record_user_message=_record_user_message),
                attention_gate=SimpleNamespace(process_event=_process_event),
                host_bridge=SimpleNamespace(suppress_default_llm=lambda event: "(ghost)"),
                sensors=None,
                context=SimpleNamespace(),
            )
            facade = _Facade()

            async def _run():
                ordinary = _StubEvent(
                    umo="default:GroupMessage:group-1",
                    sender_id="user-1",
                    sender_name="Alice",
                    group_id="group-1",
                    text="普通群消息",
                )
                at_event = _StubEvent(
                    umo="default:GroupMessage:group-1",
                    sender_id="user-2",
                    sender_name="Bob",
                    group_id="group-1",
                    text="@bot 你好",
                    message=[Comp.At("bot-1")],
                )
                reply_event = _StubEvent(
                    umo="default:GroupMessage:group-1",
                    sender_id="user-3",
                    sender_name="Carol",
                    group_id="group-1",
                    text="回复一下",
                    message=[Comp.Reply(sender_id="bot-1", sender_nickname="Mai")],
                )
                private_event = _StubEvent(
                    umo="default:FriendMessage:user-4",
                    sender_id="user-4",
                    sender_name="Dora",
                    text="私聊一下",
                )
                help_event = _StubEvent(
                    umo="default:FriendMessage:user-5",
                    sender_id="user-5",
                    sender_name="Eve",
                    text="/mai",
                )
                work_event = _StubEvent(
                    umo="default:FriendMessage:user-5",
                    sender_id="user-5",
                    sender_name="Eve",
                    text="/work test",
                )

                ordinary_results = await _collect_asyncgen(handle_global_message(runtime, facade, ordinary))
                at_results = await _collect_asyncgen(handle_global_message(runtime, facade, at_event))
                reply_results = await _collect_asyncgen(handle_global_message(runtime, facade, reply_event))
                private_results = await _collect_asyncgen(handle_global_message(runtime, facade, private_event))
                help_results = await _collect_asyncgen(handle_mai_help(facade, help_event))
                work_results = await _collect_asyncgen(handle_work_mode(facade, work_event))
                await asyncio.sleep(0)
                return (
                    ordinary_results,
                    at_results,
                    reply_results,
                    private_results,
                    help_results,
                    work_results,
                )

            ordinary_results, at_results, reply_results, private_results, help_results, work_results = asyncio.run(_run())

            self.assertEqual(ordinary_results, [])
            self.assertEqual(at_results, [{"type": "plain", "text": "(ghost)"}])
            self.assertEqual(reply_results, [{"type": "plain", "text": "(ghost)"}])
            self.assertEqual(private_results, [{"type": "plain", "text": "(ghost)"}])
            self.assertEqual(help_results, [{"type": "plain", "text": "mock-help"}])
            self.assertEqual(work_results, [{"type": "plain", "text": "mock-work"}])
            self.assertIn(("attention", "group"), calls)
            self.assertIn(("attention", "at"), calls)
            self.assertIn(("attention", "reply"), calls)
            self.assertIn(("attention", "private"), calls)

    def test_mock_system2_pipeline_preserves_state_lane_executor_reply_followup_chain(self):
        with tempfile.TemporaryDirectory(prefix="astrmai-sys2-") as temp_dir:
            _install_extended_astrbot_stubs(temp_dir)
            _purge_modules(("astrmai.conversation.execution.system2_runner",))
            from astrmai.conversation.execution.system2_runner import System2Runner

            call_order: list[str] = []

            class _DummyLock:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return False

            class _Coordinator:
                def __init__(self):
                    self.wait_updates = []

                async def get_sys2_lock(self, chat_id):
                    call_order.append("lock")
                    return _DummyLock()

                async def update_wait_targets(self, chat_id, targets, target_name):
                    call_order.append("wait_targets")
                    self.wait_updates.append((chat_id, targets, target_name))

            class _StateEngine:
                async def consume_energy(self, chat_id):
                    call_order.append("consume_energy")

            class _LaneManager:
                async def ensure_lane(self, lane_key, base_origin):
                    call_order.append("ensure_lane")

            class _Executor:
                async def execute(self, event, prompt="", system_prompt=""):
                    call_order.append("executor")
                    return SimpleNamespace(text="mock-reply")

            class _ReplyEngine:
                def __init__(self):
                    self.replies = []

                async def handle_reply(self, event, text, chat_id):
                    call_order.append("reply_service")
                    self.replies.append((chat_id, text))

            class _Planner:
                def __init__(self, runtime):
                    self.runtime = runtime

                async def plan_and_execute(self, event, queue_events):
                    call_order.append("planner")
                    result = await self.runtime.executor.execute(event, "mock prompt", "mock system")
                    await self.runtime.reply_engine.handle_reply(event, result.text, event.unified_msg_origin)
                    event.set_extra("astrmai_reply_sent", True)
                    event.set_extra("astrmai_wait_targets", ["user-2"])
                    event.set_extra("astrmai_wait_target_name", "Bob")

            class _GroupReplyWaitManager:
                def __init__(self):
                    self.events = []

                def register_from_reply_event(self, event):
                    call_order.append("group_wait")
                    self.events.append(event)

            runtime = SimpleNamespace(
                runtime_coordinator=_Coordinator(),
                state_engine=_StateEngine(),
                lane_manager=_LaneManager(),
                executor=_Executor(),
                reply_engine=_ReplyEngine(),
                private_chat_manager=None,
                group_reply_wait_manager=_GroupReplyWaitManager(),
            )
            runtime.system2_planner = _Planner(runtime)
            runner = System2Runner(runtime)
            event = _StubEvent(
                umo="default:GroupMessage:group-1",
                sender_id="user-1",
                sender_name="Alice",
                group_id="group-1",
                text="继续这个话题",
            )

            reply_sent = asyncio.run(runner.run(event))

            self.assertTrue(reply_sent)
            self.assertEqual(
                call_order,
                ["lock", "consume_energy", "ensure_lane", "planner", "executor", "reply_service", "wait_targets", "group_wait"],
            )
            self.assertEqual(runtime.reply_engine.replies, [(event.unified_msg_origin, "mock-reply")])
            self.assertEqual(
                runtime.runtime_coordinator.wait_updates,
                [(event.unified_msg_origin, ["user-2"], "Bob")],
            )

    def test_mock_multimodal_workmode_proactive_and_webui_minimal_smoke(self):
        with tempfile.TemporaryDirectory(prefix="astrmai-enhanced-") as temp_dir:
            _install_extended_astrbot_stubs(temp_dir)
            _purge_modules(
                (
                    "astrmai.multimodal.visual_cortex",
                    "astrmai.multimodal.image_pipeline",
                    "astrmai.proactive.proactive_task",
                    "astrmai.workmode.router",
                    "astrmai.webui.backend.server",
                )
            )
            from astrmai.multimodal.visual_cortex import VisualCortex
            from astrmai.proactive.proactive_task import ProactiveTask
            from astrmai.workmode.router import Sys3Router
            from astrmai.webui.backend.server import app

            img = Image.new("RGB", (2, 2), color="red")
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            png_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            stored = {}

            class _Session:
                def get(self, cls, key):
                    return stored.get(key)

                def add(self, item):
                    stored[item.picid] = item

                def commit(self):
                    return None

            class _SessionCtx:
                def __enter__(self):
                    return _Session()

                def __exit__(self, exc_type, exc, tb):
                    return False

            class _DbService:
                def get_session(self):
                    return _SessionCtx()

            class _Gateway:
                def __init__(self):
                    self.config = SimpleNamespace(
                        life=SimpleNamespace(
                            dream_interval_min=1,
                            dream_time_ranges=[],
                            silence_threshold=10,
                            wakeup_min_energy=20,
                            wakeup_cost=5,
                            wakeup_cooldown=60,
                            dream_visible=False,
                            profiling_msg_threshold=200,
                        ),
                        persona=SimpleNamespace(persona_id="global", name="Mai"),
                        evolution=SimpleNamespace(enable_expression_mining=False, enable_relationship_engine=False),
                    )

                async def call_vision_task(self, **kwargs):
                    return {"type": "emoji", "description": "mock vision", "emotion_tags": ["happy"]}

                async def call_proactive_task(self, **kwargs):
                    return "mock proactive"

            gateway = _Gateway()
            db_service = _DbService()
            cortex = VisualCortex(gateway, db_service)
            asyncio.run(cortex.process_image_async("pic-1", png_base64, scope_id="chat-1"))

            state_engine = SimpleNamespace(
                get_active_states=lambda: [],
                get_active_profiles=lambda: [],
                apply_natural_decay=lambda state: None,
            )
            persistence = SimpleNamespace(load_persona_cache=lambda: {})
            proactive = ProactiveTask(
                context=SimpleNamespace(send_message=None),
                state_engine=state_engine,
                gateway=gateway,
                persistence=persistence,
                memory_engine=SimpleNamespace(),
                reflector=None,
                config=gateway.config,
            )
            proactive.set_db_service(SimpleNamespace())

            async def _run_proactive():
                await proactive.start()
                await asyncio.sleep(0)
                running = proactive.describe_status()["running"]
                await proactive.stop()
                await asyncio.sleep(0)
                return running, proactive.describe_status()

            running, proactive_status = asyncio.run(_run_proactive())

            router = Sys3Router(SimpleNamespace(), SimpleNamespace())
            static_agents = router.get_static_agent_names()
            route_paths = {getattr(route, "path", "") for route in app.router.routes}

            self.assertIn("pic-1", stored)
            self.assertEqual(stored["pic-1"].description, "mock vision")
            self.assertTrue(running)
            self.assertIn("dream_scheduler", proactive_status)
            self.assertIn("transfer_to_cron", static_agents)
            self.assertIn("transfer_to_computer", static_agents)
            self.assertEqual(app.title, "AstrMai WebUI")
            self.assertIn("/api/dashboard", route_paths)
            self.assertIn("/api/users", route_paths)
            self.assertIn("/api/memories/events", route_paths)
            self.assertIn("/api/memories/nodes", route_paths)
            self.assertIn("/api/reviews", route_paths)


if __name__ == "__main__":
    unittest.main()
