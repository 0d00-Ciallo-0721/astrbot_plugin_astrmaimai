from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
import astrbot.api.message_components as Comp

from ..contracts.reread import RereadActionRequest, RereadDispatchResult
from ...infrastructure.runtime.outbound_send_guard import outbound_send_allowed


class RereadActionDispatcher:
    """Send a visible social reread without entering the LLM reply pipeline."""

    MAX_SETTLEMENT_RETRY_TASKS = 64

    def __init__(self, *, context=None, config=None, runtime_coordinator=None, dialogue_store=None, reread_observer=None):
        self.context = context
        self.config = config
        self.runtime_coordinator = runtime_coordinator
        self.dialogue_store = dialogue_store
        self.reread_observer = reread_observer
        self._settlement_retry_tasks: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._active_dispatches: set[asyncio.Task] = set()
        self._retry_leases: dict[str, tuple[str, str | None]] = {}
        self._retry_release_attempts: dict[str, int] = {}
        self._retry_release_exhausted: set[str] = set()
        self._pending_dispatch_shutdown = False
        self._retry_cleanup_tasks: set[asyncio.Task] = set()
        self._settlement_retry_stats = {"retry_pending": 0, "retry_exhausted": 0, "retry_rejected": 0}
        self._claim_rollback_degraded = 0
        self._shutting_down = False

    def refresh_config(self, config) -> None:
        self.config = config

    def _note_claim_rollback_degraded(self, chat_id: str, send_key: str, reason: str) -> None:
        self._claim_rollback_degraded += 1
        send_key_hash = hashlib.sha256(str(send_key or "").encode("utf-8")).hexdigest()[:16]
        logger.warning(
            "[RereadAction] claim_rollback_degraded chat=%s key_hash=%s reason=%s",
            chat_id,
            send_key_hash,
            reason,
        )

    async def _release_observer_claim(self, chat_id: str, token: str | None = None, *, strict: bool = False) -> None:
        observer = self.reread_observer
        if observer is not None and hasattr(observer, "release_dispatch"):
            try:
                parameters = inspect.signature(observer.release_dispatch).parameters
                supports_token = "token" in parameters or any(
                    item.kind is inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                )
                positional = [
                    item for item in parameters.values()
                    if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
                ]
                supports_token = supports_token or len(positional) >= 2 or any(
                    item.kind is inspect.Parameter.VAR_POSITIONAL
                    for item in parameters.values()
                )
                if supports_token:
                    await observer.release_dispatch(chat_id, token)
                else:
                    await observer.release_dispatch(chat_id)
            except Exception:
                logger.debug("[RereadAction] cooldown release degraded", exc_info=True)
                if strict:
                    raise

    async def _claim_observer(self, chat_id: str, trigger_kind: str):
        observer = self.reread_observer
        if observer is None or not hasattr(observer, "claim_dispatch"):
            return None
        try:
            parameters = inspect.signature(observer.claim_dispatch).parameters
        except (TypeError, ValueError):
            parameters = {}
        supports_trigger = "trigger_kind" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        if supports_trigger:
            return await observer.claim_dispatch(chat_id, trigger_kind=trigger_kind)
        return await observer.claim_dispatch(chat_id)

    async def _rollback_send_claim(self, chat_id: str, send_key: str, reason: str) -> None:
        """Best-effort rollback for a claim that will not produce a message."""
        coordinator = self.runtime_coordinator
        if coordinator is None:
            return
        mark_failed = getattr(coordinator, "mark_send_failed", None)
        if callable(mark_failed):
            try:
                marked = await mark_failed(chat_id, send_key, reason)
                get_claim = getattr(coordinator, "get_send_claim", None)
                if callable(get_claim):
                    claim = await get_claim(chat_id, send_key)
                    if claim is None or claim.get("status") != "claimed":
                        return
                elif marked is True:
                    return
            except BaseException:
                logger.warning(
                    "[RereadAction] send claim failure mark degraded chat=%s key=%s",
                    chat_id,
                    send_key,
                    exc_info=True,
                )
        release_claim = getattr(coordinator, "release_send_claim", None)
        if not callable(release_claim):
            self._note_claim_rollback_degraded(chat_id, send_key, "release_interface_missing")
            return
        try:
            try:
                parameters = inspect.signature(release_claim).parameters
            except (TypeError, ValueError):
                parameters = {}
            positional = [
                item for item in parameters.values()
                if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            if len(positional) >= 2 or any(
                item.kind is inspect.Parameter.VAR_POSITIONAL for item in parameters.values()
            ):
                await release_claim(chat_id, send_key)
            else:
                keyword_parameters = [
                    item for item in parameters.values()
                    if item.kind is inspect.Parameter.KEYWORD_ONLY
                ]
                if len(keyword_parameters) >= 2:
                    await release_claim(
                        **{
                            keyword_parameters[0].name: chat_id,
                            keyword_parameters[1].name: send_key,
                        }
                    )
                else:
                    await release_claim(chat_id, send_key)
            get_claim = getattr(coordinator, "get_send_claim", None)
            if callable(get_claim):
                claim = await get_claim(chat_id, send_key)
                if claim is not None and claim.get("status") == "claimed":
                    self._note_claim_rollback_degraded(chat_id, send_key, "release_still_claimed")
        except BaseException:
            self._note_claim_rollback_degraded(chat_id, send_key, "release_exception")
            logger.warning(
                "[RereadAction] send claim release degraded chat=%s key=%s",
                chat_id,
                send_key,
                exc_info=True,
            )

    async def _commit_observer(self, chat_id: str, token: str | None, trigger_kind: str) -> bool:
        observer = self.reread_observer
        if observer is None or not token or not hasattr(observer, "commit_dispatch"):
            return True
        try:
            parameters = inspect.signature(observer.commit_dispatch).parameters
        except (TypeError, ValueError):
            parameters = {}
        supports_trigger = "trigger_kind" in parameters or any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        if supports_trigger:
            return bool(await observer.commit_dispatch(chat_id, token, trigger_kind=trigger_kind))
        return bool(await observer.commit_dispatch(chat_id, token))

    async def _settle_sent(
        self,
        request: RereadActionRequest,
        send_key: str,
        outbound_ids: tuple[str, ...],
        observer_token: str | None,
        coordinator,
    ) -> bool:
        if coordinator is not None and hasattr(coordinator, "commit_send"):
            already_committed = False
            get_claim = getattr(coordinator, "get_send_claim", None)
            if callable(get_claim):
                claim = await get_claim(request.chat_id, send_key)
                already_committed = bool(claim and claim.get("status") == "committed")
            if not already_committed:
                await coordinator.commit_send(request.chat_id, send_key, list(outbound_ids))
        return await self._commit_observer(request.chat_id, observer_token, request.trigger_kind)

    async def _schedule_settlement_retry(
        self,
        request: RereadActionRequest,
        send_key: str,
        outbound_ids: tuple[str, ...],
        observer_token: str | None,
        coordinator,
    ) -> None:
        if self._shutting_down:
            self._settlement_retry_stats["retry_rejected"] += 1
            await self._release_observer_claim(request.chat_id, observer_token)
            return
        existing = self._settlement_retry_tasks.get(send_key)
        if existing is not None and not existing.done():
            return
        if len(self._settlement_retry_tasks) >= self.MAX_SETTLEMENT_RETRY_TASKS:
            self._settlement_retry_stats["retry_rejected"] += 1
            await self._release_observer_claim(request.chat_id, observer_token)
            return

        async def _retry() -> None:
            settled = False
            cancelled = False
            try:
                for attempt in range(3):
                    try:
                        settled = await self._settle_sent(
                            request, send_key, outbound_ids, observer_token, coordinator
                        )
                        if settled:
                            return
                    except asyncio.CancelledError:
                        cancelled = True
                        raise
                    except Exception:
                        logger.warning(
                            "[RereadAction] settlement retry failed attempt=%s chat=%s",
                            attempt + 1,
                            request.chat_id,
                            exc_info=True,
                        )
                    if attempt < 2:
                        await asyncio.sleep(0.1 * (attempt + 1))
            finally:
                await self._finalize_retry(send_key, settled=settled, cancelled=cancelled)

        task = asyncio.create_task(_retry())
        self._settlement_retry_tasks[send_key] = task
        self._retry_leases[send_key] = (request.chat_id, observer_token)
        self._background_tasks.add(task)
        self._settlement_retry_stats["retry_pending"] += 1

        def _forget(_task: asyncio.Task) -> None:
            if self._settlement_retry_tasks.get(send_key) is _task:
                self._settlement_retry_tasks.pop(send_key, None)
            self._background_tasks.discard(_task)
            if send_key in self._retry_leases:
                cleanup = asyncio.create_task(
                    self._finalize_retry(send_key, settled=False, cancelled=True)
                )
                self._retry_cleanup_tasks.add(cleanup)
                self._background_tasks.add(cleanup)

                def _forget_cleanup(done: asyncio.Task) -> None:
                    self._retry_cleanup_tasks.discard(done)
                    self._background_tasks.discard(done)

                cleanup.add_done_callback(_forget_cleanup)

        task.add_done_callback(_forget)

    async def _finalize_retry(self, send_key: str, *, settled: bool, cancelled: bool) -> None:
        lease = self._retry_leases.get(send_key)
        if lease is None:
            return
        chat_id, observer_token = lease
        released = settled
        try:
            if not settled:
                await asyncio.shield(self._release_observer_claim(chat_id, observer_token, strict=True))
                released = True
        except BaseException:
            attempts = self._retry_release_attempts.get(send_key, 0) + 1
            self._retry_release_attempts[send_key] = attempts
            logger.error("[RereadAction] retry lease release degraded", exc_info=True)
            if attempts < 3:
                retry_task = asyncio.create_task(
                    self._finalize_retry(send_key, settled=False, cancelled=True)
                )
                self._retry_cleanup_tasks.add(retry_task)
                self._background_tasks.add(retry_task)

                def _forget_release(done: asyncio.Task) -> None:
                    self._retry_cleanup_tasks.discard(done)
                    self._background_tasks.discard(done)

                retry_task.add_done_callback(_forget_release)
            else:
                if send_key not in self._retry_release_exhausted:
                    self._retry_release_exhausted.add(send_key)
                    self._settlement_retry_stats["release_exhausted"] = (
                        self._settlement_retry_stats.get("release_exhausted", 0) + 1
                    )
            return
        finally:
            if released and send_key in self._retry_leases:
                self._retry_leases.pop(send_key, None)
                self._retry_release_attempts.pop(send_key, None)
                if send_key in self._retry_release_exhausted:
                    self._retry_release_exhausted.discard(send_key)
                    self._settlement_retry_stats["release_exhausted"] = max(
                        0, self._settlement_retry_stats.get("release_exhausted", 0) - 1
                    )
                self._settlement_retry_stats["retry_pending"] = max(
                    0, self._settlement_retry_stats["retry_pending"] - 1
                )
                if not settled and not cancelled:
                    self._settlement_retry_stats["retry_exhausted"] += 1

    async def drain_settlement_retries(self) -> None:
        tasks = [task for task in self._settlement_retry_tasks.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self, *, wait_for_active: bool = True) -> None:
        self._shutting_down = True
        current = asyncio.current_task()
        active = [
            task for task in self._active_dispatches
            if task is not current and not task.done()
        ]
        if active and wait_for_active:
            await asyncio.gather(*active, return_exceptions=True)
        tasks = [task for task in self._settlement_retry_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for send_key in list(self._retry_leases):
            await self._finalize_retry(send_key, settled=False, cancelled=True)
        cleanup_tasks = [task for task in self._retry_cleanup_tasks if not task.done()]
        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        self._settlement_retry_tasks.clear()
        self._active_dispatches.clear()
        if not self._retry_release_exhausted:
            self._retry_leases.clear()
        if not self._active_dispatches:
            self._pending_dispatch_shutdown = False
        self._background_tasks.clear()

    async def force_shutdown(self, timeout_sec: float = 1.0) -> bool:
        """Cancel in-flight sends before forced resource teardown."""
        self._shutting_down = True
        current = asyncio.current_task()
        active = [task for task in self._active_dispatches if task is not current and not task.done()]
        for task in active:
            task.cancel()
        if active:
            try:
                await asyncio.wait_for(asyncio.gather(*active, return_exceptions=True), timeout=max(0.0, timeout_sec))
            except asyncio.TimeoutError:
                self._pending_dispatch_shutdown = True
                return False
        await self.shutdown()
        return True

    def resume(self) -> bool:
        if self._pending_dispatch_shutdown or self._retry_release_exhausted or self._active_dispatches or any(
            not task.done() for task in self._settlement_retry_tasks.values()
        ) or self._retry_leases:
            return False
        self._shutting_down = False
        return True

    def describe_status(self) -> dict[str, int | bool]:
        return {
            "shutting_down": self._shutting_down,
            "retry_pending": self._settlement_retry_stats["retry_pending"],
            "retry_exhausted": self._settlement_retry_stats["retry_exhausted"],
            "retry_rejected": self._settlement_retry_stats["retry_rejected"],
            "release_exhausted": self._settlement_retry_stats.get("release_exhausted", 0),
            "claim_rollback_degraded": self._claim_rollback_degraded,
            "pending_dispatch_shutdown": self._pending_dispatch_shutdown,
        }

    @staticmethod
    def _send_key(request: RereadActionRequest) -> str:
        payload = "\x1f".join((request.chat_id, request.trigger_kind, request.fingerprint, *request.source_event_ids))
        return "reread:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    async def dispatch(self, event: Any, request: RereadActionRequest) -> RereadDispatchResult:
        if self._shutting_down:
            return RereadDispatchResult("shutdown", detail="dispatcher_shutdown")
        task = asyncio.current_task()
        if task is not None:
            self._active_dispatches.add(task)
        try:
            return await self._dispatch_impl(event, request)
        finally:
            if task is not None:
                self._active_dispatches.discard(task)

    async def _dispatch_impl(self, event: Any, request: RereadActionRequest) -> RereadDispatchResult:
        if self._shutting_down:
            return RereadDispatchResult("shutdown", detail="dispatcher_shutdown")
        context = self.context
        if context is None or not hasattr(context, "send_message"):
            return RereadDispatchResult("failed", detail="context_unavailable")
        coordinator = self.runtime_coordinator
        send_key = self._send_key(request)
        claim_owned = False
        observer_token = None
        send_completed = False
        outbound_ids: tuple[str, ...] = ()
        try:
            if coordinator is not None and hasattr(coordinator, "claim_send"):
                if not await coordinator.claim_send(request.chat_id, send_key):
                    return RereadDispatchResult("duplicate", detail="send_claim_exists")
                claim_owned = True
            if self.reread_observer is not None and hasattr(self.reread_observer, "claim_dispatch"):
                observer_token = await self._claim_observer(request.chat_id, request.trigger_kind)
                if not observer_token:
                    if claim_owned:
                        await self._rollback_send_claim(request.chat_id, send_key, "reread_cooldown")
                    return RereadDispatchResult("cooldown", detail="group_reread_cooldown")
            turn = event.get_extra("astrmai_turn_identity", None) if hasattr(event, "get_extra") else None
            if turn is not None and coordinator is not None and hasattr(coordinator, "is_current_turn"):
                if not await coordinator.is_current_turn(turn):
                    if claim_owned:
                        await self._rollback_send_claim(request.chat_id, send_key, "stale_turn")
                    await self._release_observer_claim(request.chat_id, observer_token)
                    return RereadDispatchResult("stale", detail="stale_turn")
            if not outbound_send_allowed(event):
                if claim_owned:
                    await self._rollback_send_claim(request.chat_id, send_key, "shutdown_rejected")
                await self._release_observer_claim(request.chat_id, observer_token)
                return RereadDispatchResult("shutdown", detail="shutdown_rejected")
            chain = MessageChain()
            chain.chain.append(Comp.Plain(request.text))
            result = await context.send_message(getattr(event, "unified_msg_origin", request.chat_id), chain)
            outbound_ids = () if result is None or isinstance(result, bool) else (str(result),)
            send_completed = True
            settlement_ok = await self._settle_sent(
                request, send_key, outbound_ids, observer_token, coordinator
            )
            if not settlement_ok:
                logger.error(
                    "[RereadAction] send succeeded but cooldown settlement degraded chat=%s kind=%s",
                    request.chat_id,
                    request.trigger_kind,
                )
                await self._schedule_settlement_retry(
                    request, send_key, outbound_ids, observer_token, coordinator
                )
            await self._record_dialogue(event, request, outbound_ids)
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_group_reread_dispatched", True)
                event.set_extra("astrmai_reread_trigger_kind", request.trigger_kind)
                event.set_extra("astrmai_reread_outbound_message_ids", list(outbound_ids))
            logger.info("[RereadAction] sent kind=%s chat=%s", request.trigger_kind, request.chat_id)
            detail = "" if settlement_ok else "settlement_degraded"
            if hasattr(event, "set_extra") and detail:
                event.set_extra("astrmai_reread_settlement_status", detail)
            return RereadDispatchResult("sent", outbound_ids, detail=detail)
        except asyncio.CancelledError:
            if not send_completed:
                if claim_owned:
                    await self._rollback_send_claim(request.chat_id, send_key, "cancelled")
                await self._release_observer_claim(request.chat_id, observer_token)
                raise
            settlement_ok = False
            settlement_failed = False
            try:
                settlement_ok = await asyncio.shield(
                    self._settle_sent(request, send_key, outbound_ids, observer_token, coordinator)
                )
            except BaseException:
                settlement_failed = True
                logger.error("[RereadAction] post-send settlement failed after cancellation", exc_info=True)
            if settlement_failed or not settlement_ok:
                try:
                    await asyncio.shield(self._release_observer_claim(request.chat_id, observer_token))
                except BaseException:
                    logger.error("[RereadAction] cooldown release failed after degraded settlement", exc_info=True)
            return RereadDispatchResult(
                "sent",
                outbound_ids,
                detail="" if settlement_ok else "settlement_degraded",
            )
        except Exception as exc:
            if send_completed:
                logger.error(
                    "[RereadAction] send succeeded but settlement failed chat=%s kind=%s",
                    request.chat_id,
                    request.trigger_kind,
                    exc_info=True,
                )
                await self._schedule_settlement_retry(
                    request, send_key, outbound_ids, observer_token, coordinator
                )
                return RereadDispatchResult("sent", outbound_ids, detail="settlement_degraded")
            if claim_owned:
                await self._rollback_send_claim(request.chat_id, send_key, str(exc))
            await self._release_observer_claim(request.chat_id, observer_token)
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
