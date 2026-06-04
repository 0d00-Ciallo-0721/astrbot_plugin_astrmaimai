from __future__ import annotations

from typing import TYPE_CHECKING

from ...conversation.ingress.external_result_bridge import bridge_external_plugin_result

if TYPE_CHECKING:
    from ...app.runtime_context import PluginRuntimeContext


async def sniff_external_plugin_results(runtime: PluginRuntimeContext, event) -> None:
    await bridge_external_plugin_result(runtime, event)
