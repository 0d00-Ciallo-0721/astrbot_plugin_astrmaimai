"""Deterministic policy for handling a planner wait signal."""

from dataclasses import dataclass
from typing import Any

from .turn_outcome import get_turn_outcome


WAIT_FALLBACK_TEXT = "收到，我在看。"
_STRONG_WAKEUP_EXTRAS = (
    "astrmai_is_strong_wakeup",
    "astrmai_at_bot_wakeup",
    "astrmai_group_direct_wakeup",
    "astrmai_reply_wakeup",
)
_RISK_FLAGS_REQUIRING_ACK = {"serious", "danger", "urgent"}


@dataclass(frozen=True, slots=True)
class WaitSignalPolicy:
    """The allowed outcome for one ``[SYSTEM_WAIT_SIGNAL]`` observation."""

    allow_wait: bool
    require_visible_ack: bool
    allow_deferred_followup: bool
    reject_wait: bool
    reason: str
    strong_wakeup: bool = False
    wait_target_present: bool = False


def _extra(event: Any, key: str, default: Any = None) -> Any:
    if event is None or not hasattr(event, "get_extra"):
        return default
    try:
        return event.get_extra(key, default)
    except Exception:
        return default


def _wait_target_present(event: Any) -> bool:
    targets = _extra(event, "astrmai_wait_targets", ())
    if isinstance(targets, (str, bytes)):
        return bool(str(targets).strip())
    return bool(targets and any(str(target).strip() for target in targets))


def _strong_wakeup(event: Any) -> bool:
    if any(bool(_extra(event, key, False)) for key in _STRONG_WAKEUP_EXTRAS):
        return True
    context = _extra(event, "astrmai_turn_context")
    perception = getattr(context, "perception", None)
    return bool(getattr(perception, "is_strong_wakeup", False))


def _risk_requires_ack(event: Any) -> bool:
    values = _extra(event, "astrmai_risk_flags", ())
    context = _extra(event, "astrmai_turn_context")
    cognitive = getattr(context, "cognitive", None)
    values = list(values or ()) + list(getattr(cognitive, "risk_flags", ()) or ())
    return bool({str(value).strip().lower() for value in values} & _RISK_FLAGS_REQUIRING_ACK)


def _send_claim_consumed(event: Any) -> bool:
    if bool(_extra(event, "astrmai_reply_sent", False)):
        return True
    if _extra(event, "astrmai_committed_bot_turn", None) is not None:
        return True
    outcome = get_turn_outcome(event)
    if outcome is None:
        return False
    return bool(
        outcome.reply_sent
        or outcome.fallback_sent
        or outcome.reply_sent_segments
        or outcome.output_claim
    )


def decide_wait_signal_policy(event: Any) -> WaitSignalPolicy:
    """Decide whether a wait signal may remain invisible for this turn.

    The decision consumes only structured perception/turn fields.  It never
    infers wakeup intent from user-visible message text.
    """

    strong = _strong_wakeup(event) or _risk_requires_ack(event)
    wait_target = _wait_target_present(event)

    if _send_claim_consumed(event):
        return WaitSignalPolicy(
            allow_wait=False,
            require_visible_ack=False,
            allow_deferred_followup=False,
            reject_wait=True,
            reason="send_claim_consumed",
            strong_wakeup=strong,
            wait_target_present=wait_target,
        )

    # An explicit runtime wait target is a legitimate continuation contract,
    # including when the input itself was a strong wakeup.
    if wait_target:
        return WaitSignalPolicy(
            allow_wait=True,
            require_visible_ack=False,
            allow_deferred_followup=True,
            reject_wait=False,
            reason="wait_target_present",
            strong_wakeup=strong,
            wait_target_present=True,
        )

    if strong:
        return WaitSignalPolicy(
            allow_wait=False,
            require_visible_ack=True,
            allow_deferred_followup=False,
            reject_wait=True,
            reason="strong_wakeup_requires_visible_ack",
            strong_wakeup=True,
            wait_target_present=False,
        )

    return WaitSignalPolicy(
        allow_wait=True,
        require_visible_ack=False,
        allow_deferred_followup=False,
        reject_wait=False,
        reason="ordinary_wait",
        strong_wakeup=False,
        wait_target_present=False,
    )


__all__ = ["WAIT_FALLBACK_TEXT", "WaitSignalPolicy", "decide_wait_signal_policy"]
