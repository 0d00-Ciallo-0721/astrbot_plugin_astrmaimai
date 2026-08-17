from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class VisionCandidate:
    """One concrete chat image and the references that can resolve it."""

    message_id: str
    group_id: str
    sender_id: str
    timestamp: float
    image_index: int
    candidate_refs: tuple[str, ...]
    source_kind: str = "inline"
    reply_to_message_id: str = ""
    prefilter_selected: bool = False
    pairing_mode: str = "none"
    pairing_verified: bool = False
    paired_sender_id: str = ""
    paired_group_id: str = ""

    @property
    def primary_ref(self) -> str:
        return self.candidate_refs[0] if self.candidate_refs else ""

    def with_selection(
        self,
        *,
        selected: bool,
        pairing_mode: str | None = None,
        pairing_verified: bool | None = None,
        paired_sender_id: str | None = None,
        paired_group_id: str | None = None,
    ) -> "VisionCandidate":
        return replace(
            self,
            prefilter_selected=bool(selected),
            pairing_mode=str(pairing_mode or self.pairing_mode or "none"),
            pairing_verified=(
                self.pairing_verified
                if pairing_verified is None
                else bool(pairing_verified)
            ),
            paired_sender_id=str(
                self.paired_sender_id if paired_sender_id is None else paired_sender_id
            ),
            paired_group_id=str(
                self.paired_group_id if paired_group_id is None else paired_group_id
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "group_id": self.group_id,
            "sender_id": self.sender_id,
            "timestamp": float(self.timestamp or 0.0),
            "image_index": max(0, int(self.image_index or 0)),
            "candidate_refs": list(self.candidate_refs),
            "source_kind": self.source_kind,
            "reply_to_message_id": self.reply_to_message_id,
            "prefilter_selected": bool(self.prefilter_selected),
            "pairing_mode": self.pairing_mode,
            "pairing_verified": bool(self.pairing_verified),
            "paired_sender_id": self.paired_sender_id,
            "paired_group_id": self.paired_group_id,
        }

    @classmethod
    def from_value(cls, value: Any) -> "VisionCandidate | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            return None
        refs = unique_image_refs(value.get("candidate_refs") or value.get("refs") or [])
        if not refs:
            primary_ref = str(value.get("primary_ref") or value.get("source_ref") or "").strip()
            refs = (primary_ref,) if primary_ref else ()
        if not refs:
            return None
        try:
            timestamp = float(value.get("timestamp", 0.0) or 0.0)
        except (TypeError, ValueError):
            timestamp = 0.0
        try:
            image_index = max(0, int(value.get("image_index", 0) or 0))
        except (TypeError, ValueError):
            image_index = 0
        return cls(
            message_id=str(value.get("message_id") or ""),
            group_id=str(value.get("group_id") or ""),
            sender_id=str(value.get("sender_id") or ""),
            timestamp=timestamp,
            image_index=image_index,
            candidate_refs=refs,
            source_kind=str(value.get("source_kind") or "inline"),
            reply_to_message_id=str(value.get("reply_to_message_id") or ""),
            prefilter_selected=bool(value.get("prefilter_selected", False)),
            pairing_mode=str(value.get("pairing_mode") or "none"),
            pairing_verified=bool(value.get("pairing_verified", False)),
            paired_sender_id=str(value.get("paired_sender_id") or ""),
            paired_group_id=str(value.get("paired_group_id") or ""),
        )


def unique_image_refs(values: Iterable[Any]) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in unique:
            unique.append(normalized)
    return tuple(unique)


def load_vision_candidates(values: Iterable[Any]) -> list[VisionCandidate]:
    candidates: list[VisionCandidate] = []
    for value in values or ():
        candidate = VisionCandidate.from_value(value)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


__all__ = ["VisionCandidate", "load_vision_candidates", "unique_image_refs"]
