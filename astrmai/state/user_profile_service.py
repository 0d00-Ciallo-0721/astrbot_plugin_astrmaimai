from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from ..infrastructure.persistence.orm_models import UserProfile


_DEFAULT_PROFILE_NAME = "未知用户"
_MAX_TAGS = 10
_MAX_MEMORY_POINTS = 12
_RECENT_MESSAGES_LIMIT = 6
_DEFAULT_POINT_WEIGHT = 0.5
_MIN_POINT_WEIGHT = 0.18
_POINT_DECAY = 0.85
_PLACEHOLDER_NAMES = {
    "",
    "unknown",
    "未知用户",
    "链煡鐢ㄦ埛",
    "该用户",
    "璇ョ敤鎴?",
}


class UserProfileService:
    def __init__(self, persistence: Any):
        import threading

        self.persistence = persistence
        self.user_profiles: Dict[str, UserProfile] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}
        # ponytail: threading.Lock guards dict mutations, held for trivial ops only — safe in asyncio
        self._pool_lock_mutex = threading.Lock()
        self._profiles_dict_lock = asyncio.Lock()

    def invalidate_cache(self, user_id: str = None):
        """ponytail: invalidate cached profile(s) after external modification"""
        if user_id:
            self.user_profiles.pop(user_id, None)
        else:
            self.user_profiles.clear()

    async def _flush_profile(self, user_id: str, profile):
        """ponytail: immediately persist a dirty profile"""
        if not profile.is_dirty:
            return
        try:
            await self.persistence.save_user_profile(profile)
            profile.is_dirty = False
        except Exception as exc:
            from astrbot.api import logger

            logger.warning(f"[AstrMai-profile] flush failed for {user_id}: {exc}")

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        with self._pool_lock_mutex:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
        return lock

    async def _save_profile(self, profile: UserProfile) -> None:
        try:
            await self.persistence.save_user_profile(profile)
        except TypeError:
            await self.persistence.save_user_profile(profile.user_id, profile)

    @staticmethod
    def _clean_text(value: Any) -> str:
        return str(value or "").strip()

    @classmethod
    def _is_placeholder_name(cls, value: Any) -> bool:
        text = cls._clean_text(value)
        if not text:
            return True
        lowered = text.lower()
        if lowered in _PLACEHOLDER_NAMES:
            return True
        if text.startswith("群友") and len(text) <= 6:
            return True
        return False

    @staticmethod
    def _profile_metadata(profile: UserProfile) -> Dict[str, Any]:
        meta = getattr(profile, "profile_metadata", None)
        if not isinstance(meta, dict):
            meta = {}
            profile.profile_metadata = meta
        return meta

    @classmethod
    def _manual_locks(cls, profile: UserProfile) -> set[str]:
        meta = cls._profile_metadata(profile)
        raw = meta.get("manual_locked_fields", [])
        if not isinstance(raw, list):
            raw = []
            meta["manual_locked_fields"] = raw
        return {str(item).strip() for item in raw if str(item).strip()}

    @classmethod
    def is_manual_locked(cls, profile: UserProfile, field: str) -> bool:
        return field in cls._manual_locks(profile)

    @classmethod
    def set_manual_lock(cls, profile: UserProfile, field: str, locked: bool = True) -> None:
        meta = cls._profile_metadata(profile)
        locks = cls._manual_locks(profile)
        if locked:
            locks.add(field)
        else:
            locks.discard(field)
        meta["manual_locked_fields"] = sorted(locks)

    @classmethod
    def mark_manual_fields(cls, profile: UserProfile, fields: List[str]) -> None:
        for field in fields:
            cls.set_manual_lock(profile, field, True)

    @classmethod
    def _is_auto_nickname(cls, profile: UserProfile) -> bool:
        meta = cls._profile_metadata(profile)
        return str(meta.get("nickname_origin", "") or "").strip() != "manual"

    def _touch_profile(self, profile: UserProfile, *, now: float | None = None) -> None:
        # ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic
        ts = now if now is not None else time.time()
        profile.last_access_time = ts
        profile.last_seen = ts
        profile.is_dirty = True

    async def get_user_profile(self, user_id: str) -> UserProfile:
        async with self._get_user_lock(user_id):
            # ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic
            now = time.time()
            if user_id in self.user_profiles:
                profile = self.user_profiles[user_id]
                profile.last_access_time = now
                return profile

            data = await self.persistence.load_user_profile(user_id)
            if data:
                try:
                    profile = UserProfile(**data)
                except Exception as exc:
                    from astrbot.api import logger

                    logger.warning(
                        f"[AstrMai] UserProfile construction failed for {user_id}, "
                        f"falling back to default: {exc}"
                    )
                    profile = UserProfile(user_id=user_id, name=_DEFAULT_PROFILE_NAME)
            else:
                profile = UserProfile(user_id=user_id, name=_DEFAULT_PROFILE_NAME)

            if not self._clean_text(profile.name):
                profile.name = _DEFAULT_PROFILE_NAME
            profile.last_access_time = now
            profile.is_dirty = False
            self._migrate_relationship_vector(profile)
            self.user_profiles[user_id] = profile
            return profile

    @staticmethod
    def _migrate_relationship_vector(profile: UserProfile) -> None:
        """单向迁移：将 profile_metadata 中的旧 relationship_vector 移至字段。"""
        meta = profile.profile_metadata if isinstance(profile.profile_metadata, dict) else {}
        if "relationship_vector" in meta:
            if not profile.relationship_vector:
                profile.relationship_vector = meta.pop("relationship_vector")
            else:
                meta.pop("relationship_vector", None)

    async def update_social_score(
        self,
        user_id: str,
        score: float,
        relationship_vector: dict = None,
        *,
        touch_activity: bool = True,
    ) -> UserProfile:
        async with self._get_user_lock(user_id):
            now = time.time()
            profile = self.user_profiles.get(user_id)
            if profile is None:
                data = await self.persistence.load_user_profile(user_id)
                if data:
                    try:
                        profile = UserProfile(**data)
                    except Exception as exc:
                        from astrbot.api import logger

                        logger.warning(
                            f"[AstrMai] UserProfile construction failed for {user_id} "
                            f"in update_social_score, falling back to default: {exc}"
                        )
                        profile = UserProfile(user_id=user_id, name=_DEFAULT_PROFILE_NAME)
                else:
                    profile = UserProfile(user_id=user_id, name=_DEFAULT_PROFILE_NAME)
                self.user_profiles[user_id] = profile
            profile.social_score = score
            if relationship_vector:
                profile.relationship_vector = relationship_vector
            if touch_activity:
                self._touch_profile(profile, now=now)
            else:
                profile.is_dirty = True
            await self._save_profile(profile)
            return profile

    def get_active_profiles(self) -> List[UserProfile]:
        return list(self.user_profiles.values())

    async def apply_profile_name(self, user_id: str, new_name: str, *, source: str = "event") -> bool:
        cleaned = self._clean_text(new_name)
        if self._is_placeholder_name(cleaned):
            return False
        profile = await self.get_user_profile(user_id)
        async with self._get_user_lock(user_id):
            if self.is_manual_locked(profile, "name"):
                return False
            current = self._clean_text(profile.name)
            if cleaned == current:
                return False
            if current and not self._is_placeholder_name(current) and len(cleaned) < 2:
                return False
            profile.name = cleaned
            meta = self._profile_metadata(profile)
            meta["last_name_source"] = source
            self._touch_profile(profile)
            return True

    @classmethod
    def _ensure_footprint(cls, profile: UserProfile, chat_id: str) -> Dict[str, Any]:
        chat_key = cls._clean_text(chat_id) or "GLOBAL"
        footprints = getattr(profile, "group_footprints", None)
        if not isinstance(footprints, dict):
            footprints = {}
            profile.group_footprints = footprints
        footprint = footprints.get(chat_key)
        if not isinstance(footprint, dict):
            footprint = {}
            footprints[chat_key] = footprint
        footprint.setdefault("chat_id", chat_key)
        footprint.setdefault("recent_messages", [])
        footprint.setdefault("message_count", 0)
        footprint.setdefault("learning_touch_count", 0)
        footprint.setdefault("top_speaker_hits", 0)
        footprint.setdefault("private_touch_count", 0)
        return footprint

    async def _get_profile_inner(self, user_id: str) -> UserProfile:
        """Re-read profile without acquiring lock — caller must hold _get_user_lock."""
        if user_id in self.user_profiles:
            return self.user_profiles[user_id]
        profile = await self._load_profile(user_id)
        self.user_profiles[user_id] = profile
        return profile

    async def observe_user_activity(
        self,
        user_id: str,
        *,
        chat_id: str = "",
        sender_name: str = "",
        content: str = "",
        source: str = "message",
    ) -> None:
        profile = await self.get_user_profile(user_id)
        async with self._get_user_lock(user_id):
            # ponytail: re-read profile under lock to prevent TOCTOU (R20)
            profile = await self._get_profile_inner(user_id)
            cleaned_name = self._clean_text(sender_name)
            if cleaned_name and not self.is_manual_locked(profile, "name") and not self._is_placeholder_name(cleaned_name):
                current = self._clean_text(profile.name)
                if self._is_placeholder_name(current) or len(cleaned_name) >= len(current):
                    profile.name = cleaned_name
            footprint = self._ensure_footprint(profile, chat_id)
            footprint["last_source"] = source
            footprint["last_seen_at"] = time.time()
            footprint["message_count"] = int(footprint.get("message_count", 0) or 0) + 1
            if cleaned_name:
                footprint["display_name"] = cleaned_name
            snippet = self._clean_text(content)
            if snippet:
                recent = [item for item in footprint.get("recent_messages", []) if isinstance(item, dict)]
                if not recent or recent[-1].get("text") != snippet[:120]:
                    recent.append({"text": snippet[:120], "at": time.time()})
                footprint["recent_messages"] = recent[-_RECENT_MESSAGES_LIMIT:]
            self._touch_profile(profile)
            await self._flush_profile(user_id, profile)

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
        profile = await self.get_user_profile(user_id)
        async with self._get_user_lock(user_id):
            cleaned_name = self._clean_text(sender_name)
            if cleaned_name and not self.is_manual_locked(profile, "name") and not self._is_placeholder_name(cleaned_name):
                current = self._clean_text(profile.name)
                if self._is_placeholder_name(current) or len(cleaned_name) >= len(current):
                    profile.name = cleaned_name
            weight_value = max(1, int(round(float(weight or 1.0))))
            profile.message_count_for_profiling = int(getattr(profile, "message_count_for_profiling", 0) or 0) + weight_value
            if increment_know_times:
                profile.know_times = int(getattr(profile, "know_times", 0) or 0) + 1
            footprint = self._ensure_footprint(profile, chat_id)
            footprint["last_learning_source"] = source
            footprint["last_learning_at"] = time.time()
            footprint["learning_touch_count"] = int(footprint.get("learning_touch_count", 0) or 0) + weight_value
            if source == "group_periodic":
                footprint["top_speaker_hits"] = int(footprint.get("top_speaker_hits", 0) or 0) + 1
            if source == "private_reply":
                footprint["private_touch_count"] = int(footprint.get("private_touch_count", 0) or 0) + 1
            meta = self._profile_metadata(profile)
            meta["total_learning_touches"] = int(meta.get("total_learning_touches", 0) or 0) + weight_value
            self._touch_profile(profile)

    @classmethod
    def _normalize_tags(cls, tags: Any) -> List[str]:
        if isinstance(tags, str):
            raw_items = [item.strip() for item in tags.split(",")]
        elif isinstance(tags, list):
            raw_items = [str(item).strip() for item in tags]
        else:
            raw_items = []
        result: List[str] = []
        seen: set[str] = set()
        for item in raw_items:
            if not item:
                continue
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            result.append(item)
            if len(result) >= _MAX_TAGS:
                break
        return result

    def merge_tags(self, existing_tags: Any, new_tags: Any) -> List[str]:
        merged: List[str] = []
        seen: set[str] = set()
        for item in self._normalize_tags(new_tags) + self._normalize_tags(existing_tags):
            lowered = item.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            merged.append(item)
            if len(merged) >= _MAX_TAGS:
                break
        return merged

    @staticmethod
    def _format_point(category: str, content: str, weight: float) -> str:
        safe_category = category.strip() or "其他"
        safe_content = content.strip()
        if not safe_content:
            return ""
        return f"{safe_category}:{safe_content}:{weight:.2f}"

    @classmethod
    def _parse_memory_point(cls, raw_point: Any) -> Dict[str, Any] | None:
        text = cls._clean_text(raw_point)
        if not text:
            return None
        parts = text.split(":", 2)
        category = parts[0].strip() if parts else "其他"
        content = parts[1].strip() if len(parts) > 1 else text
        raw_weight = parts[2] if len(parts) > 2 else _DEFAULT_POINT_WEIGHT
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            weight = _DEFAULT_POINT_WEIGHT
        content = content.strip()
        if not content:
            return None
        return {
            "category": category or "其他",
            "content": content,
            "weight": max(0.0, min(1.0, weight)),
        }

    @staticmethod
    def _point_key(point: Dict[str, Any]) -> tuple[str, str]:
        return (
            str(point.get("category", "其他") or "其他").strip().lower(),
            str(point.get("content", "") or "").strip().lower(),
        )

    def merge_memory_points(self, existing_points: Any, new_points: Any) -> List[str]:
        merged: Dict[tuple[str, str], Dict[str, Any]] = {}
        for raw in list(existing_points or []):
            parsed = self._parse_memory_point(raw)
            if not parsed:
                continue
            parsed["weight"] = max(_MIN_POINT_WEIGHT, parsed["weight"] * _POINT_DECAY)
            merged[self._point_key(parsed)] = parsed

        for raw in list(new_points or []):
            parsed = self._parse_memory_point(raw)
            if not parsed:
                continue
            key = self._point_key(parsed)
            if key in merged:
                merged[key]["weight"] = max(float(merged[key].get("weight", _DEFAULT_POINT_WEIGHT) or _DEFAULT_POINT_WEIGHT), parsed["weight"])
                merged[key]["category"] = parsed["category"]
                merged[key]["content"] = parsed["content"]
            else:
                merged[key] = parsed

        ordered = sorted(
            (item for item in merged.values() if float(item.get("weight", 0.0) or 0.0) >= _MIN_POINT_WEIGHT),
            key=lambda item: float(item.get("weight", 0.0) or 0.0),
            reverse=True,
        )
        result: List[str] = []
        for item in ordered[:_MAX_MEMORY_POINTS]:
            formatted = self._format_point(
                str(item.get("category", "其他") or "其他"),
                str(item.get("content", "") or ""),
                float(item.get("weight", _DEFAULT_POINT_WEIGHT) or _DEFAULT_POINT_WEIGHT),
            )
            if formatted:
                result.append(formatted)
        return result

    def categorize_memory_points(self, memory_points: Any) -> Dict[str, List[str]]:
        buckets = {
            "identity_points": [],
            "preference_points": [],
            "relationship_points": [],
            "speech_style_points": [],
            "other_points": [],
        }
        for raw_point in list(memory_points or []):
            parsed = self._parse_memory_point(raw_point)
            if not parsed:
                continue
            normalized = f"{parsed['category']}:{parsed['content']}".strip()
            if parsed["category"] in {"身份", "经历", "技能"}:
                buckets["identity_points"].append(normalized)
            elif parsed["category"] in {"爱好", "偏好"}:
                buckets["preference_points"].append(normalized)
            elif parsed["category"] in {"关系", "互动"}:
                buckets["relationship_points"].append(normalized)
            elif parsed["category"] in {"表达", "说话", "口癖", "语气", "speech", "style", "琛ㄨ揪"}:
                buckets["speech_style_points"].append(normalized)
            else:
                buckets["other_points"].append(normalized)
        for key, values in buckets.items():
            unique: List[str] = []
            seen: set[str] = set()
            for item in values:
                lowered = item.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                unique.append(item)
            buckets[key] = unique[:6]
        return buckets

    def build_recent_interaction_summary(self, profile: UserProfile, *, max_items: int = 6) -> str:
        events: List[tuple[float, str]] = []
        for footprint in (getattr(profile, "group_footprints", {}) or {}).values():
            if not isinstance(footprint, dict):
                continue
            for item in footprint.get("recent_messages", []) or []:
                if not isinstance(item, dict):
                    continue
                text = self._clean_text(item.get("text", ""))
                if not text:
                    continue
                try:
                    ts = float(item.get("at", 0.0) or 0.0)
                except (TypeError, ValueError):
                    ts = 0.0
                events.append((ts, text))
        if not events:
            return ""
        events.sort(key=lambda item: item[0], reverse=True)
        lines = [f"- {text}" for _, text in events[:max_items]]
        return "\n".join(lines)

    def refresh_profile_from_generation(
        self,
        profile: UserProfile,
        *,
        analysis: str,
        tags: Any,
        memory_points: Any,
        source: str = "profile_generation",
    ) -> UserProfile:
        cleaned_analysis = self._clean_text(analysis)
        if cleaned_analysis and len(cleaned_analysis) >= 12 and not self.is_manual_locked(profile, "persona_analysis"):
            profile.persona_analysis = cleaned_analysis

        if not self.is_manual_locked(profile, "tags"):
            profile.tags = self.merge_tags(getattr(profile, "tags", []), tags)

        if not self.is_manual_locked(profile, "memory_points"):
            profile.memory_points = self.merge_memory_points(getattr(profile, "memory_points", []), memory_points)

        categorized = self.categorize_memory_points(getattr(profile, "memory_points", []))
        for field in ("identity_points", "preference_points", "relationship_points", "speech_style_points"):
            if self.is_manual_locked(profile, field):
                continue
            setattr(profile, field, categorized.get(field, []))

        meta = self._profile_metadata(profile)
        meta["last_refresh_source"] = source
        meta["last_refresh_at"] = time.time()
        profile.message_count_for_profiling = 0
        profile.last_persona_gen_time = time.time()
        self._touch_profile(profile)
        return profile

    def can_auto_update_nickname(self, profile: UserProfile) -> bool:
        if self.is_manual_locked(profile, "nickname"):
            return False
        nickname = self._clean_text(getattr(profile, "nickname", ""))
        if not nickname:
            return True
        return self._is_auto_nickname(profile)

    def set_auto_nickname(self, profile: UserProfile, nickname: str, reason: str) -> bool:
        cleaned = self._clean_text(nickname)
        if not cleaned or not self.can_auto_update_nickname(profile):
            return False
        profile.nickname = cleaned
        profile.nickname_reason = self._clean_text(reason)
        profile.is_known = True
        meta = self._profile_metadata(profile)
        meta["nickname_origin"] = "auto"
        meta["last_nickname_gen_time"] = time.time()
        self._touch_profile(profile)
        return True

    def get_profile_prompt_bundle(self, profile: UserProfile) -> Dict[str, Any]:
        nickname = self._clean_text(getattr(profile, "nickname", ""))
        raw_name = self._clean_text(getattr(profile, "name", "")) or _DEFAULT_PROFILE_NAME
        display_name = f"{nickname}（{raw_name}）" if nickname else raw_name
        tags = [str(item).strip() for item in (getattr(profile, "tags", []) or []) if str(item).strip()]
        analysis = self._clean_text(getattr(profile, "persona_analysis", "")) or "暂无深度侧写。"
        memory_points = [str(item).strip() for item in (getattr(profile, "memory_points", []) or []) if str(item).strip()][:6]
        structured_sections = []
        for key, label in (
            ("identity_points", "身份画像"),
            ("preference_points", "偏好画像"),
            ("relationship_points", "关系画像"),
            ("speech_style_points", "表达画像"),
        ):
            values = [str(item).strip() for item in (getattr(profile, key, []) or []) if str(item).strip()][:4]
            if values:
                structured_sections.append({"label": label, "values": values})
        return {
            "display_name": display_name,
            "tags_text": " / ".join(tags) if tags else "暂无标签",
            "analysis": analysis,
            "memory_points": memory_points,
            "structured_sections": structured_sections,
        }

    async def get_profile_prompt_bundle_for_user(self, user_id: str) -> Dict[str, Any]:
        profile = await self.get_user_profile(user_id)
        return self.get_profile_prompt_bundle(profile)

    async def flush_message_counters(self) -> None:
        async with self._profiles_dict_lock:
            dirty_user_ids = [
                user_id
                for user_id, profile in self.user_profiles.items()
                if getattr(profile, "is_dirty", False)
            ]
        for user_id in dirty_user_ids:
            async with self._get_user_lock(user_id):
                profile = self.user_profiles.get(user_id)
                if profile is None or not getattr(profile, "is_dirty", False):
                    continue
                await self._save_profile(profile)
                profile.is_dirty = False
