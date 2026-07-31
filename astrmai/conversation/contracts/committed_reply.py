from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from .turn_target import TurnTarget


def _ordered_unique(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "\x1f".join(str(part or "") for part in parts).encode("utf-8")
    return f"{prefix}_" + hashlib.sha256(payload).hexdigest()[:24]


class ReplyCommitStatus(str, Enum):
    SENT = "sent"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE = "stale"
    EXTERNAL_UNKNOWN = "external_unknown"


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    plan_id: str
    turn_id: str
    chat_id: str
    chat_kind: str
    target: TurnTarget
    planned_text: str
    planned_segments: tuple[str, ...]
    planned_attachments: tuple[str, ...] = field(default_factory=tuple)
    response_kind: str = "final"
    shape_policy: str = ""
    created_at: float = 0.0

    @classmethod
    def create(
        cls,
        *,
        turn_id: str,
        chat_id: str,
        chat_kind: str,
        target: TurnTarget,
        planned_text: str,
        planned_segments: Iterable[str],
        planned_attachments: Iterable[str] = (),
        response_kind: str = "final",
        shape_policy: str = "",
        created_at: float = 0.0,
    ) -> "ReplyPlan":
        segments = tuple(
            str(segment or "").strip()
            for segment in planned_segments
            if str(segment or "").strip()
        )
        normalized_text = str(planned_text or "").strip()
        normalized_response_kind = str(response_kind or "final").strip() or "final"
        normalized_turn_id = str(turn_id or "").strip()
        plan_id = _stable_id(
            "reply_plan",
            normalized_turn_id,
            str(chat_id or ""),
            normalized_response_kind,
            normalized_text,
            "\x1e".join(segments),
        )
        return cls(
            plan_id=plan_id,
            turn_id=normalized_turn_id,
            chat_id=str(chat_id or "").strip(),
            chat_kind=str(chat_kind or "").strip(),
            target=TurnTarget.from_value(target),
            planned_text=normalized_text,
            planned_segments=segments,
            planned_attachments=_ordered_unique(planned_attachments),
            response_kind=normalized_response_kind,
            shape_policy=str(shape_policy or "").strip(),
            created_at=float(created_at or 0.0),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "turn_id": self.turn_id,
            "chat_id": self.chat_id,
            "chat_kind": self.chat_kind,
            "target": self.target.as_dict(),
            "planned_text": self.planned_text,
            "planned_segments": list(self.planned_segments),
            "planned_attachments": list(self.planned_attachments),
            "response_kind": self.response_kind,
            "shape_policy": self.shape_policy,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ReplySendReceipt:
    status: ReplyCommitStatus
    sent_segments: tuple[str, ...] = field(default_factory=tuple)
    sent_attachment_refs: tuple[str, ...] = field(default_factory=tuple)
    outbound_message_ids: tuple[str, ...] = field(default_factory=tuple)
    visible_text: str = ""
    persistable_text: str = ""
    sent_at: float = 0.0
    failure_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ReplyCommitStatus(self.status))
        object.__setattr__(
            self,
            "sent_segments",
            tuple(
                str(segment or "").strip()
                for segment in self.sent_segments
                if str(segment or "").strip()
            ),
        )
        object.__setattr__(
            self,
            "sent_attachment_refs",
            _ordered_unique(self.sent_attachment_refs),
        )
        object.__setattr__(
            self,
            "outbound_message_ids",
            _ordered_unique(self.outbound_message_ids),
        )


@dataclass(frozen=True, slots=True)
class CommittedBotTurn:
    commit_id: str
    turn_id: str
    plan_id: str
    chat_id: str
    chat_kind: str
    target: TurnTarget
    source_event_ids: tuple[str, ...]
    topic_epoch: int
    visible_text: str
    persistable_text: str
    sent_segments: tuple[str, ...]
    sent_attachment_refs: tuple[str, ...]
    outbound_message_ids: tuple[str, ...]
    sent_at: float
    partial_send: bool
    send_status: ReplyCommitStatus
    failure_reason: str
    reply_hash: str
    provenance: str = "astrmai_send_commit_v1"

    @classmethod
    def from_plan(
        cls,
        plan: ReplyPlan,
        receipt: ReplySendReceipt,
    ) -> "CommittedBotTurn":
        if receipt.status not in {
            ReplyCommitStatus.SENT,
            ReplyCommitStatus.PARTIAL,
        }:
            raise ValueError(
                f"{receipt.status.value} receipt cannot create a visible commit"
            )
        sent_text = "\n".join(receipt.sent_segments).strip()
        visible_text = str(receipt.visible_text or sent_text).strip()
        persistable_text = str(
            receipt.persistable_text or visible_text or sent_text
        ).strip()
        if not persistable_text and receipt.sent_attachment_refs:
            persistable_text = "[附件]"
        if not visible_text:
            visible_text = persistable_text
        reply_hash = hashlib.sha256(persistable_text.encode("utf-8")).hexdigest()[:24]
        commit_id = _stable_id("reply_commit", plan.turn_id, plan.plan_id)
        return cls(
            commit_id=commit_id,
            turn_id=plan.turn_id,
            plan_id=plan.plan_id,
            chat_id=plan.chat_id,
            chat_kind=plan.chat_kind,
            target=plan.target,
            source_event_ids=plan.target.source_event_ids,
            topic_epoch=max(0, int(plan.target.topic_epoch or 0)),
            visible_text=visible_text,
            persistable_text=persistable_text,
            sent_segments=receipt.sent_segments,
            sent_attachment_refs=receipt.sent_attachment_refs,
            outbound_message_ids=receipt.outbound_message_ids,
            sent_at=float(receipt.sent_at or 0.0),
            partial_send=receipt.status == ReplyCommitStatus.PARTIAL,
            send_status=receipt.status,
            failure_reason=str(receipt.failure_reason or ""),
            reply_hash=reply_hash,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommittedBotTurn":
        return cls(
            commit_id=str(value.get("commit_id", "") or ""),
            turn_id=str(value.get("turn_id", "") or ""),
            plan_id=str(value.get("plan_id", "") or ""),
            chat_id=str(value.get("chat_id", "") or ""),
            chat_kind=str(value.get("chat_kind", "") or ""),
            target=TurnTarget.from_value(value.get("target")),
            source_event_ids=_ordered_unique(value.get("source_event_ids", ())),
            topic_epoch=max(0, int(value.get("topic_epoch", 0) or 0)),
            visible_text=str(value.get("visible_text", "") or ""),
            persistable_text=str(value.get("persistable_text", "") or ""),
            sent_segments=tuple(
                str(segment or "").strip()
                for segment in value.get("sent_segments", ()) or ()
                if str(segment or "").strip()
            ),
            sent_attachment_refs=_ordered_unique(
                value.get("sent_attachment_refs", ())
            ),
            outbound_message_ids=_ordered_unique(
                value.get("outbound_message_ids", ())
            ),
            sent_at=float(value.get("sent_at", 0.0) or 0.0),
            partial_send=bool(value.get("partial_send", False)),
            send_status=ReplyCommitStatus(
                value.get("send_status", ReplyCommitStatus.SENT.value)
            ),
            failure_reason=str(value.get("failure_reason", "") or ""),
            reply_hash=str(value.get("reply_hash", "") or ""),
            provenance=str(
                value.get("provenance", "astrmai_send_commit_v1")
                or "astrmai_send_commit_v1"
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "commit_id": self.commit_id,
            "turn_id": self.turn_id,
            "plan_id": self.plan_id,
            "chat_id": self.chat_id,
            "chat_kind": self.chat_kind,
            "target": self.target.as_dict(),
            "source_event_ids": list(self.source_event_ids),
            "topic_epoch": self.topic_epoch,
            "visible_text": self.visible_text,
            "persistable_text": self.persistable_text,
            "sent_segments": list(self.sent_segments),
            "sent_attachment_refs": list(self.sent_attachment_refs),
            "outbound_message_ids": list(self.outbound_message_ids),
            "sent_at": self.sent_at,
            "partial_send": self.partial_send,
            "send_status": self.send_status.value,
            "failure_reason": self.failure_reason,
            "reply_hash": self.reply_hash,
            "provenance": self.provenance,
        }


__all__ = [
    "CommittedBotTurn",
    "ReplyCommitStatus",
    "ReplyPlan",
    "ReplySendReceipt",
]
