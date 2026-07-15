from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HandlerBindingRepair:
    rebound_count: int = 0
    nested_binding_count: int = 0


def _unwrap_handler(handler: Any) -> tuple[Any, int]:
    bound_arg_count = 0
    while isinstance(handler, functools.partial):
        bound_arg_count += len(handler.args)
        handler = handler.func
    return handler, bound_arg_count


def repair_plugin_handler_bindings(
    plugin_instance: Any,
    module_path: str,
    *,
    registry: Any | None = None,
) -> HandlerBindingRepair:
    """Rebind this plugin's handlers after AstrBot's disabled-to-enabled reload."""
    if registry is None:
        try:
            from astrbot.core.star.star_handler import star_handlers_registry
        except ImportError:
            return HandlerBindingRepair()

        registry = star_handlers_registry

    rebound_count = 0
    nested_binding_count = 0
    for metadata in registry.get_handlers_by_module_name(module_path):
        raw_handler, bound_arg_count = _unwrap_handler(metadata.handler)
        if not callable(raw_handler):
            continue
        if getattr(raw_handler, "__module__", "") != module_path:
            continue
        metadata.handler = functools.partial(raw_handler, plugin_instance)
        rebound_count += 1
        if bound_arg_count > 1:
            nested_binding_count += 1

    return HandlerBindingRepair(
        rebound_count=rebound_count,
        nested_binding_count=nested_binding_count,
    )
