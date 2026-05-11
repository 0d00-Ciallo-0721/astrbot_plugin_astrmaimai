from importlib import import_module

_EXPORTS = {
    "BrainActionPlan": ".action_plan",
    "Judge": ".judge",
    "JUDGE_STABLE_PREFIX": ".judge_prompt",
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
