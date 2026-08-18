from __future__ import annotations

from typing import Any, Mapping

from ..contracts.social_feedback import SocialFeedbackDecision


class PostReplyFeedbackCoordinator:
    """Route post-reply feedback to distinct private and group policies."""

    def __init__(self, *, private_chat_manager=None, group_social_feedback_observer=None):
        self.private_chat_manager = private_chat_manager
        self.group_social_feedback_observer = group_social_feedback_observer

    @staticmethod
    def _event_id(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        for value in (
            getattr(message_obj, "message_id", None),
            getattr(message_obj, "id", None),
            getattr(event, "message_id", None),
        ):
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        return ""

    @staticmethod
    def _message_kind(event: Any) -> str:
        chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        names = {
            str(getattr(item, "type", item.__class__.__name__)).lstrip("_").lower()
            for item in chain
        }
        has_text = bool(str(getattr(event, "message_str", "") or "").strip())
        has_image = bool(names & {"image", "marketface"})
        if has_text and has_image:
            return "mixed"
        if has_image:
            return "image"
        return "text" if has_text else "interaction"

    async def register_committed_reply(
        self,
        event: Any,
        committed_turn: Any,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> str:
        chat_kind = str(getattr(committed_turn, "chat_kind", "") or "")
        context = dict(context or {})
        if chat_kind == "private":
            manager = self.private_chat_manager
            if manager is None or not hasattr(manager, "arm_reply_cycle"):
                return "skipped_unavailable"
            user_id = str(
                getattr(getattr(committed_turn, "target", None), "target_actor_id", "")
                or context.get("sender_id", "")
                or ""
            )
            feedback_event = manager.arm_reply_cycle(
                user_id,
                chat_id=str(getattr(committed_turn, "chat_id", "") or ""),
                turn_id=str(getattr(committed_turn, "turn_id", "") or ""),
                turn_generation=int(context.get("generation", 0) or 0),
                outbound_message_ids=list(
                    getattr(committed_turn, "outbound_message_ids", ()) or ()
                ),
            )
            if event is not None and hasattr(event, "set_extra"):
                event.set_extra("astrmai_post_reply_feedback_event", feedback_event)
                event.set_extra("astrmai_private_reply_cycle_armed", True)
            return "committed"
        observer = self.group_social_feedback_observer
        if chat_kind == "group" and observer is not None:
            observation = await observer.arm(
                committed_turn,
                event=event,
                context=context,
            )
            return "committed" if observation is not None else "skipped_disabled"
        return "skipped_unavailable"

    async def observe_incoming(self, event: Any) -> SocialFeedbackDecision:
        if getattr(event, "get_group_id", lambda: "")():
            observer = self.group_social_feedback_observer
            if observer is None:
                return SocialFeedbackDecision()
            return await observer.observe(event)
        manager = self.private_chat_manager
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if manager is None or not sender_id:
            return SocialFeedbackDecision()
        message_kind = self._message_kind(event)
        if message_kind == "interaction":
            decision = SocialFeedbackDecision(
                kind="interaction_feedback",
                action="record_only",
                actor_id=sender_id,
            )
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_private_reply_cycle_checked", True)
                event.set_extra("astrmai_social_feedback_detected", False)
                event.set_extra("astrmai_social_feedback_kind", decision.kind)
                event.set_extra("astrmai_social_feedback_action", decision.action)
            return decision
        signaled = await manager.signal_new_message(
            sender_id,
            str(getattr(event, "message_str", "") or ""),
            chat_id=str(getattr(event, "unified_msg_origin", "") or ""),
            event_id=self._event_id(event),
            message_kind=message_kind,
        )
        decision = SocialFeedbackDecision(
            detected=bool(signaled),
            kind="private_continuation" if signaled else "unrelated",
            action="attention_boost" if signaled else "none",
            actor_id=sender_id,
            confidence=1.0 if signaled else 0.0,
        )
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_private_reply_cycle_checked", True)
            event.set_extra("astrmai_private_reply_cycle_signaled", bool(signaled))
            event.set_extra("astrmai_social_feedback_detected", decision.detected)
            event.set_extra("astrmai_social_feedback_kind", decision.kind)
            event.set_extra("astrmai_social_feedback_action", decision.action)
        return decision

    async def mark_group_wait_result(self, event: Any, result: str) -> None:
        observer = self.group_social_feedback_observer
        if observer is not None:
            await observer.mark_group_wait_result(event, result)


__all__ = ["PostReplyFeedbackCoordinator"]
