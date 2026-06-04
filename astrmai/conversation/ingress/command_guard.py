from __future__ import annotations

from typing import TYPE_CHECKING

from ...presentation.dto.message_scope import IngressDecision

if TYPE_CHECKING:
    from ...app.runtime_facade_protocol import RuntimeFacadeProtocol


def check_framework_command(facade: RuntimeFacadeProtocol, message_text: str) -> IngressDecision:
    if message_text and facade.is_framework_command(message_text):
        return IngressDecision.stop("framework_command")
    return IngressDecision.allow()
