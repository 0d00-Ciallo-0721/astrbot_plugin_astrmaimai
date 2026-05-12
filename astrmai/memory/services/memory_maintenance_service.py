from __future__ import annotations

import time

from .memory_index_projector import MemoryIndexProjector
from .v2_store import MemoryV2Store


class MemoryMaintenanceService:
    def __init__(self, store: MemoryV2Store, index_projector: MemoryIndexProjector | None = None):
        self.store = store
        self.index_projector = index_projector

    async def apply_daily_decay(
        self,
        *,
        decay_rate: float,
        days: int = 1,
        min_score: float = 0.05,
        stale_grace_seconds: float = 7 * 86400,
        allow_protected_physical_delete: bool = False,
    ) -> int:
        deleted = await self.store.apply_decay(
            decay_rate=decay_rate,
            days=days,
            min_score=min_score,
            stale_grace_seconds=stale_grace_seconds,
            allow_protected_physical_delete=allow_protected_physical_delete,
        )
        if deleted and self.index_projector:
            await self.index_projector.cleanup_deleted(list(getattr(self.store, "last_physical_delete_ids", []) or []))
        return deleted

    async def run_once(self, *, now: float | None = None, policy: dict | None = None) -> dict:
        policy = dict(policy or {})
        report = {
            "ran_at": float(now or time.time()),
            "decayed": 0,
            "marked_stale": 0,
            "restored": 0,
            "physically_deleted": 0,
            "projection_deleted": 0,
            "protected_skipped": 0,
            "errors": [],
            "protected_physical_delete": bool(policy.get("allow_protected_physical_delete", False)),
        }
        before = await self.store.list_canonical(limit=1, status="stale")
        stale_before = int(before.get("total", 0) or 0)
        try:
            deleted = await self.store.apply_decay(
                decay_rate=float(policy.get("decay_rate", 0.08)),
                days=int(policy.get("days", 1)),
                min_score=float(policy.get("min_score", 0.05)),
                stale_grace_seconds=float(policy.get("stale_grace_seconds", 7 * 86400)),
                allow_protected_physical_delete=bool(policy.get("allow_protected_physical_delete", False)),
            )
        except Exception as exc:
            report["errors"].append(str(exc))
            deleted = 0
        after = await self.store.list_canonical(limit=1, status="stale")
        stale_after = int(after.get("total", 0) or 0)
        deleted_ids = list(getattr(self.store, "last_physical_delete_ids", []) or [])
        projection_deleted = 0
        if deleted_ids and self.index_projector:
            projection_deleted = await self.index_projector.cleanup_deleted(deleted_ids)
        report["decayed"] = 1
        report["marked_stale"] = max(0, stale_after - stale_before)
        report["physically_deleted"] = int(deleted or 0)
        report["projection_deleted"] = int(projection_deleted or 0)
        if not bool(policy.get("allow_protected_physical_delete", False)):
            protected = await self.store.list_canonical(limit=1, status="stale", kind="persona_lore")
            report["protected_skipped"] = int(protected.get("total", 0) or 0)
        return report

    async def soft_delete(self, memory_id: str, *, reason: str = "") -> int:
        deleted = await self.store.soft_delete(memory_id, reason=reason)
        if deleted and self.index_projector:
            await self.index_projector.cleanup_deleted([memory_id])
        return deleted

    async def soft_delete_by_filter(self, *, kind: str = "", session_id: str = "", persona_id: str = "", reason: str = "") -> int:
        ids = await self.store.soft_delete_by_filter(
            kind=kind,
            session_id=session_id,
            persona_id=persona_id,
            reason=reason,
        )
        if ids and self.index_projector:
            await self.index_projector.cleanup_deleted(ids)
        return len(ids)

    async def restore(self, memory_id: str, *, reason: str = "webui") -> int:
        restored = await self.store.restore(memory_id, reason=reason)
        if restored and self.index_projector:
            await self.index_projector.project(memory_id)
        return restored

    async def mark_stale(self, memory_id: str, *, reason: str = "webui") -> int:
        changed = await self.store.mark_stale(memory_id, reason=reason)
        if changed and self.index_projector:
            await self.index_projector.cleanup_deleted([memory_id])
        return changed

    async def mark_merged(self, memory_ids: list[str], *, superseded_by: str) -> int:
        count = await self.store.mark_merged(memory_ids, superseded_by=superseded_by)
        if count and self.index_projector:
            await self.index_projector.cleanup_deleted(memory_ids)
        return count

    async def prune_low_importance(self, *, threshold: float) -> int:
        ids = await self.store.soft_delete_low_importance(threshold=threshold)
        if ids and self.index_projector:
            await self.index_projector.cleanup_deleted(ids)
        return len(ids)


__all__ = ["MemoryMaintenanceService"]
