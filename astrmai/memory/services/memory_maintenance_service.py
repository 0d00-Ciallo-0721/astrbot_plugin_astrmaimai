from __future__ import annotations

import hashlib
import re
import time

from ..contracts.memory_query import MemoryWriteRequest
from .memory_admission_service import MemoryAdmissionService
from .memory_scoring import compute_hot_score, scoring_from_config
from .memory_index_projector import MemoryIndexProjector
from .v2_store import MemoryV2Store


class MemoryMaintenanceService:
    def __init__(self, store: MemoryV2Store, index_projector: MemoryIndexProjector | None = None, config=None):
        self.store = store
        self.index_projector = index_projector
        self.config = config
        self.scoring = scoring_from_config(config)

    def refresh_config(self, config) -> None:
        self.config = config
        self.scoring = scoring_from_config(config)

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
            "jargon_pending_deleted": 0,
            "jargon_pending_human_deleted": 0,
            "jargon_rejected_deleted": 0,
            "protected_jargon_skipped": 0,
            "expression_pending_deleted": 0,
            "expression_rejected_deleted": 0,
            "protected_expression_skipped": 0,
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
        try:
            maintenance_now = float(now or time.time())
            stale_marked = 0
            threshold = float(
                policy.get(
                    "maintenance_temporal_stale_hot_threshold",
                    self.scoring.maintenance_temporal_stale_hot_threshold,
                )
            )
            candidates = await self.store.list_candidates(
                statuses=["active"],
                include_inactive=False,
                limit=int(policy.get("maintenance_temporal_scan_limit", 200)),
            )
            for candidate in candidates:
                if candidate.status != "active":
                    continue
                if str(candidate.kind or "").strip().lower() == "fact":
                    if float(candidate.importance or 0.0) >= float(policy.get("fact_aggressive_importance_max", 0.2)):
                        continue
                elif float(candidate.importance or 0.0) >= 0.4:
                    continue
                hot_score = compute_hot_score(candidate, now=maintenance_now, config=self.scoring)
                if hot_score >= threshold:
                    continue
                stale_marked += await self.mark_stale(candidate.id, reason="maintenance_temporal_hot")
            report["marked_stale"] += stale_marked
        except Exception as exc:
            report["errors"].append(f"temporal_hot_stale:{exc}")
        try:
            pending_cleanup = await self.store.purge_jargon_candidates(
                statuses=("review_pending",),
                older_than_seconds=float(policy.get("pending_jargon_grace_seconds", 14 * 86400)),
                min_confidence_to_keep=float(policy.get("protected_jargon_confidence", 0.9)),
                min_count_to_keep=int(policy.get("protected_jargon_count", 5)),
                review_statuses=("review_pending",),
            )
            pending_human_cleanup = await self.store.purge_jargon_candidates(
                statuses=("review_pending",),
                older_than_seconds=float(policy.get("pending_human_jargon_grace_seconds", 14 * 86400)),
                min_confidence_to_keep=float(policy.get("protected_jargon_confidence", 0.9)),
                min_count_to_keep=int(policy.get("protected_jargon_count", 5)),
                review_statuses=("pending_human",),
            )
            rejected_cleanup = await self.store.purge_jargon_candidates(
                statuses=("rejected",),
                older_than_seconds=float(policy.get("rejected_jargon_grace_seconds", 7 * 86400)),
                min_confidence_to_keep=float(policy.get("protected_jargon_confidence", 0.9)),
                min_count_to_keep=int(policy.get("protected_jargon_count", 5)),
                review_statuses=("rejected",),
            )
            deleted_ids = (
                list(pending_cleanup.get("deleted_ids", []) or [])
                + list(pending_human_cleanup.get("deleted_ids", []) or [])
                + list(rejected_cleanup.get("deleted_ids", []) or [])
            )
            jargon_projection_deleted = 0
            if deleted_ids and self.index_projector:
                jargon_projection_deleted = await self.index_projector.cleanup_deleted(deleted_ids)
            report["jargon_pending_deleted"] = len(list(pending_cleanup.get("deleted_ids", []) or []))
            report["jargon_pending_human_deleted"] = len(list(pending_human_cleanup.get("deleted_ids", []) or []))
            report["jargon_rejected_deleted"] = len(list(rejected_cleanup.get("deleted_ids", []) or []))
            report["protected_jargon_skipped"] = (
                int(pending_cleanup.get("protected_skipped", 0) or 0)
                + int(rejected_cleanup.get("protected_skipped", 0) or 0)
                + int(pending_human_cleanup.get("protected_skipped", 0) or 0)
            )
            report["projection_deleted"] += int(jargon_projection_deleted or 0)
            report["physically_deleted"] += len(deleted_ids)
        except Exception as exc:
            report["errors"].append(f"jargon_cleanup:{exc}")
        try:
            pending_cleanup = await self.store.purge_kind_candidates(
                kind="expression_pattern",
                statuses=("review_pending",),
                older_than_seconds=float(policy.get("pending_expression_grace_seconds", 21 * 86400)),
                min_confidence_to_keep=float(policy.get("protected_expression_confidence", 0.95)),
                min_count_to_keep=int(policy.get("protected_expression_count", 8)),
            )
            rejected_cleanup = await self.store.purge_kind_candidates(
                kind="expression_pattern",
                statuses=("rejected",),
                older_than_seconds=float(policy.get("rejected_expression_grace_seconds", 14 * 86400)),
                min_confidence_to_keep=float(policy.get("protected_expression_confidence", 0.95)),
                min_count_to_keep=int(policy.get("protected_expression_count", 8)),
            )
            deleted_ids = list(pending_cleanup.get("deleted_ids", []) or []) + list(rejected_cleanup.get("deleted_ids", []) or [])
            expression_projection_deleted = 0
            if deleted_ids and self.index_projector:
                expression_projection_deleted = await self.index_projector.cleanup_deleted(deleted_ids)
            report["expression_pending_deleted"] = len(list(pending_cleanup.get("deleted_ids", []) or []))
            report["expression_rejected_deleted"] = len(list(rejected_cleanup.get("deleted_ids", []) or []))
            report["protected_expression_skipped"] = int(pending_cleanup.get("protected_skipped", 0) or 0) + int(
                rejected_cleanup.get("protected_skipped", 0) or 0
            )
            report["projection_deleted"] += int(expression_projection_deleted or 0)
            report["physically_deleted"] += len(deleted_ids)
        except Exception as exc:
            report["errors"].append(f"expression_pattern_cleanup:{exc}")
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

    async def quality_audit(self, *, limit: int = 5000) -> dict:
        """Classify legacy pollution without mutating canonical data."""
        max_items = max(1, min(int(limit or 5000), 20000))
        items: list[dict] = []
        offset = 0
        while len(items) < max_items:
            page = await self.store.list_canonical(
                status="active",
                limit=min(500, max_items - len(items)),
                offset=offset,
                include_inactive=True,
            )
            batch = list(page.get("items", []) or [])
            items.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("total", 0) or 0):
                break

        admission = MemoryAdmissionService(self.config)
        suspects: list[dict] = []
        seen_feedback: set[tuple[str, str]] = set()
        seen_topics: set[tuple[str, str]] = set()
        for item in items:
            memory_id = str(item.get("id") or "")
            source = str(item.get("source") or "").strip().lower()
            kind = str(item.get("kind") or "").strip().lower()
            session_id = str(item.get("session_id") or "")
            sender_id = str(item.get("sender_id") or "")
            metadata = dict(item.get("metadata") or {})
            reason = ""

            if kind == "fact" and source in {"instant_gate", "instant_gate_llm", "memory_summary"}:
                decision = admission.evaluate(
                    MemoryWriteRequest(
                        source=source,
                        kind=kind,
                        session_id=session_id,
                        sender_id=sender_id,
                        content=str(item.get("content") or ""),
                        summary=str(item.get("summary") or ""),
                        confidence=float(item.get("confidence") or 0.0),
                        source_ref=str(item.get("source_ref") or ""),
                        metadata=metadata,
                    )
                )
                if not decision.accepted:
                    reason = decision.reason
                elif ":GroupMessage:" in session_id and sender_id == session_id:
                    reason = "group_session_used_as_subject"
            elif kind == "feedback":
                key = (session_id, source)
                if key in seen_feedback:
                    reason = "superseded_rolling_feedback"
                else:
                    seen_feedback.add(key)
            elif kind == "topic":
                normalized = self._normalize_quality_text(str(item.get("summary") or item.get("content") or ""))
                key = (session_id, normalized)
                if not normalized or len(normalized) < 6:
                    reason = "low_information_topic"
                elif key in seen_topics:
                    reason = "duplicate_topic"
                else:
                    seen_topics.add(key)

            if reason:
                suspects.append(
                    {
                        "id": memory_id,
                        "reason": reason,
                        "source": source,
                        "kind": kind,
                        "session_id": session_id,
                        "summary_digest": hashlib.sha256(
                            str(item.get("summary") or "").encode("utf-8")
                        ).hexdigest()[:12],
                    }
                )

        reasons: dict[str, int] = {}
        for item in suspects:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        return {
            "mode": "dry_run",
            "scanned": len(items),
            "suspect_count": len(suspects),
            "reasons": reasons,
            "items": suspects,
        }

    async def quarantine_quality_suspects(self, *, limit: int = 5000) -> dict:
        report = await self.quality_audit(limit=limit)
        changed_ids: list[str] = []
        failed: list[dict] = []
        quarantined_at = time.time()
        for item in list(report.get("items", []) or []):
            memory_id = str(item.get("id") or "")
            current = await self.store.get_canonical(memory_id, include_inactive=True)
            if current is None or current.status != "active":
                continue
            metadata = dict(current.metadata or {})
            metadata["quality_quarantine"] = {
                "reason": str(item.get("reason") or "quality_audit"),
                "quarantined_at": quarantined_at,
                "previous_status": current.status,
                "previous_visibility": current.visibility,
            }
            try:
                changed = await self.store.update_memory(
                    memory_id,
                    status="review_pending",
                    visibility="maintenance_only",
                    metadata=metadata,
                )
                if changed:
                    changed_ids.append(memory_id)
            except Exception as exc:
                failed.append({"id": memory_id, "error": str(exc)})
        projection_deleted = 0
        if changed_ids and self.index_projector:
            projection_deleted = await self.index_projector.cleanup_deleted(changed_ids)
        return {
            **report,
            "mode": "apply",
            "changed": len(changed_ids),
            "changed_ids": changed_ids,
            "projection_deleted": int(projection_deleted or 0),
            "failed": failed,
        }

    @staticmethod
    def _normalize_quality_text(text: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


__all__ = ["MemoryMaintenanceService"]
