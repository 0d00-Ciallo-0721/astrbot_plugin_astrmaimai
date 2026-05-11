from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from ...presentation.dto.message_scope import IngressDecision

Comp = import_module("astrbot.api.message_components")

if TYPE_CHECKING:
    from ...app.runtime_context import PluginRuntimeContext


async def handle_poke_if_needed(runtime: PluginRuntimeContext, event) -> IngressDecision:
    message_chain = getattr(event.message_obj, "message", []) if event.message_obj else []
    poke_component = getattr(Comp, "Poke", None)
    if poke_component and any(isinstance(component, poke_component) for component in message_chain):
        if runtime.sensors and runtime.attention_gate:
            await runtime.sensors.process_poke_event(event, runtime.context, runtime.attention_gate)
        return IngressDecision.stop("poke_event")
    return IngressDecision.allow()
