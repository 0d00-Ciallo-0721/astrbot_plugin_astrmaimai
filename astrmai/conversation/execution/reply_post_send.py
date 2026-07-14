from __future__ import annotations

import re
from typing import Any, Sequence

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...conversation.contracts.turn_context import get_turn_context
from ...infrastructure.runtime.lane_manager import LaneKey
from ...multimodal import MEMES_DIR, send_meme
from ...state.relationship.affection_router import AffectionRouter


class ReplyPostSendMixin:
    @staticmethod
    def _resolve_affection_message_text(event: AstrMessageEvent, anchor_event: AstrMessageEvent | None = None) -> str:
        candidates = [
            anchor_event,
            event.get_extra("astrmai_focus_event", None),
            event.get_extra("astrmai_focus_thread_root_event", None),
            event.get_extra("astrmai_anchor_event", None),
            event,
        ]
        for candidate in candidates:
            text = str(getattr(candidate, "message_str", "") or "").strip() if candidate is not None else ""
            if text:
                return text
        return ""

    async def _settle_no_send_affection(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        *,
        skipped_reason: str,
        anchor_event: AstrMessageEvent | None = None,
    ) -> None:
        if bool(event.get_extra("astrmai_is_proactive_event", False)):
            return
        state_engine = getattr(self, "state_engine", None)
        if state_engine is None or not hasattr(state_engine, "settle_no_send_affection"):
            return
        sender_id = str(event.get_sender_id() or "").strip() if hasattr(event, "get_sender_id") else ""
        if not sender_id:
            return
        message_text = self._resolve_affection_message_text(event, anchor_event=anchor_event)
        if not message_text:
            return
        attack_confidence = 0.0
        try:
            attack_confidence = float(event.get_extra("astrmai_attack_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            attack_confidence = 0.0
        risk_flags = event.get_extra("astrmai_risk_flags", []) or []
        try:
            await state_engine.settle_no_send_affection(
                user_id=sender_id,
                group_id=chat_id,
                message_text=message_text,
                skipped_reason=skipped_reason,
                attack_confidence=attack_confidence,
                risk_flags=risk_flags,
            )
        except Exception as exc:
            logger.warning(f"[ReplyService] no-send affection settlement failed: {exc}")

    async def _fetch_history(self, chat_id: str, anchor_text: str, anchor_event: AstrMessageEvent = None) -> list:
        del anchor_event
        fetch_count = getattr(self.config.attention, "bg_pool_size", 20) if self.config else 20
        lane_manager = getattr(getattr(self.state_engine, "gateway", None), "lane_manager", None)
        if lane_manager is None:
            return []
        try:
            raw_history = await lane_manager.get_lane_history(
                lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
                base_origin=chat_id,
            )
            clean_anchor = re.sub(r"\s+", "", anchor_text or "")
            if clean_anchor:
                cutoff_idx = -1
                for i in range(len(raw_history) - 1, -1, -1):
                    msg_data = raw_history[i]
                    if not isinstance(msg_data, dict):
                        continue
                    content = str(msg_data.get("content", "") or "").strip()
                    if content and clean_anchor in re.sub(r"\s+", "", content):
                        cutoff_idx = i
                        break
                if cutoff_idx >= 0:
                    start_idx = max(0, cutoff_idx - fetch_count)
                    return raw_history[start_idx:cutoff_idx + 1]
            return raw_history[-fetch_count:]
        except Exception as exc:
            logger.warning(f"[ReplyService] lane history fetch failed: {exc}")
            return []

    async def _sync_native_history_mirror(self, event: AstrMessageEvent, chat_id: str, user_text: str, assistant_text: str) -> None:
        del event, chat_id, user_text, assistant_text
        return

    async def _ingest_memory_turn(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        memory_engine = getattr(self, "memory_engine", None)
        pipeline = getattr(memory_engine, "memory_pipeline", None) if memory_engine is not None else None
        instant_gate = getattr(memory_engine, "instant_gate", None) if memory_engine is not None else None
        if pipeline is None or instant_gate is None:
            return
        try:
            turn = pipeline.build_turn(
                chat_id=chat_id,
                user_text=user_text,
                assistant_text=assistant_text,
                sender_id=str(event.get_sender_id() or "").strip() if hasattr(event, "get_sender_id") else "",
                source="reply_service.post_send",
                is_proactive=bool(event.get_extra("astrmai_is_proactive_event", False)),
                think_level=self._resolve_memory_turn_think_level(event),
                persona_id=self._resolve_memory_turn_persona_id(),
            )
            record_result = await pipeline.record_turn(turn)
            if not bool(record_result.get("performed")):
                return
            await pipeline.process_instant_gate(turn)
            await pipeline.publish_turn_committed(turn)
        except Exception as exc:
            logger.warning(f"[ReplyService] memory turn ingest failed: {exc}")

    def _resolve_memory_turn_think_level(self, event: AstrMessageEvent) -> int | None:
        value = event.get_extra("astrmai_think_level", None) if hasattr(event, "get_extra") else None
        if value is None:
            try:
                turn_context = get_turn_context(event)
                if turn_context is not None:
                    value = getattr(turn_context.cognitive, "think_level", None)
            except Exception:
                value = None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _resolve_memory_turn_persona_id(self) -> str:
        config = getattr(self, "config", None)
        return str(getattr(getattr(config, "persona", None), "persona_id", "") or "")

    def _resolve_post_send_tag(self, bypassed_tag: str | None) -> tuple[str, bool]:
        tag = str(bypassed_tag or "").strip().lower()
        if not tag:
            return "neutral", False
        return tag, False

    async def _collect_affection_target(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        tag: str,
        *,
        window_events: list | None,
        anchor_event: AstrMessageEvent | None,
    ) -> str | None:
        user_id = event.get_sender_id()
        is_private_chat = "FriendMessage" in chat_id or not event.get_group_id()
        if is_private_chat:
            return str(user_id)

        anchor = anchor_event or event.get_extra("astrmai_focus_thread_root_event", None) or event.get_extra("astrmai_focus_event", None) or event.get_extra("astrmai_anchor_event", None)
        anchor_text = anchor.message_str.strip() if anchor and getattr(anchor, "message_str", None) else ""
        history_events = await self._fetch_history(chat_id, anchor_text, anchor_event=anchor)
        return AffectionRouter.route(
            history_events=history_events,
            window_events=window_events or event.get_extra("astrmai_window_events", []) or [],
            trigger_event=event,
            mood_tag=tag,
            config=self.config,
            fallback_uid=user_id,
        )

    async def _record_private_profile_touch(self, user_id: str, *, chat_id: str = "", sender_name: str = "") -> None:
        try:
            await self.state_engine.record_profile_learning_touch(
                user_id,
                chat_id=chat_id,
                source="private_reply",
                weight=1.0,
                sender_name=sender_name,
                increment_know_times=True,
            )
            persistence = getattr(self.state_engine, "persistence", None)
            profile = await self.state_engine.get_user_profile(user_id)
            if persistence and hasattr(persistence, "save_user_profile"):
                try:
                    await persistence.save_user_profile(profile)
                except TypeError:
                    await persistence.save_user_profile(user_id, profile)
        except Exception as exc:
            logger.warning(f"[ReplyService] private chat profile touch failed: {exc}")

    async def _settle_post_send(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        *,
        bypassed_tag: str | None,
        window_events: list | None,
        anchor_event: AstrMessageEvent | None,
    ) -> None:
        tag, force_meme_flag = self._resolve_post_send_tag(bypassed_tag)
        is_proactive_event = bool(event.get_extra("astrmai_is_proactive_event", False))
        try:
            await self.state_engine.atomic_update_mood(chat_id, delta=0.0 if not bypassed_tag else (0.1 if tag == "happy" else -0.1 if tag in ["sad", "angry"] else 0.0))
            is_private_chat = "FriendMessage" in chat_id or not event.get_group_id()
            if is_private_chat:
                await self._record_private_profile_touch(
                    str(event.get_sender_id()),
                    chat_id=chat_id,
                    sender_name=str(event.get_sender_name() or ""),
                )
            if not is_proactive_event:
                target_user_id = await self._collect_affection_target(
                    event,
                    chat_id,
                    tag,
                    window_events=window_events,
                    anchor_event=anchor_event,
                )
                if target_user_id and hasattr(self.state_engine, "calculate_and_update_affection"):
                    message_text = self._resolve_affection_message_text(event, anchor_event=anchor_event)
                    await self.state_engine.calculate_and_update_affection(
                        user_id=str(target_user_id),
                        group_id=chat_id,
                        mood_tag=tag,
                        intensity=1.0,
                        message_text=message_text,
                    )
        except Exception as exc:
            logger.warning(f"[ReplyService] post-send settlement failed: {exc}")
            tag = "neutral"
            force_meme_flag = False

        if tag and tag != "neutral":
            final_prob = 100 if force_meme_flag else self.meme_probability
            global_context = getattr(self.state_engine.gateway, "context", None)
            try:
                await send_meme(
                    event=event,
                    emotion_tag=tag,
                    probability=final_prob,
                    memes_dir=MEMES_DIR,
                    context=global_context,
                )
            except Exception as exc:
                logger.warning(f"[ReplyService] optional meme send degraded: {exc}")
                try:
                    event.set_extra("astrmai_meme_send_degraded", True)
                except Exception:
                    pass
