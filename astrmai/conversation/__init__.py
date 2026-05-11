from importlib import import_module

_EXPORTS = {
    "AttentionGate": ".attention",
    "Judge": ".decision",
    "ReplyService": ".execution",
    "System2Runner": ".execution",
    "ContextEngine": ".planning",
    "Planner": ".planning",
    "PromptRefiner": ".planning",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(f"{__name__}{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value
