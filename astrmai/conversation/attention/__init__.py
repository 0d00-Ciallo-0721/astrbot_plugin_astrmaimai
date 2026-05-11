from importlib import import_module

_EXPORTS = {
    "AttentionGate": ".gate",
    "SessionContext": ".gate",
    "NormalizedEvent": ".event_normalizer",
    "build_normalized_events": ".event_normalizer",
    "score_focus_candidate": ".focus_selector",
    "select_focus_event": ".focus_selector",
    "build_focus_thread": ".thread_builder",
    "resolve_thread_root": ".thread_builder",
    "extract_image_base64": ".vision_binding",
    "extract_image_base64_from_url": ".vision_binding",
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
