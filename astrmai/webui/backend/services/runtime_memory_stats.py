from __future__ import annotations

import sqlite3
from typing import Any


async def _count_canonical(store: Any, **filters: Any) -> int:
    if store is None or not hasattr(store, "list_canonical"):
        return 0
    result = await store.list_canonical(limit=1, **filters)
    if isinstance(result, dict):
        return int(result.get("total", 0) or 0)
    return 0


async def canonical_memory_stats(plugin_api: Any, legacy_repo: Any | None = None) -> dict[str, Any]:
    store = plugin_api.get_v2_store() if plugin_api is not None else None
    if store is not None and hasattr(store, "list_canonical"):
        by_kind: dict[str, int] = {}
        for kind in ("fact", "topic", "feedback", "memory", "jargon", "expression_pattern", "persona_lore"):
            by_kind[kind] = await _count_canonical(store, kind=kind)
        by_status: dict[str, int] = {}
        for status in ("active", "review_pending", "rejected", "stale", "superseded"):
            by_status[status] = await _count_canonical(store, status=status)
        return {
            "total": await _count_canonical(store),
            "by_kind": by_kind,
            "by_status": by_status,
            "source": "runtime_v2_store",
            "runtime_bound": True,
            "degraded": False,
        }

    total = 0
    degraded_reason = "runtime v2 store unavailable"
    if legacy_repo is not None:
        try:
            total = await legacy_repo.count_table("canonical_memories")
            degraded_reason = "using legacy dashboard repository"
        except sqlite3.OperationalError as exc:
            degraded_reason = str(exc)
    return {
        "total": total,
        "by_kind": {},
        "by_status": {},
        "source": "legacy_repository",
        "runtime_bound": False,
        "degraded": True,
        "degraded_reason": degraded_reason,
    }


async def canonical_kind_review_stats(
    plugin_api: Any,
    *,
    kind: str,
    legacy_expression_repo: Any | None = None,
) -> dict[str, int]:
    store = plugin_api.get_v2_store() if plugin_api is not None else None
    if store is not None and hasattr(store, "list_canonical"):
        total = await _count_canonical(store, kind=kind)
        pending = await _count_canonical(store, kind=kind, status="review_pending")
        approved = await _count_canonical(store, kind=kind, status="active")
        rejected = await _count_canonical(store, kind=kind, status="rejected")
        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
        }

    if kind == "expression_pattern" and legacy_expression_repo is not None:
        total, pending, approved, rejected = await legacy_expression_repo.expression_pattern_counts()
        return {
            "total": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
        }
    return {"total": 0, "pending": 0, "approved": 0, "rejected": 0}


__all__ = ["canonical_memory_stats", "canonical_kind_review_stats"]
