from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

from ..attention.participation_policy import ParticipationPolicy, ParticipationState
from ..attention.turn_target_resolver import resolve_turn_target
from ..contracts.committed_reply import (
    CommittedBotTurn,
    ReplyCommitStatus,
    ReplyPlan,
    ReplySendReceipt,
)
from ..contracts.conversation_event import ConversationEvent
from ..planning.message_renderer import MessageRenderer
from ..runtime.architecture_rollout import stable_structure_hash


@dataclass(slots=True)
class ReplayClock:
    value: float = 1_700_000_000.0

    def now(self) -> float:
        return float(self.value)

    def advance(self, seconds: float) -> float:
        self.value += float(seconds or 0.0)
        return self.now()


class ReplayEvent:
    def __init__(self, canonical: ConversationEvent):
        self.unified_msg_origin = canonical.chat_id
        self.message_str = canonical.visible_text
        self.timestamp = canonical.timestamp
        self._sender_id = canonical.actor_id
        self._sender_name = canonical.actor_name
        self._extras: dict[str, Any] = {
            "astrmai_conversation_event": canonical,
            "astrmai_timestamp": canonical.timestamp,
            "astrmai_rich_text": canonical.rich_text,
            "astrmai_event_provenance": canonical.provenance,
            "astrmai_interaction_kind": canonical.interaction_kind,
            "extracted_image_refs": list(canonical.image_refs),
        }

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def get_extra(self, key: str, default: Any = None) -> Any:
        return self._extras.get(key, default)

    def set_extra(self, key: str, value: Any) -> None:
        self._extras[key] = value


@dataclass(slots=True)
class ReplayCandidate:
    event: ReplayEvent
    canonical_event: ConversationEvent
    sender_id: str
    sender_name: str
    text: str
    rich_text: str
    timestamp: float
    is_reply_to_bot: bool = False
    is_at_bot: bool = False
    is_direct_wakeup: bool = False
    reply_target_sender_id: str = ""
    reply_target_sender_name: str = ""


@dataclass(frozen=True, slots=True)
class ReplayResult:
    case_id: str
    event_ids: tuple[str, ...]
    target: Mapping[str, Any]
    actor_set: Mapping[str, Any]
    participation: Mapping[str, Any]
    context: Mapping[str, Any]
    reply_plan: Mapping[str, Any]
    reply_commit: Mapping[str, Any]
    proactive_generation_current: bool
    timings_ms: Mapping[str, float] = field(default_factory=dict)


def _conversation_event(raw: Mapping[str, Any], *, clock: ReplayClock) -> ConversationEvent:
    allowed = {item.name for item in fields(ConversationEvent)}
    payload = {key: value for key, value in dict(raw).items() if key in allowed}
    payload.setdefault("timestamp", clock.now())
    payload.setdefault("chat_id", "ff:GroupMessage:replay")
    payload.setdefault("chat_kind", "group")
    payload.setdefault("actor_id", "unknown")
    payload.setdefault("actor_name", "匿名用户")
    payload.setdefault("visible_text", "")
    payload.setdefault("rich_text", payload["visible_text"])
    payload.setdefault("message_kind", "image" if payload.get("image_refs") else "text")
    payload.setdefault("role", "user")
    for key in ("at_actor_ids", "source_event_ids", "image_refs", "attachment_refs"):
        if key in payload:
            payload[key] = tuple(payload[key] or ())
    return ConversationEvent(**payload)


def _candidate(canonical: ConversationEvent) -> ReplayCandidate:
    event = ReplayEvent(canonical)
    return ReplayCandidate(
        event=event,
        canonical_event=canonical,
        sender_id=canonical.actor_id,
        sender_name=canonical.actor_name,
        text=canonical.visible_text,
        rich_text=canonical.rich_text,
        timestamp=canonical.timestamp,
        is_reply_to_bot=canonical.is_reply_to_bot,
        is_at_bot=canonical.is_at_bot,
        is_direct_wakeup=canonical.is_direct_wakeup,
        reply_target_sender_id=canonical.reply_target_actor_id,
        reply_target_sender_name=canonical.reply_target_actor_name,
    )


def load_replay_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        normalized = line.strip()
        if normalized:
            cases.append(json.loads(normalized))
    return cases


class ContextArchitectureReplayHarness:
    def __init__(self, *, clock: ReplayClock | None = None):
        self.clock = clock or ReplayClock()
        self.participation_policy = ParticipationPolicy()

    def run(self, case: Mapping[str, Any]) -> ReplayResult:
        normalization_started = time.perf_counter()
        raw_events = list(case.get("events", ()) or ())
        canonical_events = [
            _conversation_event(raw, clock=self.clock)
            for raw in raw_events
            if isinstance(raw, Mapping)
        ]
        if not canonical_events:
            raise ValueError("replay case must contain at least one event")
        normalization_ms = round(
            (time.perf_counter() - normalization_started) * 1000.0,
            3,
        )
        candidates = [_candidate(item) for item in canonical_events]
        focus_index = int(case.get("focus_index", len(candidates) - 1) or 0)
        focus = candidates[focus_index]
        target_started = time.perf_counter()
        target, actor_set = resolve_turn_target(
            focus,
            focus,
            candidates,
            bot_id=str(case.get("bot_id", "bot") or "bot"),
        )
        target_ms = round((time.perf_counter() - target_started) * 1000.0, 3)

        recent_commit_raw = case.get("recent_commit")
        recent_commit = (
            SimpleNamespace(**dict(recent_commit_raw))
            if isinstance(recent_commit_raw, Mapping)
            else None
        )
        previous_state_raw = case.get("participation_state")
        previous_state = (
            ParticipationState(**dict(previous_state_raw))
            if isinstance(previous_state_raw, Mapping)
            else None
        )
        participation, _ = self.participation_policy.evaluate(
            focus_event=focus.event,
            batch_events=[focus.event],
            strong_wakeup_event_ids=case.get("strong_wakeup_event_ids", ()) or (),
            recent_committed_turn=recent_commit,
            previous_state=previous_state,
            now=self.clock.now(),
        )

        renderer_started = time.perf_counter()
        context_package = MessageRenderer.build_context_package(
            shared_events=canonical_events,
            owned_events=(focus.canonical_event,),
            turn_instruction=f"reply_target_actor_id={target.target_actor_id}",
        )
        renderer_ms = round(
            (time.perf_counter() - renderer_started) * 1000.0,
            3,
        )
        commit_started = time.perf_counter()
        plan = ReplyPlan.create(
            turn_id=str(case.get("turn_id", f"turn:{case.get('case_id', 'replay')}") or ""),
            chat_id=focus.canonical_event.chat_id,
            chat_kind=focus.canonical_event.chat_kind,
            target=target,
            planned_text=str(case.get("planned_text", "fixture reply") or ""),
            planned_segments=case.get("planned_segments", ("fixture reply",)) or (),
            created_at=self.clock.now(),
        )
        captured_generation = int(case.get("captured_generation", 0) or 0)
        current_generation = int(case.get("current_generation", captured_generation) or 0)
        generation_current = captured_generation == current_generation
        commit: CommittedBotTurn | None = None
        if generation_current and bool(case.get("send_reply", True)):
            sent_segments = tuple(case.get("sent_segments", plan.planned_segments) or ())
            status = ReplyCommitStatus(
                str(case.get("send_status", ReplyCommitStatus.SENT.value) or "sent")
            )
            if status in {ReplyCommitStatus.SENT, ReplyCommitStatus.PARTIAL}:
                commit = CommittedBotTurn.from_plan(
                    plan,
                    ReplySendReceipt(
                        status=status,
                        sent_segments=sent_segments,
                        sent_at=self.clock.now(),
                    ),
                )
        commit_ms = round((time.perf_counter() - commit_started) * 1000.0, 3)

        context_stats = context_package.stats
        return ReplayResult(
            case_id=str(case.get("case_id", "") or ""),
            event_ids=tuple(item.event_id for item in canonical_events),
            target=target.as_dict(),
            actor_set=actor_set.as_dict(),
            participation={
                "action": participation.action,
                "reason": participation.reason,
                "score": participation.score,
                "signals": list(participation.signals),
            },
            context={
                **context_stats,
                "package_hash": stable_structure_hash(context_stats),
                "rendered_hash": stable_structure_hash(context_package.render()),
                "media_event_count": sum(bool(item.image_refs) for item in canonical_events),
            },
            reply_plan={
                "plan_id": plan.plan_id,
                "segment_count": len(plan.planned_segments),
                "target_actor_id": plan.target.target_actor_id,
            },
            reply_commit=(
                {
                    "commit_id": commit.commit_id,
                    "sent_segments": list(commit.sent_segments),
                    "partial_send": commit.partial_send,
                    "target_actor_id": commit.target.target_actor_id,
                }
                if commit is not None
                else {}
            ),
            proactive_generation_current=generation_current,
            timings_ms={
                "normalization": normalization_ms,
                "target": target_ms,
                "renderer": renderer_ms,
                "commit": commit_ms,
            },
        )


__all__ = [
    "ContextArchitectureReplayHarness",
    "ReplayClock",
    "ReplayResult",
    "load_replay_cases",
]
