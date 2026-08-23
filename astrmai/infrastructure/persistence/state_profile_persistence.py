from __future__ import annotations

import json
import time
from time import monotonic
from typing import Any, Dict, Optional
from uuid import uuid4

from .orm_models import ChatState, LastMessageMetadata
from .sqlite_helpers import connect_aiosqlite, connect_sqlite


class StateProfilePersistenceMixin:
    # Cached column names — avoids PRAGMA table_info on every load (TTL-matched with DatabaseService)
    _COL_CACHE_TTL_SEC: float = 300.0
    _chat_state_cols_cache: "list | None" = None
    _chat_state_cols_ts: float = 0.0
    _user_profile_cols_cache: "list | None" = None
    _user_profile_cols_ts: float = 0.0

    @staticmethod
    def _safe_json_loads(value: Any, default: Any = None):
        """Safe JSON deserialization with fallback for dirty/corrupted data."""
        raw = str(value or "").strip()
        if not raw:
            return default if default is not None else {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger = __import__("astrbot.api", fromlist=["logger"]).logger
            logger.warning(
                f"[AstrMai-Persistence] safe_json_loads failed, falling back to default: {exc} | "
                f"raw_preview={raw[:120]!r}"
            )
            return default if default is not None else {}

    @staticmethod
    def _relationship_vector_from_metadata(profile_metadata: Any) -> Dict[str, Any]:
        if not isinstance(profile_metadata, dict):
            return {}
        relationship_vector = profile_metadata.get("relationship_vector", {})
        return dict(relationship_vector) if isinstance(relationship_vector, dict) else {}

    async def _get_chat_state_cols(self, db) -> list:
        now = monotonic()
        if self._chat_state_cols_cache is None or (now - self._chat_state_cols_ts) > self._COL_CACHE_TTL_SEC:
            cursor = await db.execute("PRAGMA table_info(chat_states)")
            cols_info = await cursor.fetchall()
            self._chat_state_cols_cache = [c[1] for c in cols_info]
            self._chat_state_cols_ts = now
        return self._chat_state_cols_cache

    async def _get_user_profile_cols(self, db) -> list:
        now = monotonic()
        if self._user_profile_cols_cache is None or (now - self._user_profile_cols_ts) > self._COL_CACHE_TTL_SEC:
            cursor = await db.execute("PRAGMA table_info(user_profiles)")
            cols_info = await cursor.fetchall()
            self._user_profile_cols_cache = [c[1] for c in cols_info]
            self._user_profile_cols_ts = now
        return self._user_profile_cols_cache

    async def load_chat_state(self, chat_id: str) -> Optional[ChatState]:
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            if row:
                actual_col_names = [col[0] for col in (cursor.description or [])]
                now = time.time()
                if not actual_col_names or len(actual_col_names) != len(row):
                    actual_col_names = await self._get_chat_state_cols(db)
                if (
                    self._chat_state_cols_cache is None
                    or (now - self._chat_state_cols_ts) > self._COL_CACHE_TTL_SEC
                    or len(self._chat_state_cols_cache) != len(actual_col_names)
                    or self._chat_state_cols_cache != actual_col_names
                ):
                    self._chat_state_cols_cache = list(actual_col_names)
                    self._chat_state_cols_ts = now
                row_dict = dict(zip(actual_col_names, row))
                last_msg_info_raw = self._safe_json_loads(row_dict.get("last_msg_info"), {"sender_id": "", "has_image": False, "image_urls": [], "vl_executed": False})
                state = ChatState(
                    chat_id=str(row_dict.get("chat_id", chat_id) or chat_id),
                    energy=float(row_dict.get("energy", 0.5) or 0.5),
                    mood=float(row_dict.get("mood", 0.0) or 0.0),
                )
                state.group_config = self._safe_json_loads(row_dict.get("group_config"))
                state.last_reset_date = str(row_dict.get("last_reset_date", "") or "")
                state.total_replies = int(row_dict.get("total_replies") or 0)
                state.last_reply_time = float(row_dict.get("last_reply_time") or 0.0)
                state.last_passive_decay_time = float(row_dict.get("last_passive_decay_time") or 0.0)
                state.last_energy_recovery_time = float(row_dict.get("last_energy_recovery_time") or 0.0)
                state.total_messages = int(row_dict.get("total_messages") or 0)
                state.judgment_mode = str(row_dict.get("judgment_mode", "single") or "single")
                state.last_msg_info = LastMessageMetadata(
                    sender_id=last_msg_info_raw.get("sender_id", ""),
                    has_image=bool(last_msg_info_raw.get("has_image", False)),
                    image_urls=last_msg_info_raw.get("image_urls", []),
                    vl_executed=bool(last_msg_info_raw.get("vl_executed", False)),
                )
                state.last_access_time = float(row_dict.get("last_access_time") or 0.0)
                state.next_wakeup_timestamp = float(row_dict.get("next_wakeup_timestamp") or 0.0)
                state.chat_kind = str(row_dict.get("chat_kind", "") or "")
                state.last_real_user_activity_at = float(
                    row_dict.get("last_real_user_activity_at") or state.last_reply_time or 0.0
                )
                state.last_committed_bot_reply_at = float(
                    row_dict.get("last_committed_bot_reply_at") or state.last_reply_time or 0.0
                )
                state.next_proactive_due_at = float(
                    row_dict.get("next_proactive_due_at") or state.next_wakeup_timestamp or 0.0
                )
                state.proactive_generation = int(row_dict.get("proactive_generation") or 0)
                state.unanswered_proactive_count = int(row_dict.get("unanswered_proactive_count") or 0)
                state.last_proactive_commit_id = str(row_dict.get("last_proactive_commit_id", "") or "")
                state.last_proactive_cancel_reason = str(row_dict.get("last_proactive_cancel_reason", "") or "")
                state.proactive_claim_token = str(row_dict.get("proactive_claim_token", "") or "")
                state.proactive_claimed_at = float(row_dict.get("proactive_claimed_at") or 0.0)
                state.is_dirty = bool(row_dict.get("is_dirty") or False)
                return state
        return None

    async def save_chat_state(self, chat_id: str, state: ChatState):
        config_json = json.dumps(state.group_config, ensure_ascii=False)
        last_msg_info = getattr(state, "last_msg_info", None)
        last_msg_info_json = json.dumps(
            {
                "sender_id": last_msg_info.sender_id if last_msg_info else "",
                "has_image": last_msg_info.has_image if last_msg_info else False,
                "image_urls": last_msg_info.image_urls if last_msg_info else [],
                "vl_executed": last_msg_info.vl_executed if last_msg_info else False,
            },
            ensure_ascii=False,
        )
        async with connect_aiosqlite(self.db_path) as db:
            now = time.time()
            await db.execute("""
                INSERT INTO chat_states
                (chat_id, energy, mood, group_config, last_reset_date, total_replies, last_reply_time, last_passive_decay_time, last_energy_recovery_time, total_messages, judgment_mode, last_msg_info, last_access_time, next_wakeup_timestamp, chat_kind, last_real_user_activity_at, last_committed_bot_reply_at, next_proactive_due_at, proactive_generation, unanswered_proactive_count, last_proactive_commit_id, last_proactive_cancel_reason, proactive_claim_token, proactive_claimed_at, is_dirty, updated_at)
                VALUES (:chat_id, :energy, :mood, :group_config, :last_reset_date, :total_replies, :last_reply_time, :last_passive_decay_time, :last_energy_recovery_time, :total_messages, :judgment_mode, :last_msg_info, :last_access_time, :next_wakeup_timestamp, :chat_kind, :last_real_user_activity_at, :last_committed_bot_reply_at, :next_proactive_due_at, :proactive_generation, :unanswered_proactive_count, :last_proactive_commit_id, :last_proactive_cancel_reason, :proactive_claim_token, :proactive_claimed_at, :is_dirty, :updated_at)
                ON CONFLICT(chat_id) DO UPDATE SET
                    energy = :energy,
                    mood = :mood,
                    group_config = :group_config,
                    last_reset_date = :last_reset_date,
                    total_replies = :total_replies,
                    last_reply_time = :last_reply_time,
                    last_passive_decay_time = :last_passive_decay_time,
                    last_energy_recovery_time = :last_energy_recovery_time,
                    total_messages = :total_messages,
                    judgment_mode = :judgment_mode,
                    last_msg_info = :last_msg_info,
                    last_access_time = :last_access_time,
                    next_wakeup_timestamp = :next_wakeup_timestamp,
                    chat_kind = :chat_kind,
                    last_real_user_activity_at = :last_real_user_activity_at,
                    last_committed_bot_reply_at = :last_committed_bot_reply_at,
                    next_proactive_due_at = :next_proactive_due_at,
                    proactive_generation = :proactive_generation,
                    unanswered_proactive_count = :unanswered_proactive_count,
                    last_proactive_commit_id = :last_proactive_commit_id,
                    last_proactive_cancel_reason = :last_proactive_cancel_reason,
                    proactive_claim_token = :proactive_claim_token,
                    proactive_claimed_at = :proactive_claimed_at,
                    is_dirty = :is_dirty,
                    updated_at = :updated_at
            """, {
                "chat_id": chat_id,
                "energy": state.energy,
                "mood": state.mood,
                "group_config": config_json,
                "last_reset_date": state.last_reset_date,
                "total_replies": state.total_replies,
                "last_reply_time": float(getattr(state, "last_reply_time", 0.0) or 0.0),
                "last_passive_decay_time": float(getattr(state, "last_passive_decay_time", 0.0) or 0.0),
                "last_energy_recovery_time": float(getattr(state, "last_energy_recovery_time", 0.0) or 0.0),
                "total_messages": int(getattr(state, "total_messages", 0) or 0),
                "judgment_mode": str(getattr(state, "judgment_mode", "single") or "single"),
                "last_msg_info": last_msg_info_json,
                "last_access_time": float(getattr(state, "last_access_time", 0.0) or 0.0),
                "next_wakeup_timestamp": float(getattr(state, "next_wakeup_timestamp", 0.0) or 0.0),
                "chat_kind": str(getattr(state, "chat_kind", "") or ""),
                "last_real_user_activity_at": float(getattr(state, "last_real_user_activity_at", 0.0) or 0.0),
                "last_committed_bot_reply_at": float(getattr(state, "last_committed_bot_reply_at", 0.0) or 0.0),
                "next_proactive_due_at": float(getattr(state, "next_proactive_due_at", 0.0) or 0.0),
                "proactive_generation": int(getattr(state, "proactive_generation", 0) or 0),
                "unanswered_proactive_count": int(getattr(state, "unanswered_proactive_count", 0) or 0),
                "last_proactive_commit_id": str(getattr(state, "last_proactive_commit_id", "") or ""),
                "last_proactive_cancel_reason": str(getattr(state, "last_proactive_cancel_reason", "") or ""),
                "proactive_claim_token": str(getattr(state, "proactive_claim_token", "") or ""),
                "proactive_claimed_at": float(getattr(state, "proactive_claimed_at", 0.0) or 0.0),
                "is_dirty": int(getattr(state, "is_dirty", False) or False),
                "updated_at": now,
            })
            await db.commit()

    async def load_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                col_names = await self._get_user_profile_cols(db)
                row_dict = dict(zip(col_names, row))
                profile_metadata = self._safe_json_loads(row_dict.get("profile_metadata"))
                return {
                    "user_id": row_dict.get("user_id", ""),
                    "name": row_dict.get("name", "Unknown"),
                    "social_score": row_dict.get("social_score", 0.0),
                    "last_seen": row_dict.get("last_seen", 0.0),
                    "persona_analysis": row_dict.get("persona_analysis", ""),
                    "message_count_for_profiling": int(row_dict.get("message_count_for_profiling") or 0),
                    "last_persona_gen_time": float(row_dict.get("last_persona_gen_time") or 0.0),
                    "group_footprints": self._safe_json_loads(row_dict.get("group_footprints")),
                    "profile_metadata": profile_metadata,
                    "relationship_vector": self._relationship_vector_from_metadata(profile_metadata),
                    "identity": row_dict.get("identity", ""),
                    "tags": self._safe_json_loads(row_dict.get("tags"), []),
                    # Phase 8.1: 
                    "nickname": row_dict.get("nickname", ""),
                    "nickname_reason": row_dict.get("nickname_reason", ""),
                    "know_times": int(row_dict.get("know_times") or 0),
                    "is_known": bool(row_dict.get("is_known") or False),
                    "identity_points": self._safe_json_loads(row_dict.get("identity_points"), []),
                    "preference_points": self._safe_json_loads(row_dict.get("preference_points"), []),
                    "relationship_points": self._safe_json_loads(row_dict.get("relationship_points"), []),
                    "speech_style_points": self._safe_json_loads(row_dict.get("speech_style_points"), []),
                    "memory_points": self._safe_json_loads(row_dict.get("memory_points"), []),
                }
        return None

    def load_all_user_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Load all user profiles into a structured dictionary."""
        profiles: Dict[str, Dict[str, Any]] = {}
        with connect_sqlite(self.db_path) as conn:
            row_cursor = conn.execute("SELECT * FROM user_profiles")
            rows = row_cursor.fetchall()
            cols_cursor = conn.execute("PRAGMA table_info(user_profiles)")
            col_names = [col[1] for col in cols_cursor.fetchall()]

        for row in rows:
            row_dict = dict(zip(col_names, row))
            user_id = row_dict.get("user_id", "")
            if not user_id:
                continue
            profile_metadata = self._safe_json_loads(row_dict.get("profile_metadata"))
            profiles[user_id] = {
                "user_id": user_id,
                "name": row_dict.get("name", "Unknown"),
                "social_score": row_dict.get("social_score", 0.0),
                "last_seen": row_dict.get("last_seen", 0.0),
                "persona_analysis": row_dict.get("persona_analysis", ""),
                "message_count_for_profiling": int(row_dict.get("message_count_for_profiling") or 0),
                "last_persona_gen_time": float(row_dict.get("last_persona_gen_time") or 0.0),
                "group_footprints": self._safe_json_loads(row_dict.get("group_footprints")),
                "profile_metadata": profile_metadata,
                "relationship_vector": self._relationship_vector_from_metadata(profile_metadata),
                "identity": row_dict.get("identity", ""),
                "tags": self._safe_json_loads(row_dict.get("tags"), []),
                "nickname": row_dict.get("nickname", ""),
                "nickname_reason": row_dict.get("nickname_reason", ""),
                "know_times": int(row_dict.get("know_times") or 0),
                "is_known": bool(row_dict.get("is_known") or False),
                "memory_points": self._safe_json_loads(row_dict.get("memory_points"), []),
                "identity_points": self._safe_json_loads(row_dict.get("identity_points"), []),
                "preference_points": self._safe_json_loads(row_dict.get("preference_points"), []),
                "relationship_points": self._safe_json_loads(row_dict.get("relationship_points"), []),
                "speech_style_points": self._safe_json_loads(row_dict.get("speech_style_points"), []),
            }
        return profiles

    async def save_user_profile(self, profile: 'UserProfile'):
        footprints_json = json.dumps(profile.group_footprints, ensure_ascii=False)
        profile_metadata = dict(getattr(profile, "profile_metadata", {}) or {})
        relationship_vector = getattr(profile, "relationship_vector", {})
        if isinstance(relationship_vector, dict) and relationship_vector:
            profile_metadata["relationship_vector"] = dict(relationship_vector)
        profile_metadata_json = json.dumps(profile_metadata, ensure_ascii=False)
        tags_json = json.dumps(profile.tags, ensure_ascii=False)
        memory_points_json = json.dumps(profile.memory_points, ensure_ascii=False)
        identity_points_json = json.dumps(getattr(profile, "identity_points", []), ensure_ascii=False)
        preference_points_json = json.dumps(getattr(profile, "preference_points", []), ensure_ascii=False)
        relationship_points_json = json.dumps(getattr(profile, "relationship_points", []), ensure_ascii=False)
        speech_style_points_json = json.dumps(getattr(profile, "speech_style_points", []), ensure_ascii=False)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO user_profiles 
                (user_id, name, social_score, last_seen, persona_analysis, message_count_for_profiling,
                 last_persona_gen_time, group_footprints, profile_metadata,
                 identity, tags, nickname, nickname_reason, know_times, is_known,
                 memory_points, identity_points, preference_points, relationship_points,
                 speech_style_points, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (profile.user_id, profile.name, profile.social_score, profile.last_seen,
                  profile.persona_analysis, int(getattr(profile, "message_count_for_profiling", 0) or 0),
                  float(getattr(profile, "last_persona_gen_time", 0.0) or 0.0),
                  footprints_json, profile_metadata_json, profile.identity, tags_json,
                  profile.nickname, profile.nickname_reason,
                  profile.know_times, int(profile.is_known),
                  memory_points_json, identity_points_json, preference_points_json,
                  relationship_points_json, speech_style_points_json, time.time()))
            await db.commit()

    async def add_last_message_meta(self, chat_id: str, sender_id: str, has_image: bool, image_urls: list):
        """Persist last message metadata for multimodal context."""
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("""
                INSERT INTO lastmessagemetadatadb 
                (chat_id, sender_id, has_image, image_urls, vl_executed, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, sender_id, has_image, json.dumps(image_urls, ensure_ascii=False), False, time.time()))
            await db.commit()

    async def claim_profile_generation(
        self,
        user_id: str,
        *,
        lease_sec: float = 1800.0,
        now: float | None = None,
    ) -> str | None:
        """Atomically claim one user's profile generation across task instances."""
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return None
        current = float(time.time() if now is None else now)
        token = uuid4().hex
        effective_lease_sec = max(1.0, float(lease_sec or 0.0))
        claimed_until = current + effective_lease_sec
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO profile_generation_claims (user_id, claim_token, claimed_until)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    claim_token = excluded.claim_token,
                    claimed_until = excluded.claimed_until
                WHERE profile_generation_claims.claimed_until <= ?
                """,
                (normalized_user_id, token, claimed_until, current),
            )
            await db.commit()
            return token if int(cursor.rowcount or 0) == 1 else None

    async def release_profile_generation(self, user_id: str, claim_token: str) -> bool:
        normalized_user_id = str(user_id or "").strip()
        normalized_token = str(claim_token or "").strip()
        if not normalized_user_id or not normalized_token:
            return False
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM profile_generation_claims WHERE user_id = ? AND claim_token = ?",
                (normalized_user_id, normalized_token),
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1

    async def get_profile_generation_claim(
        self,
        user_id: str,
        *,
        now: float | None = None,
    ) -> str | None:
        """Return the current claim token, or None when no claim exists."""
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return None
        current = float(time.time() if now is None else now)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT claim_token FROM profile_generation_claims "
                "WHERE user_id = ? AND claimed_until > ?",
                (normalized_user_id, current),
            )
            row = await cursor.fetchone()
            return str(row[0]) if row else None

    async def list_due_chat_state_ids(
        self,
        *,
        now: float,
        limit: int = 200,
    ) -> list[str]:
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT chat_id
                FROM chat_states
                WHERE next_proactive_due_at > 0
                  AND next_proactive_due_at <= ?
                ORDER BY next_proactive_due_at ASC
                LIMIT ?
                """,
                (float(now), max(1, min(int(limit or 200), 1000))),
            )
            return [str(row[0]) for row in await cursor.fetchall() if row and row[0]]

    async def atomic_claim_proactive_due(
        self,
        chat_id: str,
        *,
        expected_generation: int,
        claim_token: str,
        now: float,
        lease_seconds: float,
    ) -> bool:
        lease_expired_at = float(now) - max(1.0, float(lease_seconds or 0.0))
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE chat_states
                SET proactive_claim_token = ?,
                    proactive_claimed_at = ?,
                    last_proactive_cancel_reason = '',
                    updated_at = ?
                WHERE chat_id = ?
                  AND proactive_generation = ?
                  AND next_proactive_due_at <= ?
                  AND (
                        proactive_claim_token = ''
                        OR proactive_claim_token IS NULL
                        OR proactive_claimed_at <= ?
                  )
                """,
                (
                    str(claim_token),
                    float(now),
                    float(now),
                    str(chat_id),
                    int(expected_generation),
                    float(now),
                    lease_expired_at,
                ),
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1

    async def atomic_settle_proactive_claim(
        self,
        chat_id: str,
        *,
        claim_token: str,
        next_due_at: float,
        cancel_reason: str,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE chat_states
                SET proactive_claim_token = '',
                    proactive_claimed_at = 0,
                    next_proactive_due_at = ?,
                    next_wakeup_timestamp = ?,
                    last_proactive_cancel_reason = ?,
                    updated_at = ?
                WHERE chat_id = ?
                  AND proactive_claim_token = ?
                """,
                (
                    float(next_due_at or 0.0),
                    float(next_due_at or 0.0),
                    str(cancel_reason or ""),
                    now,
                    str(chat_id),
                    str(claim_token or ""),
                ),
            )
            await db.commit()
            return int(cursor.rowcount or 0) == 1

    async def mark_last_message_vision_executed(self, chat_id: str, sender_id: str) -> None:
        """Mark the latest matching image message after the vision barrier ran."""
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                UPDATE lastmessagemetadatadb
                SET vl_executed = 1
                WHERE id = (
                    SELECT id FROM lastmessagemetadatadb
                    WHERE chat_id = ? AND sender_id = ? AND has_image = 1
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                )
                """,
                (chat_id, sender_id),
            )
            await db.commit()
