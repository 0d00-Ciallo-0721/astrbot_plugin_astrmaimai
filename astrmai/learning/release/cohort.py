from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Iterable


class CohortLevel(str, Enum):
    ONE = "cohort-1"
    THREE = "cohort-3"
    TWENTY_FIVE_PERCENT = "cohort-25-percent"
    FIFTY_PERCENT = "cohort-50-percent"


@dataclass(frozen=True, slots=True)
class ScopeFacts:
    scope_id: str
    eligible_turns: int
    has_platform_acknowledgement: bool
    active_incident: bool
    sensitive_scope: bool
    admin_only_scope: bool
    cursor_regression: bool
    unresolved_vector_identity: bool
    attribution_coverage: float
    dialog_outcome_unknown_rate: float
    baseline_sufficient: bool


@dataclass(frozen=True, slots=True)
class CohortSelection:
    release_id: str
    level: CohortLevel
    selected: tuple[str, ...]
    excluded: tuple[tuple[str, tuple[str, ...]], ...]
    candidate_digest: str
    blocked_reasons: tuple[str, ...]
    ranks: tuple[tuple[str, str], ...] = ()

    @property
    def rank_by_scope(self) -> dict[str, str]:
        return dict(self.ranks)


def _reasons(scope: ScopeFacts) -> tuple[str, ...]:
    reasons: list[str] = []
    if scope.eligible_turns < 100:
        reasons.append("insufficient_eligible_turns")
    if not scope.has_platform_acknowledgement:
        reasons.append("missing_platform_acknowledgement")
    if scope.active_incident:
        reasons.append("active_incident")
    if scope.sensitive_scope:
        reasons.append("sensitive_scope")
    if scope.admin_only_scope:
        reasons.append("admin_only_scope")
    if scope.cursor_regression:
        reasons.append("cursor_regression")
    if scope.unresolved_vector_identity:
        reasons.append("unresolved_vector_identity")
    if scope.attribution_coverage < 0.90:
        reasons.append("attribution_coverage_below_0.90")
    if scope.dialog_outcome_unknown_rate > 0.30:
        reasons.append("dialog_outcome_unknown_above_0.30")
    if not scope.baseline_sufficient:
        reasons.append("baseline_insufficient")
    return tuple(reasons)


def select_cohort(
    release_id: str,
    scopes: Iterable[ScopeFacts],
    *,
    level: CohortLevel = CohortLevel.ONE,
    approval: bool = False,
    expected_candidate_digest: str | None = None,
) -> CohortSelection:
    if level is not CohortLevel.ONE and not approval:
        raise ValueError("cohort expansion requires manual approval")
    facts = sorted(scopes, key=lambda item: item.scope_id)
    excluded = tuple((item.scope_id, _reasons(item)) for item in facts if _reasons(item))
    eligible = [item for item in facts if not _reasons(item)]
    candidate_payload = [
        {"scope_id": item.scope_id, "eligible_turns": item.eligible_turns}
        for item in eligible
    ]
    candidate_digest = hashlib.sha256(
        json.dumps(candidate_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    blocked: list[str] = []
    if expected_candidate_digest and expected_candidate_digest != candidate_digest:
        blocked.append("candidate_list_changed")
    if not eligible:
        blocked.append("no_eligible_scopes")
    if any("baseline_insufficient" in reasons for _, reasons in excluded) and not eligible:
        blocked.append("baseline_insufficient")
    ranked = sorted(
        eligible,
        key=lambda item: hashlib.sha256(f"{item.scope_id}{release_id}".encode("utf-8")).hexdigest(),
    )
    if level is CohortLevel.ONE:
        count = 1
    elif level is CohortLevel.THREE:
        count = 3
    elif level is CohortLevel.TWENTY_FIVE_PERCENT:
        count = max(1, (len(ranked) * 25 + 99) // 100)
    else:
        count = max(1, (len(ranked) * 50 + 99) // 100)
    ranks = tuple(
        (item.scope_id, hashlib.sha256(f"{item.scope_id}{release_id}".encode("utf-8")).hexdigest())
        for item in ranked
    )
    selected = tuple(item.scope_id for item in ranked[:count]) if not blocked else ()
    return CohortSelection(release_id, level, selected, excluded, candidate_digest, tuple(dict.fromkeys(blocked)), ranks)
