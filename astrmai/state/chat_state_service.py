from __future__ import annotations

import asyncio
import copy
import datetime
import inspect
import math
import time
import uuid
from time import monotonic
from typing import Any, Dict, List

from astrbot.api import logger

from ..infrastructure.persistence.orm_models import ChatState, LastMessageMetadata, UserProfile

from .contracts.profile_summary import UserProfileSummary
from .energy.energy_manager import EnergyManager
from .mood.mood_decay import apply_natural_decay
from .mood.mood_manager import MoodManager
from .relationship.affection_router import AffectionRouter
from .relationship.relationship_ledger import RelationshipEventProposal, RelationshipLedgerEntry
from .relationship.relationship_engine import RelationshipEngine, RelationshipEvent
from .user_profile_service import UserProfileService


class ChatStateService:
    def __init__(self, persistence: Any, config: Any):
        import threading

        self.persistence = persistence
        self.config = config
        self.chat_states: Dict[str, ChatState] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = {}
        # ponytail: threading.Lock guards dict mutations, held for trivial ops only — safe in asyncio
        self._pool_lock_mutex = threading.Lock()
        self._last_lock_prune: float = 0.0
        self._chat_generations: Dict[str, int] = {}

    def _get_chat_lock(self, chat_id: str) -> asyncio.Lock:
        # ponytail: FIFO eviction when >500 locks. Python 3.7+ dict is insertion-ordered.
        # Edge case: very old but still-active chats could be evicted if they're at the front of the dict.
        # Mitigation: move-to-end on access keeps active chats near the back.
        now = monotonic()
        MAX_CHAT_LOCKS = 500
        if len(self._chat_locks) > MAX_CHAT_LOCKS and now - self._last_lock_prune > 300:
            excess = len(self._chat_locks) - 300
            keys_to_remove = [
                key for key, lock in self._chat_locks.items()
                if key != chat_id and not lock.locked()
            ][:excess]
            for key in keys_to_remove:
                self._chat_locks.pop(key, None)
                self._chat_generations.pop(key, None)
            self._last_lock_prune = now
        # Move current chat_id to end (LRU approximation in Python 3.7+ ordered dict)
        if chat_id in self._chat_locks:
            self._chat_locks[chat_id] = self._chat_locks.pop(chat_id)

        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    def _chat_generation(self, chat_id: str) -> int:
        return self._chat_generations.get(chat_id, 0)

    def _is_current_generation(self, chat_id: str, generation: int) -> bool:
        return self._chat_generation(chat_id) == generation

    @staticmethod
    def _is_finite_number(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

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
    def _clamp_mood(value: float, fallback: float = 0.0) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric = float(fallback)
        if not math.isfinite(numeric):
            numeric = float(fallback)
        return max(-1.0, min(1.0, numeric))

    def _mark_dirty(self, state: ChatState) -> ChatState:
        state.is_dirty = True
        return state

    async def _persist_if_dirty(self, state: ChatState) -> None:
        if not state.is_dirty:
            return
        try:
            await self.persistence.save_chat_state(state.chat_id, state)
            state.is_dirty = False
        except Exception:
            logger.exception(f"[ChatState] DB save failed for {state.chat_id} in _persist_if_dirty")

    async def _get_state_inner(self, chat_id: str) -> ChatState:
        now = time.time()
        if chat_id in self.chat_states:
            state = self.chat_states[chat_id]
            self._touch_state(state, now)
            self._check_daily_reset(state)
            await self._persist_if_dirty(state)
            return state

        try:
            state = await self.persistence.load_chat_state(chat_id)
        except Exception:
            from astrbot.api import logger as _log
            _log.exception(f"[ChatState] DB load failed for {chat_id}, using default")
            state = self._create_default_state(chat_id)
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
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            return await self._get_state_inner(chat_id)

    async def peek_state(self, chat_id: str) -> ChatState:
        """Return a detached state snapshot without resets, dirty writes, or cache touches."""
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            cached = self.chat_states.get(chat_id)
            if cached is not None:
                return copy.deepcopy(cached)
            try:
                loaded = await self.persistence.load_chat_state(chat_id)
            except Exception:
                loaded = None
            if loaded is None:
                return self._create_default_state(chat_id)
            return copy.deepcopy(self._coerce_chat_state(chat_id, loaded))

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

    async def clear_chat_state(self, chat_id: str) -> bool:
        async with self._get_chat_lock(chat_id):
            removed = self.chat_states.pop(chat_id, None) is not None
            self._chat_generations[chat_id] = self._chat_generation(chat_id) + 1
            return removed

    async def atomic_update_mood(
        self,
        chat_id: str,
        delta: float = 0.0,
        absolute_val: float | None = None,
    ) -> float:
        if absolute_val is not None and not self._is_finite_number(absolute_val):
            state = self.chat_states.get(chat_id)
            return float(getattr(state, "mood", 0.0) or 0.0)
        if absolute_val is None and not self._is_finite_number(delta):
            state = self.chat_states.get(chat_id)
            return float(getattr(state, "mood", 0.0) or 0.0)
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                state = self.chat_states.get(chat_id)
                return float(getattr(state, "mood", 0.0) or 0.0)
            state = await self._get_state_inner(chat_id)
            apply_natural_decay(state, self.config)
            if absolute_val is not None:
                state.mood = self._clamp_mood(absolute_val)
            else:
                state.mood = self._clamp_mood(state.mood + delta)
            self._mark_dirty(state)
            try:
                await self.persistence.save_chat_state(chat_id, state)
            except Exception:
                logger.exception(f"[ChatState] DB save failed for {chat_id} in atomic_update_mood")
            return state.mood

    async def mark_energy_consumed(self, chat_id: str, amount: float) -> ChatState:
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            state = await self._get_state_inner(chat_id)
            old_energy = state.energy
            state.energy = max(0.0, old_energy - amount)
            state.total_replies += 1
            state.last_reply_time = time.time()
            self._mark_dirty(state)
            logger.debug(f"[{chat_id}] energy settlement: {old_energy:.2f} -> {state.energy:.2f}")
            try:
                await self.persistence.save_chat_state(chat_id, state)
                state.is_dirty = False
            except Exception:
                logger.exception(f"[ChatState] DB save failed for {chat_id} in mark_energy_consumed")
            return state

    async def settle_wakeup(self, chat_id: str, amount: float, next_wakeup_timestamp: float) -> ChatState:
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            state = await self._get_state_inner(chat_id)
            state.energy = max(0.0, float(state.energy or 0.0) - float(amount or 0.0))
            state.total_replies += 1
            committed_at = time.time()
            state.last_reply_time = committed_at
            state.last_committed_bot_reply_at = committed_at
            state.next_wakeup_timestamp = float(next_wakeup_timestamp or 0.0)
            state.next_proactive_due_at = state.next_wakeup_timestamp
            state.proactive_claim_token = ""
            state.proactive_claimed_at = 0.0
            state.is_dirty = True
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
            return state

    async def record_real_user_activity(
        self,
        chat_id: str,
        *,
        chat_kind: str = "",
        occurred_at: float | None = None,
        effective_response: bool = True,
    ) -> ChatState:
        """Record a semantic human message, not arbitrary runtime activity.

        ``effective_response`` distinguishes a member's ordinary group
        conversation from a direct response to the bot. Both reset the
        silence timer, but only the latter clears unanswered proactive turns.
        """
        occurred_at = float(occurred_at or time.time())
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            state = await self._get_state_inner(chat_id)
            life = getattr(self.config, "life", None)
            silence_minutes = max(0.0, float(getattr(life, "silence_threshold", 120) or 0.0))
            state.chat_kind = str(chat_kind or state.chat_kind or "")
            state.last_real_user_activity_at = max(
                float(getattr(state, "last_real_user_activity_at", 0.0) or 0.0),
                occurred_at,
            )
            state.last_reply_time = state.last_real_user_activity_at
            state.proactive_generation = int(getattr(state, "proactive_generation", 0) or 0) + 1
            if effective_response:
                state.unanswered_proactive_count = 0
                state.last_proactive_cancel_reason = "user_activity"
            else:
                state.last_proactive_cancel_reason = "meaningful_group_activity"
            state.proactive_claim_token = ""
            state.proactive_claimed_at = 0.0
            state.next_proactive_due_at = occurred_at + silence_minutes * 60.0
            state.next_wakeup_timestamp = state.next_proactive_due_at
            self._mark_dirty(state)
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
            return state

    async def record_committed_bot_reply(
        self,
        chat_id: str,
        *,
        committed_at: float | None = None,
        is_proactive: bool = False,
        commit_id: str = "",
    ) -> ChatState:
        committed_at = float(committed_at or time.time())
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            state = await self._get_state_inner(chat_id)
            state.last_committed_bot_reply_at = max(
                float(getattr(state, "last_committed_bot_reply_at", 0.0) or 0.0),
                committed_at,
            )
            if is_proactive:
                state.unanswered_proactive_count = int(
                    getattr(state, "unanswered_proactive_count", 0) or 0
                ) + 1
                state.last_proactive_commit_id = str(commit_id or "")
            self._mark_dirty(state)
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
            return state

    async def is_proactive_generation_current(self, chat_id: str, captured_generation: int) -> bool:
        state = await self.get_state(chat_id)
        return int(getattr(state, "proactive_generation", 0) or 0) == int(captured_generation)

    async def claim_proactive_due(
        self,
        chat_id: str,
        *,
        expected_generation: int,
        now: float | None = None,
        lease_seconds: float = 300.0,
    ) -> str:
        now = float(now or time.time())
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return ""
            state = await self._get_state_inner(chat_id)
            if int(getattr(state, "proactive_generation", 0) or 0) != int(expected_generation):
                return ""
            due_at = float(getattr(state, "next_proactive_due_at", 0.0) or 0.0)
            if due_at > now:
                return ""
            claim_token = str(getattr(state, "proactive_claim_token", "") or "")
            claimed_at = float(getattr(state, "proactive_claimed_at", 0.0) or 0.0)
            if claim_token and now - claimed_at < max(1.0, float(lease_seconds or 0.0)):
                return ""
            claim_token = uuid.uuid4().hex
            atomic_claim = getattr(self.persistence, "atomic_claim_proactive_due", None)
            if callable(atomic_claim):
                claimed = await atomic_claim(
                    chat_id,
                    expected_generation=int(expected_generation),
                    claim_token=claim_token,
                    now=now,
                    lease_seconds=lease_seconds,
                )
                if not claimed:
                    return ""
                state.proactive_claim_token = claim_token
                state.proactive_claimed_at = now
                state.last_proactive_cancel_reason = ""
                state.is_dirty = False
                return claim_token
            state.proactive_claim_token = claim_token
            state.proactive_claimed_at = now
            state.last_proactive_cancel_reason = ""
            self._mark_dirty(state)
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
            return claim_token

    async def settle_proactive_attempt(
        self,
        chat_id: str,
        *,
        claim_token: str = "",
        next_due_at: float,
        cancel_reason: str = "",
    ) -> ChatState:
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return self._create_default_state(chat_id)
            state = await self._get_state_inner(chat_id)
            current_claim = str(getattr(state, "proactive_claim_token", "") or "")
            if claim_token and current_claim and current_claim != str(claim_token):
                return state
            atomic_settle = getattr(self.persistence, "atomic_settle_proactive_claim", None)
            if callable(atomic_settle) and claim_token:
                settled = await atomic_settle(
                    chat_id,
                    claim_token=str(claim_token),
                    next_due_at=float(next_due_at or 0.0),
                    cancel_reason=str(cancel_reason or ""),
                )
                if not settled:
                    return state
                state.proactive_claim_token = ""
                state.proactive_claimed_at = 0.0
                state.next_proactive_due_at = float(next_due_at or 0.0)
                state.next_wakeup_timestamp = state.next_proactive_due_at
                state.last_proactive_cancel_reason = str(cancel_reason or "")
                state.is_dirty = False
                return state
            state.proactive_claim_token = ""
            state.proactive_claimed_at = 0.0
            state.next_proactive_due_at = float(next_due_at or 0.0)
            state.next_wakeup_timestamp = state.next_proactive_due_at
            state.last_proactive_cancel_reason = str(cancel_reason or "")
            self._mark_dirty(state)
            await self.persistence.save_chat_state(chat_id, state)
            state.is_dirty = False
            return state

    async def should_drop_by_energy(self, chat_id: str, msg_count: int, energy_manager: EnergyManager) -> bool:
        generation = self._chat_generation(chat_id)
        async with self._get_chat_lock(chat_id):
            if not self._is_current_generation(chat_id, generation):
                return False
            state = await self._get_state_inner(chat_id)
            should_drop = energy_manager.should_drop_by_energy(state, msg_count)
            if getattr(state, "is_dirty", False):
                try:
                    await self.persistence.save_chat_state(chat_id, state)
                    state.is_dirty = False
                except Exception:
                    logger.exception(f"[ChatState] DB save failed for {chat_id} in should_drop_by_energy")
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
        self._relationship_settlement_locks: Dict[str, asyncio.Lock] = {}

    def _relationship_settlement_lock(self, key: str) -> asyncio.Lock:
        if key not in self._relationship_settlement_locks:
            self._relationship_settlement_locks[key] = asyncio.Lock()
        return self._relationship_settlement_locks[key]

    def refresh_config(self, config):
        """ponytail: hot-reload config into state engine and sub-components"""
        self.config = config
        self.chat_state_service.config = config
        for attr in ("mood_manager", "energy_manager", "relationship_engine"):
            comp = getattr(self, attr, None)
            if comp is not None:
                refresh = getattr(comp, "refresh_config", None)
                if callable(refresh):
                    refresh(config)
                else:
                    comp.config = config

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
            RelationshipEvent.BOUNDARY_VIOLATION,
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

    async def peek_state(self, chat_id: str) -> ChatState:
        return await self.chat_state_service.peek_state(chat_id)

    async def get_user_profile(self, user_id: str) -> UserProfile:
        return await self._load_profile_with_relationship(user_id)

    async def update_mood(self, chat_id: str, text: str):
        generation = self.chat_state_service._chat_generation(chat_id)
        # Phase 1: snapshot read from current state without mutating the cached object.
        state = await self.get_state(chat_id)
        snapshot_state = copy.deepcopy(state)
        apply_natural_decay(snapshot_state, self.config)
        snapshot_mood = snapshot_state.mood

        # Phase 2: LLM analysis (no lock — parallel-safe, no blocking)
        tag, new_value = await self._resolve_mood_analysis(chat_id, text, snapshot_mood)
        try:
            mood_is_finite = math.isfinite(float(new_value))
        except (TypeError, ValueError):
            mood_is_finite = False
        if not mood_is_finite:
            return tag, snapshot_mood

        # Phase 3: CAS write under lock
        lock = self.chat_state_service._get_chat_lock(chat_id)
        async with lock:
            if not self.chat_state_service._is_current_generation(chat_id, generation):
                return tag, snapshot_mood
            current_state = await self.chat_state_service._get_state_inner(chat_id)
            apply_natural_decay(current_state, self.config)
            current_mood = current_state.mood

            if abs(current_mood - snapshot_mood) < 0.0001:
                current_state.mood = ChatStateService._clamp_mood(new_value)
            else:
                logger.debug(f"[AstrMai-state] CAS shifted for mood update, applying delta to current baseline")
                current_state.mood = ChatStateService._clamp_mood(current_mood + (new_value - snapshot_mood))

            current_state.is_dirty = True
            await self.chat_state_service.persistence.save_chat_state(chat_id, current_state)
            current_state.is_dirty = False
            return tag, current_state.mood

    async def update_social_score_from_fact(
        self,
        user_id: str,
        impact_score: float,
        *,
        touch_activity: bool = True,
    ):
        profile = await self.user_profile_service.get_user_profile(user_id)
        if not self.relationship_engine.has_vector(user_id):
            self.relationship_engine.load_from_profile(user_id, profile.__dict__)
        old_score = profile.social_score
        new_score = max(-100.0, min(100.0, old_score + impact_score))
        aligned_score = self.relationship_engine.align_social_score(user_id, new_score)
        rel_vector = self.relationship_engine.export_user_vector(user_id)
        await self.user_profile_service.update_social_score(
            user_id,
            aligned_score,
            rel_vector,
            touch_activity=touch_activity,
        )
        logger.info(
            f"[Social] user {profile.name}({user_id}) score {old_score:.1f} -> "
            f"{aligned_score:.1f} ({impact_score:+.1f})"
        )

    def get_active_states(self) -> List[ChatState]:
        return self.chat_state_service.get_active_states()

    async def clear_chat_state(self, chat_id: str) -> bool:
        return await self.chat_state_service.clear_chat_state(chat_id)

    def get_active_profiles(self) -> List[UserProfile]:
        return self.user_profile_service.get_active_profiles()

    def apply_natural_decay(self, state: ChatState):
        apply_natural_decay(state, self.config)

    def _relationship_settings(self) -> tuple[str, float, str]:
        settings = getattr(self.config, "relationship", None)
        mode = str(getattr(settings, "settlement_mode", "legacy") or "legacy").strip().lower()
        if mode not in {"legacy", "shadow", "event_only"}:
            mode = "legacy"
        try:
            min_confidence = float(getattr(settings, "min_confidence", 0.75))
        except (TypeError, ValueError):
            min_confidence = 0.75
        version = str(getattr(settings, "policy_version", "relationship-v1") or "relationship-v1").strip()
        return mode, max(0.0, min(1.0, min_confidence)), version

    @staticmethod
    def _relationship_vector_delta(before: dict, after: dict) -> dict[str, float]:
        return {
            name: round(float(after.get(name, 0.0) or 0.0) - float(before.get(name, 0.0) or 0.0), 6)
            for name in ("trust", "familiarity", "emotion_bond", "respect")
        }

    async def settle_relationship_event(
        self,
        proposal: RelationshipEventProposal,
        *,
        event: Any = None,
    ) -> RelationshipLedgerEntry:
        """Settle one canonical event and record an idempotent audit entry."""
        mode, min_confidence, policy_version = self._relationship_settings()
        async with self._relationship_settlement_lock(proposal.idempotency_key):
            lookup_entry = getattr(self.persistence, "get_relationship_ledger_entry", None)
            if callable(lookup_entry):
                existing = await lookup_entry(proposal.idempotency_key)
                if existing is not None:
                    entry = RelationshipLedgerEntry(
                        proposal=proposal,
                        policy_version=str(existing.get("policy_version", policy_version) or policy_version),
                        disposition="duplicate",
                        event_id=str(existing.get("event_id", "") or ""),
                        created_at=float(existing.get("created_at", 0.0) or 0.0),
                    )
                    if event is not None and hasattr(event, "set_extra"):
                        event.set_extra("astrmai_relationship_event_id", entry.event_id)
                        event.set_extra("astrmai_relationship_event_type", proposal.event_type)
                        event.set_extra("astrmai_relationship_event_source", proposal.source)
                        event.set_extra("astrmai_relationship_event_confidence", proposal.confidence)
                        event.set_extra("astrmai_relationship_event_disposition", entry.disposition)
                        event.set_extra("astrmai_relationship_policy_version", entry.policy_version)
                        event.set_extra("astrmai_relationship_delta", {})
                    return entry
            disposition = "applied"
            if proposal.event_type not in self.relationship_engine.EVENT_MATRIX:
                disposition = "rejected"
            elif proposal.confidence < min_confidence:
                disposition = "suppressed"
            elif mode == "shadow":
                disposition = "suppressed"
            elif mode == "event_only" and proposal.event_type == RelationshipEvent.NORMAL_CHAT:
                disposition = "suppressed"

            before: dict[str, float] = {}
            after: dict[str, float] = {}
            delta: dict[str, float] = {}
            old_score = 0.0
            new_score = 0.0
            if disposition == "applied":
                profile = await self.get_user_profile(proposal.user_id)
                before = self.relationship_engine.export_user_vector(proposal.user_id)
                old_score = profile.social_score
                new_score = self.relationship_engine.process_event(
                    proposal.user_id,
                    proposal.event_type,
                    intensity=proposal.intensity,
                )
                after = self.relationship_engine.export_user_vector(proposal.user_id)
                delta = self._relationship_vector_delta(before, after)
                await self.user_profile_service.update_social_score(proposal.user_id, new_score, after)
                await self.affection_router.publish_change(
                    proposal.user_id,
                    old_score,
                    new_score,
                    proposal.mood_tag,
                    proposal.event_type,
                )

            entry = RelationshipLedgerEntry(
                proposal=proposal,
                policy_version=policy_version,
                disposition=disposition,
                before_vector=before,
                delta_vector=delta,
                after_vector=after,
            )
            append_entry = getattr(self.persistence, "append_relationship_ledger_entry", None)
            if callable(append_entry):
                inserted, existing = await append_entry(entry)
                if not inserted:
                    entry = RelationshipLedgerEntry(
                        proposal=proposal,
                        policy_version=str(existing.get("policy_version", policy_version) or policy_version),
                        disposition="duplicate",
                        event_id=str(existing.get("event_id", entry.event_id) or entry.event_id),
                        created_at=float(existing.get("created_at", entry.created_at) or entry.created_at),
                    )
            if event is not None and hasattr(event, "set_extra"):
                event.set_extra("astrmai_relationship_event_id", entry.event_id)
                event.set_extra("astrmai_relationship_event_type", proposal.event_type)
                event.set_extra("astrmai_relationship_event_source", proposal.source)
                event.set_extra("astrmai_relationship_event_confidence", proposal.confidence)
                event.set_extra("astrmai_relationship_event_disposition", entry.disposition)
                event.set_extra("astrmai_relationship_policy_version", entry.policy_version)
                event.set_extra("astrmai_relationship_delta", dict(entry.delta_vector))
            return entry

    async def calculate_and_update_affection(
        self,
        user_id: str,
        group_id: str,
        mood_tag: str,
        intensity: float = 1.0,
        message_text: str = "",
        event_type: str | None = None,
        event: Any = None,
        turn_id: str = "",
        source_event_ids: tuple[str, ...] | list[str] = (),
    ):
        resolved_event_type = event_type or self._resolve_affection_event_type(message_text)
        if (
            event_type is None
            and self.relationship_engine.should_soften_support_event_for_message(message_text, resolved_event_type)
        ):
            resolved_event_type = RelationshipEvent.NORMAL_CHAT
        effective_mood_tag = "" if str(mood_tag or "").strip().lower() == "neutral" else mood_tag
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
        mood_mapping_source = "not_applied"
        mode, _, _ = self._relationship_settings()
        if effective_mood_tag and resolved_event_type == RelationshipEvent.NORMAL_CHAT and mode == "legacy":
            mood_resolution = self.relationship_engine.resolve_mood_event(effective_mood_tag)
            effective_event_type = mood_resolution.event_type
            mood_mapping_source = mood_resolution.source
        else:
            effective_event_type = resolved_event_type
        proposal = RelationshipEventProposal(
            event_type=effective_event_type,
            user_id=str(user_id),
            chat_id=str(group_id),
            turn_id=str(turn_id),
            source_event_ids=tuple(source_event_ids),
            confidence=1.0,
            intensity=effective_intensity,
            evidence_codes=("explicit_event" if event_type else "interaction_classifier",),
            source=("legacy_mood_mapping" if mood_mapping_source != "not_applied" else "deterministic_rule"),
            mood_tag=effective_mood_tag,
        )
        entry = await self.settle_relationship_event(proposal, event=event)
        logger.debug(
            "[StateEngine] relationship settlement user=%s tag=%s event=%s source=%s disposition=%s",
            user_id,
            effective_mood_tag or "neutral",
            effective_event_type,
            mood_mapping_source,
            entry.disposition,
        )
        return entry

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
        event: Any = None,
        turn_id: str = "",
        source_event_ids: tuple[str, ...] | list[str] = (),
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
            event=event,
            turn_id=turn_id,
            source_event_ids=source_event_ids,
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

    async def settle_proactive_wakeup(
        self,
        chat_id: str,
        *,
        amount: float,
        next_wakeup_timestamp: float,
    ) -> ChatState:
        if "FriendMessage" in chat_id:
            amount = 0.0
        return await self.chat_state_service.settle_wakeup(chat_id, amount, next_wakeup_timestamp)

    async def record_real_user_activity(
        self,
        chat_id: str,
        *,
        chat_kind: str = "",
        occurred_at: float | None = None,
        effective_response: bool = True,
    ) -> ChatState:
        return await self.chat_state_service.record_real_user_activity(
            chat_id,
            chat_kind=chat_kind,
            occurred_at=occurred_at,
            effective_response=effective_response,
        )

    async def record_committed_bot_reply(
        self,
        chat_id: str,
        *,
        committed_at: float | None = None,
        is_proactive: bool = False,
        commit_id: str = "",
    ) -> ChatState:
        return await self.chat_state_service.record_committed_bot_reply(
            chat_id,
            committed_at=committed_at,
            is_proactive=is_proactive,
            commit_id=commit_id,
        )

    async def is_proactive_generation_current(self, chat_id: str, captured_generation: int) -> bool:
        return await self.chat_state_service.is_proactive_generation_current(chat_id, captured_generation)

    async def claim_proactive_due(self, chat_id: str, **kwargs) -> str:
        return await self.chat_state_service.claim_proactive_due(chat_id, **kwargs)

    async def settle_proactive_attempt(self, chat_id: str, **kwargs) -> ChatState:
        return await self.chat_state_service.settle_proactive_attempt(chat_id, **kwargs)

    async def list_due_proactive_chat_ids(
        self,
        *,
        now: float,
        limit: int = 200,
    ) -> list[str]:
        loader = getattr(self.persistence, "list_due_chat_state_ids", None)
        if not callable(loader):
            return []
        return list(await loader(now=now, limit=limit) or [])

    async def get_user_profile_summary(self, user_id: str) -> UserProfileSummary:
        profile = await self.get_user_profile(user_id)
        return UserProfileSummary.from_profile(profile)
