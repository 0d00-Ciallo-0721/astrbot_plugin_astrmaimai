from __future__ import annotations

from ...app.runtime_context import PluginRuntimeContext
from ...conversation.ingress.external_result_bridge import bridge_external_plugin_result


async def sniff_external_plugin_results(runtime: PluginRuntimeContext, event) -> None:
    await bridge_external_plugin_result(runtime, event)
