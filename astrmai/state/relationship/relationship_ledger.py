from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


LEDGER_SCHEMA_VERSION = 1
SETTLEMENT_DISPOSITIONS = frozenset({"applied", "suppressed", "rejected", "duplicate"})


def _normalized_ids(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    return tuple(dict.fromkeys(str(value or "").strip() for value in (values or ()) if str(value or "").strip()))


def _vector(value: Mapping[str, Any] | None) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, raw in dict(value or {}).items():
        try:
            result[str(key)] = float(raw)
        except (TypeError, ValueError):
            continue
    return result


@dataclass(frozen=True, slots=True)
class RelationshipEventProposal:
    """A privacy-preserving, pre-settlement relationship signal."""

    event_type: str
    user_id: str
    chat_id: str
    turn_id: str = ""
    source_event_ids: tuple[str, ...] = ()
    actor_kind: str = "user"
    target_kind: str = "bot"
    confidence: float = 1.0
    intensity: float = 1.0
    evidence_codes: tuple[str, ...] = ()
    source: str = "deterministic_rule"
    mood_tag: str = ""
    proposal_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", str(self.event_type or "").strip().lower())
        object.__setattr__(self, "user_id", str(self.user_id or "").strip())
        object.__setattr__(self, "chat_id", str(self.chat_id or "").strip())
        object.__setattr__(self, "turn_id", str(self.turn_id or "").strip())
        object.__setattr__(self, "source_event_ids", _normalized_ids(self.source_event_ids))
        object.__setattr__(self, "evidence_codes", _normalized_ids(self.evidence_codes))
        object.__setattr__(self, "source", str(self.source or "deterministic_rule").strip().lower())
        object.__setattr__(self, "mood_tag", str(self.mood_tag or "").strip().lower())
        try:
            confidence = float(self.confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            intensity = float(self.intensity)
        except (TypeError, ValueError):
            intensity = 1.0
        object.__setattr__(self, "confidence", max(0.0, min(1.0, confidence)))
        object.__setattr__(self, "intensity", max(0.1, min(3.0, intensity)))

    @property
    def idempotency_key(self) -> str:
        source_key = self.turn_id or ",".join(self.source_event_ids) or self.proposal_id
        material = "|".join((source_key, self.user_id, self.actor_kind, self.target_kind, self.event_type))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RelationshipLedgerEntry:
    proposal: RelationshipEventProposal
    policy_version: str
    disposition: str
    before_vector: dict[str, float] = field(default_factory=dict)
    delta_vector: dict[str, float] = field(default_factory=dict)
    after_vector: dict[str, float] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        disposition = str(self.disposition or "rejected").strip().lower()
        if disposition not in SETTLEMENT_DISPOSITIONS:
            disposition = "rejected"
        object.__setattr__(self, "disposition", disposition)
        object.__setattr__(self, "policy_version", str(self.policy_version or "relationship-v1").strip())
        object.__setattr__(self, "before_vector", _vector(self.before_vector))
        object.__setattr__(self, "delta_vector", _vector(self.delta_vector))
        object.__setattr__(self, "after_vector", _vector(self.after_vector))

    def to_persistence_row(self) -> dict[str, Any]:
        proposal = self.proposal
        return {
            "event_id": self.event_id,
            "idempotency_key": proposal.idempotency_key,
            "user_id": proposal.user_id,
            "chat_id": proposal.chat_id,
            "turn_id": proposal.turn_id,
            "source_event_ids_json": json.dumps(list(proposal.source_event_ids), ensure_ascii=False),
            "actor_kind": proposal.actor_kind,
            "target_kind": proposal.target_kind,
            "event_type": proposal.event_type,
            "confidence": proposal.confidence,
            "intensity": proposal.intensity,
            "evidence_json": json.dumps(list(proposal.evidence_codes), ensure_ascii=False),
            "policy_version": self.policy_version,
            "source": proposal.source,
            "mood_tag": proposal.mood_tag,
            "before_vector_json": json.dumps(self.before_vector, ensure_ascii=False, sort_keys=True),
            "delta_vector_json": json.dumps(self.delta_vector, ensure_ascii=False, sort_keys=True),
            "after_vector_json": json.dumps(self.after_vector, ensure_ascii=False, sort_keys=True),
            "disposition": self.disposition,
            "created_at": float(self.created_at),
        }


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "RelationshipEventProposal",
    "RelationshipLedgerEntry",
    "SETTLEMENT_DISPOSITIONS",
]
