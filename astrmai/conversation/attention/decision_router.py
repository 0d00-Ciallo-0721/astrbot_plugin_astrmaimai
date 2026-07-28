from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.turn_call_ledger import clamp_timeout_to_turn_budget
from ...shared.helpers.plugin_helpers import is_direct_call_event
from ..reply_shape_policy import resolve_reply_shape_policy


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
    def _consume_background_result(task: asyncio.Task) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _judge_supports_context_kwargs(judge_evaluate: Any) -> bool:
        try:
            parameters = inspect.signature(judge_evaluate).parameters.values()
        except (TypeError, ValueError):
            return True
        return any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
            parameter.name == "focus_thread" for parameter in parameters
        )

    @staticmethod
    def _is_private_event(event: Any) -> bool:
        try:
            return not bool(event.get_group_id())
        except Exception:
            return False

    @staticmethod
    def _is_direct_event(event: Any) -> bool:
        try:
            return bool(is_direct_call_event(event))
        except Exception:
            return False

    def _fallback_decision(
        self,
        focus_event: Any,
        *,
        interaction_kind: str,
        reason: str,
    ) -> AttentionDecision:
        """Choose a conservative fallback when System1 has no usable result.

        Private messages and explicit bot mentions are direct requests and must
        remain responsive. Unmentioned group traffic should not be promoted to
        a reply merely because the judge failed.
        """
        if interaction_kind == "peer_poke":
            action = "IGNORE"
            fallback_reason = "peer_poke"
        elif self._is_private_event(focus_event) or self._is_direct_event(focus_event):
            action = "PASS"
            fallback_reason = "direct"
        else:
            action = "WAIT"
            fallback_reason = "group_unmentioned"
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_fallback_action", action.lower())
            focus_event.set_extra("astrmai_judge_fallback_reason", fallback_reason)
        return AttentionDecision(
            action=action,
            raw_action=action,
            reason=(
                f"peer_poke_{reason}"
                if interaction_kind == "peer_poke"
                else f"{reason}:{fallback_reason}"
            ),
        )

    async def _run_judge_with_hard_deadline(
        self,
        coroutine: Any,
        *,
        timeout_sec: float,
    ) -> Any:
        task = asyncio.create_task(coroutine)
        done, _ = await asyncio.wait({task}, timeout=timeout_sec)
        if task in done:
            return task.result()
        task.cancel()
        task.add_done_callback(self._consume_background_result)
        raise asyncio.TimeoutError

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
        interaction_kind = ""
        if focus_event is not None and hasattr(focus_event, "get_extra"):
            interaction_kind = str(focus_event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        if (
            focus_event is not None
            and hasattr(focus_event, "get_extra")
            and hasattr(focus_event, "set_extra")
            and not bool(focus_event.get_extra("astrmai_is_proactive_event", False))
            and not bool(focus_event.get_extra("astrmai_primary_mood_applied", False))
            # OPT-08/RT-03: mood 后置开启时，判决前不再做情绪 LLM（由 gate 在
            # judge_action 确定后 fire-and-forget，WAIT/IGNORE 不付调用）
            and not (
                callable(getattr(self.gate, "_mood_post_judge_enabled", None))
                and self.gate._mood_post_judge_enabled()
            )
            and hasattr(self.gate, "state_engine")
            and hasattr(self.gate.state_engine, "update_mood")
        ):
            focus_text = str(getattr(focus_event, "message_str", "") or "").strip()
            reply_config = getattr(getattr(self.gate, "config", None), "reply", None)
            shape_policy = resolve_reply_shape_policy(focus_event, focus_text, reply_config)
            skip_micro_mood = str(shape_policy.get("mode", "")) == "micro"
            if skip_micro_mood:
                focus_event.set_extra("astrmai_primary_mood_skipped_reason", "micro_utterance")
            elif focus_text:
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
        judge_timeout = clamp_timeout_to_turn_budget(
            focus_event,
            judge_timeout,
            reserve_for_reply=True,
        )
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_timeout_sec", round(judge_timeout, 3))
            focus_event.set_extra("astrmai_judge_timeout", False)
        if judge_timeout <= 0.0:
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "budget_exhausted")
                focus_event.set_extra("astrmai_judge_failure_type", "budget_exhausted")
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_budget_exhausted",
            )

        judge_evaluate = self.gate.judge.evaluate
        if self._judge_supports_context_kwargs(judge_evaluate):
            judge_coroutine = judge_evaluate(
                chat_id,
                message,
                False,
                "",
                len(events),
                False,
                focus_thread=focus_thread,
                window_events=events,
                focus_event=focus_event,
            )
        else:
            judge_coroutine = judge_evaluate(chat_id, message, False, "", len(events), False)
        try:
            result = await self._run_judge_with_hard_deadline(
                judge_coroutine,
                timeout_sec=judge_timeout,
            )
        except asyncio.TimeoutError:
            self._consecutive_timeouts += 1
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "timeout")
                focus_event.set_extra("astrmai_judge_timeout", True)
                focus_event.set_extra("astrmai_judge_failure_type", "timeout")
            if self._consecutive_timeouts % 10 == 1:
                logger.warning(
                    f"[AttentionGate] Judge timeout x{self._consecutive_timeouts} for {chat_id}; passing through"
                )
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_timeout",
            )
        except Exception as exc:
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "degraded")
                focus_event.set_extra("astrmai_judge_failure_type", type(exc).__name__)
            logger.debug(f"[AttentionGate] Judge degraded: {exc}")
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_degraded",
            )

        if result is None or not str(getattr(result, "action", "") or "").strip():
            if focus_event is not None and hasattr(focus_event, "set_extra"):
                focus_event.set_extra("astrmai_judge_outcome", "empty_response")
                focus_event.set_extra("astrmai_judge_failure_type", "empty_response")
            return self._fallback_decision(
                focus_event,
                interaction_kind=interaction_kind,
                reason="judge_empty_response",
            )
        raw_action = str(getattr(result, "action", "PASS") or "PASS").upper()
        if focus_event is not None and hasattr(focus_event, "set_extra"):
            focus_event.set_extra("astrmai_judge_outcome", raw_action.lower())
        self._consecutive_timeouts = 0  # 重置计数器
        if raw_action in {"WAIT", "IGNORE", "TOOL_CALL"}:
            return AttentionDecision(action=raw_action, raw_action=raw_action, reason="judge_gate")
        return AttentionDecision(action="PASS", raw_action=raw_action, reason="judge_pass")


__all__ = ["AttentionDecision", "AttentionDecisionRouter"]
