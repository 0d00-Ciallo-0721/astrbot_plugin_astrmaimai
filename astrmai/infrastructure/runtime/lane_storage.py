from __future__ import annotations

from typing import Any, List, Optional

from astrbot.api import logger

from .runtime_contracts import VisibleReplyArtifact


class LaneStorageMixin:
    async def ensure_lane(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
        template_id: str = "",
        schema_id: str = "",
        persona_core_version: str = "",
    ) -> tuple[str, str, List[dict], LanePolicy]:
        lane_umo = self.resolve_lane_umo(base_origin, lane_key)
        policy = self.get_policy(lane_key)
        conversation_id = await self.conversation_manager.get_curr_conversation_id(lane_umo)
        rotate = False
        rotate_reason = ""
        old_history: List[dict] = []
        if conversation_id:
            try:
                old_conversation = await self.conversation_manager.get_conversation(
                    lane_umo,
                    conversation_id,
                    create_if_not_exists=False,
                )
                old_history = self._load_history(old_conversation)
            except Exception:
                old_history = []
            rotate_reason = await self._rotation_reason(
                lane_umo=lane_umo,
                prompt_version=lane_key.prompt_version,
                prefix_hash=prefix_hash,
                model_id=model_id,
                persona_id=persona_id,
                template_id=template_id,
                schema_id=schema_id,
                persona_core_version=persona_core_version,
            )
            rotate = bool(rotate_reason)

        target_conversation_id = conversation_id
        if not target_conversation_id or rotate:
            target_conversation_id = await self.conversation_manager.new_conversation(
                unified_msg_origin=lane_umo,
                title=self._build_title(lane_key),
                persona_id=persona_id or None,
            )
            if rotate:
                expired_count = self.expire_remote_sessions_for_lane(lane_umo)
                if expired_count > 0:
                    logger.warning(
                        f"[Lane] rotation detected for {lane_umo}: "
                        f"expired {expired_count} remote session mapping(s); "
                        f"old sessions on provider side may still consume resources "
                        f"(conversation_manager has no terminate API)"
                    )
            if rotate and old_history and (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
                await self.conversation_manager.update_conversation(
                    unified_msg_origin=lane_umo,
                    conversation_id=target_conversation_id,
                    history=[{"role": "assistant", "content": self._build_rolling_summary(old_history)}],
                    title=self._build_title(lane_key),
                    persona_id=persona_id or None,
                    token_usage=None,
                )

        async def _load_and_normalize(curr_conversation_id: str) -> List[dict]:
            conversation = await self.conversation_manager.get_conversation(
                lane_umo,
                curr_conversation_id,
                create_if_not_exists=True,
            )
            loaded_history = self._load_history(conversation)
            normalized_history = self._normalize_history(loaded_history, lane_key)
            if loaded_history != normalized_history and (lane_key.subsystem, lane_key.task_family) == ("sys2", "dialog"):
                await self.conversation_manager.update_conversation(
                    unified_msg_origin=lane_umo,
                    conversation_id=curr_conversation_id,
                    history=normalized_history,
                    title=self._build_title(lane_key),
                    persona_id=persona_id or None,
                    token_usage=None,
                )
            return normalized_history

        history = await _load_and_normalize(target_conversation_id)
        final_conversation_id = await self.conversation_manager.get_curr_conversation_id(lane_umo) or target_conversation_id
        if final_conversation_id != target_conversation_id:
            history = await _load_and_normalize(final_conversation_id)

        lane_lock = await self._get_lane_lock(lane_umo)
        async with lane_lock:
            async with self._meta_lock:
                self._runtime_meta[lane_umo] = {
                    "conversation_id": final_conversation_id,
                    "prompt_version": lane_key.prompt_version,
                    "prefix_hash": prefix_hash,
                    "model_id": model_id,
                    "persona_id": persona_id,
                    "template_id": template_id,
                    "schema_id": schema_id,
                    "persona_core_version": persona_core_version,
                    "lane_rotated": bool(rotate),
                    "lane_rotate_reason": rotate_reason,
                }
        return lane_umo, final_conversation_id, history, policy

    async def save_lane_history(
        self,
        lane_key: LaneKey,
        lane_umo: str,
        conversation_id: str,
        history: List[dict],
        token_usage: Optional[int] = None,
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
        template_id: str = "",
        schema_id: str = "",
        persona_core_version: str = "",
    ) -> List[dict]:
        normalized = self._normalize_history(history, lane_key)
        await self.conversation_manager.update_conversation(
            unified_msg_origin=lane_umo,
            conversation_id=conversation_id,
            history=normalized,
            title=self._build_title(lane_key),
            persona_id=persona_id or None,
            token_usage=token_usage,
        )
        async with self._meta_lock:
            self._runtime_meta[lane_umo] = {
                "conversation_id": conversation_id,
                "prompt_version": lane_key.prompt_version,
                "prefix_hash": prefix_hash,
                "model_id": model_id,
                "persona_id": persona_id,
                "template_id": template_id,
                "schema_id": schema_id,
                "persona_core_version": persona_core_version,
                "lane_rotated": False,
                "lane_rotate_reason": "",
            }
        return normalized

    async def append_exchange(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        user_content: Any,
        assistant_content: Any,
        token_usage: Optional[int] = None,
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
        template_id: str = "",
        schema_id: str = "",
        persona_core_version: str = "",
    ) -> List[dict]:
        lane_umo, conversation_id, history, _ = await self.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=prefix_hash,
            model_id=model_id,
            persona_id=persona_id,
            template_id=template_id,
            schema_id=schema_id,
            persona_core_version=persona_core_version,
        )
        user_turn = self.build_history_turn("user", user_content)
        assistant_turn = self.build_history_turn("assistant", assistant_content)
        if user_turn:
            history.append(user_turn)
        if assistant_turn:
            history.append(assistant_turn)
        return await self.save_lane_history(
            lane_key=lane_key,
            lane_umo=lane_umo,
            conversation_id=conversation_id,
            history=history,
            token_usage=token_usage,
            prefix_hash=prefix_hash,
            model_id=model_id,
            persona_id=persona_id,
            template_id=template_id,
            schema_id=schema_id,
            persona_core_version=persona_core_version,
        )

    async def append_visible_reply_artifact(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
        raw_user_text: Any,
        artifact: VisibleReplyArtifact,
        token_usage: Optional[int] = None,
        prefix_hash: str = "",
        model_id: str = "",
        persona_id: str = "",
        template_id: str = "",
        schema_id: str = "",
        persona_core_version: str = "",
    ) -> List[dict]:
        if artifact.blocked or not artifact.persistable_text:
            lane_umo, conversation_id, history, _ = await self.ensure_lane(
                lane_key=lane_key,
                base_origin=base_origin,
                prefix_hash=prefix_hash,
                model_id=model_id,
                persona_id=persona_id,
                template_id=template_id,
                schema_id=schema_id,
                persona_core_version=persona_core_version,
            )
            return history
        return await self.append_exchange(
            lane_key=lane_key,
            base_origin=base_origin,
            user_content=raw_user_text,
            assistant_content=artifact.persistable_text,
            token_usage=token_usage,
            prefix_hash=prefix_hash,
            model_id=model_id,
            persona_id=persona_id,
            template_id=template_id,
            schema_id=schema_id,
            persona_core_version=persona_core_version,
        )

    async def get_lane_history(
        self,
        lane_key: LaneKey,
        base_origin: Optional[str],
    ) -> List[dict]:
        _lane_umo, _conversation_id, history, _ = await self.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
        )
        return list(history)
