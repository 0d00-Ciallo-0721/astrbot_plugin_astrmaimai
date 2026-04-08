import asyncio
import importlib
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace

from tests.test_persistence_regressions import _install_astrbot_stubs


def _install_planner_stubs():
    def _make_module(name):
        module = types.ModuleType(name)
        sys.modules[name] = module
        return module

    gateway_mod = _make_module("astrmai.infra.gateway")
    gateway_mod.GlobalModelGateway = type("GlobalModelGateway", (), {})

    context_engine_mod = _make_module("astrmai.Brain.context_engine")
    context_engine_mod.ContextEngine = type("ContextEngine", (), {})

    executor_mod = _make_module("astrmai.Brain.executor")

    class ConcurrentExecutor:
        def __init__(self, *args, **kwargs):
            pass

    executor_mod.ConcurrentExecutor = ConcurrentExecutor

    reply_engine_mod = _make_module("astrmai.Brain.reply_engine")
    reply_engine_mod.ReplyEngine = type("ReplyEngine", (), {})

    goal_manager_mod = _make_module("astrmai.Brain.goal_manager")

    class GoalManager:
        def __init__(self, *args, **kwargs):
            pass

        async def analyze_and_update(self, *args, **kwargs):
            return ""

        def get_goals_context(self, *args, **kwargs):
            return ""

    goal_manager_mod.GoalManager = GoalManager

    action_modifier_mod = _make_module("astrmai.Brain.action_modifier")

    class ActionModifier:
        def __init__(self, *args, **kwargs):
            pass

        def modify_tools(self, tools, **kwargs):
            return tools

    action_modifier_mod.ActionModifier = ActionModifier

    expression_selector_mod = _make_module("astrmai.Brain.expression_selector")

    class ExpressionSelector:
        def __init__(self, *args, **kwargs):
            pass

        async def select(self, *args, **kwargs):
            return ""

    expression_selector_mod.ExpressionSelector = ExpressionSelector

    pfc_mod = _make_module("astrmai.Brain.tools.pfc_tools")

    def _tool_class(name):
        class Tool:
            def __init__(self, *args, **kwargs):
                self.name = name
                self.description = name

        Tool.__name__ = name
        return Tool

    for tool_name in [
        "WaitTool",
        "OmniPerceptionTool",
        "ConstructAtEventTool",
        "ProactivePokeTool",
        "ProactiveMemeTool",
        "MemeResonanceTool",
        "TopicHijackTool",
        "SpaceTransitionTool",
        "RegretAndWithdrawTool",
        "MessageReactionTool",
        "ProactiveLikeTool",
        "SelfLoreQueryTool",
    ]:
        setattr(pfc_mod, tool_name, _tool_class(tool_name))

    memory_engine_mod = _make_module("astrmai.memory.engine")
    memory_engine_mod.MemoryEngine = type("MemoryEngine", (), {})

    evolution_mod = _make_module("astrmai.evolution.processor")
    evolution_mod.EvolutionManager = type("EvolutionManager", (), {})


class PlannerFollowUpTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        _install_planner_stubs()
        sys.modules.pop("astrmai.Brain.planner", None)
        self.planner_mod = importlib.import_module("astrmai.Brain.planner")
        self.planner_mod = importlib.reload(self.planner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_should_follow_up_awaits_state_engine(self):
        called = {"awaited": False}

        async def _get_state(chat_id):
            called["awaited"] = True
            return SimpleNamespace(energy=0.2)

        planner = self.planner_mod.Planner(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace()),
            context_engine=SimpleNamespace(db=SimpleNamespace()),
            reply_engine=SimpleNamespace(),
            memory_engine=SimpleNamespace(),
            evolution_manager=SimpleNamespace(),
            state_engine=SimpleNamespace(get_state=_get_state),
            prompt_refiner=SimpleNamespace(),
            sys3_router=None,
        )

        result = asyncio.run(
            planner._should_follow_up("chat-1", "这是一条足够长的回复内容，用来避免长度分支过早返回。")
        )

        self.assertIsNone(result)
        self.assertTrue(called["awaited"])


if __name__ == "__main__":
    unittest.main()
