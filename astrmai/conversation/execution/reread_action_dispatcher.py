from __future__ import annotations

import hashlib
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp

from ..contracts.reread import RereadActionRequest, RereadDispatchResult


class RereadActionDispatcher:
    """Send a visible social reread without entering the LLM reply pipeline."""

    def __init__(self, *, context=None, config=None, runtime_coordinator=None, dialogue_store=None):
        self.context = context
        self.config = config
        self.runtime_coordinator = runtime_coordinator
        self.dialogue_store = dialogue_store

    def refresh_config(self, config) -> None:
        self.config = config

    @staticmethod
    def _send_key(request: RereadActionRequest) -> str:
        payload = "\x1f".join((request.chat_id, request.trigger_kind, request.fingerprint, *request.source_event_ids))
        return "reread:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    async def dispatch(self, event: Any, request: RereadActionRequest) -> RereadDispatchResult:
        context = self.context
        if context is None or not hasattr(context, "send_message"):
            return RereadDispatchResult("failed", detail="context_unavailable")
        coordinator = self.runtime_coordinator
        send_key = self._send_key(request)
        if coordinator is not None and hasattr(coordinator, "claim_send"):
            if not await coordinator.claim_send(request.chat_id, send_key):
                return RereadDispatchResult("duplicate", detail="send_claim_exists")
        try:
            turn = event.get_extra("astrmai_turn_identity", None) if hasattr(event, "get_extra") else None
            if turn is not None and coordinator is not None and hasattr(coordinator, "is_current_turn"):
                if not await coordinator.is_current_turn(turn):
                    if hasattr(coordinator, "mark_send_failed"):
                        await coordinator.mark_send_failed(request.chat_id, send_key, "stale_turn")
                    return RereadDispatchResult("stale", detail="stale_turn")
            chain = MessageChain()
            chain.chain.append(Comp.Plain(request.text))
            result = await context.send_message(getattr(event, "unified_msg_origin", request.chat_id), chain)
            outbound_ids = () if result is None or isinstance(result, bool) else (str(result),)
            if coordinator is not None and hasattr(coordinator, "commit_send"):
                await coordinator.commit_send(request.chat_id, send_key, list(outbound_ids))
            await self._record_dialogue(event, request, outbound_ids)
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_group_reread_dispatched", True)
                event.set_extra("astrmai_reread_trigger_kind", request.trigger_kind)
                event.set_extra("astrmai_reread_outbound_message_ids", list(outbound_ids))
            logger.info("[RereadAction] sent kind=%s chat=%s", request.trigger_kind, request.chat_id)
            return RereadDispatchResult("sent", outbound_ids)
        except Exception as exc:
            if coordinator is not None and hasattr(coordinator, "mark_send_failed"):
                await coordinator.mark_send_failed(request.chat_id, send_key, str(exc))
            logger.warning("[RereadAction] send failed: %s", exc)
            return RereadDispatchResult("failed", detail=str(exc))

    async def _record_dialogue(self, event: Any, request: RereadActionRequest, outbound_ids: tuple[str, ...]) -> None:
        store = self.dialogue_store
        if store is None or not hasattr(store, "append_segment"):
            return
        event_id = "reread_" + hashlib.sha256((request.chat_id + request.fingerprint + str(time.time())).encode("utf-8")).hexdigest()[:20]
        if outbound_ids:
            event_id = outbound_ids[0]
        try:
            await store.append_segment(
                request.chat_id,
                event_id=event_id,
                speaker_id=str(getattr(event, "get_self_id", lambda: "")() or ""),
                speaker_name="Bot",
                content=request.text,
                role="assistant",
                is_bot=True,
                timestamp=time.time(),
                provenance=request.trigger_kind,
                source_event_ids=list(request.source_event_ids),
                outcome="sent",
                internal_note=request.explanation,
            )
        except Exception:
            logger.debug("[RereadAction] dialogue record degraded", exc_info=True)


__all__ = ["RereadActionDispatcher"]
