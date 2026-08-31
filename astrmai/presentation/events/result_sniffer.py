from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...conversation.ingress.external_result_bridge import snapshot_external_plugin_result
from ...conversation.ingress.external_result_dispatcher import ExternalResultDispatcher
from ...infrastructure.runtime.trace_runtime import debug_trace, ensure_external_result_id

if TYPE_CHECKING:
    from ...app.runtime_context import PluginRuntimeContext


async def sniff_external_plugin_results(runtime: PluginRuntimeContext, event) -> None:
    external_result_id = ensure_external_result_id(event)
    started = time.monotonic()
    debug_trace(event, "external_result.hook_enter", external_result_id=external_result_id)
    try:
        envelope = snapshot_external_plugin_result(runtime, event)
        if envelope is None:
            debug_trace(
                event,
                "external_result.hook_return",
                external_result_id=external_result_id,
                status="skipped",
                elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
            )
            return
        lifecycle = getattr(runtime, "lifecycle", None)
        dispatcher = getattr(lifecycle, "external_result_dispatcher", None)
        if dispatcher is None:
            dispatcher = ExternalResultDispatcher(runtime)
            if lifecycle is not None:
                lifecycle.external_result_dispatcher = dispatcher
        status = dispatcher.enqueue(envelope)
        debug_trace(
            event,
            "external_result.task_scheduled",
            external_result_id=external_result_id,
            status=status,
            queue_depth=dispatcher.describe_status().get("queue_depth", 0),
        )
    except Exception as exc:
        debug_trace(
            event,
            "external_result.hook_failed",
            external_result_id=external_result_id,
            elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
            error_type=type(exc).__name__,
        )
        raise
    debug_trace(
        event,
        "external_result.hook_return",
        external_result_id=external_result_id,
        status=status,
        elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
    )
