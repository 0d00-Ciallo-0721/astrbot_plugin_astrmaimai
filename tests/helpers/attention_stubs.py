import sys
import types


def install_attention_stubs():
    state_mod = types.ModuleType("astrmai.Heart.state_engine")
    state_mod.StateEngine = type("StateEngine", (), {})
    sys.modules["astrmai.Heart.state_engine"] = state_mod

    judge_mod = types.ModuleType("astrmai.Heart.judge")
    judge_mod.Judge = type("Judge", (), {})
    sys.modules["astrmai.Heart.judge"] = judge_mod

    sensors_mod = types.ModuleType("astrmai.Heart.sensors")
    sensors_mod.PreFilters = type("PreFilters", (), {})
    sys.modules["astrmai.Heart.sensors"] = sensors_mod

    message_components_mod = types.ModuleType("astrbot.api.message_components")
    for name in ["Image", "Plain", "At", "Face"]:
        setattr(message_components_mod, name, type(name, (), {}))
    sys.modules["astrbot.api.message_components"] = message_components_mod
