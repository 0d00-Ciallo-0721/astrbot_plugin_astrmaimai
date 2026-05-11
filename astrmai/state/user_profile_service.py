from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

from ..infrastructure.persistence.orm_models import UserProfile


class UserProfileService:
    def __init__(self, persistence: Any):
        import threading

        self.persistence = persistence
        self.user_profiles: Dict[str, UserProfile] = {}
        self._user_locks: Dict[str, asyncio.Lock] = {}
        self._pool_lock_mutex = threading.Lock()

    def _get_user_lock(self, user_id: str) -> asyncio.Lock:
        with self._pool_lock_mutex:
            lock = self._user_locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._user_locks[user_id] = lock
        return lock

    async def get_user_profile(self, user_id: str) -> UserProfile:
        async with self._get_user_lock(user_id):
            now = time.time()
            if user_id in self.user_profiles:
                profile = self.user_profiles[user_id]
                profile.last_access_time = now
                return profile

            data = await self.persistence.load_user_profile(user_id)
            if data:
                profile = UserProfile(**data)
            else:
                profile = UserProfile(user_id=user_id, name="未知用户")

            profile.last_access_time = now
            profile.is_dirty = True
            self.user_profiles[user_id] = profile
            return profile

    async def update_social_score(self, user_id: str, score: float) -> UserProfile:
        async with self._get_user_lock(user_id):
            now = time.time()
            profile = self.user_profiles.get(user_id)
            if profile is None:
                data = await self.persistence.load_user_profile(user_id)
                profile = UserProfile(**data) if data else UserProfile(user_id=user_id, name="未知用户")
                self.user_profiles[user_id] = profile
            profile.social_score = score
            profile.last_access_time = now
            profile.last_seen = now
            profile.is_dirty = True
            await self.persistence.save_user_profile(user_id, profile)
            return profile

    def get_active_profiles(self) -> List[UserProfile]:
        return list(self.user_profiles.values())

    async def increment_user_message_count(self, user_id: str) -> None:
        profile = await self.get_user_profile(user_id)
        profile.message_count = int(getattr(profile, "message_count", 0) or 0) + 1
        profile.is_dirty = True

    async def flush_message_counters(self) -> None:
        dirty_profiles = [profile for profile in self.user_profiles.values() if getattr(profile, "is_dirty", False)]
        for profile in dirty_profiles:
            await self.persistence.save_user_profile(profile.user_id, profile)
            profile.is_dirty = False
