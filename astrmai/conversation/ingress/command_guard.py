from __future__ import annotations

from ...presentation.dto.message_scope import IngressDecision


def check_framework_command(facade, message_text: str) -> IngressDecision:
    if message_text and facade.is_framework_command(message_text):
        return IngressDecision.stop("framework_command")
    return IngressDecision.allow()
