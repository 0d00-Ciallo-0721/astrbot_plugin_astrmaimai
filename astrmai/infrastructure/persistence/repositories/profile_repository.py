from __future__ import annotations


class ProfileRepository:
    def __init__(self, db_service):
        self.db = db_service
        self.persistence = db_service.persistence

    def get_profile_by_name(self, name: str):
        return self.db.get_profile_by_name(name)

    def load_all_user_profiles(self):
        return self.persistence.load_all_user_profiles()

    async def load_user_profile(self, user_id: str):
        return await self.persistence.load_user_profile(user_id)

    async def save_user_profile(self, profile):
        return await self.persistence.save_user_profile(profile)

    async def load_chat_state(self, chat_id: str):
        return await self.persistence.load_chat_state(chat_id)

    async def save_chat_state(self, chat_id: str, state):
        return await self.persistence.save_chat_state(chat_id, state)
