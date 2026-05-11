from __future__ import annotations


class ChatRepository:
    def __init__(self, db_service):
        self.db = db_service

    def add_message_log(self, group_id: str, sender_id: str, sender_name: str, content: str):
        return self.db.add_message_log(group_id, sender_id, sender_name, content)

    async def add_message_log_async(self, group_id: str, sender_id: str, sender_name: str, content: str):
        return await self.db.add_message_log_async(group_id, sender_id, sender_name, content)

    def get_unprocessed_logs(self, group_id: str, limit: int = 50):
        return self.db.get_unprocessed_logs(group_id, limit=limit)

    async def get_unprocessed_logs_async(self, group_id: str, limit: int = 50):
        return await self.db.get_unprocessed_logs_async(group_id, limit=limit)

    def get_recent_message_logs(
        self,
        group_id: str,
        limit: int = 8,
        max_age_seconds: float | None = None,
        include_processed: bool = True,
    ):
        return self.db.get_recent_message_logs(
            group_id,
            limit=limit,
            max_age_seconds=max_age_seconds,
            include_processed=include_processed,
        )

    async def get_recent_message_logs_async(
        self,
        group_id: str,
        limit: int = 8,
        max_age_seconds: float | None = None,
        include_processed: bool = True,
    ):
        return await self.db.get_recent_message_logs_async(
            group_id,
            limit=limit,
            max_age_seconds=max_age_seconds,
            include_processed=include_processed,
        )

    def mark_logs_processed(self, log_ids: list[int]):
        return self.db.mark_logs_processed(log_ids)

    async def mark_logs_processed_async(self, log_ids: list[int]):
        return await self.db.mark_logs_processed_async(log_ids)

    def get_chat_state(self, chat_id: str):
        return self.db.get_chat_state(chat_id)

    async def get_chat_state_async(self, chat_id: str):
        return await self.db.get_chat_state_async(chat_id)

    def update_social_relation(self, group_id: str, from_user: str, to_user: str, relation_type: str, strength_delta: float):
        return self.db.update_social_relation(group_id, from_user, to_user, relation_type, strength_delta)

    async def update_social_relation_async(self, group_id: str, from_user: str, to_user: str, relation_type: str, strength_delta: float):
        return await self.db.update_social_relation_async(group_id, from_user, to_user, relation_type, strength_delta)

    def get_user_relations(self, group_id: str, user_id: str):
        return self.db.get_user_relations(group_id, user_id)

    async def get_user_relations_async(self, group_id: str, user_id: str):
        return await self.db.get_user_relations_async(group_id, user_id)
