"""Pure vector identity decision logic used by snapshot migration tooling.

This module deliberately does not touch files, providers, or the live
``MemoryEngine``.  It turns measured snapshot facts into an explicit action so
callers cannot silently trust a legacy manifest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


VectorMigrationAction = Literal[
    "reuse_active",
    "rebuild_new_generation",
    "lexical_fallback_pending_provider",
    "blocked_identity_unknown",
    "blocked_dimension_mismatch",
]


@dataclass(frozen=True)
class VectorMigrationDecision:
    action: VectorMigrationAction
    reasons: tuple[str, ...] = ()

    @property
    def rebuild_required(self) -> bool:
        return self.action == "rebuild_new_generation"


def decide_vector_migration(
    manifest: dict[str, Any] | None,
    *,
    expected_model: str,
    expected_provider_source: str,
    expected_api_base_fingerprint: str,
    expected_dimension: int | None,
    physical_readable: bool,
    physical_dimension: int | None,
    physical_ids: set[str] | None = None,
    expected_ids: set[str] | None = None,
    provider_available: bool = True,
) -> VectorMigrationDecision:
    """Decide whether a snapshot index may be reused.

    Missing identity is never repaired in-place.  A provider outage permits
    lexical fallback while a new generation remains pending.
    """

    payload = manifest or {}
    reasons: list[str] = []
    if expected_dimension is not None and physical_dimension is not None:
        if int(physical_dimension) != int(expected_dimension):
            return VectorMigrationDecision(
                "blocked_dimension_mismatch",
                (f"physical_dimension:{physical_dimension}!={expected_dimension}",),
            )
    if not physical_readable:
        reasons.append("physical_index_unreadable")
    if physical_dimension is None:
        reasons.append("physical_dimension_unknown")

    manifest_dimension = payload.get("dimension")
    if manifest_dimension not in (None, "") and expected_dimension is not None:
        try:
            manifest_dimension_value = int(manifest_dimension)
        except (TypeError, ValueError):
            return VectorMigrationDecision("blocked_identity_unknown", ("manifest_dimension_invalid",))
        if manifest_dimension_value != int(expected_dimension):
            return VectorMigrationDecision(
                "blocked_dimension_mismatch",
                (f"manifest_dimension:{manifest_dimension}!={expected_dimension}",),
            )

    models = payload.get("embedding_models")
    if isinstance(models, (list, tuple)):
        manifest_model = models[0] if models else ""
    else:
        manifest_model = payload.get("embedding_model", "")
    identity_fields = {
        "embedding_model": (str(manifest_model or ""), str(expected_model or "")),
        "provider_source": (str(payload.get("provider_source_id") or payload.get("provider_source") or ""), str(expected_provider_source or "")),
        "api_base_fingerprint": (str(payload.get("api_base_fingerprint") or ""), str(expected_api_base_fingerprint or "")),
    }
    missing = [name for name, (actual, expected) in identity_fields.items() if not actual or not expected]
    if missing:
        reasons.append("identity_unknown:" + ",".join(missing))
    else:
        mismatches = [name for name, (actual, expected) in identity_fields.items() if actual != expected]
        if mismatches:
            reasons.append("identity_mismatch:" + ",".join(mismatches))

    if expected_ids is not None and physical_ids is not None and expected_ids != physical_ids:
        reasons.append("id_set_mismatch")

    generation = payload.get("generation")
    if generation in (None, "") or (isinstance(generation, int) and generation < 0):
        reasons.append("generation_unknown")

    if reasons:
        if not provider_available:
            return VectorMigrationDecision("lexical_fallback_pending_provider", tuple(reasons))
        if any(reason.startswith("identity_unknown") for reason in reasons) and not expected_model:
            return VectorMigrationDecision("blocked_identity_unknown", tuple(reasons))
        return VectorMigrationDecision("rebuild_new_generation", tuple(reasons))
    return VectorMigrationDecision("reuse_active")


__all__ = ["VectorMigrationAction", "VectorMigrationDecision", "decide_vector_migration"]
