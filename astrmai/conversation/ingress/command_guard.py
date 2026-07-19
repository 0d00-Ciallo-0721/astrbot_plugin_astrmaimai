from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from ...app.runtime_facade_protocol import RuntimeFacadeProtocol


@dataclass(slots=True, frozen=True)
class CommandPassthroughDecision:
    should_passthrough: bool
    reason: str = ""
    command_name: str = ""
    owner_module: str = ""
    handler_name: str = ""
    detection_source: str = ""

    @classmethod
    def allow(cls) -> "CommandPassthroughDecision":
        return cls(should_passthrough=False)

    @classmethod
    def passthrough(
        cls,
        *,
        command_name: str = "",
        owner_module: str = "",
        handler_name: str = "",
        detection_source: str,
    ) -> "CommandPassthroughDecision":
        return cls(
            should_passthrough=True,
            reason="framework_command",
            command_name=command_name,
            owner_module=owner_module,
            handler_name=handler_name,
            detection_source=detection_source,
        )


def _event_extra(event: Any, key: str, default: Any) -> Any:
    getter = getattr(event, "get_extra", None)
    if not callable(getter):
        return default
    try:
        return getter(key, default)
    except TypeError:
        try:
            value = getter(key)
        except Exception:
            return default
        return default if value is None else value
    except Exception:
        return default


def _filter_command_name(filter_ref: Any) -> str:
    for attr in ("command_name", "group_name"):
        value = str(getattr(filter_ref, attr, "") or "").strip()
        if value:
            return value
    return ""


def _activated_command(event: Any) -> CommandPassthroughDecision | None:
    handlers = _event_extra(event, "activated_handlers", [])
    if not isinstance(handlers, (list, tuple)):
        return None
    for handler in handlers:
        if not bool(getattr(handler, "enabled", True)):
            continue
        for filter_ref in getattr(handler, "event_filters", ()) or ():
            command_name = _filter_command_name(filter_ref)
            if not command_name:
                continue
            return CommandPassthroughDecision.passthrough(
                command_name=command_name,
                owner_module=str(getattr(handler, "handler_module_path", "") or ""),
                handler_name=str(getattr(handler, "handler_name", "") or ""),
                detection_source="activated_handler",
            )
    return None


def _message_command_name(message_text: str) -> str:
    normalized = str(message_text or "").replace("\u200b", "").strip()
    if not normalized:
        return ""
    normalized = normalized.lstrip("/!！").strip()
    return normalized.split()[0] if normalized else ""


def check_framework_command(
    facade: RuntimeFacadeProtocol,
    message_text: str,
    *,
    event: Any | None = None,
) -> CommandPassthroughDecision:
    activated = _activated_command(event)
    if activated is not None:
        return activated
    if message_text and facade.is_framework_command(message_text):
        return CommandPassthroughDecision.passthrough(
            command_name=_message_command_name(message_text),
            detection_source="runtime_command_registry",
        )
    return CommandPassthroughDecision.allow()
