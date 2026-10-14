from __future__ import annotations

import asyncio
import re
from typing import Any


class DeliveryFailure(RuntimeError):
    """A failure raised after model execution reached the host send boundary."""

    def __init__(self, kind: str, detail: str = "") -> None:
        normalized = str(kind or "uncertain").strip().lower()
        if normalized not in {"rejected", "uncertain"}:
            normalized = "uncertain"
        self.kind = normalized
        self.detail = str(detail or "")
        super().__init__(self.detail or f"host delivery {self.kind}")


_MEMBERSHIP_REJECTION = re.compile(
    r"(?:removed from (?:the )?group|kicked|not in (?:the )?group|被移出(?:该)?群|不在群|已被踢|群不存在|group.*(?:not found|left))",
    re.IGNORECASE,
)


def classify_delivery_failure(error: Any) -> DeliveryFailure:
    """Normalize only errors observed in the host send phase.

    A membership rejection is deliberately narrower than a generic non-zero
    retcode: transport errors and unknown platform responses remain uncertain
    and must not retire a group or trigger a second visible send.
    """
    detail = str(error or "")
    lowered = detail.lower()
    rejected = bool(_MEMBERSHIP_REJECTION.search(detail)) or (
        "retcode=1200" in lowered and _MEMBERSHIP_REJECTION.search(detail) is not None
    )
    failure = DeliveryFailure("rejected" if rejected else "uncertain", detail)
    try:
        setattr(error, "_astrmai_delivery_failure_kind", failure.kind)
        setattr(error, "_astrmai_delivery_failure_detail", failure.detail)
    except Exception:
        pass
    return failure


def delivery_failure_from_exception(error: BaseException) -> DeliveryFailure | None:
    if isinstance(error, DeliveryFailure):
        return error
    kind = str(getattr(error, "_astrmai_delivery_failure_kind", "") or "").strip()
    if kind:
        return DeliveryFailure(
            kind,
            str(getattr(error, "_astrmai_delivery_failure_detail", "") or error),
        )
    return None


async def send_host_message(context: Any, chat_id: str, chain: Any) -> Any:
    """Call AstrBot's host sender while preserving its receipt contract."""
    try:
        return await context.send_message(chat_id, chain)
    except asyncio.CancelledError:
        raise
    except DeliveryFailure:
        raise
    except Exception as exc:
        raise classify_delivery_failure(exc) from exc


__all__ = [
    "DeliveryFailure",
    "classify_delivery_failure",
    "delivery_failure_from_exception",
    "send_host_message",
]
