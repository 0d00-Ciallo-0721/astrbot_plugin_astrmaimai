from __future__ import annotations

import asyncio
import inspect
import sys
import uuid

from astrbot.api import logger

from ...infrastructure.runtime.dialog_lane_identity import resolve_dialog_lane_identity
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import debug_trace, record_terminal_outcome
from ...infrastructure.runtime.background_task_budget import current_background_execution_timeout
from ...infrastructure.runtime.turn_call_ledger import (
    begin_stage,
    clamp_timeout_to_turn_budget,
    finish_stage,
    remaining_turn_budget,
)
from ..contracts.turn_context import ensure_turn_context
from ..contracts.turn_outcome import mark_system2_handled
from .followup_manager import FollowupManager


class System2QueueTimeout(asyncio.TimeoutError):
    def __init__(self, stage: str) -> None:
        self.stage = str(stage or "system2.chat_lock_wait")
        super().__init__(self.stage)


class System2Runner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.followup_manager = FollowupManager(runtime)

    async def get_sys2_lock(self, chat_id: str, thread_id: str = ""):
        getter = self.runtime.runtime_coordinator.get_sys2_lock
        try:
            inspect.signature(getter).bind(chat_id, thread_id=thread_id)
        except (TypeError, ValueError):
            lock = await getter(chat_id)
            self._last_lock_scope = "chat"
            return lock
        else:
            lock = await getter(chat_id, thread_id=thread_id)
            self._last_lock_scope = "thread" if thread_id else "chat_fallback"
            return lock

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

    def _resolve_dialog_lane_identity(self, event, chat_id: str) -> tuple[LaneKey, str]:
        return resolve_dialog_lane_identity(event, chat_id)

    async def _prepare_system2_runtime(self, main_event, chat_id: str) -> None:
        energy_stage = begin_stage(
            main_event,
            "system2.energy_prepare",
            critical_path=True,
            metadata={"chat_id": str(chat_id or "")},
        )
        timing = getattr(getattr(self.runtime, "config", None), "timing", None)
        try:
            configured_energy_timeout = float(
                getattr(timing, "energy_prepare_timeout_sec", 5.0) or 5.0
            )
        except (TypeError, ValueError):
            configured_energy_timeout = 5.0
        energy_timeout = clamp_timeout_to_turn_budget(
            main_event,
            max(0.1, configured_energy_timeout),
            reserve_for_reply=True,
        )
        try:
            await asyncio.wait_for(
                self.runtime.state_engine.consume_energy(chat_id),
                timeout=max(0.1, energy_timeout),
            )
        except asyncio.TimeoutError:
            finish_stage(main_event, energy_stage, status="timeout", reason="queue_timeout")
            main_event.set_extra("astrmai_execution_status", "queue_timeout")
            # A preparation timeout is retryable deferred work, not a handled
            # System2 turn.  Marking it handled would make deferred replay
            # reject the same turn before the retry factory gets a chance to
            # run.
            main_event.set_extra("astrmai_queue_timeout_stage", "system2.energy_prepare")
            raise System2QueueTimeout("system2.energy_prepare")
        except asyncio.CancelledError:
            finish_stage(main_event, energy_stage, status="cancelled", reason="acquire_cancelled")
            raise
        except Exception as exc:
            finish_stage(main_event, energy_stage, status="error", reason=type(exc).__name__)
            raise
        finish_stage(main_event, energy_stage, metadata={"timeout_sec": energy_timeout})
        lane_stage = begin_stage(
            main_event,
            "system2.lane_prepare",
            critical_path=True,
            metadata={"chat_id": str(chat_id or "")},
        )
        timeout_sec = self._lane_prepare_timeout(main_event)
        try:
            lane_key, base_origin = self._resolve_dialog_lane_identity(main_event, chat_id)
            if timeout_sec <= 0.0:
                raise asyncio.TimeoutError
            await asyncio.wait_for(
                self.runtime.lane_manager.ensure_lane(
                    lane_key=lane_key,
                    base_origin=base_origin,
                ),
                timeout=max(0.1, timeout_sec),
            )
        except asyncio.TimeoutError:
            finish_stage(main_event, lane_stage, status="timeout", reason="queue_timeout")
            main_event.set_extra("astrmai_execution_status", "queue_timeout")
            main_event.set_extra("astrmai_queue_timeout_stage", "system2.lane_prepare")
            raise System2QueueTimeout("system2.lane_prepare")
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
            result = acquire()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=max(0.1, timeout_sec))
            # The compatibility contract is deliberately strict: only an
            # explicit True means that the caller owns the lock.  In
            # particular, None must not be guessed as successful acquisition.
            if result is not True:
                raise asyncio.TimeoutError("system2 lock acquire did not return True")
            return "acquire"
        enter = getattr(lock, "__aenter__", None)
        exit_method = getattr(lock, "__aexit__", None)
        if callable(enter) and callable(exit_method):
            try:
                result = enter()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=max(0.1, timeout_sec))
            except BaseException as exc:
                # A legacy __aenter__ may acquire before its final await.  It
                # is not safe to assume that a cancelled/timeout enter left
                # the lock untouched, so make a best-effort compensating exit.
                try:
                    cleanup = exit_method(type(exc), exc, exc.__traceback__)
                    if inspect.isawaitable(cleanup):
                        await cleanup
                except BaseException as cleanup_exc:
                    logger.warning(
                        "[AstrMai] system2 context lock cleanup failed "
                        f"after {type(exc).__name__}: {type(cleanup_exc).__name__}"
                    )
                raise
            return "context"
        raise TypeError("system2 lock must provide acquire() or async context manager")

    @staticmethod
    async def _release_lock(lock, acquire_mode: str, exc_info=None) -> None:
        if acquire_mode == "context":
            exit_method = getattr(lock, "__aexit__", None)
            if callable(exit_method):
                exc_type, exc_value, traceback = exc_info or (None, None, None)
                result = exit_method(exc_type, exc_value, traceback)
                if inspect.isawaitable(result):
                    await result
            return
        locked = getattr(lock, "locked", None)
        release = getattr(lock, "release", None)
        if callable(release) and (not callable(locked) or locked()):
            release()

    async def _execute_planner(self, main_event, queue_events: list) -> bool:
        await self.runtime.system2_planner.plan_and_execute(main_event, queue_events)
        return bool(main_event.get_extra("astrmai_reply_sent", False))

    async def _record_pre_planner_timeout(self, event, stage: str) -> None:
        trace_state = str(event.get_extra("astrmai_pre_planner_trace_state", "") or "")
        if trace_state in {"pending", "persisted"} or event.get_extra(
            "astrmai_pre_planner_trace_finalized", False
        ):
            return
        event.set_extra("astrmai_pre_planner_trace_state", "pending")
        recorder = getattr(getattr(self.runtime, "system2_planner", None), "record_turn_trace", None)
        if not callable(recorder):
            event.set_extra("astrmai_pre_planner_trace_state", "retryable")
            return
        try:
            result = recorder(
                str(getattr(event, "unified_msg_origin", "") or ""),
                event,
                status="queue_timeout",
                reply_text=None,
            )
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            event.set_extra("astrmai_pre_planner_trace_state", "retryable")
            raise
        except Exception as exc:
            event.set_extra("astrmai_pre_planner_trace_state", "retryable")
            logger.warning(
                "[AstrMai] pre-planner queue timeout trace failed "
                f"stage={stage} error_type={type(exc).__name__}"
            )
            return
        event.set_extra("astrmai_pre_planner_timeout_recorded", True)
        event.set_extra("astrmai_pre_planner_trace_finalized", True)
        event.set_extra("astrmai_pre_planner_trace_state", "persisted")

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
        attempt_id = uuid.uuid4().hex
        main_event.set_extra("astrmai_attempt_id", attempt_id)
        for key in (
            "astrmai_execution_status", "astrmai_execution_signal",
            "astrmai_queue_timeout_stage", "astrmai_cancel_source",
            "astrmai_terminal_outcome", "astrmai_terminal_attempt_id",
            "astrmai_budget_timeout_stage",
        ):
            main_event.set_extra(key, "")
        main_event.set_extra("astrmai_reply_sent", False)
        main_event.set_extra("astrmai_side_input_timings", [])
        ensure_turn_context(main_event).side_inputs.timings.clear()
        chat_id = main_event.unified_msg_origin
        thread_id = self._turn_thread_id(main_event)
        queue_events = self._prepare_queue_events(main_event, events_to_process)
        debug_trace(main_event, "system2.enter", chat_id=chat_id, queue_size=len(queue_events))
        logger.debug(f"[{chat_id}] System 2 request queued and waiting for execution slot.")

        lock_stage = begin_stage(
            main_event,
            "system2.chat_lock_resolve",
            critical_path=True,
            metadata={
                "chat_id": str(chat_id or ""),
                "thread_id": self._turn_thread_id(main_event),
                "queue_size": len(queue_events),
            },
        )
        lock = None
        acquired_mode = ""
        lock_exc_info = None
        release_exc = None
        try:
            timeout_sec = self._lock_wait_timeout(main_event)
            if timeout_sec <= 0.0:
                raise System2QueueTimeout("system2.chat_lock_resolve")
            lock_deadline = asyncio.get_running_loop().time() + timeout_sec
            try:
                lock = await asyncio.wait_for(
                    self.get_sys2_lock(chat_id, thread_id),
                    timeout=max(0.1, timeout_sec),
                )
            except asyncio.TimeoutError as exc:
                raise System2QueueTimeout("system2.chat_lock_resolve") from exc
            if lock is None:
                raise System2QueueTimeout("system2.chat_lock_resolve")
            finish_stage(main_event, lock_stage, metadata={"timeout_sec": timeout_sec})
            lock_stage = begin_stage(
                main_event,
                "system2.chat_lock_wait",
                critical_path=True,
                metadata={
                    "chat_id": str(chat_id or ""),
                    "thread_id": self._turn_thread_id(main_event),
                    "lock_scope": str(
                        getattr(
                            self,
                            "_last_lock_scope",
                            "thread" if thread_id else "chat_fallback",
                        )
                    ),
                    "queue_size": len(queue_events),
                },
            )
            remaining_timeout_sec = lock_deadline - asyncio.get_running_loop().time()
            if remaining_timeout_sec <= 0.0:
                raise System2QueueTimeout("system2.chat_lock_wait")
            try:
                acquired_mode = await self._acquire_lock_bounded(
                    lock,
                    remaining_timeout_sec,
                )
            except asyncio.TimeoutError as exc:
                raise System2QueueTimeout("system2.chat_lock_wait") from exc
            finish_stage(
                main_event,
                lock_stage,
                metadata={"timeout_sec": remaining_timeout_sec},
            )
            lock_stage = ""
            self._reset_runtime_reply_extras(main_event)
            await self._prepare_system2_runtime(main_event, chat_id)
            strong = any(main_event.get_extra(key, False) for key in (
                "astrmai_at_bot_wakeup", "astrmai_group_direct_wakeup", "astrmai_reply_wakeup",
            ))
            remaining = remaining_turn_budget(main_event) if strong else None
            budget_stage = begin_stage(main_event, "system2.planner", metadata={
                "attempt_id": attempt_id, "budget_kind": "turn",
                "configured_timeout": remaining,
                "replay_count": int(main_event.get_extra("astrmai_atwake_replay_count", 0)),
            })
            deadline = asyncio.timeout(remaining)
            try:
                async with deadline:
                    reply_sent = await self._execute_planner(main_event, queue_events)
            except asyncio.TimeoutError:
                if not deadline.expired():
                    finish_stage(main_event, budget_stage, status="error", reason="TimeoutError")
                    raise
                main_event.set_extra("astrmai_execution_status", "budget_exhausted")
                main_event.set_extra("astrmai_budget_timeout_stage", "system2.planner")
                finish_stage(main_event, budget_stage, status="timeout", reason="budget_exhausted",
                             metadata={"remaining_budget": remaining_turn_budget(main_event)})
                return bool(main_event.get_extra("astrmai_reply_sent", False))
            except asyncio.CancelledError:
                execution_timed_out = current_background_execution_timeout()
                finish_stage(main_event, budget_stage, status="timeout" if execution_timed_out else "cancelled",
                             reason="execution_timeout" if execution_timed_out else "CancelledError",
                             metadata={"cancel_source": "background_execution_timeout" if execution_timed_out else
                                       str(main_event.get_extra("astrmai_cancel_source", ""))})
                raise
            except BaseException as exc:
                finish_stage(main_event, budget_stage, status="error", reason=type(exc).__name__)
                raise
            finish_stage(main_event, budget_stage, metadata={"remaining_budget": remaining_turn_budget(main_event)})
            await self._finalize_followups(chat_id, main_event, reply_sent)
            return reply_sent
        except System2QueueTimeout as exc:
            lock_exc_info = (type(exc), exc, exc.__traceback__)
            if lock_stage:
                finish_stage(main_event, lock_stage, status="timeout", reason="queue_timeout")
            timeout_stage = str(main_event.get_extra("astrmai_queue_timeout_stage", "") or exc.stage)
            if not timeout_stage:
                timeout_stage = "system2.chat_lock_wait"
            main_event.set_extra("astrmai_execution_status", "queue_timeout")
            main_event.set_extra("astrmai_queue_timeout_stage", timeout_stage)
            debug_trace(main_event, "system2.queue_timeout", wait_stage=timeout_stage)
            await self._record_pre_planner_timeout(main_event, timeout_stage)
            return False
        except asyncio.CancelledError:
            lock_exc_info = sys.exc_info()
            if current_background_execution_timeout():
                main_event.set_extra("astrmai_execution_status", "execution_timeout")
                main_event.set_extra("astrmai_budget_timeout_stage", "attention.background_execution")
                main_event.set_extra("astrmai_cancel_source", "background_execution_timeout")
            mark_system2_handled(main_event, "system2_cancelled")
            finish_stage(main_event, lock_stage, status="cancelled", reason="acquire_cancelled")
            raise
        except Exception as exc:
            lock_exc_info = (type(exc), exc, exc.__traceback__)
            finish_stage(main_event, lock_stage, status="error", reason=type(exc).__name__)
            raise
        except BaseException as exc:
            lock_exc_info = (type(exc), exc, exc.__traceback__)
            finish_stage(main_event, lock_stage, status="error", reason=type(exc).__name__)
            raise
        finally:
            if acquired_mode:
                try:
                    await self._release_lock(lock, acquired_mode, lock_exc_info)
                except BaseException as release_error:
                    if lock_exc_info:
                        logger.warning(
                            "[AstrMai] system2 lock release failed while preserving "
                            f"{lock_exc_info[0].__name__}: {type(release_error).__name__}"
                        )
                    else:
                        release_exc = release_error
                        lock_exc_info = (type(release_error), release_error, release_error.__traceback__)
            logger.debug(f"[AstrMai] System2 execution finished safely for {chat_id}.")
            execution_status = str(main_event.get_extra("astrmai_execution_status", "") or "")
            if bool(main_event.get_extra("astrmai_reply_sent", False)):
                terminal_status = "reply_sent"
            elif lock_exc_info and issubclass(lock_exc_info[0], asyncio.CancelledError):
                terminal_status = (
                    "timeout" if execution_status == "execution_timeout" else
                    "superseded" if main_event.get_extra("astrmai_cancel_source", "")
                    in {"generation_advanced", "turn_task_replaced"} else "cancelled"
                )
            elif lock_exc_info and not isinstance(lock_exc_info[1], System2QueueTimeout):
                terminal_status = "error"
            elif execution_status in {"queue_timeout", "background_queue_timeout"}:
                terminal_status = "queue_timeout"
            elif execution_status == "budget_exhausted":
                terminal_status = "timeout"
            elif execution_status in {"cancelled", "stale_drop", "skipped_wait"}:
                terminal_status = execution_status
            else:
                terminal_status = "no_visible_reply"
            try:
                record_terminal_outcome(
                    main_event,
                    terminal_status,
                    stage=str(main_event.get_extra("astrmai_queue_timeout_stage", "")
                              or main_event.get_extra("astrmai_budget_timeout_stage", "") or "system2"),
                    reason=execution_status,
                )
                planner = getattr(self.runtime, "system2_planner", None)
                store = getattr(planner, "raw_trace_store", None)
                if store is not None:
                    builder = getattr(planner, "_build_raw_trace_events", None)
                    rows = builder(chat_id, main_event) if callable(builder) else main_event.get_extra("astrmai_trace_log", [])
                    terminal_rows = [
                        {**row, "event_id": f"{attempt_id}:turn.terminal"}
                        for row in rows
                        if row.get("stage") == "turn.terminal" and row.get("attempt_id") == attempt_id
                    ]
                    await store.append_many(chat_id, terminal_rows)
            except asyncio.CancelledError:
                if lock_exc_info is None:
                    raise
                logger.warning("[AstrMai] terminal persistence cancelled while preserving original exception")
            except Exception as diagnostic_error:
                logger.warning("[AstrMai] terminal diagnostic failed: %s", type(diagnostic_error).__name__)
            except BaseException as diagnostic_error:
                if lock_exc_info is None:
                    raise
                logger.warning(
                    "[AstrMai] terminal diagnostic aborted while preserving %s: %s",
                    lock_exc_info[0].__name__, type(diagnostic_error).__name__,
                )
            debug_trace(
                main_event,
                "system2.exit",
                reply_sent=bool(main_event.get_extra("astrmai_reply_sent", False)),
            )
            if release_exc is not None:
                raise release_exc
__all__ = ["System2Runner"]
