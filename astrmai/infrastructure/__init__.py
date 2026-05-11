from importlib import import_module

_EXPORTS = {
    "DatabaseService": ".persistence",
    "EventBus": ".runtime",
    "GlobalModelGateway": ".gateway",
    "ChatRuntimeCoordinator": ".runtime",
    "HostBridge": ".runtime",
    "LaneKey": ".runtime",
    "LaneManager": ".runtime",
    "PersistenceManager": ".persistence",
    "append_trace_stage": ".runtime",
    "debug_trace": ".runtime",
    "ensure_trace_id": ".runtime",
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
