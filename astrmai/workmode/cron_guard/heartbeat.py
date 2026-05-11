from __future__ import annotations

import asyncio
import time
from datetime import datetime

from astrbot.api import logger


class CronHeartbeatGuard:
    """Refactoring-side cron snapshot guard owned by workmode."""

    HEARTBEAT_INTERVAL = 60

    def __init__(self, db_service, context):
        self.db_service = db_service
        self.context = context
        self._is_running = True

    async def reload_all_lost_jobs(self) -> int:
        cron_mgr = getattr(self.context, "cron_manager", None)
        if not cron_mgr:
            await self._clean_expired_snapshots()
            return 0

        snapshots = await self.db_service.get_all_active_cron_snapshots()
        if not snapshots:
            return 0

        active_jobs = await cron_mgr.list_jobs()
        active_job_ids = {str(getattr(job, "id", getattr(job, "job_id", job))) for job in active_jobs}

        revived = 0
        now = time.time()
        for snap in snapshots:
            if snap.run_once and snap.run_at and snap.run_at < now:
                await self.db_service.deactivate_cron_snapshot(snap.job_id)
                continue
            if snap.job_id not in active_job_ids:
                if await self._revive_job(cron_mgr, snap):
                    revived += 1
        return revived

    async def run_heartbeat(self):
        while self._is_running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await self._heartbeat_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[CronGuard] heartbeat degraded: {exc}")

    def stop(self):
        self._is_running = False

    def describe_status(self) -> dict:
        return {
            "running": self._is_running,
            "interval_seconds": self.HEARTBEAT_INTERVAL,
            "db_bound": self.db_service is not None,
            "cron_manager_available": getattr(self.context, "cron_manager", None) is not None,
        }

    async def _clean_expired_snapshots(self):
        snapshots = await self.db_service.get_all_active_cron_snapshots()
        if not snapshots:
            return
        now = time.time()
        for snap in snapshots:
            if snap.run_once and snap.run_at and snap.run_at < now:
                await self.db_service.deactivate_cron_snapshot(snap.job_id)

    async def _heartbeat_tick(self):
        cron_mgr = getattr(self.context, "cron_manager", None)
        if not cron_mgr:
            await self._clean_expired_snapshots()
            return

        snapshots = await self.db_service.get_all_active_cron_snapshots()
        if not snapshots:
            return
        active_jobs = await cron_mgr.list_jobs()
        active_job_ids = {str(getattr(job, "id", getattr(job, "job_id", job))) for job in active_jobs}
        now = time.time()
        for snap in snapshots:
            if snap.run_once and snap.run_at and snap.run_at < now:
                await self.db_service.deactivate_cron_snapshot(snap.job_id)
                continue
            if snap.job_id not in active_job_ids:
                await self._revive_job(cron_mgr, snap)

    async def _revive_job(self, cron_mgr, snap) -> bool:
        if not hasattr(cron_mgr, "add_job"):
            return False
        from astrbot.core.db.po import CronJob

        job = CronJob(
            id=snap.job_id,
            name=snap.name,
            cron_expression=snap.cron_expression,
            run_at=datetime.fromtimestamp(snap.run_at) if snap.run_at else None,
            run_once=snap.run_once,
            payload=snap.payload,
        )
        await cron_mgr.add_job(job)
        return True


__all__ = ["CronHeartbeatGuard"]
