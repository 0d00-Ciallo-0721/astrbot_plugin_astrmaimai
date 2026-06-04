from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from astrbot.api import logger

from ...memory.contracts.memory_query import MemoryQuery
from ..dispatcher import ProactiveMessageIntent
from ..rhythm import evaluate_proactive_rhythm
from .feedback_bridge import HeartflowFeedbackBridge
from .models import HeartflowActionDecision, HeartflowChatState, HeartflowImpulseDecision, HeartflowPulse, HeartflowSessionState


class HeartflowManager:
    ACTIVE_CHAT_TTL_SECONDS = 30 * 60
    ACTIVE_SESSION_TTL_SECONDS = 5 * 60
    MAX_CHATS_PER_TICK = 12
    PULSE_HISTORY_LIMIT = 32
    IMPULSE_DECISION_HISTORY_LIMIT = 64
    ACTION_DECISION_HISTORY_LIMIT = 64
    VISIBLE_CANDIDATE_COOLDOWN_SECONDS = 15 * 60
    VISIBLE_CANDIDATE_MIN_SILENCE_PRESSURE = 0.80
    VISIBLE_CANDIDATE_MIN_TALK_WILLINGNESS = 0.42
    VISIBLE_CANDIDATE_MIN_URGENCY = 0.55
    VISIBLE_CANDIDATE_SCORE_THRESHOLD = 0.72
    PREPARE_CANDIDATE_SCORE_THRESHOLD = 0.55

    def __init__(
        self,
        *,
        runtime_coordinator: Any = None,
        state_engine: Any = None,
        memory_engine: Any = None,
        semaphore: asyncio.Semaphore | None = None,
        dispatcher: Any = None,
        config: Any = None,
    ):
        self.runtime_coordinator = runtime_coordinator
        self.state_engine = state_engine
        self.memory_engine = memory_engine
        self._semaphore = semaphore
        self.dispatcher = dispatcher
        self.config = config
        self.feedback_bridge = HeartflowFeedbackBridge(memory_engine)
        self._states: dict[str, HeartflowChatState] = {}
        self._sessions: dict[str, HeartflowSessionState] = {}
        self._pulses_by_chat: dict[str, list[HeartflowPulse]] = {}
        self._impulse_decisions_by_chat: dict[str, list[HeartflowImpulseDecision]] = {}
        self._action_decisions_by_chat: dict[str, list[HeartflowActionDecision]] = {}
        self._last_tick_time = 0.0

    @staticmethod
    def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
        return max(low, min(high, value))

    @staticmethod
    def _float_attr(obj: Any, key: str, default: float) -> float:
        try:
            return float(getattr(obj, key, default) if obj is not None else default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _int_snapshot(snapshot: dict, key: str, default: int = 0) -> int:
        try:
            return int(snapshot.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float_snapshot(snapshot: dict, key: str, default: float = 0.0) -> float:
        try:
            return float(snapshot.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _recent_bot_reply_penalty(self, session: HeartflowSessionState, *, now: float) -> float:
        penalty = min(int(session.recent_bot_reply_count or 0), 5) * 0.05
        if session.last_bot_reply_ts > 0 and now - float(session.last_bot_reply_ts or 0.0) <= 120:
            penalty += 0.20
        return self._clamp(penalty, 0.0, 0.45)

    def _direct_relevance(self, session: HeartflowSessionState, *, now: float) -> float:
        direct_count_score = min(int(session.recent_direct_count or 0), 4) * 0.18
        direct_recency = 0.0
        if session.last_user_direct_ts > 0:
            direct_age = now - float(session.last_user_direct_ts or 0.0)
            if direct_age <= 120:
                direct_recency = 0.32
            elif direct_age <= 300:
                direct_recency = 0.16
        return self._clamp(direct_count_score + direct_recency)

    @staticmethod
    def _snapshot_tags(snapshot: dict) -> list[str]:
        tags = snapshot.get("cooldown_tags", []) or []
        return [str(tag or "").strip() for tag in tags if str(tag or "").strip()]

    def _conflict_penalty(self, snapshot: dict, state: HeartflowChatState | None = None) -> float:
        tags = set(self._snapshot_tags(snapshot))
        if state is not None:
            tags.update(str(tag or "").strip() for tag in (state.cooldown_tags or []) if str(tag or "").strip())
        return 0.45 if tags & {"boundary", "pushback", "sharp_reply", "conflict"} else 0.0

    def _refresh_session_rhythm(
        self,
        session: HeartflowSessionState,
        snapshot: dict,
        *,
        now: float,
        fatigue: float = 0.4,
        mood_bias: float = 0.0,
    ) -> None:
        age_seconds = max(0.0, now - float(session.last_activity_ts or 0.0)) if session.last_activity_ts > 0 else self.ACTIVE_CHAT_TTL_SECONDS
        silence_pressure = self._clamp((age_seconds - 300.0) / 1500.0)
        recent_bot_reply_penalty = self._recent_bot_reply_penalty(session, now=now)
        direct_relevance = self._direct_relevance(session, now=now)
        conflict_penalty = self._conflict_penalty(snapshot)
        insert_pressure = self._clamp(
            int(session.recent_bot_reply_count or 0) * 0.18
            + recent_bot_reply_penalty
            + float(session.consecutive_prepare_count or 0) * 0.06
            + conflict_penalty * 0.35
            - max(0.0, age_seconds - 300.0) / 1500.0 * 0.18
        )
        reply_pressure = self._clamp(
            float(session.topic_heat or 0.0) * 0.34
            + direct_relevance * 0.26
            + silence_pressure * 0.24
            + max(mood_bias, 0.0) * 0.08
            - insert_pressure * 0.18
            - fatigue * 0.14
        )
        components = {
            "base": 1.0,
            "direct_wakeup_boost": direct_relevance * 0.65,
            "topic_heat_boost": float(session.topic_heat or 0.0) * 1.35,
            "silence_pressure_boost": silence_pressure * 0.45,
            "recent_bot_reply_penalty": recent_bot_reply_penalty,
            "insert_pressure_penalty": insert_pressure * 0.95,
            "fatigue_penalty": fatigue * 0.75,
            "conflict_penalty": conflict_penalty * 0.75,
        }
        talk_frequency_adjust = self._clamp(
            components["base"]
            + components["direct_wakeup_boost"]
            + components["topic_heat_boost"]
            + components["silence_pressure_boost"]
            - components["recent_bot_reply_penalty"]
            - components["insert_pressure_penalty"]
            - components["fatigue_penalty"]
            - components["conflict_penalty"]
            - min(float(session.consecutive_no_reply_count or 0), 5.0) * 0.10,
            0.1,
            5.0,
        )
        session.direct_relevance = direct_relevance
        session.insert_pressure = insert_pressure
        session.reply_pressure = reply_pressure
        session.talk_frequency_adjust = talk_frequency_adjust
        session.frequency_components = {key: round(float(value or 0.0), 4) for key, value in components.items()}

    def _topic_heat(self, snapshot: dict, *, now: float) -> float:
        latest_ts = self._float_snapshot(snapshot, "latest_activity_ts")
        age_seconds = max(0.0, now - latest_ts) if latest_ts > 0 else self.ACTIVE_CHAT_TTL_SECONDS
        recent_count = self._int_snapshot(snapshot, "recent_activity_count")
        recent_count_60s = self._int_snapshot(snapshot, "recent_activity_count_60s")
        preview = str(snapshot.get("latest_activity_preview", "") or "")
        question_boost = 0.10 if "?" in preview or "？" in preview else 0.0
        return self._clamp(0.10 + min(recent_count, 6) * 0.10 + min(recent_count_60s, 4) * 0.08 + question_boost - age_seconds / 1800.0 * 0.20)

    def _materialize_session(
        self,
        chat_id: str,
        snapshot: dict,
        *,
        now: float,
        persist: bool,
    ) -> HeartflowSessionState | None:
        latest_ts = self._float_snapshot(snapshot, "latest_activity_ts")
        if latest_ts <= 0:
            return None
        age_seconds = max(0.0, now - latest_ts)
        if age_seconds > self.ACTIVE_CHAT_TTL_SECONDS:
            if persist:
                self._sessions.pop(chat_id, None)
            return None

        existing = self._sessions.get(chat_id)
        previous_topic_heat = float(getattr(existing, "topic_heat", 0.0) or 0.0) if existing else 0.0
        previous_tick_ts = float(getattr(existing, "last_tick_ts", 0.0) or 0.0) if existing else 0.0
        previous_activity_ts = float(getattr(existing, "last_activity_ts", 0.0) or 0.0) if existing else 0.0
        start_new = existing is None or (
            latest_ts > float(getattr(existing, "last_activity_ts", 0.0) or 0.0)
            and latest_ts - float(getattr(existing, "last_activity_ts", 0.0) or 0.0) > self.ACTIVE_SESSION_TTL_SECONDS
        )
        if start_new:
            session = HeartflowSessionState(
                chat_id=chat_id,
                started_at=now,
                last_activity_ts=latest_ts,
                last_tick_ts=0.0,
                expires_at=latest_ts + self.ACTIVE_SESSION_TTL_SECONDS,
            )
        elif existing is not None:
            session = replace(existing, frequency_components=dict(getattr(existing, "frequency_components", {}) or {}))
        else:
            session = HeartflowSessionState(
                chat_id=chat_id,
                started_at=now,
                last_activity_ts=latest_ts,
                last_tick_ts=0.0,
                expires_at=latest_ts + self.ACTIVE_SESSION_TTL_SECONDS,
            )

        recent_count = self._int_snapshot(snapshot, "recent_activity_count")
        recent_direct_count = self._int_snapshot(snapshot, "recent_direct_count")
        recent_bot_reply_count = self._int_snapshot(snapshot, "recent_bot_reply_count")
        last_bot_reply_ts = self._float_snapshot(snapshot, "last_bot_reply_ts")
        last_user_direct_ts = self._float_snapshot(snapshot, "last_user_direct_ts")
        topic_heat = self._topic_heat(snapshot, now=now)
        if previous_topic_heat > 0 and previous_tick_ts > 0 and latest_ts <= previous_activity_ts:
            elapsed = max(0.0, now - previous_tick_ts)
            retained_heat = previous_topic_heat * max(0.25, 1.0 - elapsed / 2400.0)
            topic_heat = self._clamp(max(topic_heat, retained_heat))
        low_cost_retained = age_seconds > self.ACTIVE_SESSION_TTL_SECONDS

        session.last_activity_ts = latest_ts
        session.last_tick_ts = now
        session.expires_at = latest_ts + self.ACTIVE_SESSION_TTL_SECONDS
        session.tick_count += 1
        session.recent_message_count = recent_count
        session.recent_direct_count = recent_direct_count
        session.recent_bot_reply_count = recent_bot_reply_count
        session.last_bot_reply_ts = last_bot_reply_ts
        session.last_user_direct_ts = last_user_direct_ts
        session.topic_heat = topic_heat
        session.low_cost_retained = low_cost_retained
        self._refresh_session_rhythm(session, snapshot, now=now)
        if persist:
            self._sessions[chat_id] = session
        return session

    def _get_or_refresh_session(self, chat_id: str, snapshot: dict, *, now: float) -> HeartflowSessionState | None:
        return self._materialize_session(chat_id, snapshot, now=now, persist=True)

    def _preview_session(self, chat_id: str, snapshot: dict, *, now: float) -> HeartflowSessionState | None:
        return self._materialize_session(chat_id, snapshot, now=now, persist=False)

    def _cleanup_sessions(self, *, now: float) -> None:
        expired = [
            chat_id
            for chat_id, session in self._sessions.items()
            if float(session.last_activity_ts or 0.0) <= 0 or now - float(session.last_activity_ts or 0.0) > self.ACTIVE_CHAT_TTL_SECONDS
        ]
        for chat_id in expired:
            self._sessions.pop(chat_id, None)

    async def _load_state_values(self, chat_id: str) -> tuple[float, float]:
        if not self.state_engine or not hasattr(self.state_engine, "get_state"):
            return 0.6, 0.0
        try:
            state = await self.state_engine.get_state(chat_id)
        except Exception as exc:
            logger.debug(f"[Heartflow] state lookup degraded for {chat_id}: {exc}")
            return 0.6, 0.0
        return self._clamp(self._float_attr(state, "energy", 0.6)), self._clamp(self._float_attr(state, "mood", 0.0), -1.0, 1.0)

    async def _compute_chat_cycle(
        self,
        chat_id: str,
        *,
        snapshot: dict | None,
        now: float,
        persist_session: bool,
    ) -> tuple[HeartflowSessionState | None, HeartflowChatState | None, HeartflowPulse | None, HeartflowActionDecision | None, HeartflowImpulseDecision | None]:
        snapshot = dict(snapshot or {})
        if not snapshot:
            return None, None, None, None, None
        session = self._get_or_refresh_session(chat_id, snapshot, now=now) if persist_session else self._preview_session(chat_id, snapshot, now=now)
        if not session:
            return None, None, None, None, None
        state = await self._build_chat_state(chat_id, snapshot, now=now, session=session)
        pulse = self._build_pulse(state, now=now, session=session)
        action = self._build_action_decision(session, state, pulse, snapshot, now=now)
        decision = self._build_impulse_decision(session, state, pulse, snapshot, now=now)
        decision = self._apply_action_to_impulse_decision(action, decision)
        return session, state, pulse, action, decision

    @staticmethod
    def _build_preview_payload(
        chat_id: str,
        pulse: HeartflowPulse | None,
        action: HeartflowActionDecision | None,
        decision: HeartflowImpulseDecision | None,
    ) -> dict[str, Any]:
        action_type = str(getattr(action, "action_type", "") or "")
        blocked_reason = str(getattr(action, "blocked_reason", "") or getattr(decision, "blocked_reason", "") or "")
        visible_candidate_allowed = bool(getattr(decision, "visible_candidate_allowed", False))
        should_dispatch_candidate = bool(getattr(action, "should_dispatch_candidate", False))
        eligible = bool(action_type and action_type not in {"observe", "idle", "none"} and (should_dispatch_candidate or visible_candidate_allowed))
        return {
            "chat_id": chat_id,
            "eligible": eligible,
            "action_type": action_type,
            "blocked_reason": blocked_reason,
            "visible_candidate_allowed": visible_candidate_allowed,
            "should_dispatch_candidate": should_dispatch_candidate,
            "pulse_type": str(getattr(pulse, "pulse_type", "") or ""),
            "impulse_hidden_only": bool(getattr(decision, "hidden_only", True)),
        }

    async def preview_chat(self, chat_id: str, snapshot: dict | None = None, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if snapshot is None and self.runtime_coordinator and hasattr(self.runtime_coordinator, "get_activity_snapshot"):
            try:
                snapshot = await self.runtime_coordinator.get_activity_snapshot(chat_id)
            except Exception as exc:
                logger.debug(f"[Heartflow] preview snapshot degraded for {chat_id}: {exc}")
                snapshot = {}
        _, _, pulse, action, decision = await self._compute_chat_cycle(chat_id, snapshot=snapshot, now=now, persist_session=False)
        return self._build_preview_payload(chat_id, pulse, action, decision)

    async def tick_chat(self, chat_id: str, snapshot: dict | None = None, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        if snapshot is None and self.runtime_coordinator and hasattr(self.runtime_coordinator, "get_activity_snapshot"):
            snapshot = await self.runtime_coordinator.get_activity_snapshot(chat_id)
        session, state, pulse, action, decision = await self._compute_chat_cycle(chat_id, snapshot=snapshot, now=now, persist_session=True)
        if not all((session, state, pulse, action, decision)):
            return {"chat_id": chat_id, "eligible": False, "action_type": "", "blocked_reason": "snapshot_unavailable"}
        self._states[chat_id] = state
        self._remember_pulse(pulse)
        self._remember_action_decision(action)
        if action.should_dispatch_candidate:
            decision = await self._maybe_dispatch_visible_candidate(state, pulse, decision)
        self._remember_impulse_decision(decision)
        await self.feedback_bridge.maybe_flush(self._pulses_by_chat, chat_id)
        payload = self._build_preview_payload(chat_id, pulse, action, decision)
        payload["performed"] = True
        payload["synthetic_event_queued"] = bool(getattr(decision, "synthetic_event_queued", False))
        payload["visible_dispatch_performed"] = bool(getattr(decision, "synthetic_event_queued", False))
        return payload

    async def tick(self, *, now: float | None = None) -> None:
        if not self.runtime_coordinator or not hasattr(self.runtime_coordinator, "list_active_chats"):
            return
        if self._semaphore and self._semaphore.locked():
            return
        if self._semaphore:
            async with self._semaphore:
                await self._tick_inner(now=now)
            return
        await self._tick_inner(now=now)

    async def _tick_inner(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._last_tick_time = now
        self._cleanup_sessions(now=now)
        try:
            chat_ids = await self.runtime_coordinator.list_active_chats(self.ACTIVE_CHAT_TTL_SECONDS)
        except Exception as exc:
            logger.debug(f"[Heartflow] active chat scan degraded: {exc}")
            return

        for chat_id in chat_ids[: self.MAX_CHATS_PER_TICK]:
            try:
                snapshot = await self.runtime_coordinator.get_activity_snapshot(chat_id)
                if not snapshot:
                    continue
                await self.tick_chat(chat_id, snapshot=snapshot, now=now)
            except Exception as exc:
                logger.debug(f"[Heartflow] tick degraded for {chat_id}: {exc}")

    async def _build_chat_state(
        self,
        chat_id: str,
        snapshot: dict,
        *,
        now: float,
        session: HeartflowSessionState | None = None,
    ) -> HeartflowChatState:
        latest_ts = float(snapshot.get("latest_activity_ts", 0.0) or 0.0)
        age_seconds = max(0.0, now - latest_ts) if latest_ts > 0 else self.ACTIVE_CHAT_TTL_SECONDS
        recent_count = int(snapshot.get("recent_activity_count", 0) or 0)
        recent_count_60s = int(snapshot.get("recent_activity_count_60s", 0) or 0)
        preview = str(snapshot.get("latest_activity_preview", "") or "").strip()
        energy, mood = await self._load_state_values(chat_id)

        fatigue = self._clamp(1.0 - energy)
        if session is not None:
            self._refresh_session_rhythm(session, snapshot, now=now, fatigue=fatigue, mood_bias=mood)
        interest = self._clamp(
            0.12
            + min(recent_count, 6) * 0.10
            + (0.22 if age_seconds <= 90 else 0.0)
            + float(getattr(session, "topic_heat", 0.0) if session else 0.0) * 0.50
            + (0.08 if "?" in preview or "？" in preview else 0.0)
        )
        engagement = self._clamp(0.08 + min(recent_count_60s, 4) * 0.22 + min(recent_count, 6) * 0.04)
        silence_pressure = self._clamp((age_seconds - 300.0) / 1500.0)
        talk_willingness = self._clamp(
            0.12
            + interest * 0.45
            + energy * 0.28
            + max(mood, 0.0) * 0.10
            - fatigue * 0.35
            - silence_pressure * 0.12
            + (float(getattr(session, "talk_frequency_adjust", 1.0) or 1.0) - 1.0) * 0.08
            - float(getattr(session, "insert_pressure", 0.0) or 0.0) * 0.10
        )
        impulse = self._resolve_impulse(
            interest=interest,
            talk_willingness=talk_willingness,
            silence_pressure=silence_pressure,
            fatigue=fatigue,
        )
        return HeartflowChatState(
            chat_id=chat_id,
            last_tick_ts=now,
            last_activity_ts=latest_ts,
            interest=interest,
            engagement=engagement,
            talk_willingness=talk_willingness,
            silence_pressure=silence_pressure,
            fatigue=fatigue,
            mood_bias=mood,
            current_focus=preview[:160],
            recent_impulse=impulse,
            cooldown_tags=self._recent_tags(chat_id),
        )

    @staticmethod
    def _resolve_impulse(*, interest: float, talk_willingness: float, silence_pressure: float, fatigue: float) -> str:
        if fatigue > 0.75 or talk_willingness < 0.25:
            return "observe"
        if silence_pressure > 0.80 and interest > 0.35:
            return "proactive_hint"
        if interest > 0.70:
            return "join"
        if interest > 0.52:
            return "prepare_reply"
        return "observe"

    def _build_pulse(
        self,
        state: HeartflowChatState,
        *,
        now: float,
        session: HeartflowSessionState | None = None,
    ) -> HeartflowPulse:
        pulse_type = "observe"
        social_intent = "observe"
        action_tier = "none"
        guidance = "Observe quietly unless the current message directly needs a response."
        tags: list[str] = ["observe"]

        topic_heat = float(getattr(session, "topic_heat", state.interest) if session else state.interest)
        if state.fatigue > 0.75 or state.talk_willingness < 0.25:
            pulse_type = "cool_down"
            guidance = "Prefer short or no response; avoid heavy tools while fatigue is high."
            tags = ["low_talk", "fatigue"]
        elif state.silence_pressure > 0.80 and state.interest > 0.35:
            pulse_type = "proactive_hint"
            social_intent = "join"
            action_tier = "chat"
            guidance = "There may be room to rejoin later, but do not force old topics."
            tags = ["proactive_hint"]
        elif state.interest > 0.70 or topic_heat > 0.72:
            pulse_type = "prepare_reply"
            social_intent = "inquire" if "?" in state.current_focus or "？" in state.current_focus else "join"
            action_tier = "chat"
            guidance = "The chat looks engaging; join only if the current clue is still fresh."
            tags = ["high_interest"]
        elif state.interest > 0.52:
            pulse_type = "prepare_reply"
            social_intent = "answer"
            action_tier = "chat"
            guidance = "A light natural reply may fit if the next event invites it."
            tags = ["medium_interest"]

        reply_pressure = float(getattr(session, "reply_pressure", 0.0) or 0.0) if session else 0.0
        urgency = self._clamp(max(state.talk_willingness, state.interest * 0.75, state.silence_pressure * 0.55, reply_pressure))
        return HeartflowPulse(
            chat_id=state.chat_id,
            timestamp=now,
            pulse_type=pulse_type,
            reason=f"interest={state.interest:.2f}, talk_willingness={state.talk_willingness:.2f}, fatigue={state.fatigue:.2f}",
            guidance=guidance,
            suggested_social_intent=social_intent,
            suggested_action_tier=action_tier,
            urgency=urgency,
            visible_action_allowed=False,
            tags=tags,
        )

    def _compute_visible_candidate_score(
        self,
        state: HeartflowChatState,
        session: HeartflowSessionState | None = None,
        *,
        now: float,
    ) -> tuple[float, dict[str, float]]:
        del now
        topic_heat = float(getattr(session, "topic_heat", 0.0) or 0.0) if session else 0.0
        direct_relevance = float(getattr(session, "direct_relevance", 0.0) or 0.0) if session else 0.0
        insert_pressure = float(getattr(session, "insert_pressure", 0.0) or 0.0) if session else 0.0
        recent_bot_reply_penalty = min(float(getattr(session, "recent_bot_reply_count", 0) or 0), 5.0) * 0.04 if session else 0.0
        mood_bias_positive = max(float(state.mood_bias or 0.0), 0.0)
        components = {
            "silence_pressure": float(state.silence_pressure or 0.0) * 0.30,
            "talk_willingness": float(state.talk_willingness or 0.0) * 0.25,
            "interest": float(state.interest or 0.0) * 0.20,
            "topic_heat": topic_heat * 0.10,
            "direct_relevance": direct_relevance * 0.10,
            "mood_bias_positive": mood_bias_positive * 0.05,
            "fatigue_penalty": float(state.fatigue or 0.0) * 0.25,
            "insert_pressure_penalty": insert_pressure * 0.25,
            "recent_bot_reply_penalty": recent_bot_reply_penalty,
        }
        score = self._clamp(
            components["silence_pressure"]
            + components["talk_willingness"]
            + components["interest"]
            + components["topic_heat"]
            + components["direct_relevance"]
            + components["mood_bias_positive"]
            - components["fatigue_penalty"]
            - components["insert_pressure_penalty"]
            - components["recent_bot_reply_penalty"]
        )
        if session is not None:
            session.visible_candidate_score = score
        return score, {key: round(float(value or 0.0), 4) for key, value in components.items()}

    def _build_action_decision(
        self,
        session: HeartflowSessionState,
        state: HeartflowChatState,
        pulse: HeartflowPulse,
        snapshot: dict,
        *,
        now: float,
    ) -> HeartflowActionDecision:
        wait_targets = [str(item) for item in snapshot.get("wait_targets", []) or [] if str(item).strip()]
        executor_pending = self._int_snapshot(snapshot, "executor_pending")
        age_seconds = max(0.0, now - float(session.last_activity_ts or 0.0)) if session.last_activity_ts > 0 else self.ACTIVE_CHAT_TTL_SECONDS
        visible_score, score_components = self._compute_visible_candidate_score(state, session, now=now)
        rhythm = evaluate_proactive_rhythm(self.config, now=now)
        visible_threshold = rhythm.threshold(self.VISIBLE_CANDIDATE_SCORE_THRESHOLD)
        prepare_threshold = rhythm.threshold(self.PREPARE_CANDIDATE_SCORE_THRESHOLD)
        checks: dict[str, object] = {
            "pulse_type": pulse.pulse_type,
            "low_cost_retained": bool(session.low_cost_retained),
            "age_seconds": round(age_seconds, 2),
            "wait_targets_empty": not wait_targets,
            "executor_idle": executor_pending <= 0,
            "insert_pressure": round(float(session.insert_pressure or 0.0), 3),
            "reply_pressure": round(float(session.reply_pressure or 0.0), 3),
            "direct_relevance": round(float(session.direct_relevance or 0.0), 3),
            "topic_heat": round(float(session.topic_heat or 0.0), 3),
            "talk_frequency_adjust": round(float(session.talk_frequency_adjust or 0.0), 3),
            "visible_candidate_score": round(visible_score, 3),
            "visible_candidate_threshold": round(visible_threshold, 3),
            "prepare_candidate_threshold": round(prepare_threshold, 3),
            "score_components": score_components,
            "frequency_components": dict(session.frequency_components or {}),
            "consecutive_no_reply_count": int(session.consecutive_no_reply_count or 0),
            "consecutive_prepare_count": int(session.consecutive_prepare_count or 0),
            "quiet_hours": rhythm.quiet_hours,
            "time_bucket": rhythm.time_bucket,
            "base_frequency": rhythm.base_frequency,
            "base_frequency_factor": rhythm.base_frequency_factor,
            "topic_source_priority": ["conversation_continuity", "recent_memory", "fresh_small_talk"],
        }
        action_type = "observe"
        guidance = "Observe quietly; do not force a response."
        blocked_reason = "hidden_action"
        conflict_cooldown = self._conflict_penalty(snapshot, state) > 0
        old_topic_blocked = bool(
            session.low_cost_retained
            and (
                session.topic_heat < 0.32
                or session.insert_pressure > 0.65
                or conflict_cooldown
                or age_seconds > self.ACTIVE_CHAT_TTL_SECONDS
            )
        )

        if rhythm.quiet_hours:
            action_type = "no_reply"
            guidance = "Respect quiet hours; keep the thought as hidden context only."
            blocked_reason = "quiet_hours"
        elif state.fatigue > 0.75 or state.talk_willingness < 0.25:
            action_type = "cool_down"
            guidance = "Prefer silence or very short future replies while fatigue or low willingness is high."
            blocked_reason = "cool_down"
        elif wait_targets or executor_pending > 0:
            action_type = "wait"
            guidance = "Wait for the pending user turn or executor result before considering any proactive reply."
            blocked_reason = "user_waiting"
        elif old_topic_blocked:
            action_type = "complete_topic" if session.topic_heat < 0.18 else "observe"
            guidance = "The last topic is outside the active rhythm window; do not revive it without a new cue."
            blocked_reason = "low_cost_retained"
        elif session.insert_pressure > 0.65 or session.recent_bot_reply_count >= 3 or session.consecutive_no_reply_count >= 3:
            action_type = "no_reply"
            guidance = "Stay silent this tick; recent rhythm suggests another insertion would be abrupt."
            blocked_reason = "insert_pressure"
        elif pulse.pulse_type == "proactive_hint" and visible_score >= visible_threshold:
            action_type = "proactive_candidate"
            guidance = pulse.guidance
            blocked_reason = ""
        elif visible_score >= prepare_threshold or state.interest > 0.70 or pulse.pulse_type in {"prepare_reply", "join"}:
            action_type = "prepare_reply"
            guidance = "Keep a reply prepared, but wait for a fresh relevant user signal."
            blocked_reason = "not_visible_candidate"
        elif session.topic_heat < 0.12 and age_seconds > self.ACTIVE_SESSION_TTL_SECONDS:
            action_type = "complete_topic"
            guidance = "Treat the old topic as complete; do not revive it without a new cue."
            blocked_reason = "topic_complete"

        return HeartflowActionDecision(
            chat_id=session.chat_id,
            timestamp=now,
            action_type=action_type,
            reason=(
                f"action={action_type}; interest={state.interest:.2f}; talk_willingness={state.talk_willingness:.2f}; "
                f"insert_pressure={session.insert_pressure:.2f}; topic_heat={session.topic_heat:.2f}"
            ),
            guidance=guidance,
            should_dispatch_candidate=action_type == "proactive_candidate",
            blocked_reason=blocked_reason,
            safety_checks=checks,
        )

    @staticmethod
    def _apply_action_to_impulse_decision(
        action: HeartflowActionDecision,
        decision: HeartflowImpulseDecision,
    ) -> HeartflowImpulseDecision:
        decision.safety_checks["heartflow_action"] = action.action_type
        decision.safety_checks["heartflow_action_reason"] = action.reason[:160]
        if action.should_dispatch_candidate:
            return decision
        decision.visible_candidate_allowed = False
        decision.requires_synthetic_event = False
        decision.hidden_only = True
        decision.dispatch_enabled = False
        decision.synthetic_event_queued = False
        decision.blocked_reason = action.blocked_reason or decision.blocked_reason or f"heartflow_action:{action.action_type}"
        return decision

    def _recent_visible_candidate_cooldown(self, chat_id: str, now: float) -> bool:
        for decision in reversed(self._impulse_decisions_by_chat.get(chat_id, [])):
            if not decision.visible_candidate_allowed:
                continue
            if now - float(decision.timestamp or 0.0) <= self.VISIBLE_CANDIDATE_COOLDOWN_SECONDS:
                return True
            return False
        return False

    def _build_impulse_decision(
        self,
        session: HeartflowSessionState,
        state: HeartflowChatState,
        pulse: HeartflowPulse,
        snapshot: dict,
        *,
        now: float,
    ) -> HeartflowImpulseDecision:
        latest_ts = float(snapshot.get("latest_activity_ts", state.last_activity_ts) or 0.0)
        age_seconds = max(0.0, now - latest_ts) if latest_ts > 0 else self.ACTIVE_CHAT_TTL_SECONDS + 1.0
        wait_targets = [str(item) for item in snapshot.get("wait_targets", []) or [] if str(item).strip()]
        executor_pending = int(snapshot.get("executor_pending", 0) or 0)
        visible_score, score_components = self._compute_visible_candidate_score(state, session, now=now)
        rhythm = evaluate_proactive_rhythm(self.config, now=now)
        visible_threshold = rhythm.threshold(self.VISIBLE_CANDIDATE_SCORE_THRESHOLD)
        prepare_threshold = rhythm.threshold(self.PREPARE_CANDIDATE_SCORE_THRESHOLD)
        recent_proactive_cooldown = bool(
            session
            and session.last_visible_candidate_ts > 0
            and now - float(session.last_visible_candidate_ts or 0.0) <= self.VISIBLE_CANDIDATE_COOLDOWN_SECONDS
        )
        conflict_cooldown = self._conflict_penalty(snapshot, state) > 0
        safety_checks: dict[str, object] = {
            "pulse_type": pulse.pulse_type,
            "chat_active": latest_ts > 0 and age_seconds <= self.ACTIVE_CHAT_TTL_SECONDS,
            "age_seconds": round(age_seconds, 2),
            "silence_pressure": round(float(state.silence_pressure or 0.0), 3),
            "talk_willingness": round(float(state.talk_willingness or 0.0), 3),
            "urgency": round(float(pulse.urgency or 0.0), 3),
            "energy_enough": float(state.fatigue or 0.0) <= 0.75,
            "wait_targets_empty": not wait_targets,
            "executor_idle": executor_pending <= 0,
            "recent_visible_candidate_cooldown": self._recent_visible_candidate_cooldown(state.chat_id, now),
            "recent_proactive_candidate_cooldown": recent_proactive_cooldown,
            "conflict_cooldown": conflict_cooldown,
            "visible_candidate_score": round(visible_score, 3),
            "visible_candidate_threshold": round(visible_threshold, 3),
            "prepare_candidate_threshold": round(prepare_threshold, 3),
            "score_components": score_components,
            "frequency_components": dict(getattr(session, "frequency_components", {}) or {}),
            "insert_pressure": round(float(getattr(session, "insert_pressure", 0.0) or 0.0), 3),
            "reply_pressure": round(float(getattr(session, "reply_pressure", 0.0) or 0.0), 3),
            "direct_relevance": round(float(getattr(session, "direct_relevance", 0.0) or 0.0), 3),
            "dispatch_enabled": False,
            "quiet_hours": rhythm.quiet_hours,
            "time_bucket": rhythm.time_bucket,
            "base_frequency": rhythm.base_frequency,
            "base_frequency_factor": rhythm.base_frequency_factor,
            "topic_source_priority": ["conversation_continuity", "recent_memory", "fresh_small_talk"],
        }
        blocked_reason = ""
        if pulse.pulse_type != "proactive_hint":
            blocked_reason = "hidden_impulse"
        elif rhythm.quiet_hours:
            blocked_reason = "quiet_hours"
        elif not safety_checks["chat_active"]:
            blocked_reason = "chat_inactive"
        elif not safety_checks["energy_enough"]:
            blocked_reason = "low_energy"
        elif float(state.talk_willingness or 0.0) < self.VISIBLE_CANDIDATE_MIN_TALK_WILLINGNESS:
            blocked_reason = "low_talk_willingness"
        elif wait_targets or executor_pending > 0:
            blocked_reason = "user_waiting"
        elif safety_checks["recent_visible_candidate_cooldown"] or recent_proactive_cooldown:
            blocked_reason = "cooldown"
        elif conflict_cooldown:
            blocked_reason = "conflict_cooldown"
        elif float(pulse.urgency or 0.0) < self.VISIBLE_CANDIDATE_MIN_URGENCY:
            blocked_reason = "low_urgency"
        elif visible_score < visible_threshold:
            blocked_reason = "low_candidate_score"

        allowed = not blocked_reason
        preview = ""
        if allowed:
            preview = (
                f"[Heartflow proactive_hint] chat={state.chat_id}; "
                f"intent={pulse.suggested_social_intent}; tier={pulse.suggested_action_tier}; "
                f"guidance={pulse.guidance[:160]}"
            )
        return HeartflowImpulseDecision(
            chat_id=state.chat_id,
            timestamp=now,
            pulse_type=pulse.pulse_type,
            visible_candidate_allowed=allowed,
            requires_synthetic_event=allowed,
            hidden_only=not allowed,
            dispatch_enabled=False,
            synthetic_event_queued=False,
            blocked_reason=blocked_reason,
            safety_checks=safety_checks,
            synthetic_event_preview=preview,
        )

    async def _maybe_dispatch_visible_candidate(
        self,
        state: HeartflowChatState,
        pulse: HeartflowPulse,
        decision: HeartflowImpulseDecision,
    ) -> HeartflowImpulseDecision:
        if not decision.visible_candidate_allowed:
            return decision
        if not self.dispatcher or not hasattr(self.dispatcher, "dispatch"):
            decision.safety_checks["dispatcher_available"] = False
            return decision

        guidance = await self._build_visible_candidate_guidance(state, pulse)
        intent = ProactiveMessageIntent(
            chat_id=state.chat_id,
            source="heartflow",
            reason=pulse.reason,
            guidance=guidance,
            suggested_social_intent=pulse.suggested_social_intent,
            suggested_action_tier="chat",
            urgency=float(pulse.urgency or 0.0),
            cost=0.0,
            cooldown=float(self.VISIBLE_CANDIDATE_COOLDOWN_SECONDS),
            metadata={
                "group_id": state.chat_id,
                "talk_willingness": float(state.talk_willingness or 0.0),
                "silence_pressure": float(state.silence_pressure or 0.0),
                "heartflow_pulse_type": pulse.pulse_type,
                "topic_source_priority": ["conversation_continuity", "recent_memory", "fresh_small_talk"],
                "time_bucket": decision.safety_checks.get("time_bucket", ""),
            },
        )
        try:
            dispatch_decision = await self.dispatcher.dispatch(intent)
        except Exception as exc:
            decision.visible_candidate_allowed = False
            decision.requires_synthetic_event = False
            decision.hidden_only = True
            decision.dispatch_enabled = False
            decision.synthetic_event_queued = False
            decision.blocked_reason = f"dispatcher_error:{exc.__class__.__name__}"
            decision.safety_checks["dispatcher_available"] = True
            decision.safety_checks["dispatcher_error"] = str(exc)[:160]
            return decision

        decision.safety_checks["dispatcher_available"] = True
        decision.safety_checks["dispatcher"] = dict(getattr(dispatch_decision, "safety_checks", {}) or {})
        decision.safety_checks["dispatch_intent_id"] = getattr(dispatch_decision, "intent_id", "")
        if not getattr(dispatch_decision, "allowed", False):
            decision.visible_candidate_allowed = False
            decision.requires_synthetic_event = False
            decision.hidden_only = True
            decision.dispatch_enabled = False
            decision.synthetic_event_queued = False
            decision.blocked_reason = getattr(dispatch_decision, "blocked_reason", "") or "dispatcher_blocked"
            return decision

        decision.dispatch_enabled = True
        decision.requires_synthetic_event = True
        decision.synthetic_event_queued = bool(getattr(dispatch_decision, "synthetic_event_queued", False))
        decision.hidden_only = not decision.synthetic_event_queued
        if not decision.synthetic_event_queued:
            decision.blocked_reason = getattr(dispatch_decision, "blocked_reason", "") or "synthetic_event_not_queued"
        return decision

    async def _build_visible_candidate_guidance(self, state: HeartflowChatState, pulse: HeartflowPulse) -> str:
        focus = " ".join(str(state.current_focus or "").split())
        memory_hint = await self._recall_topic_memory(state.chat_id, focus)
        topic_source = "conversation_continuity" if focus else ("recent_memory" if memory_hint else "fresh_small_talk")
        parts = [
            f"Topic source: {topic_source}.",
            "Priority: continue the current chat lightly; use one private memory hint only if it fits; use fresh small talk only when there is no current topic.",
            "Write at most one short natural line, easy to ignore.",
            "Do not @ anyone, do not ask whether anyone is here, do not mention hidden state, schedules, scores, or proactive logic.",
            pulse.guidance,
        ]
        if focus:
            parts.append(f"Current chat clue: {focus[:160]}")
        if memory_hint:
            parts.append(f"Optional private memory hint, do not quote directly: {memory_hint[:180]}")
        if not focus and not memory_hint:
            parts.append("If nothing feels natural, staying quiet is better than inventing a topic.")
        return "\n".join(part for part in parts if str(part or "").strip())

    async def _recall_topic_memory(self, chat_id: str, focus: str) -> str:
        if not focus or not self.memory_engine:
            return ""
        retrieval = getattr(self.memory_engine, "retrieval_service", None)
        if retrieval and hasattr(retrieval, "retrieve"):
            try:
                memory_query = MemoryQuery(query=str(focus or ""), session_id=str(chat_id or ""), top_k=1)
                candidates = await retrieval.retrieve(memory_query)
                if hasattr(retrieval, "render_recall"):
                    return " ".join(str(retrieval.render_recall(memory_query, candidates) or "").split())
                return " ".join(str(getattr(candidates[0], "summary", "") or getattr(candidates[0], "content", "")).split()) if candidates else ""
            except Exception as exc:
                logger.debug(f"[Heartflow] v2 memory hint degraded for {chat_id}: {exc}")
        return ""

    def _remember_pulse(self, pulse: HeartflowPulse) -> None:
        items = [*self._pulses_by_chat.get(pulse.chat_id, []), pulse]
        self._pulses_by_chat[pulse.chat_id] = items[-self.PULSE_HISTORY_LIMIT :]
        state = self._states.get(pulse.chat_id)
        if state:
            state.cooldown_tags = self._recent_tags(pulse.chat_id)

    def _remember_impulse_decision(self, decision: HeartflowImpulseDecision) -> None:
        items = [*self._impulse_decisions_by_chat.get(decision.chat_id, []), decision]
        self._impulse_decisions_by_chat[decision.chat_id] = items[-self.IMPULSE_DECISION_HISTORY_LIMIT :]

    def _remember_action_decision(self, decision: HeartflowActionDecision) -> None:
        items = [*self._action_decisions_by_chat.get(decision.chat_id, []), decision]
        self._action_decisions_by_chat[decision.chat_id] = items[-self.ACTION_DECISION_HISTORY_LIMIT :]
        session = self._sessions.get(decision.chat_id)
        if not session:
            return
        session.last_impulse = decision.action_type
        if decision.action_type == "observe":
            session.consecutive_observe_count += 1
            session.consecutive_prepare_count = 0
        elif decision.action_type == "no_reply":
            session.consecutive_no_reply_count += 1
            session.consecutive_prepare_count = 0
        elif decision.action_type == "prepare_reply":
            session.consecutive_prepare_count += 1
            session.consecutive_observe_count = 0
        elif decision.action_type == "proactive_candidate":
            session.last_visible_candidate_ts = decision.timestamp
            session.consecutive_observe_count = 0
            session.consecutive_no_reply_count = 0
            session.consecutive_prepare_count = 0
        elif decision.action_type in {"wait", "cool_down", "complete_topic"}:
            session.consecutive_prepare_count = 0

    def _recent_tags(self, chat_id: str) -> list[str]:
        tags: list[str] = []
        for pulse in self._pulses_by_chat.get(chat_id, [])[-6:]:
            for tag in pulse.tags:
                clean = str(tag or "").strip()
                if clean and clean not in tags:
                    tags.append(clean)
        return tags

    def get_state(self, chat_id: str) -> HeartflowChatState | None:
        return self._states.get(chat_id)

    def get_session(self, chat_id: str) -> HeartflowSessionState | None:
        return self._sessions.get(chat_id)

    def list_sessions(self, limit: int = 50) -> list[HeartflowSessionState]:
        limit = max(1, min(int(limit or 50), 300))
        items = list(self._sessions.values())
        items.sort(key=lambda item: float(item.last_activity_ts or 0.0), reverse=True)
        return items[:limit]

    def get_latest_pulse(self, chat_id: str) -> HeartflowPulse | None:
        pulses = self._pulses_by_chat.get(chat_id, [])
        return pulses[-1] if pulses else None

    def get_latest_impulse_decision(self, chat_id: str) -> HeartflowImpulseDecision | None:
        decisions = self._impulse_decisions_by_chat.get(chat_id, [])
        return decisions[-1] if decisions else None

    def get_latest_action_decision(self, chat_id: str) -> HeartflowActionDecision | None:
        decisions = self._action_decisions_by_chat.get(chat_id, [])
        return decisions[-1] if decisions else None

    def list_impulse_decisions(self, chat_id: str | None = None, limit: int = 50) -> list[HeartflowImpulseDecision]:
        limit = max(1, min(int(limit or 50), 300))
        if chat_id:
            return list(self._impulse_decisions_by_chat.get(chat_id, []))[-limit:][::-1]
        items: list[HeartflowImpulseDecision] = []
        for decisions in self._impulse_decisions_by_chat.values():
            items.extend(decisions)
        items.sort(key=lambda item: float(item.timestamp or 0.0), reverse=True)
        return items[:limit]

    def list_action_decisions(self, chat_id: str | None = None, limit: int = 50) -> list[HeartflowActionDecision]:
        limit = max(1, min(int(limit or 50), 300))
        if chat_id:
            return list(self._action_decisions_by_chat.get(chat_id, []))[-limit:][::-1]
        items: list[HeartflowActionDecision] = []
        for decisions in self._action_decisions_by_chat.values():
            items.extend(decisions)
        items.sort(key=lambda item: float(item.timestamp or 0.0), reverse=True)
        return items[:limit]

    def list_timeline(self, chat_id: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 80), 300))
        chat_ids = [chat_id] if chat_id else sorted(
            set(self._pulses_by_chat) | set(self._action_decisions_by_chat) | set(self._impulse_decisions_by_chat)
        )
        items: list[dict[str, Any]] = []
        for current_chat_id in chat_ids:
            if not current_chat_id:
                continue
            for pulse in self._pulses_by_chat.get(current_chat_id, []):
                items.append(
                    {
                        "kind": "pulse",
                        "chat_id": current_chat_id,
                        "timestamp": float(pulse.timestamp or 0.0),
                        "label": pulse.pulse_type,
                        "summary": pulse.reason[:180],
                        "guidance": pulse.guidance[:180],
                        "payload": {
                            "suggested_social_intent": pulse.suggested_social_intent,
                            "suggested_action_tier": pulse.suggested_action_tier,
                            "urgency": pulse.urgency,
                            "tags": list(pulse.tags or []),
                        },
                    }
                )
            for action in self._action_decisions_by_chat.get(current_chat_id, []):
                items.append(
                    {
                        "kind": "action",
                        "chat_id": current_chat_id,
                        "timestamp": float(action.timestamp or 0.0),
                        "label": action.action_type,
                        "summary": action.reason[:180],
                        "guidance": action.guidance[:180],
                        "payload": {
                            "should_dispatch_candidate": action.should_dispatch_candidate,
                            "blocked_reason": action.blocked_reason,
                            "safety_checks": dict(action.safety_checks or {}),
                        },
                    }
                )
            for decision in self._impulse_decisions_by_chat.get(current_chat_id, []):
                items.append(
                    {
                        "kind": "impulse_decision",
                        "chat_id": current_chat_id,
                        "timestamp": float(decision.timestamp or 0.0),
                        "label": decision.pulse_type,
                        "summary": decision.blocked_reason or ("queued" if decision.synthetic_event_queued else "candidate"),
                        "guidance": decision.synthetic_event_preview[:180],
                        "payload": {
                            "visible_candidate_allowed": decision.visible_candidate_allowed,
                            "requires_synthetic_event": decision.requires_synthetic_event,
                            "hidden_only": decision.hidden_only,
                            "dispatch_enabled": decision.dispatch_enabled,
                            "synthetic_event_queued": decision.synthetic_event_queued,
                            "safety_checks": dict(decision.safety_checks or {}),
                        },
                    }
                )
        items.sort(key=lambda item: float(item.get("timestamp", 0.0) or 0.0), reverse=True)
        return items[:limit]

    def get_hidden_context(self, chat_id: str) -> str:
        state = self.get_state(chat_id)
        if not state:
            return ""
        session = self.get_session(chat_id)
        pulse = self.get_latest_pulse(chat_id)
        decision = self.get_latest_impulse_decision(chat_id)
        action = self.get_latest_action_decision(chat_id)
        lines = [
            (
                f"interest={state.interest:.2f}; engagement={state.engagement:.2f}; "
                f"talk_willingness={state.talk_willingness:.2f}; fatigue={state.fatigue:.2f}; "
                f"silence_pressure={state.silence_pressure:.2f}; mood_bias={state.mood_bias:.2f}"
            ),
            f"recent_impulse={state.recent_impulse}; current_focus={state.current_focus[:120]}",
        ]
        if session:
            lines.append(
                "session="
                f"tick_count={session.tick_count}; low_cost_retained={session.low_cost_retained}; "
                f"talk_frequency_adjust={session.talk_frequency_adjust:.2f}; insert_pressure={session.insert_pressure:.2f}; "
                f"reply_pressure={session.reply_pressure:.2f}; direct_relevance={session.direct_relevance:.2f}; "
                f"visible_candidate_score={session.visible_candidate_score:.2f}; topic_heat={session.topic_heat:.2f}; "
                f"consecutive_no_reply={session.consecutive_no_reply_count}; "
                f"consecutive_prepare={session.consecutive_prepare_count}"
            )
            if session.frequency_components:
                lines.append(
                    "frequency_components="
                    + ", ".join(f"{key}={value:.2f}" for key, value in session.frequency_components.items())
                )
        if state.cooldown_tags:
            lines.append("cooldown_tags=" + ", ".join(state.cooldown_tags[:8]))
        if pulse:
            lines.append(
                f"latest_pulse={pulse.pulse_type}; suggested_social_intent={pulse.suggested_social_intent}; "
                f"suggested_action_tier={pulse.suggested_action_tier}; guidance={pulse.guidance}"
            )
        if action:
            lines.append(
                "latest_heartflow_action="
                f"action_type={action.action_type}; should_dispatch_candidate={action.should_dispatch_candidate}; "
                f"blocked_reason={action.blocked_reason or 'none'}; guidance={action.guidance}"
            )
            lines.append("Heartflow action is hidden only; do not quote it.")
        if decision:
            lines.append(
                "latest_impulse_decision="
                f"visible_candidate_allowed={decision.visible_candidate_allowed}; "
                f"requires_synthetic_event={decision.requires_synthetic_event}; "
                f"hidden_only={decision.hidden_only}; dispatch_enabled={decision.dispatch_enabled}; "
                f"synthetic_event_queued={decision.synthetic_event_queued}; blocked_reason={decision.blocked_reason or 'none'}"
            )
            lines.append("Impulse decision is hidden only in v1; do not quote it.")
        lines.append("This is hidden context; do not quote it.")
        return "\n".join(lines)

    def describe_status(self) -> dict:
        pending_pulses = sum(len(items) for items in self._pulses_by_chat.values())
        pending_impulse_decisions = sum(len(items) for items in self._impulse_decisions_by_chat.values())
        pending_action_decisions = sum(len(items) for items in self._action_decisions_by_chat.values())
        return {
            "enabled": self.runtime_coordinator is not None,
            "active_chats": len(self._states),
            "active_sessions": len(self._sessions),
            "last_tick_time": self._last_tick_time,
            "last_feedback_time": self.feedback_bridge.last_feedback_time,
            "pending_pulses": pending_pulses,
            "pending_impulse_decisions": pending_impulse_decisions,
            "pending_action_decisions": pending_action_decisions,
            "preview_mode": "readonly",
        }


__all__ = ["HeartflowManager"]
