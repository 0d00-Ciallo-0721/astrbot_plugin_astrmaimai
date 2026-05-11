from __future__ import annotations

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import debug_trace
from .followup_manager import FollowupManager


class System2Runner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.followup_manager = FollowupManager(runtime)

    async def get_sys2_lock(self, chat_id: str):
        return await self.runtime.runtime_coordinator.get_sys2_lock(chat_id)

    def _prepare_queue_events(self, main_event, events_to_process: list | None) -> list:
        return events_to_process.copy() if isinstance(events_to_process, list) and events_to_process else [main_event]

    def _reset_runtime_reply_extras(self, main_event) -> None:
        main_event.set_extra("astrmai_reply_sent", False)
        main_event.set_extra("astrmai_wait_targets", [])
        main_event.set_extra("astrmai_wait_target_name", "")

    async def _prepare_system2_runtime(self, chat_id: str) -> None:
        await self.runtime.state_engine.consume_energy(chat_id)
        await self.runtime.lane_manager.ensure_lane(
            lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
            base_origin=chat_id,
        )

    async def _execute_planner(self, main_event, queue_events: list) -> bool:
        await self.runtime.system2_planner.plan_and_execute(main_event, queue_events)
        return bool(main_event.get_extra("astrmai_reply_sent", False))

    async def _finalize_followups(self, chat_id: str, main_event, reply_sent: bool) -> None:
        await self.followup_manager.sync_wait_targets(chat_id, main_event)
        await self.followup_manager.finalize_after_reply(chat_id, main_event, reply_sent)

    async def run(self, main_event, events_to_process: list | None = None):
        chat_id = main_event.unified_msg_origin
        lock = await self.get_sys2_lock(chat_id)
        queue_events = self._prepare_queue_events(main_event, events_to_process)
        debug_trace(main_event, "system2.enter", chat_id=chat_id, queue_size=len(queue_events))
        logger.debug(f"[{chat_id}] System 2 request queued and waiting for execution slot.")

        async with lock:
            try:
                self._reset_runtime_reply_extras(main_event)
                await self._prepare_system2_runtime(chat_id)
                reply_sent = await self._execute_planner(main_event, queue_events)
                await self._finalize_followups(chat_id, main_event, reply_sent)
                return reply_sent
            finally:
                logger.debug(f"[AstrMai] System2 execution finished safely for {chat_id}.")
                debug_trace(
                    main_event,
                    "system2.exit",
                    reply_sent=bool(main_event.get_extra("astrmai_reply_sent", False)),
                )
__all__ = ["System2Runner"]
