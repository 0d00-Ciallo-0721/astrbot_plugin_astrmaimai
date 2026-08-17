from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Mapping, Sequence

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...conversation.contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from ...conversation.contracts.turn_context import get_turn_context
from ...conversation.contracts.turn_target import TargetKind, TurnTarget
from ...infrastructure.runtime.lane_manager import LaneKey
from ...multimodal import MEMES_DIR, send_meme
from ...state.relationship.affection_router import AffectionRouter


class ReplyPostSendMixin:
    @staticmethod
    def _resolve_source_event_id(event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        return str(
            getattr(message_obj, "message_id", "")
            or getattr(event, "message_id", "")
            or ""
        ).strip()

    def _resolve_reply_target(self, event: AstrMessageEvent) -> TurnTarget:
        turn_context = get_turn_context(event)
        if turn_context is not None:
            target = TurnTarget.from_value(
                getattr(turn_context.attention, "turn_target", None)
            )
            if target.target_kind != TargetKind.NONE:
                return target
        source_event_id = self._resolve_source_event_id(event)
        sender_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or "").strip()
        return TurnTarget(
            target_kind=TargetKind.ACTOR if sender_id else TargetKind.NONE,
            target_actor_id=sender_id,
            target_actor_name=sender_name,
            target_event_id=source_event_id,
            source_event_ids=(source_event_id,) if source_event_id else (),
            evidence="reply_service_fallback",
            confidence=0.5 if sender_id else 0.0,
            resolved_by="reply_service_compat_v1",
            created_at=time.time(),
        )

    def _build_reply_plan(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        artifact,
    ) -> ReplyPlan:
        turn = event.get_extra("astrmai_turn_identity", None)
        stable_turn_id = ""
        if turn is not None:
            stable_turn_id = ":".join(
                (
                    str(getattr(turn, "mode", "") or ""),
                    str(getattr(turn, "chat_id", "") or ""),
                    str(getattr(turn, "thread_id", "") or ""),
                    str(int(getattr(turn, "generation", 0) or 0)),
                )
            ).strip(":")
        turn_id = str(
            getattr(turn, "turn_id", "")
            or stable_turn_id
            or event.get_extra("astrmai_trace_id", "")
            or self._resolve_source_event_id(event)
            or f"{chat_id}:{int(time.time() * 1000)}"
        ).strip()
        response_kind = str(
            event.get_extra("astrmai_response_kind", "final") or "final"
        ).strip()
        plan = ReplyPlan.create(
            turn_id=turn_id,
            chat_id=chat_id,
            chat_kind=(
                "private"
                if "FriendMessage" in chat_id or not event.get_group_id()
                else "group"
            ),
            target=self._resolve_reply_target(event),
            planned_text=str(artifact.visible_text or ""),
            planned_segments=tuple(artifact.segments or ()),
            response_kind=response_kind,
            shape_policy=str(
                artifact.metadata.get("segment_strategy", "") or ""
            ),
            created_at=time.time(),
        )
        artifact.metadata["reply_plan_id"] = plan.plan_id
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_reply_plan", plan)
            event.set_extra("astrmai_reply_plan_id", plan.plan_id)
            event.set_extra("astrmai_draft_history_write_attempt", 0)
            event.set_extra(
                "astrmai_reply_planned_segment_count",
                len(plan.planned_segments),
            )
        return plan

    @staticmethod
    def _build_send_receipt(artifact) -> ReplySendReceipt:
        raw_status = str(
            artifact.metadata.get("send_status", "sent") or "sent"
        ).strip()
        status = (
            ReplyCommitStatus.PARTIAL
            if raw_status == "partial_sent"
            else ReplyCommitStatus.SENT
        )
        sent_count = max(
            0,
            int(artifact.metadata.get("sent_segment_count", 0) or 0),
        )
        attachment_refs: list[str] = []
        if bool(artifact.metadata.get("tts_sent", False)):
            attachment_refs.append("tts:record")
        return ReplySendReceipt(
            status=status,
            sent_segments=tuple((artifact.segments or [])[:sent_count]),
            sent_attachment_refs=tuple(attachment_refs),
            outbound_message_ids=tuple(
                artifact.metadata.get("outbound_message_ids", ()) or ()
            ),
            visible_text=str(artifact.visible_text or ""),
            persistable_text=str(artifact.persistable_text or ""),
            sent_at=time.time(),
            failure_reason=str(
                artifact.metadata.get("send_failure_reason", "") or ""
            ),
        )

    @staticmethod
    def _normalize_commit_stance(social_event: str, stance: str) -> str:
        normalized = str(stance or "").strip()
        if social_event in {"boundary_violation", "pushback", "boundary"}:
            normalized = normalized or "reject"
        if normalized in {"pushback", "boundary", "refuse", "refusal"}:
            normalized = "reject"
        return normalized

    def _build_reply_commit_repair_context(
        self,
        event: AstrMessageEvent,
        *,
        user_text: str,
    ) -> dict[str, Any]:
        social_event = str(
            event.get_extra("astrmai_group_social_signal", "")
            or event.get_extra("astrmai_social_intent", "")
            or ""
        ).strip()
        configured_names = list(
            getattr(getattr(self.config, "system1", None), "nicknames", [])
            or []
        )
        return {
            "user_text": str(user_text or ""),
            "sender_id": str(event.get_sender_id() or ""),
            "bot_id": str(
                (
                    event.get_self_id()
                    if hasattr(event, "get_self_id")
                    else ""
                )
                or getattr(getattr(self.state_engine, "gateway", None), "bot_id", "")
                or ""
            ),
            "bot_name": str(configured_names[0] if configured_names else "Bot"),
            "social_event": social_event,
            "stance": self._normalize_commit_stance(
                social_event,
                str(event.get_extra("astrmai_stance", "") or ""),
            ),
            "is_proactive": bool(
                event.get_extra("astrmai_is_proactive_event", False)
            ),
            "think_level": self._resolve_memory_turn_think_level(event),
            "persona_id": self._resolve_memory_turn_persona_id(),
            "skip_semantic_persistence": bool(
                event.get_extra("astrmai_media_only_failure", False)
            ),
        }

    async def _commit_group_dialogue_turn_from_context(
        self,
        committed_turn: CommittedBotTurn,
        context: Mapping[str, Any],
    ) -> str:
        store = getattr(self, "dialogue_store", None)
        if store is None or committed_turn.chat_kind != "group":
            return "skipped_unavailable"
        social_event = str(context.get("social_event", "") or "").strip()
        stance = self._normalize_commit_stance(
            social_event,
            str(context.get("stance", "") or ""),
        )
        bot_id = str(context.get("bot_id", "") or "")
        bot_name = str(context.get("bot_name", "Bot") or "Bot")
        await store.append_committed_bot_turn(
            committed_turn,
            bot_id=bot_id,
            bot_name=bot_name,
            stance=stance,
            social_event=social_event,
        )
        if social_event in {
            "boundary_violation",
            "insult",
            "conflict",
            "promise",
        } and committed_turn.target.target_actor_id:
            await store.observe_social_incident(
                committed_turn.chat_id,
                kind=social_event,
                actor_id=committed_turn.target.target_actor_id,
                actor_name=committed_turn.target.target_actor_name,
                target_id=bot_id,
                target_name=bot_name,
                evidence_event_id=(
                    committed_turn.source_event_ids[-1]
                    if committed_turn.source_event_ids
                    else ""
                ),
                topic_epoch=committed_turn.topic_epoch,
                stance=stance,
            )
        return "committed"

    def _build_reply_commit_consumers(
        self,
        committed_turn: CommittedBotTurn,
        context: Mapping[str, Any],
        event: AstrMessageEvent | None = None,
    ):
        def should_skip_semantic_persistence() -> bool:
            return bool(context.get("skip_semantic_persistence", False))

        async def group_dialogue(turn: CommittedBotTurn) -> str:
            if should_skip_semantic_persistence():
                return "skipped_nonsemantic_media"
            return await self._commit_group_dialogue_turn_from_context(
                turn,
                context,
            )

        async def native_history(turn: CommittedBotTurn) -> str:
            if should_skip_semantic_persistence():
                return "skipped_nonsemantic_media"
            await self._sync_native_history_mirror(
                event=event,
                chat_id=turn.chat_id,
                user_text=str(context.get("user_text", "") or ""),
                assistant_text=turn.persistable_text,
            )
            return "committed"

        async def memory(turn: CommittedBotTurn) -> str:
            if should_skip_semantic_persistence():
                return "skipped_nonsemantic_media"
            return await self._ingest_memory_turn_from_context(
                chat_id=turn.chat_id,
                assistant_text=turn.persistable_text,
                commit_id=turn.commit_id,
                context=context,
            )

        async def learning(turn: CommittedBotTurn) -> str:
            if should_skip_semantic_persistence():
                return "skipped_nonsemantic_media"
            manager = getattr(self, "evolution_manager", None)
            if manager is None or not hasattr(manager, "process_bot_reply"):
                return "skipped_unavailable"
            await manager.process_bot_reply(
                turn.chat_id,
                str(context.get("bot_id", "") or ""),
                turn.persistable_text,
            )
            return "committed"

        return {
            "group_dialogue": group_dialogue,
            "native_history": native_history,
            "memory": memory,
            "learning": learning,
        }

    async def _commit_visible_reply(
        self,
        event: AstrMessageEvent,
        committed_turn: CommittedBotTurn,
        *,
        user_text: str,
    ):
        repair_context = self._build_reply_commit_repair_context(
            event,
            user_text=user_text,
        )
        result = await self.reply_commit_service.commit(
            event,
            committed_turn,
            consumers=self._build_reply_commit_consumers(
                committed_turn,
                repair_context,
                event,
            ),
            repair_context=repair_context,
        )
        return result

    async def repair_pending_reply_commits(self, *, limit: int = 50) -> int:
        return await self.reply_commit_service.repair_pending(
            self._build_reply_commit_consumers,
            limit=limit,
        )

    async def run_reply_commit_repair_worker(
        self,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        while True:
            try:
                repaired = await self.repair_pending_reply_commits()
                if repaired:
                    logger.info(
                        f"[ReplyCommit] repaired {repaired} pending commit(s)"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[ReplyCommit] repair worker degraded: {exc}")
            await asyncio.sleep(max(1.0, float(interval_seconds)))

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
    ) -> str:
        context = self._build_reply_commit_repair_context(
            event,
            user_text=user_text,
        )
        return await self._ingest_memory_turn_from_context(
            chat_id=chat_id,
            assistant_text=assistant_text,
            commit_id="",
            context=context,
        )

    async def _ingest_memory_turn_from_context(
        self,
        *,
        chat_id: str,
        assistant_text: str,
        commit_id: str,
        context: Mapping[str, Any],
    ) -> str:
        memory_engine = getattr(self, "memory_engine", None)
        pipeline = getattr(memory_engine, "memory_pipeline", None) if memory_engine is not None else None
        instant_gate = getattr(memory_engine, "instant_gate", None) if memory_engine is not None else None
        if pipeline is None or instant_gate is None:
            return "skipped_unavailable"
        turn = pipeline.build_turn(
            chat_id=chat_id,
            user_text=str(context.get("user_text", "") or ""),
            assistant_text=assistant_text,
            sender_id=str(context.get("sender_id", "") or ""),
            source="reply_service.post_send",
            is_proactive=bool(context.get("is_proactive", False)),
            think_level=context.get("think_level"),
            persona_id=str(context.get("persona_id", "") or ""),
        )
        if commit_id:
            turn.turn_id = commit_id
        record_result = await pipeline.record_turn(turn)
        if not bool(record_result.get("performed")):
            return "skipped_not_performed"
        await pipeline.process_instant_gate(turn)
        await pipeline.publish_turn_committed(turn)
        return "committed"

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
        if hasattr(event, "get_extra") and event.get_extra("astrmai_force_meme", False):
            force_meme_flag = True
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
        else:
            try:
                event.set_extra(
                    "astrmai_meme_send_result",
                    {
                        "status": "skipped",
                        "reason": "neutral",
                        "emotion_tag": str(tag or "neutral"),
                        "probability": 100 if force_meme_flag else self.meme_probability,
                        "file": "",
                    },
                )
            except Exception:
                pass
