from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from astrbot.api import logger

from ..contracts.prompt_envelope import PromptEnvelope
from ..contracts.turn_context import ensure_turn_context
from ...infrastructure.runtime.trace_runtime import debug_trace


@dataclass(slots=True)
class ToolStateInputs:
    state: Any = None
    profile: Any = None
    relationship_vec: Any = None


@dataclass(slots=True)
class PreBudgetInputs:
    reflection_summary: str = ""
    cooldown_tags: list[str] = field(default_factory=list)
    conversation_summary: str = ""
    heartflow_context: str = ""
    timings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TimedSideInput:
    name: str
    value: Any
    elapsed_ms: float
    ok: bool = True
    error: str = ""
    skipped_reason: str = ""


class PlanningInputLoader:
    """Load read-only planning side inputs with budget-aware concurrency."""

    def __init__(self, planner):
        self.planner = planner

    async def _run_timed(
        self,
        event,
        name: str,
        loader: Callable[[], Any],
        default: Any,
    ) -> TimedSideInput:
        started = time.perf_counter()
        ok = True
        error = ""
        value = default
        try:
            loaded = loader()
            value = await loaded if inspect.isawaitable(loaded) else loaded
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            value = default
            logger.debug(f"[PlanningInputLoader] {name} degraded: {exc}")
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        debug_trace(event, "planning.side_input", name=name, elapsed_ms=elapsed_ms, ok=ok, error=error)
        return TimedSideInput(name=name, value=value, elapsed_ms=elapsed_ms, ok=ok, error=error)

    def _record_timing(self, event, item: TimedSideInput) -> dict[str, Any]:
        timing = {
            "name": item.name,
            "elapsed_ms": item.elapsed_ms,
            "ok": bool(item.ok),
        }
        if item.error:
            timing["error"] = item.error[:160]
        if item.skipped_reason:
            timing["skipped_reason"] = item.skipped_reason
        if hasattr(event, "get_extra") and hasattr(event, "set_extra"):
            timings = list(event.get_extra("astrmai_side_input_timings", []) or [])
            timings.append(timing)
            event.set_extra("astrmai_side_input_timings", timings)
        ensure_turn_context(event).side_inputs.timings.append(timing)
        return timing

    def _record_skip(self, event, name: str, reason: str) -> dict[str, Any]:
        item = TimedSideInput(
            name=name,
            value=None,
            elapsed_ms=0.0,
            ok=True,
            skipped_reason=reason,
        )
        debug_trace(event, "planning.side_input", name=name, elapsed_ms=0.0, ok=True, skipped_reason=reason)
        return self._record_timing(event, item)

    async def load_pre_budget(self, event, chat_id: str) -> PreBudgetInputs:
        actor_id = self._current_actor_id(event)
        agency_task = self._run_timed(
            event,
            "agency_snapshot",
            lambda: self._agency_snapshot(chat_id, actor_id),
            {},
        )
        continuity_task = self._run_timed(event, "continuity_snapshot", lambda: self._continuity_snapshot(chat_id), {})
        heartflow_task = self._run_timed(event, "heartflow_snapshot", lambda: self._heartflow_snapshot(chat_id), {})
        agency_item, continuity_item, heartflow_item = await asyncio.gather(
            agency_task,
            continuity_task,
            heartflow_task,
            return_exceptions=True,
        )
        timings = [self._record_timing(event, item) for item in (agency_item, continuity_item, heartflow_item)]
        agency = agency_item.value or {}
        continuity = continuity_item.value or {}
        heartflow = heartflow_item.value or {}
        self._apply_agency(event, agency)
        self._apply_continuity(event, continuity)
        self._apply_heartflow(event, heartflow)
        return PreBudgetInputs(
            reflection_summary=str(agency.get("reflection_summary", "") or ""),
            cooldown_tags=list(agency.get("cooldown_tags", []) or []),
            conversation_summary=str(continuity.get("summary", "") or ""),
            heartflow_context=str(heartflow.get("context_text", "") or ""),
            timings=timings,
        )

    async def load_memory_feedback(self, event, chat_id: str, think_level: int) -> str:
        if int(think_level or 0) < 2:
            self._record_skip(event, "memory_feedback", f"think_level_{int(think_level or 0)}")
            return ""
        item = await self._run_timed(
            event,
            "memory_feedback",
            lambda: self.planner._load_memory_feedback_summary(chat_id),
            "",
        )
        self._record_timing(event, item)
        summary = str(item.value or "")
        if summary and hasattr(event, "set_extra"):
            event.set_extra("astrmai_memory_feedback_summary", summary)
            ensure_turn_context(event).continuity.memory_feedback_summary = summary
        return summary

    async def load_prompt_inputs(
        self,
        event,
        chat_id: str,
        prompt_envelope: PromptEnvelope,
        window_lines: list[str],
        think_level: int,
        *,
        user_id=None,
    ) -> dict[str, Any]:
        level = int(think_level or 0)
        result: dict[str, Any] = {
            # Deprecated compatibility mirrors for legacy readers outside the main reply chain.
            "slang_context": "",
            "goal_text": "",
            "expression_habits": "",
            "jargon_explanation": "",
            "stable_expression_habits": "",
            "situational_style_cues": "",
            "stable_jargon_explanation": "",
            "planner_reasoning": "",
            "goals_context": "",
            "tool_state": ToolStateInputs(),
        }
        tasks = [
            self._run_timed(
                event,
                "expression_habits",
                lambda: self._load_expression_habits(event, chat_id, prompt_envelope, window_lines, level),
                "",
            ),
            self._run_timed(
                event,
                "jargon_explanation",
                lambda: self._load_jargon_explanation(event, chat_id, prompt_envelope, window_lines),
                "",
            ),
        ]
        if level >= 1:
            tasks.append(
                self._run_timed(
                    event,
                    "tool_state",
                    lambda: self._load_tool_state(chat_id, user_id),
                    ToolStateInputs(),
                )
            )
        if level >= 2:
            tasks.extend(
                [
                    self._run_timed(event, "slang_context", lambda: self._load_slang_context(chat_id), ""),
                ]
            )
        loaded_items = await asyncio.gather(*tasks, return_exceptions=True)
        for item in loaded_items:
            self._record_timing(event, item)
            result[item.name] = item.value

        result["stable_expression_habits"] = str(result.get("expression_habits", "") or "")
        situational_style_parts = [
            str(result.get("slang_context", "") or "").strip(),
        ]
        result["situational_style_cues"] = "\n".join(part for part in situational_style_parts if part).strip()
        result["stable_jargon_explanation"] = str(result.get("jargon_explanation", "") or "")

        if level >= 2:
            goal_item = await self._run_timed(
                event,
                "goal_update",
                lambda: self._load_goal_update(chat_id, prompt_envelope, window_lines),
                "",
            )
            self._record_timing(event, goal_item)
            goal_text = str(goal_item.value or "")
            result["goal_text"] = goal_text
            result["planner_reasoning"] = goal_text
            goals_context_item = await self._run_timed(
                event,
                "goals_context",
                lambda: self._load_goals_context(chat_id),
                "",
            )
            self._record_timing(event, goals_context_item)
            result["goals_context"] = str(goals_context_item.value or "")
        else:
            self._record_skip(event, "goal_update", f"think_level_{level}")
            self._record_skip(event, "slang_context", f"think_level_{level}")
        return result

    @staticmethod
    def _current_actor_id(event) -> str:
        turn_context = ensure_turn_context(event)
        actor_id = str(getattr(turn_context.perception, "sender_id", "") or "").strip()
        if actor_id:
            return actor_id
        if hasattr(event, "get_extra"):
            focus_context = event.get_extra("astrmai_focus_thread_context", None)
            actor_set = getattr(focus_context, "actor_set", None)
            actor_id = str(getattr(actor_set, "current_actor_id", "") or "").strip()
            actor_id = actor_id or str(getattr(focus_context, "focus_sender_id", "") or "").strip()
            if actor_id:
                return actor_id
        if hasattr(event, "get_sender_id"):
            try:
                return str(event.get_sender_id() or "").strip()
            except Exception:
                return ""
        return ""

    def _agency_snapshot(self, chat_id: str, actor_id: str = "") -> dict[str, Any]:
        runtime = getattr(self.planner, "agency_runtime", None)
        if not runtime:
            return {}
        return {
            "reflection_summary": str(runtime.summary(chat_id, actor_id=actor_id) or ""),
            "cooldown_tags": sorted(runtime.cooldown_tags(chat_id)),
        }

    def _continuity_snapshot(self, chat_id: str) -> dict[str, Any]:
        store = getattr(self.planner, "conversation_continuity", None)
        if not store:
            return {}
        return {
            "summary": store.summary(chat_id),
            "snapshot": store.snapshot(chat_id),
        }

    def _heartflow_snapshot(self, chat_id: str) -> dict[str, Any]:
        manager = getattr(self.planner, "heartflow_manager", None)
        if not manager or not hasattr(manager, "get_hidden_context"):
            return {}
        context_text = str(manager.get_hidden_context(chat_id) or "").strip()
        state = manager.get_state(chat_id) if hasattr(manager, "get_state") else None
        session = manager.get_session(chat_id) if hasattr(manager, "get_session") else None
        pulse = manager.get_latest_pulse(chat_id) if hasattr(manager, "get_latest_pulse") else None
        action = manager.get_latest_action_decision(chat_id) if hasattr(manager, "get_latest_action_decision") else None
        return {
            "context_text": context_text,
            "interest": float(getattr(state, "interest", 0.0) or 0.0) if state else 0.0,
            "talk_willingness": float(getattr(state, "talk_willingness", 0.0) or 0.0) if state else 0.0,
            "pulse_type": str(getattr(pulse, "pulse_type", "") or "") if pulse else "",
            "pulse_urgency": float(getattr(pulse, "urgency", 0.0) or 0.0) if pulse else 0.0,
            "action_type": str(getattr(action, "action_type", "") or "") if action else "",
            "talk_frequency_adjust": float(getattr(session, "talk_frequency_adjust", 0.0) or 0.0) if session else 0.0,
            "insert_pressure": float(getattr(session, "insert_pressure", 0.0) or 0.0) if session else 0.0,
            "reply_pressure": float(getattr(session, "reply_pressure", 0.0) or 0.0) if session else 0.0,
            "candidate_score": float(getattr(session, "visible_candidate_score", 0.0) or 0.0) if session else 0.0,
        }

    async def _load_expression_habits(
        self,
        event,
        chat_id: str,
        prompt_envelope: PromptEnvelope,
        window_lines: list[str],
        think_level: int,
    ) -> str:
        selector = getattr(self.planner, "expression_selector", None)
        if not selector:
            return ""
        recent_text = self.planner._planner_side_input_text(prompt_envelope, window_lines, recent_only=True)
        expression_think_level = 1 if think_level >= 1 and (len(recent_text) >= 40 or len(window_lines) >= 2) else 0
        if hasattr(selector, "select_with_trace"):
            text, selected = await selector.select_with_trace(
                chat_id=chat_id,
                context_text=recent_text,
                think_level=expression_think_level,
                shared_scope=chat_id,
            )
        else:
            text = await selector.select(
                chat_id=chat_id,
                context_text=recent_text,
                think_level=expression_think_level,
                shared_scope=chat_id,
            )
            selected = []
        decision = ensure_turn_context(event).expression_patterns
        decision.source = "canonical_expression_pattern"
        decision.selected_ids = [str(getattr(item, "id", "") or "") for item in selected if str(getattr(item, "id", "") or "").strip()]
        decision.injected = bool(text and selected)
        decision.skip_reason = "" if text else f"selector_empty_think_level_{expression_think_level}"
        decision.summary_preview = str(text or "")[:180]
        if hasattr(event, "set_extra"):
            event.set_extra(
                "astrmai_expression_pattern_trace",
                type(
                    "ExpressionPatternTrace",
                    (),
                    {
                        "selected_ids": list(decision.selected_ids),
                        "items": list(selected),
                        "source": decision.source,
                        "injected": decision.injected,
                        "skip_reason": decision.skip_reason,
                        "summary_preview": decision.summary_preview,
                    },
                )(),
            )
        return text

    async def _load_slang_context(self, chat_id: str) -> str:
        return ""

    @staticmethod
    def _render_jargon_lines(items: list[Any]) -> list[str]:
        lines: list[str] = []
        for item in items or []:
            if isinstance(item, dict):
                text = str(item.get("text") or item.get("content") or "").strip()
                meaning = str(item.get("meaning") or item.get("summary") or "").strip()
                scene = str(item.get("situation") or item.get("scene") or "").strip()
                selected_senses = list(item.get("selected_senses") or [])
            else:
                text = str(getattr(item, "content", "") or "").strip()
                metadata = dict(getattr(item, "metadata", {}) or {})
                meaning = str(metadata.get("meaning") or getattr(item, "summary", "") or "").strip()
                scene = str(metadata.get("scene") or "").strip()
                selected_senses = list(metadata.get("selected_senses") or [])
            if text and selected_senses:
                for sense in selected_senses:
                    sense_meaning = str(sense.get("meaning") or "").strip()
                    if not sense_meaning:
                        continue
                    line = f"{text} -> {sense_meaning}"
                    sense_scene = str(sense.get("scene") or "").strip()
                    if sense_scene:
                        line += f" (scene: {sense_scene})"
                    if line not in lines:
                        lines.append(line)
                continue
            if not text or not meaning:
                continue
            line = f"{text} -> {meaning}"
            if scene:
                line += f" (scene: {scene})"
            lines.append(line)
        return lines

    async def _load_jargon_explanation(self, event, chat_id: str, prompt_envelope: PromptEnvelope, window_lines: list[str]) -> str:
        memory_engine = getattr(self.planner, "memory_engine", None)
        retrieval_service = getattr(memory_engine, "retrieval_service", None) if memory_engine else None
        policy = getattr(retrieval_service, "jargon_policy", None) if retrieval_service else None
        if not policy:
            return ""
        query_parts = []
        if isinstance(prompt_envelope, PromptEnvelope):
            query_parts.extend(
                (
                    getattr(prompt_envelope, "raw_user_text", ""),
                    getattr(prompt_envelope, "focus_message_text", ""),
                )
            )
        query_parts.append(getattr(event, "message_str", ""))
        if not any(str(part or "").strip() for part in query_parts):
            query_parts.extend(window_lines[-1:])
        current_text = "\n".join(
            dict.fromkeys(str(part or "").strip() for part in query_parts if str(part or "").strip())
        )
        if not current_text:
            return ""
        trace: dict[str, Any] = {}
        items = await policy.search(
            query=current_text,
            session_id="",
            top_k=5,
            visibility_mode="auto",
            trace=trace,
            exact_only=True,
        )
        lines = self._render_jargon_lines(items)
        decision = ensure_turn_context(event).jargon
        decision.source = "canonical_global_jargon"
        decision.selected_ids = [
            str(getattr(item, "id", "") or "")
            for item in items
            if str(getattr(item, "id", "") or "").strip()
        ]
        decision.injected = bool(lines)
        decision.skip_reason = "" if lines else "no_exact_global_jargon_match"
        decision.summary_preview = "\n".join(lines)[:180]
        if hasattr(event, "set_extra"):
            event.set_extra(
                "astrmai_jargon_route_trace",
                {
                    "source": decision.source,
                    "selected_ids": list(decision.selected_ids),
                    "matched_terms": list(trace.get("matched_terms", []) or []),
                    "injected": decision.injected,
                    "skip_reason": decision.skip_reason,
                },
            )
        if not lines:
            return ""
        return "以下词义来自全局黑话词典，仅在当前消息实际出现时使用：\n" + "\n".join(lines)

    async def _load_goal_update(self, chat_id: str, prompt_envelope: PromptEnvelope, window_lines: list[str]) -> str:
        manager = getattr(self.planner, "goal_manager", None)
        if not manager or not hasattr(manager, "analyze_and_update"):
            return ""
        window_text = self.planner._planner_side_input_text(prompt_envelope, window_lines)
        if not window_text:
            return ""
        result = await manager.analyze_and_update(chat_id, window_text)
        logger.debug(f"[{chat_id}] planning goal update: {result}")
        return str(result or "")

    def _load_goals_context(self, chat_id: str) -> str:
        manager = getattr(self.planner, "goal_manager", None)
        if not manager or not hasattr(manager, "get_goals_context"):
            return ""
        return str(manager.get_goals_context(chat_id) or "")

    async def _load_tool_state(self, chat_id: str, user_id) -> ToolStateInputs:
        state_engine = getattr(self.planner, "state_engine", None)
        if not state_engine:
            return ToolStateInputs()

        async def _get_state():
            if hasattr(state_engine, "get_state"):
                return await state_engine.get_state(chat_id)
            return None

        async def _get_profile():
            if user_id and hasattr(state_engine, "get_user_profile"):
                return await state_engine.get_user_profile(str(user_id))
            return None

        state, profile = await asyncio.gather(_get_state(), _get_profile(), return_exceptions=True)
        relationship_vec = None
        if user_id and hasattr(state_engine, "relationship_engine"):
            relationship_engine = getattr(state_engine, "relationship_engine", None)
            if relationship_engine and hasattr(relationship_engine, "get_or_create"):
                relationship_vec = relationship_engine.get_or_create(str(user_id))
        return ToolStateInputs(state=state, profile=profile, relationship_vec=relationship_vec)

    def _apply_agency(self, event, agency: dict[str, Any]) -> None:
        turn_context = ensure_turn_context(event)
        reflection_summary = str(agency.get("reflection_summary", "") or "")
        cooldown_tags = list(agency.get("cooldown_tags", []) or [])
        if reflection_summary and hasattr(event, "set_extra"):
            event.set_extra("astrmai_agency_reflection_summary", reflection_summary)
        if cooldown_tags and hasattr(event, "set_extra"):
            event.set_extra("astrmai_agency_cooldown_tags", cooldown_tags)
        turn_context.continuity.agency_reflection_summary = reflection_summary
        turn_context.continuity.agency_cooldown_tags = list(cooldown_tags)

    def _apply_continuity(self, event, continuity: dict[str, Any]) -> None:
        summary = str(continuity.get("summary", "") or "")
        snapshot = continuity.get("snapshot", {}) or {}
        private_topic_context = ""
        private_topic_label = ""
        private_topic_inherited = False
        if hasattr(event, "get_extra"):
            private_topic_context = str(
                event.get_extra("astrmai_private_topic_context", "") or ""
            ).strip()
            private_topic_label = str(
                event.get_extra("astrmai_private_topic_label", "") or ""
            ).strip()
            private_topic_inherited = bool(
                event.get_extra("astrmai_private_topic_inherited", False)
            )
        if private_topic_context:
            summary = private_topic_context
            snapshot = dict(snapshot)
            snapshot["current_topic"] = private_topic_label or str(
                snapshot.get("current_topic", "") or ""
            )
            snapshot["continuity_weight"] = "strong" if private_topic_inherited else ""
        turn_context = ensure_turn_context(event)
        turn_context.continuity.current_topic = str(snapshot.get("current_topic", "") or "")
        turn_context.continuity.current_goal = str(snapshot.get("current_goal", "") or "")
        turn_context.continuity.goal_status = str(snapshot.get("goal_status", "") or "")
        turn_context.continuity.continuity_weight = str(snapshot.get("continuity_weight", "") or "")
        turn_context.continuity.turn_count = int(snapshot.get("turn_count", 0) or 0)
        if summary and hasattr(event, "set_extra"):
            event.set_extra("astrmai_conversation_continuity_summary", summary)
        turn_context.continuity.conversation_summary = summary

    def _apply_heartflow(self, event, heartflow: dict[str, Any]) -> None:
        context_text = str(heartflow.get("context_text", "") or "")
        if not context_text:
            return
        turn_context = ensure_turn_context(event)
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_heartflow_context", context_text)
            event.set_extra("astrmai_heartflow_interest", float(heartflow.get("interest", 0.0) or 0.0))
            event.set_extra(
                "astrmai_heartflow_talk_willingness",
                float(heartflow.get("talk_willingness", 0.0) or 0.0),
            )
            if heartflow.get("pulse_type"):
                event.set_extra("astrmai_heartflow_pulse", str(heartflow.get("pulse_type", "") or ""))
            if heartflow.get("action_type"):
                event.set_extra("astrmai_heartflow_action", str(heartflow.get("action_type", "") or ""))
        turn_context.continuity.heartflow_context = context_text
        turn_context.continuity.heartflow_interest = float(heartflow.get("interest", 0.0) or 0.0)
        turn_context.continuity.heartflow_talk_willingness = float(heartflow.get("talk_willingness", 0.0) or 0.0)
        turn_context.continuity.heartflow_pulse = str(heartflow.get("pulse_type", "") or "")
        turn_context.continuity.heartflow_action = str(heartflow.get("action_type", "") or "")
        turn_context.continuity.heartflow_urgency = float(heartflow.get("pulse_urgency", 0.0) or 0.0)
        turn_context.continuity.heartflow_talk_frequency_adjust = float(heartflow.get("talk_frequency_adjust", 0.0) or 0.0)
        turn_context.continuity.heartflow_insert_pressure = float(heartflow.get("insert_pressure", 0.0) or 0.0)
        turn_context.continuity.heartflow_reply_pressure = float(heartflow.get("reply_pressure", 0.0) or 0.0)
        turn_context.continuity.heartflow_candidate_score = float(heartflow.get("candidate_score", 0.0) or 0.0)


__all__ = [
    "PlanningInputLoader",
    "PreBudgetInputs",
    "TimedSideInput",
    "ToolStateInputs",
]
