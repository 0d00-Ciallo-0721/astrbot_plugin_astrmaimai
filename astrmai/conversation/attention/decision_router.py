from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger


@dataclass(slots=True)
class AttentionDecision:
    action: str = "PASS"
    raw_action: str = "PASS"
    reason: str = ""


class AttentionDecisionRouter:
    """Runs System1 Judge as a lightweight attention gate."""

    def __init__(self, gate: Any):
        self.gate = gate

    @staticmethod
    def build_judge_window_message(events: list[Any]) -> str:
        lines: list[str] = []
        for event in events:
            text = str(event.get_extra("astrmai_rich_text", getattr(event, "message_str", "")) or "").strip()
            if not text:
                continue
            speaker = event.get_sender_name() if hasattr(event, "get_sender_name") else ""
            lines.append(f"{speaker or 'User'}: {text}")
        return "\n".join(lines).strip()

    async def evaluate(
        self,
        chat_id: str,
        focus_event: Any,
        focus_thread: Any,
        events: list[Any],
        *,
        is_strong_wakeup: bool,
    ) -> AttentionDecision:
        if is_strong_wakeup or not self.gate.judge or not hasattr(self.gate.judge, "evaluate"):
            return AttentionDecision(action="PASS", raw_action="PASS", reason="skip_judge")
        message = self.build_judge_window_message(events) or str(getattr(focus_event, "message_str", "") or "")
        try:
            result = await asyncio.wait_for(
                self.gate.judge.evaluate(
                    chat_id,
                    message,
                    False,
                    "",
                    len(events),
                    False,
                    focus_thread=focus_thread,
                    window_events=events,
                    focus_event=focus_event,
                ),
                timeout=2.0,
            )
        except TypeError:
            try:
                result = await asyncio.wait_for(
                    self.gate.judge.evaluate(chat_id, message, False, "", len(events), False),
                    timeout=2.0,
                )
            except Exception as exc:
                logger.debug(f"[AttentionGate] Judge degraded: {exc}")
                return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_degraded")
        except asyncio.TimeoutError:
            logger.debug(f"[AttentionGate] Judge timeout for {chat_id}; pass through")
            return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_timeout")
        except Exception as exc:
            logger.debug(f"[AttentionGate] Judge degraded: {exc}")
            return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_degraded")

        raw_action = str(getattr(result, "action", "PASS") or "PASS").upper()
        if raw_action in {"WAIT", "IGNORE"}:
            return AttentionDecision(action=raw_action, raw_action=raw_action, reason="judge_gate")
        return AttentionDecision(action="PASS", raw_action=raw_action, reason="judge_pass")


__all__ = ["AttentionDecision", "AttentionDecisionRouter"]
