"""Pure, read-only reconciliation helpers for Memory/Vector audits.

This module deliberately has no database or FAISS side effects.  Callers feed
observed canonical/document/index identifiers and repair rows, then receive a
stable classification suitable for diagnostics and tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
import re
from typing import Any


def validate_vector_identity(descriptor: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the complete durable identity required before publishing an index."""
    descriptor = descriptor if isinstance(descriptor, Mapping) else {}
    required = (
        "asset_revision_digest", "index_file", "embedding_model", "provider_source",
        "api_base_fingerprint", "physical_dimension", "configured_dimension",
        "document_count", "vector_count", "mapping_hash", "index_hash",
        "generation", "revision",
    )
    missing = [name for name in required if descriptor.get(name) in (None, "")]
    errors: list[str] = []
    for field in ("generation", "revision", "document_count", "vector_count"):
        value = descriptor.get(field)
        if value not in (None, "") and (type(value) is not int or value < 0):
            errors.append(f"{field}:invalid")
    for field in ("physical_dimension", "configured_dimension"):
        value = descriptor.get(field)
        if value not in (None, "") and (type(value) is not int or value <= 0):
            errors.append(f"{field}:invalid")
    index_file = descriptor.get("index_file")
    if isinstance(index_file, str) and index_file and Path(index_file).name != index_file:
        errors.append("index_file:not_basename")
    physical = descriptor.get("physical_dimension")
    configured = descriptor.get("configured_dimension")
    if type(physical) is int and type(configured) is int and physical != configured:
        errors.append("dimension:mismatch")
    documents = descriptor.get("document_count")
    vectors = descriptor.get("vector_count")
    if type(documents) is int and type(vectors) is int and documents != vectors:
        errors.append("count:mismatch")
    for field in ("asset_revision_digest", "api_base_fingerprint", "mapping_hash", "index_hash"):
        value = descriptor.get(field)
        if value not in (None, "") and re.fullmatch(r"sha256:v1:[0-9a-f]{64}", str(value)) is None:
            errors.append(f"{field}:invalid")
    status = "valid" if not missing and not errors else "blocked"
    failure_kind = ""
    if missing:
        failure_kind = "identity_missing"
    elif errors:
        failure_kind = errors[0].replace(":", "_")
    return {
        "status": status,
        "missing": missing,
        "errors": errors,
        "publish_allowed": status == "valid",
        "failure_stage": "" if status == "valid" else "vector_identity",
        "failure_kind": failure_kind,
        "retryable": False,
        "expected": {
            "dimension": configured,
            "document_count": documents,
        },
        "actual": {
            "dimension": physical,
            "vector_count": vectors,
        },
        "diagnostics": {"missing": list(missing), "errors": list(errors)},
    }


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
