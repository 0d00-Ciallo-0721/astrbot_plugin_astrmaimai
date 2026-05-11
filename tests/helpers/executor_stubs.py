import sys
import types


def install_executor_stubs():
    tool_mod = types.ModuleType("astrbot.core.agent.tool")

    class ToolSet:
        def __init__(self, tools):
            self.tools = tools

    tool_mod.ToolSet = ToolSet
    sys.modules["astrbot.core.agent.tool"] = tool_mod

    reply_mod = types.ModuleType("astrmai.Brain.reply_engine")
    reply_mod.ReplyEngine = type("ReplyEngine", (), {})
    sys.modules["astrmai.Brain.reply_engine"] = reply_mod

    lane_mod = types.ModuleType("astrmai.infra.lane_manager")
    lane_mod.LaneKey = type("LaneKey", (), {})
    lane_mod.LaneManager = type("LaneManager", (), {})
    sys.modules["astrmai.infra.lane_manager"] = lane_mod
