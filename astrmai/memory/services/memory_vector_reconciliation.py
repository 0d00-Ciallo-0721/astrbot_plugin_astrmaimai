"""Pure, read-only reconciliation helpers for Memory/Vector audits.

This module deliberately has no database or FAISS side effects.  Callers feed
observed canonical/document/index identifiers and repair rows, then receive a
stable classification suitable for diagnostics and tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def validate_vector_identity(descriptor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the minimum identity required before publishing an index."""
    descriptor = descriptor if isinstance(descriptor, Mapping) else {}
    required = ("embedding_model", "provider_source", "physical_dimension", "vector_count", "generation", "revision")
    missing = [name for name in required if descriptor.get(name) in (None, "")]
    errors: list[str] = []
    for field in ("physical_dimension", "vector_count", "generation", "revision"):
        value = descriptor.get(field)
        if value not in (None, ""):
            try:
                if int(value) < 0:
                    errors.append(f"{field}:negative")
            except (TypeError, ValueError):
                errors.append(f"{field}:invalid")
    status = "valid" if not missing and not errors else "blocked"
    return {"status": status, "missing": missing, "errors": errors, "publish_allowed": status == "valid"}


def reconcile_memory_vector(
    canonical_ids: Iterable[Any],
    document_ids: Iterable[Any],
    index_ids: Iterable[Any],
    *,
    current_generation: Any = None,
    repair_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return a deterministic three-way reconciliation without mutating input."""
    canonical = {str(item) for item in canonical_ids if item is not None}
    documents = {str(item) for item in document_ids if item is not None}
    index = {str(item) for item in index_ids if item is not None}
    missing_documents = sorted(canonical - documents)
    orphan_documents = sorted(documents - canonical)
    missing_index = sorted(documents - index)
    orphan_index = sorted(index - documents)
    records: list[dict[str, Any]] = []
    for kind, values in (
        ("missing_documents", missing_documents),
        ("orphan_documents", orphan_documents),
        ("missing_faiss", missing_index),
        ("orphan_faiss", orphan_index),
    ):
        records.extend({"mismatch_kind": kind, "memory_id": value, "classification": "current"} for value in values)

    for row in repair_rows:
        if not isinstance(row, Mapping):
            continue
        memory_id = str(row.get("memory_id") or row.get("document_id") or "")
        if not memory_id:
            continue
        generation = row.get("generation")
        status = str(row.get("status") or "pending").lower()
        if current_generation is not None and generation is not None and str(generation) != str(current_generation):
            classification = "stale"
            reason = "generation_mismatch"
        elif status in {"blocked", "repair_exhausted", "dead_letter"}:
            classification = "blocked"
            reason = status
        elif status in {"retry_wait", "pending", "waiting", "closing"}:
            classification = "retryable"
            reason = status
        elif status in {"completed", "deleted", "superseded"}:
            classification = "stale" if status != "completed" else "completed"
            reason = status
        else:
            classification = "current"
            reason = status or "unknown"
        records.append({
            "mismatch_kind": str(row.get("mismatch_kind") or "unknown_resource"),
            "memory_id": memory_id,
            "document_id": row.get("document_id"),
            "faiss_id": row.get("faiss_id"),
            "generation": generation,
            "expected_revision": row.get("expected_revision"),
            "actual_revision": row.get("actual_revision"),
            "index_hash": row.get("index_hash"),
            "repair_status": status,
            "classification": classification,
            "reason": reason,
        })
    return {
        "canonical_count": len(canonical),
        "document_count": len(documents),
        "index_count": len(index),
        "aligned": not any((missing_documents, orphan_documents, missing_index, orphan_index)),
        "missing_documents": missing_documents,
        "orphan_documents": orphan_documents,
        "missing_faiss": missing_index,
        "orphan_faiss": orphan_index,
        "records": records,
        "current_count": sum(item["classification"] == "current" for item in records),
        "stale_count": sum(item["classification"] == "stale" for item in records),
        "retryable_count": sum(item["classification"] == "retryable" for item in records),
        "blocked_count": sum(item["classification"] == "blocked" for item in records),
    }


__all__ = ["reconcile_memory_vector", "validate_vector_identity"]
