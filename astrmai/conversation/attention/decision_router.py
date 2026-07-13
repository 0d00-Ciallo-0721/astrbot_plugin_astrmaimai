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
        self._consecutive_timeouts = 0

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
        if (
            focus_event is not None
            and hasattr(focus_event, "get_extra")
            and hasattr(focus_event, "set_extra")
            and not bool(focus_event.get_extra("astrmai_is_proactive_event", False))
            and not bool(focus_event.get_extra("astrmai_primary_mood_applied", False))
            and hasattr(self.gate, "state_engine")
            and hasattr(self.gate.state_engine, "update_mood")
        ):
            focus_text = str(getattr(focus_event, "message_str", "") or "").strip()
            if focus_text:
                try:
                    mood_tag, mood_value = await self.gate.state_engine.update_mood(chat_id, focus_text)
                    focus_event.set_extra("astrmai_primary_mood_applied", True)
                    focus_event.set_extra("astrmai_primary_mood_tag", str(mood_tag or "neutral"))
                    focus_event.set_extra("astrmai_primary_mood_value", float(mood_value))
                    focus_event.set_extra("astrmai_primary_mood_source", "attention_pre_judge")
                except Exception as exc:
                    logger.debug(f"[AttentionGate] primary mood update degraded: {exc}")
        message = self.build_judge_window_message(events) or str(getattr(focus_event, "message_str", "") or "")
        # ponytail: judgment timeout configurable; default 3.0s (was 2.0s) for cold-start LLM resilience
        attention_config = getattr(getattr(self.gate, "config", None), "attention", None)
        judge_timeout = float(
            getattr(attention_config, "judge_timeout", getattr(getattr(self.gate, "config", None), "judge_timeout", 3.0))
            or 3.0
        )
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
                timeout=judge_timeout,
            )
        except TypeError:
            try:
                result = await asyncio.wait_for(
                    self.gate.judge.evaluate(chat_id, message, False, "", len(events), False),
                    timeout=judge_timeout,
                )
            except Exception as exc:
                logger.debug(f"[AttentionGate] Judge degraded: {exc}")
                return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_degraded")
        except asyncio.TimeoutError:
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts % 10 == 1:
                logger.warning(
                    f"[AttentionGate] Judge timeout x{self._consecutive_timeouts} for {chat_id}; passing through"
                )
            return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_timeout")
        except Exception as exc:
            logger.debug(f"[AttentionGate] Judge degraded: {exc}")
            return AttentionDecision(action="PASS", raw_action="PASS", reason="judge_degraded")

        raw_action = str(getattr(result, "action", "PASS") or "PASS").upper()
        self._consecutive_timeouts = 0  # 重置计数器
        if raw_action in {"WAIT", "IGNORE", "TOOL_CALL"}:
            return AttentionDecision(action=raw_action, raw_action=raw_action, reason="judge_gate")
        return AttentionDecision(action="PASS", raw_action=raw_action, reason="judge_pass")


__all__ = ["AttentionDecision", "AttentionDecisionRouter"]
