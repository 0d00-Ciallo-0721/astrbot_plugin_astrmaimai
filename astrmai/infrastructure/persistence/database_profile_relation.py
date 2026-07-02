import asyncio
import re
import time
from typing import List, Optional

from astrbot.api import logger
from sqlmodel import desc, select

from .orm_models import MessageLog, SocialRelation, UserProfile


class ProfileRelationPersistenceMixin:
    def get_profile_by_name(self, name: str) -> Optional[UserProfile]:
        if not name:
            return None
        profiles = self.persistence.load_all_user_profiles()
        for profile_data in profiles.values():
            if profile_data.get("name") == name:
                try:
                    return UserProfile(**profile_data)
                except Exception:
                    logger.warning("[AstrMai-profile] construction failed", exc_info=True)
                    return None
        for profile_data in profiles.values():
            if profile_data.get("nickname") == name:
                try:
                    return UserProfile(**profile_data)
                except Exception:
                    logger.warning("[AstrMai-profile] construction failed", exc_info=True)
                    return None
        return None

    def update_social_relation(
        self,
        group_id: str,
        from_user: str,
        to_user: str,
        relation_type: str,
        strength_delta: float,
    ):
        with self.get_session() as session:
            statement = select(SocialRelation).where(
                SocialRelation.group_id == group_id,
                SocialRelation.from_user == from_user,
                SocialRelation.to_user == to_user,
                SocialRelation.relation_type == relation_type,
            )
            existing = session.exec(statement).first()
            strength = min(1.0, max(0.0, strength_delta))
            if existing:
                existing.strength = min(1.0, max(0.0, existing.strength + strength_delta))
                existing.frequency += 1
                existing.last_interaction = time.time()
                session.add(existing)
            else:
                session.add(
                    SocialRelation(
                        group_id=group_id,
                        from_user=from_user,
                        to_user=to_user,
                        relation_type=relation_type,
                        strength=strength,
                        frequency=1,
                    )
                )
            session.commit()

    def get_user_relations(self, group_id: str, user_id: str) -> List[SocialRelation]:
        with self.get_session() as session:
            statement = select(SocialRelation).where(
                (SocialRelation.group_id == group_id)
                & ((SocialRelation.from_user == user_id) | (SocialRelation.to_user == user_id))
            ).order_by(desc(SocialRelation.strength))
            results = session.exec(statement).all()
            return [SocialRelation.model_validate(item.model_dump()) for item in results]

    def _resolve_event_group_id(self, current_event) -> str:
        group_id = current_event.get_group_id()
        if not group_id:
            group_id = current_event.unified_msg_origin
        return str(group_id)

    def _resolve_at_target(self, current_event) -> Optional[str]:
        try:
            import astrbot.api.message_components as Comp
        except Exception:
            return None
        message_obj = getattr(current_event, "message_obj", None)
        if not message_obj or not hasattr(message_obj, "message"):
            return None
        self_id = str(current_event.get_self_id())
        at_targets = [
            str(comp.qq)
            for comp in message_obj.message
            if isinstance(comp, Comp.At) and str(comp.qq) != self_id
        ]
        return at_targets[0] if len(at_targets) == 1 else None

    def _resolve_window_sender(self, clean_name: str, current_event) -> Optional[str]:
        if current_event.get_sender_name() == clean_name:
            return str(current_event.get_sender_id())
        for window_event in reversed(current_event.get_extra("astrmai_window_events", [])):
            if window_event.get_sender_name() == clean_name:
                return str(window_event.get_sender_id())
        return None

    async def _resolve_history_sender(self, clean_name: str, current_event, astr_ctx=None) -> Optional[str]:
        if not astr_ctx or not hasattr(astr_ctx, "conversation_manager"):
            return None
        try:
            conv_mgr = astr_ctx.conversation_manager
            uid = current_event.unified_msg_origin
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
            conversation = await conv_mgr.get_conversation(uid, curr_cid)
        except Exception:
            return None
        if not conversation or not getattr(conversation, "history", None):
            return None
        for msg_data in reversed(conversation.history):
            sender_name = ""
            sender_id = ""
            if isinstance(msg_data, dict):
                sender = msg_data.get("sender", {}) or {}
                sender_name = sender.get("nickname", "") or msg_data.get("name", "")
                sender_id = sender.get("user_id", "")
            elif hasattr(msg_data, "sender"):
                sender = msg_data.sender
                sender_name = getattr(sender, "nickname", getattr(sender, "name", ""))
                sender_id = getattr(sender, "user_id", "")
            if sender_name == clean_name and sender_id:
                return str(sender_id)
        return None

    def _resolve_sender_from_logs(self, group_id: str, clean_name: str) -> Optional[str]:
        with self.get_session() as session:
            statement = select(MessageLog.sender_id).where(
                MessageLog.group_id == group_id,
                MessageLog.sender_name == clean_name,
            ).distinct()
            results = session.exec(statement).all()
            if len(results) == 1:
                return str(results[0])
        return None

    async def resolve_entity_spatio_temporal(self, target_name: str, current_event, astr_ctx=None):
        if not target_name or not current_event:
            return None
        group_id = self._resolve_event_group_id(current_event)
        clean_name = target_name.strip().lstrip("@")
        if clean_name.isdigit():
            return (clean_name, group_id)
        match = re.search(r"^(.*?)\(([0-9]+)\)$", clean_name)
        if match:
            return (match.group(2).strip(), group_id)
        at_target = self._resolve_at_target(current_event)
        if at_target:
            return (at_target, group_id)
        sender_id = self._resolve_window_sender(clean_name, current_event)
        if sender_id:
            return (sender_id, group_id)
        history_sender = await self._resolve_history_sender(clean_name, current_event, astr_ctx)
        if history_sender:
            return (history_sender, group_id)
        log_sender = await asyncio.to_thread(self._resolve_sender_from_logs, group_id, clean_name)
        if log_sender:
            return (log_sender, group_id)
        return None

    async def update_social_relation_async(
        self,
        group_id: str,
        from_user: str,
        to_user: str,
        relation_type: str,
        strength_delta: float,
    ):
        async with self._db_lock:
            return await asyncio.to_thread(
                self.update_social_relation,
                group_id,
                from_user,
                to_user,
                relation_type,
                strength_delta,
            )

    async def get_user_relations_async(self, group_id: str, user_id: str):
        return await asyncio.to_thread(self.get_user_relations, group_id, user_id)