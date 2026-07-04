from __future__ import annotations

import asyncio
import inspect
import json
import time
from datetime import datetime, timezone

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
            try:
                if snap.run_once and snap.run_at and snap.run_at < now:
                    await self.db_service.deactivate_cron_snapshot(snap.job_id)
                    continue
                if not snap.job_id or not str(snap.job_id).strip():
                    continue
                if snap.job_id not in active_job_ids:
                    logger.info(
                        f"[CronGuard] reviving job '{snap.name}' "
                        f"from session '{getattr(snap, 'target_origin', '')}' (id={snap.job_id})"
                    )
                    if await self._revive_job(cron_mgr, snap):
                        revived += 1
            except Exception as exc:
                logger.error(f"[CronGuard] failed to restore job '{getattr(snap, 'job_id', '')}': {exc}")
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
            try:
                if snap.run_once and snap.run_at and snap.run_at < now:
                    await self.db_service.deactivate_cron_snapshot(snap.job_id)
                    continue
                if not snap.job_id or not str(snap.job_id).strip():
                    continue
                if snap.job_id not in active_job_ids:
                    await self._revive_job(cron_mgr, snap)
            except Exception as exc:
                logger.error(f"[CronGuard] heartbeat restore failed for '{getattr(snap, 'job_id', '')}': {exc}")

    async def _revive_job(self, cron_mgr, snap) -> bool:
        payload = self._decode_payload(getattr(snap, "payload", {}))
        run_at = self._resolve_run_at(snap, payload)
        add_method = getattr(cron_mgr, "add_active_job", None) or getattr(cron_mgr, "add_basic_job", None)
        if add_method is None:
            return False

        result, id_hint_used = await self._call_add_job(
            add_method,
            snap=snap,
            payload=payload,
            run_at=run_at,
        )
        new_job_id = self._extract_job_id(result)
        if not new_job_id and id_hint_used:
            new_job_id = str(snap.job_id)
        await self._sync_revived_snapshot(snap, payload=payload, run_at=run_at, new_job_id=new_job_id)
        return True

    @staticmethod
    def _decode_payload(payload) -> dict:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _resolve_run_at(snap, payload: dict):
        snap_run_at = getattr(snap, "run_at", None)
        if snap_run_at and snap_run_at > 0:
            return datetime.fromtimestamp(snap_run_at, tz=timezone.utc)
        raw_run_at = payload.get("run_at")
        if isinstance(raw_run_at, str) and raw_run_at.strip():
            try:
                return datetime.fromisoformat(raw_run_at)
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def _extract_job_id(job) -> str:
        if job is None:
            return ""
        if isinstance(job, str):
            return job.strip()
        return str(getattr(job, "id", getattr(job, "job_id", "")) or "").strip()

    async def _call_add_job(self, add_method, *, snap, payload: dict, run_at):
        kwargs = {
            "name": getattr(snap, "name", ""),
            "cron_expression": getattr(snap, "cron_expression", None),
            "payload": payload,
            "run_once": bool(getattr(snap, "run_once", False)),
            "run_at": run_at,
        }
        id_hint_used = False
        try:
            signature = inspect.signature(add_method)
            params = signature.parameters
            accepts_var_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
            if "job_id" in params or accepts_var_kwargs:
                kwargs["job_id"] = str(getattr(snap, "job_id", "") or "")
                id_hint_used = True
            elif "id" in params:
                kwargs["id"] = str(getattr(snap, "job_id", "") or "")
                id_hint_used = True
        except (TypeError, ValueError):
            pass
        try:
            return await add_method(**kwargs), id_hint_used
        except TypeError:
            kwargs.pop("job_id", None)
            kwargs.pop("id", None)
            return await add_method(**kwargs), False

    async def _sync_revived_snapshot(self, snap, *, payload: dict, run_at, new_job_id: str) -> None:
        old_job_id = str(getattr(snap, "job_id", "") or "")
        if new_job_id and hasattr(self.db_service, "save_cron_snapshot"):
            if new_job_id != old_job_id and hasattr(self.db_service, "deactivate_cron_snapshot"):
                await self.db_service.deactivate_cron_snapshot(old_job_id)
            from ...infrastructure.persistence.orm_models import CronSnapshot

            await self.db_service.save_cron_snapshot(
                CronSnapshot(
                    job_id=new_job_id,
                    name=getattr(snap, "name", ""),
                    cron_expression=getattr(snap, "cron_expression", None),
                    run_at=run_at.timestamp() if run_at else None,
                    run_once=bool(getattr(snap, "run_once", False)),
                    target_origin=str(getattr(snap, "target_origin", "") or ""),
                    payload=json.dumps(payload, ensure_ascii=False),
                    note="revived by CronHeartbeatGuard",
                    is_active=True,
                )
            )
            return
        if old_job_id and hasattr(self.db_service, "deactivate_cron_snapshot"):
            await self.db_service.deactivate_cron_snapshot(old_job_id)
            logger.warning(
                f"[CronGuard] revived job '{getattr(snap, 'name', '')}' without a returned id; "
                f"deactivated stale snapshot {old_job_id} to avoid repeated restores"
            )


__all__ = ["CronHeartbeatGuard"]
