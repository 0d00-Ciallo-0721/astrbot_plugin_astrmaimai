from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import defaultdict
from typing import Any, Mapping

from astrbot.api import logger

from ...shared.helpers.plugin_helpers import event_mentions_actor
from ..contracts.social_feedback import (
    SocialFeedbackDecision,
    SocialFeedbackEvidence,
    SocialFeedbackObservation,
)


class GroupSocialFeedbackObserver:
    """Observe whether later group activity responds to a committed bot turn."""

    DEFAULT_WINDOW_SEC = 45.0
    DEFAULT_MAX_ACTIVE = 5

    def __init__(self, *, config=None, dialogue_store=None, gateway=None, owner_registry=None):
        self.config = config
        self.dialogue_store = dialogue_store
        self.gateway = gateway
        self.owner_registry = owner_registry
        self._active: dict[str, dict[str, SocialFeedbackObservation]] = defaultdict(dict)
        self._turn_index: dict[tuple[str, str], str] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._stats: dict[str, int] = defaultdict(int)

    def refresh_config(self, config) -> None:
        self.config = config

    def _conversation_config(self):
        return getattr(self.config, "conversation", None)

    def _enabled(self) -> bool:
        return bool(
            getattr(
                self._conversation_config(),
                "social_feedback_observation_enabled",
                True,
            )
        )

    def _window_seconds(self) -> float:
        value = getattr(
            self._conversation_config(),
            "social_feedback_window_sec",
            self.DEFAULT_WINDOW_SEC,
        )
        return max(5.0, float(value or self.DEFAULT_WINDOW_SEC))

    def _max_active(self) -> int:
        value = getattr(
            self._conversation_config(),
            "social_feedback_max_active_per_chat",
            self.DEFAULT_MAX_ACTIVE,
        )
        return max(1, int(value or self.DEFAULT_MAX_ACTIVE))

    @staticmethod
    def _stable_id(chat_id: str, turn_id: str) -> str:
        raw = f"{chat_id}\x1f{turn_id}".encode("utf-8")
        return "feedback_" + hashlib.sha256(raw).hexdigest()[:20]

    @staticmethod
    def _event_id(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        for value in (
            getattr(message_obj, "message_id", None),
            getattr(message_obj, "id", None),
            getattr(event, "message_id", None),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _reply_to_message_id(event: Any) -> str:
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in chain:
            component_type = str(
                getattr(component, "type", component.__class__.__name__)
            ).lstrip("_").lower()
            if component_type != "reply":
                continue
            for attr_name in ("message_id", "id", "reply_id", "target_id"):
                value = str(getattr(component, attr_name, "") or "").strip()
                if value:
                    return value
        return ""

    @staticmethod
    def _is_reaction_only(event: Any) -> bool:
        if str(getattr(event, "message_str", "") or "").strip():
            return False
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        if not chain:
            return False
        reaction_types = {"face", "reaction", "poke", "emoji", "marketface"}
        return all(
            str(getattr(item, "type", item.__class__.__name__)).lstrip("_").lower()
            in reaction_types
            for item in chain
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").lower(), flags=re.UNICODE)

    @classmethod
    def _text_overlap(cls, left: str, right: str) -> float:
        a = cls._normalize_text(left)
        b = cls._normalize_text(right)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if len(a) < 2 or len(b) < 2:
            return 0.0
        a_pairs = {a[index : index + 2] for index in range(len(a) - 1)}
        b_pairs = {b[index : index + 2] for index in range(len(b) - 1)}
        return len(a_pairs & b_pairs) / max(1, min(len(a_pairs), len(b_pairs)))

    def _track_task(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)
        registry = getattr(self, "owner_registry", None)
        register = getattr(registry, "register", None)
        if callable(register):
            try:
                register(
                    task,
                    task_family="social_feedback.expiry",
                    scope_id="GLOBAL",
                    run_id=f"social-feedback-{int(time.time() * 1000)}",
                    owner="GroupSocialFeedbackObserver",
                    generation=getattr(registry, "generation", 0),
                    cancel_status="cancelled",
                )
            except Exception as exc:
                logger.debug("[SocialFeedback] owner registry registration degraded: %s", exc)
        task.add_done_callback(self._background_tasks.discard)

    async def _record_feedback(
        self,
        observation: SocialFeedbackObservation,
        *,
        kind: str,
        actor_id: str = "",
        event_id: str = "",
        confidence: float = 0.0,
        status: str = "impacted",
        observed_at: float | None = None,
        caused_reply: bool = False,
    ) -> None:
        store = self.dialogue_store
        recorder = getattr(store, "record_bot_turn_feedback", None)
        if not callable(recorder):
            return
        timestamp = time.time() if observed_at is None else float(observed_at)
        try:
            await recorder(
                observation.chat_id,
                observation_id=observation.observation_id,
                bot_turn_id=observation.bot_turn_ids[-1] if observation.bot_turn_ids else "",
                feedback_kind=kind,
                status=status,
                actor_id=actor_id,
                evidence_event_id=event_id,
                confidence=confidence,
                feedback_at=timestamp,
                latency_ms=max(0.0, timestamp - observation.last_bot_sent_at) * 1000.0,
                caused_reply=caused_reply,
            )
        except Exception as exc:
            logger.debug(f"[SocialFeedback] feedback persistence degraded: {exc}")

    def _arm_timeout(self, observation: SocialFeedbackObservation) -> None:
        old_task = self._timeout_tasks.pop(observation.observation_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _expire(expected_expiry: float) -> None:
            try:
                remaining = max(0.0, expected_expiry - time.monotonic())
                if remaining:
                    await asyncio.sleep(remaining)
                async with self._lock:
                    current = self._active.get(observation.chat_id, {}).get(
                        observation.observation_id
                    )
                    if current is not observation or current.expires_at != expected_expiry:
                        return
                    self._active[observation.chat_id].pop(observation.observation_id, None)
                    self._turn_index.pop((observation.chat_id, observation.turn_id), None)
                    observation.status = "silent" if not observation.evidence else "impacted"
                    observation.terminal_reason = "observation_timeout"
                    self._stats["silent_timeout" if not observation.evidence else "impacted_timeout"] += 1
                await self._record_feedback(
                    observation,
                    kind="silent" if not observation.evidence else "observation_closed",
                    status=observation.status,
                )
            except asyncio.CancelledError:
                return
            finally:
                current_task = asyncio.current_task()
                if self._timeout_tasks.get(observation.observation_id) is current_task:
                    self._timeout_tasks.pop(observation.observation_id, None)

        task = asyncio.create_task(_expire(observation.expires_at))
        self._timeout_tasks[observation.observation_id] = task
        self._track_task(task)

    async def arm(
        self,
        committed_turn: Any,
        *,
        event: Any = None,
        context: Mapping[str, Any] | None = None,
    ) -> SocialFeedbackObservation | None:
        if not self._enabled() or str(getattr(committed_turn, "chat_kind", "")) != "group":
            return None
        context = dict(context or {})
        chat_id = str(getattr(committed_turn, "chat_id", "") or "").strip()
        turn_id = str(getattr(committed_turn, "turn_id", "") or "").strip()
        if not chat_id or not turn_id:
            return None
        thread_id = str(
            context.get("thread_id")
            or (event.get_extra("astrmai_turn_thread_id", "") if event is not None else "")
            or chat_id
        )
        thread_signature = str(
            context.get("thread_signature")
            or (event.get_extra("astrmai_thread_signature", "") if event is not None else "")
            or ""
        )
        generation = int(
            context.get("generation")
            or (event.get_extra("astrmai_turn_generation", 0) if event is not None else 0)
            or 0
        )
        target_id = str(
            getattr(getattr(committed_turn, "target", None), "target_actor_id", "")
            or ""
        ).strip()
        now_mono = time.monotonic()
        expires_at = now_mono + self._window_seconds()
        observation_id = self._stable_id(chat_id, turn_id)
        superseded: list[SocialFeedbackObservation] = []
        async with self._lock:
            existing_id = self._turn_index.get((chat_id, turn_id))
            observation = self._active.get(chat_id, {}).get(existing_id or "")
            if observation is None:
                for active_id, candidate in list(self._active.get(chat_id, {}).items()):
                    if candidate.thread_id != thread_id or candidate.turn_id == turn_id:
                        continue
                    candidate.status = "superseded"
                    candidate.terminal_reason = "new_bot_turn_same_thread"
                    self._active[chat_id].pop(active_id, None)
                    self._turn_index.pop((chat_id, candidate.turn_id), None)
                    timeout_task = self._timeout_tasks.pop(candidate.observation_id, None)
                    if timeout_task is not None and not timeout_task.done():
                        timeout_task.cancel()
                    superseded.append(candidate)
                observation = SocialFeedbackObservation(
                    observation_id=observation_id,
                    chat_id=chat_id,
                    turn_id=turn_id,
                    thread_id=thread_id,
                    thread_signature=thread_signature,
                    topic_epoch=max(0, int(getattr(committed_turn, "topic_epoch", 0) or 0)),
                    turn_generation=generation,
                    bot_turn_ids=[],
                    outbound_message_ids=[],
                    target_user_ids=[target_id] if target_id else [],
                    reply_text=str(
                        getattr(committed_turn, "persistable_text", "")
                        or getattr(committed_turn, "visible_text", "")
                        or ""
                    ),
                    bot_id=str(context.get("bot_id", "") or ""),
                    started_at=float(getattr(committed_turn, "sent_at", 0.0) or time.time()),
                    last_bot_sent_at=float(getattr(committed_turn, "sent_at", 0.0) or time.time()),
                    expires_at=expires_at,
                )
                bucket = self._active[chat_id]
                if len(bucket) >= self._max_active():
                    oldest = min(bucket.values(), key=lambda item: item.last_bot_sent_at)
                    bucket.pop(oldest.observation_id, None)
                    self._turn_index.pop((chat_id, oldest.turn_id), None)
                    oldest.status = "superseded"
                    oldest.terminal_reason = "active_observation_limit"
                    task = self._timeout_tasks.pop(oldest.observation_id, None)
                    if task is not None and not task.done():
                        task.cancel()
                bucket[observation_id] = observation
                self._turn_index[(chat_id, turn_id)] = observation_id
            observation.merge_outbound(
                commit_id=str(getattr(committed_turn, "commit_id", "") or ""),
                message_ids=list(getattr(committed_turn, "outbound_message_ids", ()) or ()),
                sent_at=float(getattr(committed_turn, "sent_at", 0.0) or time.time()),
                expires_at=expires_at,
            )
            if target_id and target_id not in observation.target_user_ids:
                observation.target_user_ids.append(target_id)
        self._arm_timeout(observation)
        for item in superseded:
            await self._record_feedback(
                item,
                kind="observation_closed",
                status="superseded",
            )
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("astrmai_post_reply_feedback_event", observation.feedback_event)
            event.set_extra("astrmai_social_feedback_observation_id", observation.observation_id)
        self._stats["armed"] += 1
        return observation

    async def clear_chat(self, chat_id: str) -> bool:
        async with self._lock:
            observations = list(self._active.pop(str(chat_id or ""), {}).values())
            for observation in observations:
                self._turn_index.pop((observation.chat_id, observation.turn_id), None)
                timeout_task = self._timeout_tasks.pop(observation.observation_id, None)
                if timeout_task is not None and not timeout_task.done():
                    timeout_task.cancel()
        return bool(observations)

    def get_active_observations(self, chat_id: str) -> list[SocialFeedbackObservation]:
        now = time.monotonic()
        return [
            item
            for item in self._active.get(str(chat_id or ""), {}).values()
            if item.status == "observing" and item.expires_at > now
        ]

    @staticmethod
    def _apply_decision(event: Any, decision: SocialFeedbackDecision) -> None:
        if not hasattr(event, "set_extra"):
            return
        event.set_extra("astrmai_social_feedback_detected", decision.detected)
        event.set_extra("astrmai_social_feedback_kind", decision.kind)
        event.set_extra("astrmai_social_feedback_action", decision.action)
        event.set_extra("astrmai_social_feedback_observation_id", decision.observation_id)
        event.set_extra("astrmai_social_feedback_actor_id", decision.actor_id)
        event.set_extra("astrmai_social_feedback_confidence", decision.confidence)
        if decision.action == "force_engage":
            event.set_extra("astrmai_force_engage", True)
            event.set_extra("astrmai_wait_resume_thought", "有人明确接上了你刚才的发言，自然回应当前消息。")
        elif decision.action == "attention_boost":
            event.set_extra("astrmai_social_feedback_attention_boost", True)

    async def observe(self, event: Any) -> SocialFeedbackDecision:
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not self._enabled() or not getattr(event, "get_group_id", lambda: "")():
            return SocialFeedbackDecision()
        active = self.get_active_observations(chat_id)
        if not active:
            return SocialFeedbackDecision()

        reply_id = self._reply_to_message_id(event)
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        incoming_thread = str(event.get_extra("astrmai_turn_thread_id", "") or "")
        event_id = self._event_id(event)
        text = str(getattr(event, "message_str", "") or "").strip()
        matched: SocialFeedbackObservation | None = None
        kind = "unrelated"
        action = "none"
        confidence = 0.0

        if reply_id:
            matched = next(
                (item for item in active if reply_id in item.outbound_message_ids),
                None,
            )
            if matched is not None:
                kind, action, confidence = "direct_quote", "force_engage", 1.0

        if matched is None:
            exact_thread = [item for item in active if incoming_thread and item.thread_id == incoming_thread]
            mention_candidates = exact_thread or ([max(active, key=lambda item: item.last_bot_sent_at)] if len(active) == 1 else [])
            for candidate in mention_candidates:
                if candidate.bot_id and event_mentions_actor(event, candidate.bot_id):
                    matched = candidate
                    kind, action, confidence = "direct_mention", "force_engage", 1.0
                    break

        if matched is None and self._is_reaction_only(event):
            exact_thread = [item for item in active if incoming_thread and item.thread_id == incoming_thread]
            if len(exact_thread) == 1:
                matched = exact_thread[0]
                kind, action, confidence = "reaction", "record_only", 0.8

        if matched is None and sender_id:
            target_matches = [item for item in active if sender_id in item.target_user_ids]
            exact_target = [item for item in target_matches if incoming_thread and item.thread_id == incoming_thread]
            candidates = exact_target or (target_matches if len(target_matches) == 1 else [])
            if len(candidates) == 1 and text:
                matched = candidates[0]
                kind, action, confidence = "target_followup", "attention_boost", 0.85

        if matched is None and text:
            exact_thread = [item for item in active if incoming_thread and item.thread_id == incoming_thread]
            candidates = exact_thread or (active if len(active) == 1 else [])
            scored = [
                (self._text_overlap(text, item.reply_text), item)
                for item in candidates
            ]
            overlap, candidate = max(scored, default=(0.0, None), key=lambda pair: pair[0])
            if candidate is not None and self._normalize_text(text) == self._normalize_text(candidate.reply_text):
                matched = candidate
                kind, action, confidence = "echo", "record_only", 0.9
            elif candidate is not None and (candidate in exact_thread or overlap >= 0.25):
                matched = candidate
                kind, action = "semantic_followup", "attention_boost"
                confidence = max(0.55, min(0.8, 0.55 + overlap * 0.25))

        if matched is None:
            decision = SocialFeedbackDecision(actor_id=sender_id)
            self._apply_decision(event, decision)
            self._stats["unrelated"] += 1
            return decision

        observed_at = time.time()
        evidence = SocialFeedbackEvidence(
            event_id=event_id,
            actor_id=sender_id,
            kind=kind,
            confidence=confidence,
            observed_at=observed_at,
        )
        async with self._lock:
            if event_id and any(item.event_id == event_id for item in matched.evidence):
                decision = SocialFeedbackDecision(
                    detected=True,
                    kind=kind,
                    action="record_only",
                    observation_id=matched.observation_id,
                    actor_id=sender_id,
                    confidence=confidence,
                    reason="duplicate_evidence",
                )
                self._apply_decision(event, decision)
                return decision
            matched.evidence.append(evidence)
            matched.evidence = matched.evidence[-12:]
            if sender_id and sender_id not in matched.responders:
                matched.responders.append(sender_id)
            matched.strongest_confidence = max(matched.strongest_confidence, confidence)
            matched.first_feedback_at = matched.first_feedback_at or observed_at
            matched.status = "engaged" if action == "force_engage" else "impacted"
            matched.feedback_event.set()
            if action == "force_engage":
                self._active.get(chat_id, {}).pop(matched.observation_id, None)
                self._turn_index.pop((chat_id, matched.turn_id), None)
                task = self._timeout_tasks.pop(matched.observation_id, None)
                if task is not None and not task.done():
                    task.cancel()
        decision = SocialFeedbackDecision(
            detected=True,
            kind=kind,
            action=action,
            observation_id=matched.observation_id,
            actor_id=sender_id,
            confidence=confidence,
        )
        self._apply_decision(event, decision)
        self._stats[kind] += 1
        await self._record_feedback(
            matched,
            kind=kind,
            actor_id=sender_id,
            event_id=event_id,
            confidence=confidence,
            status=matched.status,
            observed_at=observed_at,
            caused_reply=action == "force_engage",
        )
        return decision

    async def mark_group_wait_result(self, event: Any, result: str) -> None:
        if str(result or "") != "RESUME":
            return
        observation_id = str(event.get_extra("astrmai_social_feedback_observation_id", "") or "")
        if not observation_id:
            return
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        async with self._lock:
            observation = self._active.get(chat_id, {}).get(observation_id)
            if observation is not None:
                observation.status = "engaged"

    def describe_status(self) -> dict[str, Any]:
        return {
            "active_observations": sum(len(items) for items in self._active.values()),
            "stats": dict(self._stats),
        }


__all__ = ["GroupSocialFeedbackObserver"]
