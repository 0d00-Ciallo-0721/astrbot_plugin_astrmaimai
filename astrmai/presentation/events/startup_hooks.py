from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

if TYPE_CHECKING:
    from ...app.lifecycle import PluginLifecycleManager
    from ...app.runtime_context import PluginRuntimeContext


async def on_program_start(
    runtime: PluginRuntimeContext,
    lifecycle_manager: PluginLifecycleManager,
    *,
    source: str = "",
) -> None:
    try:
        # G4/PL-10: source 决定终止闩锁能否复位（仅 plugin_initialize 可复活）
        await lifecycle_manager.on_program_start(source=source)
    except Exception:
        logger.exception("[AstrMai] lifecycle_manager.on_program_start failed")
        raise  # ponytail: R13 — propagate startup failure
