from __future__ import annotations

from ...app.runtime_context import PluginRuntimeContext
from ...conversation.execution.outbound_error_policy import intercept_outbound_error


async def intercept_and_notify_errors(runtime: PluginRuntimeContext, event) -> None:
    await intercept_outbound_error(runtime, event)
