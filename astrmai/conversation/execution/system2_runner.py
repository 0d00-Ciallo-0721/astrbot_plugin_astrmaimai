from __future__ import annotations

import asyncio

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import debug_trace
from ...infrastructure.runtime.turn_call_ledger import (
    begin_stage,
    clamp_timeout_to_turn_budget,
    finish_stage,
)
from .followup_manager import FollowupManager


class System2Runner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.followup_manager = FollowupManager(runtime)

    async def get_sys2_lock(self, chat_id: str, thread_id: str = ""):
        getter = self.runtime.runtime_coordinator.get_sys2_lock
        try:
            lock = await getter(chat_id, thread_id=thread_id)
            self._last_lock_scope = "thread" if thread_id else "chat_fallback"
            return lock
        except TypeError:
            self._last_lock_scope = "chat"
            return await getter(chat_id)

    def _prepare_queue_events(self, main_event, events_to_process: list | None) -> list:
        return events_to_process.copy() if isinstance(events_to_process, list) and events_to_process else [main_event]

    def _reset_runtime_reply_extras(self, main_event) -> None:
        main_event.set_extra("astrmai_reply_sent", False)
        main_event.set_extra("astrmai_wait_targets", [])
        main_event.set_extra("astrmai_wait_target_name", "")

    @staticmethod
    def _turn_thread_id(event) -> str:
        turn = event.get_extra("astrmai_turn_identity", None)
        return str(
            getattr(turn, "thread_id", "")
            or event.get_extra("astrmai_turn_thread_id", "")
            or ""
        ).strip()

    def _lane_prepare_timeout(self, event) -> float:
        timing = getattr(getattr(self.runtime, "config", None), "timing", None)
        try:
            configured = float(getattr(timing, "lane_prepare_timeout_sec", 20.0) or 20.0)
        except (TypeError, ValueError):
            configured = 20.0
        return clamp_timeout_to_turn_budget(event, max(0.1, configured), reserve_for_reply=True)

    async def _prepare_system2_runtime(self, main_event, chat_id: str) -> None:
        await self.runtime.state_engine.consume_energy(chat_id)
        lane_stage = begin_stage(
            main_event,
            "system2.lane_prepare",
            critical_path=True,
            metadata={"chat_id": str(chat_id or "")},
        )
        timeout_sec = self._lane_prepare_timeout(main_event)
        try:
            if timeout_sec <= 0.0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(
                self.runtime.lane_manager.ensure_lane(
                    lane_key=LaneKey(
                        subsystem="sys2",
                        task_family="dialog",
                        scope_id=(
                            f"{chat_id}#thread:{self._turn_thread_id(main_event)}"
                            if getattr(main_event.get_extra("astrmai_turn_identity", None), "thread_id", "")
                            else chat_id
                        ),
                    ),
                    base_origin=(
                        f"{chat_id}@@thread:{self._turn_thread_id(main_event)}"
                        if getattr(main_event.get_extra("astrmai_turn_identity", None), "thread_id", "")
                        else chat_id
                    ),
                ),
                timeout=max(0.1, timeout_sec),
            )
        except asyncio.TimeoutError:
            finish_stage(main_event, lane_stage, status="timeout", reason="queue_timeout")
            main_event.set_extra("astrmai_execution_status", "queue_timeout")
            main_event.set_extra("astrmai_queue_timeout_stage", "system2.lane_prepare")
            raise
        except asyncio.CancelledError:
            finish_stage(main_event, lane_stage, status="cancelled", reason="acquire_cancelled")
            raise
        except Exception as exc:
            finish_stage(main_event, lane_stage, status="error", reason=type(exc).__name__)
            raise
        finish_stage(main_event, lane_stage, metadata={"timeout_sec": timeout_sec})

    @staticmethod
    async def _acquire_lock_bounded(lock, timeout_sec: float) -> str:
        acquire = getattr(lock, "acquire", None)
        if callable(acquire):
            await asyncio.wait_for(acquire(), timeout=max(0.1, timeout_sec))
            return "acquire"
        enter = getattr(lock, "__aenter__", None)
        if callable(enter):
            await asyncio.wait_for(enter(), timeout=max(0.1, timeout_sec))
            return "context"
        raise TypeError("system2 lock must provide acquire() or async context manager")

    @staticmethod
    async def _release_lock(lock, acquire_mode: str) -> None:
        if acquire_mode == "context":
            exit_method = getattr(lock, "__aexit__", None)
            if callable(exit_method):
                await exit_method(None, None, None)
            return
        locked = getattr(lock, "locked", None)
        release = getattr(lock, "release", None)
        if callable(release) and (not callable(locked) or locked()):
            release()

    async def _execute_planner(self, main_event, queue_events: list) -> bool:
        await self.runtime.system2_planner.plan_and_execute(main_event, queue_events)
        return bool(main_event.get_extra("astrmai_reply_sent", False))

    def _lock_wait_timeout(self, event) -> float:
        timing = getattr(getattr(self.runtime, "config", None), "timing", None)
        try:
            configured = float(getattr(timing, "sys2_lock_wait_timeout_sec", 20.0) or 20.0)
        except (TypeError, ValueError):
            configured = 20.0
        return clamp_timeout_to_turn_budget(event, max(0.1, configured), reserve_for_reply=True)

    async def _finalize_followups(self, chat_id: str, main_event, reply_sent: bool) -> None:
        await self.followup_manager.sync_wait_targets(chat_id, main_event)
        await self.followup_manager.finalize_after_reply(chat_id, main_event, reply_sent)

    async def run(self, main_event, events_to_process: list | None = None):
        chat_id = main_event.unified_msg_origin
        thread_id = self._turn_thread_id(main_event)
        lock = await self.get_sys2_lock(chat_id, thread_id)
        lock_scope = str(getattr(self, "_last_lock_scope", "thread" if thread_id else "chat_fallback"))
        queue_events = self._prepare_queue_events(main_event, events_to_process)
        debug_trace(main_event, "system2.enter", chat_id=chat_id, queue_size=len(queue_events))
        logger.debug(f"[{chat_id}] System 2 request queued and waiting for execution slot.")

        lock_stage = begin_stage(
            main_event,
            "system2.chat_lock_wait",
            critical_path=True,
            metadata={
                "chat_id": str(chat_id or ""),
                "thread_id": self._turn_thread_id(main_event),
                "lock_scope": lock_scope,
                "queue_size": len(queue_events),
            },
        )
        acquired_mode = ""
        try:
            timeout_sec = self._lock_wait_timeout(main_event)
            if timeout_sec <= 0.0:
                raise asyncio.TimeoutError
            acquired_mode = await self._acquire_lock_bounded(lock, timeout_sec)
            finish_stage(main_event, lock_stage, metadata={"timeout_sec": timeout_sec})
            lock_stage = ""
            self._reset_runtime_reply_extras(main_event)
            await self._prepare_system2_runtime(main_event, chat_id)
            reply_sent = await self._execute_planner(main_event, queue_events)
            await self._finalize_followups(chat_id, main_event, reply_sent)
            return reply_sent
        except asyncio.TimeoutError:
            if lock_stage:
                finish_stage(main_event, lock_stage, status="timeout", reason="queue_timeout")
            timeout_stage = str(main_event.get_extra("astrmai_queue_timeout_stage", "") or "")
            if not timeout_stage:
                timeout_stage = "system2.chat_lock_wait"
                main_event.set_extra("astrmai_execution_status", "queue_timeout")
                main_event.set_extra("astrmai_queue_timeout_stage", timeout_stage)
            debug_trace(main_event, "system2.queue_timeout", wait_stage=timeout_stage)
            return False
        except asyncio.CancelledError:
            finish_stage(main_event, lock_stage, status="cancelled", reason="acquire_cancelled")
            raise
        except Exception as exc:
            finish_stage(main_event, lock_stage, status="error", reason=type(exc).__name__)
            raise
        finally:
            if acquired_mode:
                await self._release_lock(lock, acquired_mode)
            logger.debug(f"[AstrMai] System2 execution finished safely for {chat_id}.")
            debug_trace(
                main_event,
                "system2.exit",
                reply_sent=bool(main_event.get_extra("astrmai_reply_sent", False)),
            )
__all__ = ["System2Runner"]
