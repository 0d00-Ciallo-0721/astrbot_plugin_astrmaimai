from __future__ import annotations

from ...app.lifecycle import PluginLifecycleManager
from ...app.runtime_context import PluginRuntimeContext


async def on_program_start(runtime: PluginRuntimeContext, lifecycle_manager: PluginLifecycleManager) -> None:
    await lifecycle_manager.on_program_start()
