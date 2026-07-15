from __future__ import annotations

import asyncio
import functools
import sys
from types import SimpleNamespace
from unittest.mock import patch

from astrmai.infrastructure.runtime.handler_binding_compat import repair_plugin_handler_bindings


MODULE_PATH = "data.plugins.astrmai.main"


async def _raw_handler(self, event):
    return self, event


_raw_handler.__module__ = MODULE_PATH


class _Registry:
    def __init__(self, handlers):
        self.handlers = handlers

    def get_handlers_by_module_name(self, module_path):
        assert module_path == MODULE_PATH
        return self.handlers


def test_repairs_nested_disabled_to_enabled_handler_binding():
    stale_plugin = object()
    current_plugin = object()
    event = object()
    metadata = SimpleNamespace(
        handler=functools.partial(
            functools.partial(_raw_handler, stale_plugin),
            current_plugin,
        )
    )

    report = repair_plugin_handler_bindings(
        current_plugin,
        MODULE_PATH,
        registry=_Registry([metadata]),
    )

    assert report.rebound_count == 1
    assert report.nested_binding_count == 1
    assert asyncio.run(metadata.handler(event)) == (current_plugin, event)


def test_repair_is_idempotent_and_keeps_one_current_instance_binding():
    current_plugin = object()
    event = object()
    metadata = SimpleNamespace(handler=functools.partial(_raw_handler, current_plugin))
    registry = _Registry([metadata])

    first = repair_plugin_handler_bindings(current_plugin, MODULE_PATH, registry=registry)
    second = repair_plugin_handler_bindings(current_plugin, MODULE_PATH, registry=registry)

    assert first.nested_binding_count == 0
    assert second.nested_binding_count == 0
    assert asyncio.run(metadata.handler(event)) == (current_plugin, event)


def test_repair_ignores_handlers_owned_by_other_modules():
    async def foreign_handler(self, event):
        return self, event

    metadata = SimpleNamespace(handler=functools.partial(foreign_handler, object()))
    report = repair_plugin_handler_bindings(
        object(),
        MODULE_PATH,
        registry=_Registry([metadata]),
    )

    assert report.rebound_count == 0
    assert report.nested_binding_count == 0


def test_repair_degrades_when_astrbot_internal_registry_is_unavailable():
    with patch.dict(sys.modules, {"astrbot.core.star.star_handler": None}):
        report = repair_plugin_handler_bindings(object(), MODULE_PATH)

    assert report.rebound_count == 0
    assert report.nested_binding_count == 0
