from importlib import import_module

_EXPORTS = {
    "ConcurrentExecutor": ".executor",
    "FollowupManager": ".followup_manager",
    "intercept_outbound_error": ".outbound_error_policy",
    "ReplyService": ".reply_service",
    "QQActionDispatcher": ".qq_action_dispatcher",
    "System2Runner": ".system2_runner",
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
