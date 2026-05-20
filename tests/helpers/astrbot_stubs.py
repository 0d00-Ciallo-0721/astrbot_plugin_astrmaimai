import sys
import types


class _DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


def install_astrbot_stubs(data_dir: str):
    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.__path__ = []
    api_mod = types.ModuleType("astrbot.api")
    api_mod.__path__ = []
    api_mod.logger = _DummyLogger()
    api_star_mod = types.ModuleType("astrbot.api.star")
    api_event_mod = types.ModuleType("astrbot.api.event")
    api_message_components_mod = types.ModuleType("astrbot.api.message_components")
    api_star_mod.Context = type("Context", (), {})
    api_event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    api_event_mod.MessageChain = type("MessageChain", (), {})
    for name in ["Plain", "At", "Image", "Face"]:
        setattr(api_message_components_mod, name, type(name, (), {}))

    core_mod = types.ModuleType("astrbot.core")
    core_mod.__path__ = []
    core_star_mod = types.ModuleType("astrbot.core.star")
    core_star_mod.__path__ = []
    core_star_command_mod = types.ModuleType("astrbot.core.star.command_management")
    utils_mod = types.ModuleType("astrbot.core.utils")
    utils_mod.__path__ = []
    path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")
    agent_mod = types.ModuleType("astrbot.core.agent")
    agent_mod.__path__ = []
    agent_message_mod = types.ModuleType("astrbot.core.agent.message")
    agent_run_context_mod = types.ModuleType("astrbot.core.agent.run_context")
    agent_tool_mod = types.ModuleType("astrbot.core.agent.tool")
    astr_agent_context_mod = types.ModuleType("astrbot.core.astr_agent_context")
    fake_workmode_mod = types.ModuleType("astrmai.workmode")
    db_mod = types.ModuleType("astrbot.core.db")
    db_mod.__path__ = []
    vec_db_mod = types.ModuleType("astrbot.core.db.vec_db")
    vec_db_mod.__path__ = []
    faiss_impl_mod = types.ModuleType("astrbot.core.db.vec_db.faiss_impl")
    faiss_impl_mod.__path__ = []
    faiss_vec_db_mod = types.ModuleType("astrbot.core.db.vec_db.faiss_impl.vec_db")

    agent_message_mod.SystemMessageSegment = type("SystemMessageSegment", (), {})
    agent_message_mod.UserMessageSegment = type("UserMessageSegment", (), {})
    agent_message_mod.TextPart = type("TextPart", (), {})
    agent_message_mod.ImagePart = type("ImagePart", (), {})
    agent_run_context_mod.ContextWrapper = type("ContextWrapper", (), {})
    agent_tool_mod.FunctionTool = type("FunctionTool", (), {"__class_getitem__": classmethod(lambda cls, item: cls)})
    agent_tool_mod.ToolSet = type(
        "ToolSet",
        (),
        {
            "__init__": lambda self, items=None: setattr(self, "items", list(items or [])),
            "get_light_tool_set": lambda self: self,
        },
    )
    astr_agent_context_mod.AstrAgentContext = type("AstrAgentContext", (), {})
    fake_workmode_mod.CronHeartbeatGuard = type("CronHeartbeatGuard", (), {})
    fake_workmode_mod.Sys3Router = type("Sys3Router", (), {})
    core_star_command_mod.list_commands = lambda *args, **kwargs: []
    faiss_vec_db_mod.FaissVecDB = type("FaissVecDB", (), {})

    def _get_astrbot_data_path():
        return data_dir

    path_mod.get_astrbot_data_path = _get_astrbot_data_path

    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod
    sys.modules["astrbot.api.star"] = api_star_mod
    sys.modules["astrbot.api.event"] = api_event_mod
    sys.modules["astrbot.api.message_components"] = api_message_components_mod
    sys.modules["astrbot.core"] = core_mod
    sys.modules["astrbot.core.star"] = core_star_mod
    sys.modules["astrbot.core.star.command_management"] = core_star_command_mod
    sys.modules["astrbot.core.db"] = db_mod
    sys.modules["astrbot.core.db.vec_db"] = vec_db_mod
    sys.modules["astrbot.core.db.vec_db.faiss_impl"] = faiss_impl_mod
    sys.modules["astrbot.core.db.vec_db.faiss_impl.vec_db"] = faiss_vec_db_mod
    sys.modules["astrbot.core.utils"] = utils_mod
    sys.modules["astrbot.core.utils.astrbot_path"] = path_mod
    sys.modules["astrbot.core.agent"] = agent_mod
    sys.modules["astrbot.core.agent.message"] = agent_message_mod
    sys.modules["astrbot.core.agent.run_context"] = agent_run_context_mod
    sys.modules["astrbot.core.agent.tool"] = agent_tool_mod
    sys.modules["astrbot.core.astr_agent_context"] = astr_agent_context_mod
    sys.modules.setdefault("astrmai.workmode", fake_workmode_mod)
