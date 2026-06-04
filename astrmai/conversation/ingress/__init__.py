from importlib import import_module

_EXPORTS = {
    "build_message_signature_text": ".dedupe",
    "check_message_dedup": ".dedupe",
    "check_framework_command": ".command_guard",
    "check_command_access": ".permission_guard",
    "check_message_scope_access": ".permission_guard",
    "handle_poke_if_needed": ".poke_handler",
    "bridge_external_plugin_result": ".external_result_bridge",
    "extract_external_reply_text": ".external_result_bridge",
    "PreFilters": ".sensors",
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
