from __future__ import annotations

from typing import TYPE_CHECKING

from ...conversation.execution.outbound_error_policy import intercept_outbound_error

if TYPE_CHECKING:
    from ...app.runtime_context import PluginRuntimeContext


async def intercept_and_notify_errors(runtime: PluginRuntimeContext, event) -> None:
    await intercept_outbound_error(runtime, event)
