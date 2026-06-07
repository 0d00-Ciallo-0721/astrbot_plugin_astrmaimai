from __future__ import annotations

import asyncio
import copy
import datetime
import inspect
import time
from typing import Any, Dict, List

from astrbot.api import logger

from ..infrastructure.persistence.orm_models import ChatState, LastMessageMetadata, UserProfile

from .contracts.profile_summary import UserProfileSummary
from .energy.energy_manager import EnergyManager
from .mood.mood_decay import apply_natural_decay
from .mood.mood_manager import MoodManager
from .relationship.affection_router import AffectionRouter
from .relationship.relationship_engine import RelationshipEngine, RelationshipEvent
from .user_profile_service import UserProfileService


class ChatStateService:
    def __init__(self, persistence: Any, config: Any):
        import threading

        self.persistence = persistence
        self.config = config
        self.chat_states: Dict[str, ChatState] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        self._pool_lock_mutex = threading.Lock()

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        with self._pool_lock_mutex:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[chat_id] = lock
        return lock

    def _touch_state(self, state: ChatState, now: float) -> ChatState:
        state.last_access_time = now
        return state

    @staticmethod
    def _coerce_last_msg_info(payload: Any) -> LastMessageMetadata:
        if isinstance(payload, LastMessageMetadata):
            return payload
        if isinstance(payload, dict):
            allowed_keys = set(LastMessageMetadata.__dataclass_fields__.keys())
            return LastMessageMetadata(**{key: payload[key] for key in allowed_keys if key in payload})
        return LastMessageMetadata()

    def _coerce_chat_state(self, chat_id: str, state: Any) -> ChatState:
        if isinstance(state, ChatState):
            return state
        if isinstance(state, dict):
            allowed_keys = set(ChatState.__dataclass_fields__.keys())
            payload = {key: state[key] for key in allowed_keys if key in state}
            payload["chat_id"] = str(payload.get("chat_id") or chat_id)
            payload["last_msg_info"] = self._coerce_last_msg_info(payload.get("last_msg_info"))
            if not isinstance(payload.get("group_config"), dict):
                payload["group_config"] = {}
            return ChatState(**payload)
        return state

    def _create_default_state(self, chat_id: str) -> ChatState:
        state = ChatState(chat_id=chat_id, energy=0.5, mood=0.0)
        state.last_reset_date = datetime.date.today().isoformat()
        return state

    @staticmethod
    def _clamp_mood(value: float) -> float:
        return max(-1.0, min(1.0, value))

    def _mark_dirty(self, state: ChatState) -> ChatState:
        state.is_dirty = True
        return state

    async def _persist_if_dirty(self, state: ChatState) -> None:
        if not state.is_dirty:
            return
        await self.persistence.save_chat_state(state.chat_id, state)
        state.is_dirty = False

    async def _get_state_inner(self, chat_id: str) -> ChatState:
        now = time.time()
        if chat_id in self.chat_states:
            state = self.chat_states[chat_id]
            self._touch_state(state, now)
            self._check_daily_reset(state)
            await self._persist_if_dirty(state)
            return state

        state = await self.persistence.load_chat_state(chat_id)
        if state is None:
            state = self._create_default_state(chat_id)
        else:
            state = self._coerce_chat_state(chat_id, state)

        self._touch_state(state, now)
        self._check_daily_reset(state)
        await self._persist_if_dirty(state)
        self.chat_states[chat_id] = state
        return state

    async def get_state(self, chat_id: str) -> ChatState:
        async with self._get_chat_lock(chat_id):
            return await self._get_state_inner(chat_id)

    def _check_daily_reset(self, state: ChatState) -> None:
        today = datetime.date.today().isoformat()
        if state.last_reset_date != today:
            state.last_reset_date = today
            _energy_cfg = getattr(self.config, "energy", None)
            daily_recovery = getattr(_energy_cfg, "daily_recovery", 0.05) if _energy_cfg else 0.05
            state.energy = min(1.0, state.energy + daily_recovery)
            state.mood = 0.0
            self._mark_dirty(state)

    def get_active_states(self) -> List[ChatState]:
        return list(self.chat_states.values())

    async def atomic_update_mood(
        self,
        chat_id: str,
        delta: float = 0.0,
        absolute_val: float | None = None,
    ) -> float:
        async with self._get_chat_lock(chat_id):
            state = await self._get_state_inner(chat_id)
            apply_natural_decay(state, self.config)
            if absolute_val is not None:
                state.mood = self._clamp_mood(absolute_val)
            else:
                state.mood = self._clamp_mood(state.mood + delta)
            self._mark_dirty(state)
            await self.persistence.save_chat_state(chat_id, state)
            return state.mood

    async def mark_energy_consumed(self, chat_id: str, amount: float) -> ChatState:
        async with self._get_chat_lock(chat_id):
            state = await self._get_state_inner(chat_id)
            old_energy = state.energy
            state.energy = max(0.0, old_energy - amount)
            state.total_replies += 1
            state.last_reply_time = time.time()
            self._mark_dirty(state)
            logger.debug(f"[{chat_id}] energy settlement: {old_energy:.2f} -> {state.energy:.2f}")
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
            return state

    async def should_drop_by_energy(self, chat_id: str, msg_count: int, energy_manager: EnergyManager) -> bool:
        async with self._get_chat_lock(chat_id):
            state = await self._get_state_inner(chat_id)
            should_drop = energy_manager.should_drop_by_energy(state, msg_count)
            if should_drop and getattr(state, "is_dirty", False):
                await self.persistence.save_chat_state(chat_id, state)
                state.is_dirty = False
            return should_drop


class StateEngine:
    def __init__(self, persistence: Any, gateway: Any, config: Any = None, event_bus: Any = None):
        self.persistence = persistence
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.event_bus = event_bus

        self.chat_state_service = ChatStateService(persistence, self.config)
        self.user_profile_service = UserProfileService(persistence)
        self.mood_manager = MoodManager(gateway, self.config)
        self.relationship_engine = RelationshipEngine(config=self.config)
        self.affection_router = AffectionRouter(self.relationship_engine, event_bus=event_bus)
        self.energy_manager = EnergyManager(self.config)

    async def _load_profile_with_relationship(self, user_id: str) -> UserProfile:
        profile = await self.user_profile_service.get_user_profile(user_id)
        had_runtime_vector = self.relationship_engine.has_vector(user_id)
        if not had_runtime_vector:
            self.relationship_engine.load_from_profile(user_id, profile.__dict__)
        relationship_vector = self.relationship_engine.export_user_vector(user_id)
        if relationship_vector:
            profile.relationship_vector = relationship_vector
            if had_runtime_vector:
                profile.social_score = self.relationship_engine.get_social_score(user_id)
            if isinstance(profile.profile_metadata, dict):
                profile.profile_metadata["relationship_vector"] = dict(relationship_vector)
        return profile

    async def _resolve_mood_analysis(self, chat_id: str, text: str, snapshot_mood: float):
        analyze_mood = self.mood_manager.analyze_mood
        try:
            signature = inspect.signature(analyze_mood)
        except (TypeError, ValueError):
            signature = None

        if signature is not None:
            parameters = signature.parameters.values()
            accepts_chat_id = "chat_id" in signature.parameters or any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in parameters
            )
            if accepts_chat_id:
                return await analyze_mood(
                    text,
                    snapshot_mood,
                    chat_id=chat_id,
                )
        return await analyze_mood(text, snapshot_mood)

    def _resolve_affection_event_type(self, message_text: str) -> RelationshipEvent:
        if not message_text:
            return RelationshipEvent.NORMAL_CHAT
        return self.relationship_engine.classify_interaction_type(message_text)

    def _resolve_no_send_affection_event_type(
        self,
        message_text: str,
        *,
        skipped_reason: str = "",
        attack_confidence: float = 0.0,
        risk_flags: list[str] | None = None,
    ) -> RelationshipEvent | None:
        event_type = self._resolve_affection_event_type(message_text)
        if event_type in {
            RelationshipEvent.INSULT,
            RelationshipEvent.RUDENESS,
            RelationshipEvent.ARGUMENT,
            RelationshipEvent.SPAM,
        }:
            return event_type

        normalized_reason = str(skipped_reason or "").strip().lower()
        risk_set = {str(flag or "").strip().lower() for flag in (risk_flags or []) if str(flag or "").strip()}
        if normalized_reason in {"ignore", "blocked", "stale", "send_failed"} and (
            attack_confidence >= 0.5 or "direct_attack_to_bot" in risk_set
        ):
            return RelationshipEvent.ARGUMENT
        return None

    @property
    def chat_states(self) -> Dict[str, ChatState]:
        return self.chat_state_service.chat_states

    @property
    def user_profiles(self) -> Dict[str, UserProfile]:
        return self.user_profile_service.user_profiles

    def _get_user_lock(self, user_id: str):
        return self.user_profile_service._get_user_lock(user_id)

    async def get_state(self, chat_id: str) -> ChatState:
        return await self.chat_state_service.get_state(chat_id)

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return await self._load_profile_with_relationship(user_id)

    async def update_mood(self, chat_id: str, text: str):
        # Phase 1: snapshot read from current state without mutating the cached object.
        state = await self.get_state(chat_id)
        snapshot_state = copy.deepcopy(state)
        apply_natural_decay(snapshot_state, self.config)
        snapshot_mood = snapshot_state.mood

        # Phase 2: LLM analysis (no lock — parallel-safe, no blocking)
        tag, new_value = await self._resolve_mood_analysis(chat_id, text, snapshot_mood)

        # Phase 3: CAS write under lock
        lock = self.chat_state_service._get_chat_lock(chat_id)
        async with lock:
            current_state = await self.chat_state_service._get_state_inner(chat_id)
            apply_natural_decay(current_state, self.config)
            current_mood = current_state.mood

            if abs(current_mood - snapshot_mood) < 0.001:
                current_state.mood = ChatStateService._clamp_mood(new_value)
            else:
                delta = new_value - snapshot_mood
                current_state.mood = ChatStateService._clamp_mood(current_mood + delta)

            current_state.is_dirty = True
            await self.chat_state_service.persistence.save_chat_state(chat_id, current_state)
            current_state.is_dirty = False
            return tag, current_state.mood

    async def update_social_score_from_fact(self, user_id: str, impact_score: float):
        profile = await self.user_profile_service.get_user_profile(user_id)
        if not self.relationship_engine.has_vector(user_id):
            self.relationship_engine.load_from_profile(user_id, profile.__dict__)
        old_score = profile.social_score
        new_score = max(-100.0, min(100.0, old_score + impact_score))
        aligned_score = self.relationship_engine.align_social_score(user_id, new_score)
        rel_vector = self.relationship_engine.export_user_vector(user_id)
        await self.user_profile_service.update_social_score(user_id, aligned_score, rel_vector)
        logger.info(
            f"[Social] user {profile.name}({user_id}) score {old_score:.1f} -> "
            f"{aligned_score:.1f} ({impact_score:+.1f})"
        )

    def get_active_states(self) -> List[ChatState]:
        return self.chat_state_service.get_active_states()

    def get_active_profiles(self) -> List[UserProfile]:
        return self.user_profile_service.get_active_profiles()

    def apply_natural_decay(self, state: ChatState):
        apply_natural_decay(state, self.config)

    async def calculate_and_update_affection(
        self,
        user_id: str,
        group_id: str,
        mood_tag: str,
        intensity: float = 1.0,
        message_text: str = "",
        event_type: str | None = None,
    ):
        profile = await self.get_user_profile(user_id)
        old_score = profile.social_score
        resolved_event_type = event_type or self._resolve_affection_event_type(message_text)
        if (
            event_type is None
            and self.relationship_engine.should_soften_support_event_for_message(message_text, resolved_event_type)
        ):
            resolved_event_type = RelationshipEvent.NORMAL_CHAT
        effective_mood_tag = mood_tag
        if (
            event_type is None
            and resolved_event_type == RelationshipEvent.NORMAL_CHAT
            and self.relationship_engine.should_preserve_normal_chat_for_message(message_text, mood_tag)
        ):
            effective_mood_tag = ""
        effective_intensity = intensity
        if resolved_event_type == RelationshipEvent.NORMAL_CHAT:
            effective_intensity *= self.relationship_engine.normal_chat_affection_intensity_bias(
                message_text,
                mood_tag,
            )
        elif resolved_event_type == RelationshipEvent.IGNORE:
            effective_intensity *= self.relationship_engine.ignore_affection_intensity_bias(message_text)
        effective_event_type = (
            self.relationship_engine.MOOD_TO_EVENT.get(effective_mood_tag, resolved_event_type)
            if effective_mood_tag and resolved_event_type == RelationshipEvent.NORMAL_CHAT
            else resolved_event_type
        )
        new_score = self.relationship_engine.process_event(
            user_id=user_id,
            event_type=effective_event_type,
            intensity=effective_intensity,
            mood_tag=effective_mood_tag,
        )
        rel_vector = self.relationship_engine.export_user_vector(user_id)
        await self.user_profile_service.update_social_score(user_id, new_score, rel_vector)
        await self.affection_router.publish_change(
            user_id,
            old_score,
            new_score,
            effective_mood_tag,
            effective_event_type,
        )

    async def settle_no_send_affection(
        self,
        user_id: str,
        group_id: str,
        message_text: str,
        *,
        skipped_reason: str = "",
        attack_confidence: float = 0.0,
        risk_flags: list[str] | None = None,
        intensity: float = 0.75,
    ) -> bool:
        event_type = self._resolve_no_send_affection_event_type(
            message_text,
            skipped_reason=skipped_reason,
            attack_confidence=attack_confidence,
            risk_flags=risk_flags,
        )
        if not event_type:
            return False
        await self.calculate_and_update_affection(
            user_id=user_id,
            group_id=group_id,
            mood_tag="neutral",
            intensity=intensity,
            message_text=message_text,
            event_type=event_type,
        )
        return True

    async def should_drop_by_energy(self, chat_id: str, msg_count: int) -> bool:
        return await self.chat_state_service.should_drop_by_energy(chat_id, msg_count, self.energy_manager)

    async def increment_user_message_count(self, user_id: str):
        await self.user_profile_service.observe_user_activity(user_id, source="message_counter")

    async def on_learning_message_recorded(self, payload: dict) -> None:
        sender_id = str((payload or {}).get("sender_id", "") or "")
        if sender_id:
            await self.user_profile_service.observe_user_activity(
                sender_id,
                chat_id=str((payload or {}).get("chat_id", "") or ""),
                sender_name=str((payload or {}).get("sender_name", "") or ""),
                content=str((payload or {}).get("content", "") or ""),
                source="learning_message",
            )

    async def record_profile_learning_touch(
        self,
        user_id: str,
        *,
        chat_id: str = "",
        source: str = "private_reply",
        weight: float = 1.0,
        sender_name: str = "",
        increment_know_times: bool = False,
    ) -> None:
        await self.user_profile_service.record_profile_learning_touch(
            user_id,
            chat_id=chat_id,
            source=source,
            weight=weight,
            sender_name=sender_name,
            increment_know_times=increment_know_times,
        )

    async def apply_profile_name(self, user_id: str, new_name: str, *, source: str = "event") -> bool:
        return await self.user_profile_service.apply_profile_name(user_id, new_name, source=source)

    async def get_profile_prompt_bundle(self, user_id: str) -> dict:
        return await self.user_profile_service.get_profile_prompt_bundle_for_user(user_id)

    async def flush_message_counters(self):
        await self.user_profile_service.flush_message_counters()

    async def atomic_update_mood(
        self,
        chat_id: str,
        delta: float = 0.0,
        absolute_val: float | None = None,
    ) -> float:
        return await self.chat_state_service.atomic_update_mood(chat_id, delta=delta, absolute_val=absolute_val)

    async def consume_energy(self, chat_id: str, amount: float | None = None):
        amount = self.energy_manager.get_reply_cost(amount)
        # Design intent: private (FriendMessage) chats never consume energy.
        # The energy economy is group-chat-only; private replies are always allowed.
        if "FriendMessage" in chat_id:
            return
        await self.chat_state_service.mark_energy_consumed(chat_id, amount)

    async def get_user_profile_summary(self, user_id: str) -> UserProfileSummary:
        profile = await self.get_user_profile(user_id)
        return UserProfileSummary.from_profile(profile)
