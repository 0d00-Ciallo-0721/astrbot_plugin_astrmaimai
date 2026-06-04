from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional

import aiosqlite

from .orm_models import ChatState


class StateProfilePersistenceMixin:
    @staticmethod
    def _relationship_vector_from_metadata(profile_metadata: Any) -> Dict[str, Any]:
        if not isinstance(profile_metadata, dict):
            return {}
        relationship_vector = profile_metadata.get("relationship_vector", {})
        return dict(relationship_vector) if isinstance(relationship_vector, dict) else {}

    async def load_chat_state(self, chat_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            if row:
                cursor2 = await db.execute("PRAGMA table_info(chat_states)")
                cols_info = await cursor2.fetchall()
                col_names = [c[1] for c in cols_info]
                row_dict = dict(zip(col_names, row))
                return {
                    "chat_id": row_dict.get("chat_id", chat_id),
                    "energy": row_dict.get("energy", 0.5),
                    "mood": row_dict.get("mood", 0.0),
                    "group_config": json.loads(row_dict.get("group_config") or "{}"),
                    "last_reset_date": row_dict.get("last_reset_date", ""),
                    "total_replies": int(row_dict.get("total_replies") or 0),
                    "last_reply_time": float(row_dict.get("last_reply_time") or 0.0),
                    "last_passive_decay_time": float(row_dict.get("last_passive_decay_time") or 0.0),
                }
        return None

    async def save_chat_state(self, chat_id: str, state: ChatState):
        config_json = json.dumps(state.group_config, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO chat_states 
                (chat_id, energy, mood, group_config, last_reset_date, total_replies, last_reply_time, last_passive_decay_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                chat_id,
                state.energy,
                state.mood,
                config_json,
                state.last_reset_date,
                state.total_replies,
                float(getattr(state, "last_reply_time", 0.0) or 0.0),
                float(getattr(state, "last_passive_decay_time", 0.0) or 0.0),
                time.time(),
            ))
            await db.commit()

    async def load_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                cursor2 = await db.execute("PRAGMA table_info(user_profiles)")
                cols_info = await cursor2.fetchall()
                col_names = [c[1] for c in cols_info]
                row_dict = dict(zip(col_names, row))
                profile_metadata = json.loads(row_dict.get("profile_metadata") or "{}")
                return {
                    "user_id": row_dict.get("user_id", ""),
                    "name": row_dict.get("name", "Unknown"),
                    "social_score": row_dict.get("social_score", 0.0),
                    "last_seen": row_dict.get("last_seen", 0.0),
                    "persona_analysis": row_dict.get("persona_analysis", ""),
                    "message_count_for_profiling": int(row_dict.get("message_count_for_profiling") or 0),
                    "last_persona_gen_time": float(row_dict.get("last_persona_gen_time") or 0.0),
                    "group_footprints": json.loads(row_dict.get("group_footprints") or "{}"),
                    "profile_metadata": profile_metadata,
                    "relationship_vector": self._relationship_vector_from_metadata(profile_metadata),
                    "identity": row_dict.get("identity", ""),
                    "tags": json.loads(row_dict.get("tags") or "[]"),
                    # Phase 8.1: 
                    "nickname": row_dict.get("nickname", ""),
                    "nickname_reason": row_dict.get("nickname_reason", ""),
                    "know_times": int(row_dict.get("know_times") or 0),
                    "is_known": bool(row_dict.get("is_known") or False),
                    "identity_points": json.loads(row_dict.get("identity_points") or "[]"),
                    "preference_points": json.loads(row_dict.get("preference_points") or "[]"),
                    "relationship_points": json.loads(row_dict.get("relationship_points") or "[]"),
                    "speech_style_points": json.loads(row_dict.get("speech_style_points") or "[]"),
                    "memory_points": json.loads(row_dict.get("memory_points") or "[]"),
                }
        return None

    def load_all_user_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Load all user profiles into a structured dictionary."""
        profiles: Dict[str, Dict[str, Any]] = {}
        with sqlite3.connect(self.db_path) as conn:
            row_cursor = conn.execute("SELECT * FROM user_profiles")
            rows = row_cursor.fetchall()
            cols_cursor = conn.execute("PRAGMA table_info(user_profiles)")
            col_names = [col[1] for col in cols_cursor.fetchall()]

        for row in rows:
            row_dict = dict(zip(col_names, row))
            user_id = row_dict.get("user_id", "")
            if not user_id:
                continue
            profile_metadata = json.loads(row_dict.get("profile_metadata") or "{}")
            profiles[user_id] = {
                "user_id": user_id,
                "name": row_dict.get("name", "Unknown"),
                "social_score": row_dict.get("social_score", 0.0),
                "last_seen": row_dict.get("last_seen", 0.0),
                "persona_analysis": row_dict.get("persona_analysis", ""),
                "message_count_for_profiling": int(row_dict.get("message_count_for_profiling") or 0),
                "last_persona_gen_time": float(row_dict.get("last_persona_gen_time") or 0.0),
                "group_footprints": json.loads(row_dict.get("group_footprints") or "{}"),
                "profile_metadata": profile_metadata,
                "relationship_vector": self._relationship_vector_from_metadata(profile_metadata),
                "identity": row_dict.get("identity", ""),
                "tags": json.loads(row_dict.get("tags") or "[]"),
                "nickname": row_dict.get("nickname", ""),
                "nickname_reason": row_dict.get("nickname_reason", ""),
                "know_times": int(row_dict.get("know_times") or 0),
                "is_known": bool(row_dict.get("is_known") or False),
                "memory_points": json.loads(row_dict.get("memory_points") or "[]"),
                "identity_points": json.loads(row_dict.get("identity_points") or "[]"),
                "preference_points": json.loads(row_dict.get("preference_points") or "[]"),
                "relationship_points": json.loads(row_dict.get("relationship_points") or "[]"),
                "speech_style_points": json.loads(row_dict.get("speech_style_points") or "[]"),
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
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO lastmessagemetadatadb 
                (chat_id, sender_id, has_image, image_urls, vl_executed, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, sender_id, has_image, json.dumps(image_urls), False, time.time()))
            await db.commit()
