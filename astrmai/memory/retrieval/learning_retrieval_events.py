from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from ...infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from ..contracts.learning_retrieval import (
    LearningAssetProvenance,
    LearningRetrievalEvent,
    LearningRetrievalEventMutation,
)


_SOURCE_LAYERS = {"memory_injection", "react_retriever", "prompt_refiner", "reply_commit"}
_STAGES = {"eligible", "selected", "accepted_for_prompt", "prompt_visible", "reply_outcome"}
_EVENT_STATUSES = {"observed", "blocked", "unknown", "failed"}
_TRIMMED_REASONS = {"", "budget_zero", "fast_mode", "near_context", "priority_eviction", "policy", "error"}
_OUTCOMES = {"unknown", "reply_sent", "reply_failed", "cancelled", "no_reply"}
_SAFE_DIAGNOSTIC_KEYS = {
    "failure_stage", "failure_kind", "expected", "actual", "count", "candidate_count",
    "selected_count", "accepted_count", "visible_count", "profile_version", "source",
    "retryable", "conflict", "legacy", "mode", "threshold_version",
    "cosine_similarity", "trigram_overlap", "attempt",
    "repetition_attempts",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _safe_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for raw_key, raw_value in dict(value or {}).items():
        key = str(raw_key)
        if key not in _SAFE_DIAGNOSTIC_KEYS:
            continue
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            safe[key] = raw_value
        elif isinstance(raw_value, str):
            safe[key] = raw_value[:240]
        elif key == "repetition_attempts" and isinstance(raw_value, (list, tuple)):
            attempts = []
            for item in raw_value[:20]:
                if not isinstance(item, dict):
                    continue
                attempts.append({
                    "asset_id": str(item.get("asset_id") or "")[:120],
                    "profile_version": str(item.get("profile_version") or "")[:120],
                    "outcome": str(item.get("outcome") or "")[:120],
                    "attempt_count": int(item.get("attempt_count") or 0),
                    "first_cosine_similarity": float(item.get("first_cosine_similarity") or 0.0),
                    "first_trigram_overlap": float(item.get("first_trigram_overlap") or 0.0),
                    "second_cosine_similarity": float(item.get("second_cosine_similarity") or 0.0),
                    "second_trigram_overlap": float(item.get("second_trigram_overlap") or 0.0),
                    "reason_code": str(item.get("reason_code") or "")[:120],
                })
            safe[key] = attempts
        elif isinstance(raw_value, (list, tuple)):
            safe[key] = [str(item)[:120] for item in raw_value[:20]]
    return safe


class LearningRetrievalEventWriter:
    @staticmethod
    def merge_shadow(event: Any, payload: dict[str, Any], *, source_layer: str) -> dict[str, Any]:
        """Merge source-layer observations into the turn-level prompt/reply handoff."""
        if not hasattr(event, "set_extra"):
            return payload
        layer_key = f"astrmai_learning_retrieval_{source_layer}_shadow"
        event.set_extra(layer_key, payload)
        existing = (
            event.get_extra("astrmai_learning_retrieval_shadow", None)
            if hasattr(event, "get_extra")
            else None
        )
        if not isinstance(existing, dict):
            merged = dict(payload)
            merged["source_layers"] = (source_layer,)
            event.set_extra("astrmai_learning_retrieval_shadow", merged)
            return merged
        left = existing.get("correlation")
        right = payload.get("correlation")
        same_turn = bool(
            left is not None
            and right is not None
            and getattr(left, "durable", False)
            and getattr(right, "durable", False)
            and left.turn_id == right.turn_id
            and left.correlation_id == right.correlation_id
            and existing.get("generation") == payload.get("generation")
            and existing.get("policy_version") == payload.get("policy_version")
        )
        if not same_turn:
            existing["merge_conflict"] = "turn_or_generation_mismatch"
            event.set_extra("astrmai_learning_retrieval_shadow", existing)
            return existing

        merged = dict(existing)
        for name in ("asset_revision_ids", "selected_ids", "accepted_ids"):
            merged[name] = tuple(sorted(set(existing.get(name) or ()) | set(payload.get(name) or ())))
        selected = {}
        for item in tuple(existing.get("selected") or ()) + tuple(payload.get("selected") or ()):
            selected[(str(getattr(item, "asset_id", "")), getattr(item, "asset_revision", None))] = item
        merged["selected"] = tuple(selected[key] for key in sorted(selected))
        provenance = {}
        for item in tuple(existing.get("asset_provenance") or ()) + tuple(payload.get("asset_provenance") or ()):
            provenance[(str(getattr(item, "asset_id", "")), getattr(item, "asset_revision", None))] = item
        merged["asset_provenance"] = tuple(provenance[key] for key in sorted(provenance))
        for name in ("candidate_revision", "review_revision", "admission_revision"):
            values = {
                value
                for value in (existing.get(name), payload.get(name))
                if type(value) is int
            }
            merged[name] = next(iter(values)) if len(values) == 1 else None
        merged["created_at"] = min(
            float(existing.get("created_at", 0.0) or 0.0),
            float(payload.get("created_at", 0.0) or 0.0),
        )
        merged["source_layers"] = tuple(
            sorted(set(existing.get("source_layers") or ()) | {source_layer})
        )
        event.set_extra("astrmai_learning_retrieval_shadow", merged)
        return merged

    @staticmethod
    def revision_facts(candidates) -> tuple[int | None, int | None, int | None]:
        items = tuple(candidates or ())
        if not items:
            return None, None, None

        def unique(name: str) -> int | None:
            values = {
                getattr(item, name, None)
                for item in items
                if type(getattr(item, name, None)) is int
            }
            return next(iter(values)) if len(values) == 1 else None

        return unique("candidate_revision"), unique("review_revision"), unique("admission_revision")

    @staticmethod
    def asset_provenance(candidates, *, generation: int) -> tuple[LearningAssetProvenance, ...]:
        facts = []
        for item in tuple(candidates or ()):
            source_ids = tuple(
                sorted({str(value).strip() for value in tuple(getattr(item, "source_example_ids", ()) or ()) if str(value).strip()})
            )
            facts.append(LearningAssetProvenance(
                asset_id=str(getattr(item, "asset_id", "") or "").strip(),
                asset_revision=getattr(item, "asset_revision", -1),
                generation=generation,
                candidate_revision=getattr(item, "candidate_revision", -1),
                review_revision=getattr(item, "review_revision", -1),
                admission_revision=getattr(item, "admission_revision", -1),
                canonical_memory_id=str(getattr(item, "canonical_memory_id", "") or "").strip(),
                provenance_hash=str(getattr(item, "provenance_hash", "") or "").strip(),
                source_evidence_ids=source_ids,
            ))
        return tuple(sorted(facts, key=lambda item: (item.asset_id, item.asset_revision)))

    @staticmethod
    def _provenance_payload(items: tuple[LearningAssetProvenance, ...]) -> list[dict[str, Any]]:
        return [
            {
                "asset_id": item.asset_id,
                "asset_revision": item.asset_revision,
                "generation": item.generation,
                "candidate_revision": item.candidate_revision,
                "review_revision": item.review_revision,
                "admission_revision": item.admission_revision,
                "canonical_memory_id": item.canonical_memory_id,
                "provenance_hash": item.provenance_hash,
                "source_evidence_ids": sorted(set(item.source_evidence_ids)),
            }
            for item in sorted(items, key=lambda item: (item.asset_id, item.asset_revision))
        ]

    @classmethod
    def _provenance_covers(
        cls,
        asset_ids: tuple[str, ...],
        items: tuple[LearningAssetProvenance, ...],
        *,
        generation: int | None,
        asset_revision_ids: tuple[str, ...] = (),
    ) -> bool:
        requested = {str(item).strip() for item in asset_ids if str(item).strip()}
        facts = {item.asset_id: item for item in items}
        expected_revisions = {}
        try:
            for raw_identity in asset_revision_ids:
                raw_asset_id, raw_revision = str(raw_identity).rsplit(":", 1)
                expected_revisions[raw_asset_id] = int(raw_revision)
        except (TypeError, ValueError):
            return False
        if (
            not requested
            or not requested.issubset(facts)
            or set(expected_revisions) != set(facts)
            or type(generation) is not int
            or generation < 0
        ):
            return False
        for asset_id in requested:
            item = facts[asset_id]
            if (
                not item.asset_id
                or type(item.asset_revision) is not int or item.asset_revision < 0
                or item.asset_revision != expected_revisions.get(asset_id)
                or type(item.generation) is not int or item.generation != generation
                or type(item.candidate_revision) is not int or item.candidate_revision < 0
                or type(item.review_revision) is not int or item.review_revision < 0
                or type(item.admission_revision) is not int or item.admission_revision < 0
                or not item.canonical_memory_id.strip()
                or not item.provenance_hash.strip()
                or not item.source_evidence_ids
                or any(not str(value).strip() for value in item.source_evidence_ids)
            ):
                return False
        return True

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @staticmethod
    def selection_events(
        *,
        correlation,
        source_layer: str,
        asset_revision_ids: tuple[str, ...],
        selected_ids: tuple[str, ...],
        accepted_ids: tuple[str, ...],
        generation: int | None,
        policy_version: str,
        created_at: float,
        accepted: bool,
        reason_code: str = "",
        include_accepted: bool = True,
        candidate_revision: int | None = None,
        review_revision: int | None = None,
        admission_revision: int | None = None,
        asset_provenance: tuple[LearningAssetProvenance, ...] = (),
    ) -> tuple[LearningRetrievalEvent, ...]:
        events = [
            LearningRetrievalEvent(
                correlation=correlation,
                source_layer=source_layer,
                stage="eligible",
                event_status=("observed" if correlation.durable and asset_revision_ids else "blocked"),
                policy_version=policy_version,
                asset_revision_ids=asset_revision_ids,
                asset_provenance=asset_provenance,
                generation=generation,
                candidate_revision=candidate_revision,
                review_revision=review_revision,
                admission_revision=admission_revision,
                reason_code=reason_code,
                created_at=created_at,
            ),
            LearningRetrievalEvent(
                correlation=correlation,
                source_layer=source_layer,
                stage="selected",
                event_status=("observed" if correlation.durable and selected_ids else "blocked"),
                policy_version=policy_version,
                asset_revision_ids=asset_revision_ids,
                asset_provenance=asset_provenance,
                selected_ids=selected_ids,
                generation=generation,
                candidate_revision=candidate_revision,
                review_revision=review_revision,
                admission_revision=admission_revision,
                reason_code=reason_code,
                created_at=created_at,
            ),
        ]
        if include_accepted:
            events.append(
                LearningRetrievalEventWriter.accepted_for_prompt_event(
                    correlation=correlation,
                    source_layer=source_layer,
                    asset_revision_ids=asset_revision_ids,
                    selected_ids=selected_ids,
                    accepted_ids=accepted_ids if accepted else (),
                    generation=generation,
                    policy_version=policy_version,
                    created_at=created_at,
                    accepted=accepted,
                    reason_code=reason_code,
                    candidate_revision=candidate_revision,
                    review_revision=review_revision,
                    admission_revision=admission_revision,
                    asset_provenance=asset_provenance,
                )
            )
        return tuple(events)

    @staticmethod
    def accepted_for_prompt_event(
        *,
        correlation,
        source_layer: str,
        asset_revision_ids: tuple[str, ...],
        selected_ids: tuple[str, ...],
        accepted_ids: tuple[str, ...],
        generation: int | None,
        policy_version: str,
        created_at: float,
        accepted: bool,
        reason_code: str = "",
        candidate_revision: int | None = None,
        review_revision: int | None = None,
        admission_revision: int | None = None,
        asset_provenance: tuple[LearningAssetProvenance, ...] = (),
        diagnostics: dict[str, Any] | None = None,
    ) -> LearningRetrievalEvent:
        return LearningRetrievalEvent(
            correlation=correlation,
            source_layer=source_layer,
            stage="accepted_for_prompt",
            event_status=("observed" if accepted and correlation.durable else "blocked"),
            policy_version=policy_version,
            asset_revision_ids=asset_revision_ids,
            asset_provenance=asset_provenance,
            selected_ids=selected_ids,
            accepted_ids=accepted_ids if accepted else (),
            generation=generation,
            candidate_revision=candidate_revision,
            review_revision=review_revision,
            admission_revision=admission_revision,
            reason_code="" if accepted else (reason_code or "prompt_injection_disabled"),
            diagnostics=dict(diagnostics or {}),
            created_at=created_at,
        )

    @staticmethod
    def prompt_visibility_event(
        *,
        correlation,
        asset_revision_ids: tuple[str, ...],
        accepted_ids: tuple[str, ...],
        visible_ids: tuple[str, ...],
        generation: int | None,
        policy_version: str,
        created_at: float,
        budget_chars: int | None = None,
        trimmed_reason: str = "",
        render_failed: bool = False,
        candidate_revision: int | None = None,
        review_revision: int | None = None,
        admission_revision: int | None = None,
        asset_provenance: tuple[LearningAssetProvenance, ...] = (),
        diagnostics: dict[str, Any] | None = None,
    ) -> LearningRetrievalEvent:
        prompt_revision_available = bool(
            str(getattr(correlation, "prompt_revision", "") or "").strip()
        )
        requested_visible = bool(visible_ids and not render_failed)
        provenance_available = LearningRetrievalEventWriter._provenance_covers(
            visible_ids,
            asset_provenance,
            generation=generation,
            asset_revision_ids=asset_revision_ids,
        ) if requested_visible else True
        visible = bool(
            requested_visible
            and correlation.durable
            and prompt_revision_available
            and provenance_available
        )
        blocked_reason = (
            "prompt_revision_unavailable"
            if requested_visible and not prompt_revision_available
            else "asset_provenance_unavailable"
            if requested_visible and not provenance_available
            else trimmed_reason or "not_visible"
        )
        return LearningRetrievalEvent(
            correlation=correlation,
            source_layer="prompt_refiner",
            stage="prompt_visible",
            event_status=(
                "failed" if render_failed else "observed" if visible else "blocked"
            ),
            policy_version=policy_version,
            asset_revision_ids=asset_revision_ids,
            asset_provenance=asset_provenance,
            accepted_ids=accepted_ids,
            visible_ids=visible_ids if visible else (),
            generation=generation,
            candidate_revision=candidate_revision,
            review_revision=review_revision,
            admission_revision=admission_revision,
            trimmed_reason="error" if render_failed else trimmed_reason,
            budget_chars=budget_chars,
            reason_code=("render_error" if render_failed else "" if visible else blocked_reason),
            diagnostics=dict(diagnostics or {}),
            created_at=created_at,
        )

    @staticmethod
    def reply_outcome_event(
        *,
        correlation,
        asset_revision_ids: tuple[str, ...],
        generation: int | None,
        policy_version: str,
        outcome: str,
        reply_id: str,
        created_at: float,
        candidate_revision: int | None = None,
        review_revision: int | None = None,
        admission_revision: int | None = None,
        asset_provenance: tuple[LearningAssetProvenance, ...] = (),
    ) -> LearningRetrievalEvent:
        status = "observed" if correlation.durable and outcome in _OUTCOMES - {"unknown"} else "unknown"
        if outcome in {"reply_sent", "reply_failed"} and not str(reply_id or "").strip():
            status = "unknown"
        return LearningRetrievalEvent(
            correlation=correlation,
            source_layer="reply_commit",
            stage="reply_outcome",
            event_status=status,
            policy_version=policy_version,
            asset_revision_ids=asset_revision_ids,
            asset_provenance=asset_provenance,
            generation=generation,
            candidate_revision=candidate_revision,
            review_revision=review_revision,
            admission_revision=admission_revision,
            outcome=outcome if outcome in _OUTCOMES else "unknown",
            reply_id=str(reply_id or "").strip(),
            reason_code="" if status == "observed" else "terminal_identity_unavailable",
            created_at=created_at,
        )

    @staticmethod
    def _payload(event: LearningRetrievalEvent) -> dict[str, Any]:
        correlation = event.correlation
        return {
            "turn_id": correlation.turn_id.strip(),
            "correlation_id": correlation.correlation_id.strip(),
            "source_layer": event.source_layer,
            "stage": event.stage,
            "event_status": event.event_status,
            "scope_id": correlation.scope_id.strip(),
            "sender_id": correlation.sender_id.strip(),
            "query_fingerprint": correlation.query_fingerprint.strip(),
            "candidate_revision": event.candidate_revision,
            "review_revision": event.review_revision,
            "admission_revision": event.admission_revision,
            "asset_provenance": LearningRetrievalEventWriter._provenance_payload(event.asset_provenance),
            "asset_revision_ids": sorted(set(event.asset_revision_ids)),
            "selected_ids": sorted(set(event.selected_ids)),
            "accepted_ids": sorted(set(event.accepted_ids)),
            "visible_ids": sorted(set(event.visible_ids)),
            "generation": event.generation,
            "policy_version": event.policy_version,
            "prompt_revision": correlation.prompt_revision.strip(),
            "reason_code": event.reason_code,
            "trimmed_reason": event.trimmed_reason,
            "budget_chars": event.budget_chars,
            "budget_tokens": event.budget_tokens,
            "outcome": event.outcome,
            "reply_id": event.reply_id.strip(),
            "diagnostics": _safe_diagnostics(event.diagnostics),
            "created_at": float(event.created_at),
        }

    @staticmethod
    def _validate(event: LearningRetrievalEvent) -> str:
        if event.event_status == "observed" and not event.correlation.durable:
            return "correlation_unavailable"
        if event.source_layer not in _SOURCE_LAYERS:
            return "source_layer_invalid"
        if event.stage not in _STAGES:
            return "stage_invalid"
        if event.event_status not in _EVENT_STATUSES:
            return "event_status_invalid"
        if event.trimmed_reason not in _TRIMMED_REASONS:
            return "trimmed_reason_invalid"
        if event.outcome not in _OUTCOMES:
            return "outcome_invalid"
        if not event.policy_version.strip():
            return "policy_version_unavailable"
        if event.stage != "reply_outcome" and event.outcome != "unknown":
            return "outcome_stage_mismatch"
        if event.stage == "prompt_visible" and event.event_status == "observed" and not event.correlation.prompt_revision.strip():
            return "prompt_revision_unavailable"
        if (
            event.stage == "prompt_visible"
            and event.event_status == "observed"
            and not LearningRetrievalEventWriter._provenance_covers(
                event.visible_ids,
                event.asset_provenance,
                generation=event.generation,
                asset_revision_ids=event.asset_revision_ids,
            )
        ):
            return "asset_provenance_unavailable"
        if event.event_status == "observed":
            if event.stage == "eligible" and not event.asset_revision_ids:
                return "eligible_assets_unavailable"
            if event.stage == "selected" and not event.selected_ids:
                return "selected_assets_unavailable"
            if event.stage == "accepted_for_prompt" and not event.accepted_ids:
                return "accepted_assets_unavailable"
            if event.stage == "prompt_visible" and not event.visible_ids:
                return "visible_assets_unavailable"
            if event.stage == "reply_outcome" and event.outcome in {"reply_sent", "reply_failed"} and not event.reply_id.strip():
                return "reply_id_unavailable"
        return ""

    @classmethod
    def _key(cls, event: LearningRetrievalEvent, _payload_json: str) -> str:
        if event.idempotency_key.strip():
            return event.idempotency_key.strip()
        work_id = str(event.work_id or "").strip() or _canonical_json(
            {
                "turn_id": event.correlation.turn_id.strip(),
                "correlation_id": event.correlation.correlation_id.strip(),
                "source_layer": event.source_layer,
                "stage": event.stage,
                "asset_revision_ids": sorted(set(event.asset_revision_ids)),
                "generation": event.generation,
                "policy_version": event.policy_version,
                "prompt_revision": event.correlation.prompt_revision.strip(),
            }
        )
        material = {
            "schema": "learning-retrieval-event-v1",
            "work_id": work_id,
        }
        return "sha256:v1:" + hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()

    async def append(self, event: LearningRetrievalEvent) -> LearningRetrievalEventMutation:
        failure = self._validate(event)
        if failure:
            return LearningRetrievalEventMutation(False, True, failure_kind=failure)
        payload = self._payload(event)
        payload_json = _canonical_json(payload)
        key = self._key(event, payload_json)
        event_id = "lre-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if event.stage == "reply_outcome":
                terminal_cursor = await db.execute(
                    """SELECT event_id,idempotency_key,outcome,reply_id,created_at
                       FROM learning_retrieval_event
                       WHERE turn_id=? AND correlation_id=? AND source_layer='reply_commit'
                         AND stage='reply_outcome' AND prompt_revision=?
                         AND asset_revision_ids_json=?""",
                    (
                        payload["turn_id"],
                        payload["correlation_id"],
                        payload["prompt_revision"],
                        _canonical_json(payload["asset_revision_ids"]),
                    ),
                )
                terminal = await terminal_cursor.fetchone()
                await terminal_cursor.close()
                if terminal is not None:
                    same_terminal = bool(
                        str(terminal[2]) == payload["outcome"]
                        and str(terminal[3]) == payload["reply_id"]
                    )
                    if str(terminal[1]) != key or not same_terminal:
                        await db.rollback()
                        return LearningRetrievalEventMutation(
                            False,
                            not same_terminal,
                            idempotent=same_terminal,
                            event_id=str(terminal[0]),
                            idempotency_key=str(terminal[1]),
                            failure_kind="" if same_terminal else "terminal_outcome_conflict",
                        )
            cursor = await db.execute(
                "SELECT event_id, diagnostics_json FROM learning_retrieval_event WHERE idempotency_key = ?",
                (key,),
            )
            current = await cursor.fetchone()
            await cursor.close()
            if current is not None:
                stored_cursor = await db.execute(
                    """SELECT turn_id,correlation_id,source_layer,stage,event_status,scope_id,sender_id,
                       query_fingerprint,candidate_revision,review_revision,admission_revision,
                       asset_revision_ids_json,asset_provenance_json,selected_ids_json,accepted_ids_json,visible_ids_json,
                       generation,policy_version,prompt_revision,reason_code,trimmed_reason,budget_chars,
                       budget_tokens,outcome,reply_id,diagnostics_json,created_at
                       FROM learning_retrieval_event WHERE idempotency_key = ?""",
                    (key,),
                )
                row = await stored_cursor.fetchone()
                await stored_cursor.close()
                stored_payload = {
                    name: value
                    for name, value in zip(
                        (
                            "turn_id","correlation_id","source_layer","stage","event_status","scope_id","sender_id",
                            "query_fingerprint","candidate_revision","review_revision","admission_revision",
                            "asset_revision_ids","asset_provenance","selected_ids","accepted_ids","visible_ids","generation","policy_version",
                            "prompt_revision","reason_code","trimmed_reason","budget_chars","budget_tokens","outcome",
                            "reply_id","diagnostics","created_at",
                        ),
                        row,
                    )
                }
                for name in ("asset_revision_ids", "asset_provenance", "selected_ids", "accepted_ids", "visible_ids", "diagnostics"):
                    stored_payload[name] = json.loads(stored_payload[name])
                await db.rollback()
                stored_payload.pop("created_at", None)
                replay_payload = dict(payload)
                replay_payload.pop("created_at", None)
                idempotent = _canonical_json(stored_payload) == _canonical_json(replay_payload)
                return LearningRetrievalEventMutation(
                    False,
                    not idempotent,
                    idempotent=idempotent,
                    event_id=str(current[0]),
                    idempotency_key=key,
                    failure_kind="" if idempotent else "idempotency_conflict",
                )
            await db.execute(
                """INSERT INTO learning_retrieval_event(
                   event_id,idempotency_key,turn_id,correlation_id,source_layer,stage,event_status,
                   scope_id,sender_id,query_fingerprint,candidate_revision,review_revision,admission_revision,
                   asset_revision_ids_json,asset_provenance_json,selected_ids_json,accepted_ids_json,visible_ids_json,generation,
                   policy_version,prompt_revision,reason_code,trimmed_reason,budget_chars,budget_tokens,outcome,
                   reply_id,diagnostics_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,key,payload["turn_id"],payload["correlation_id"],payload["source_layer"],
                    payload["stage"],payload["event_status"],payload["scope_id"],payload["sender_id"],
                    payload["query_fingerprint"],payload["candidate_revision"],payload["review_revision"],
                    payload["admission_revision"],_canonical_json(payload["asset_revision_ids"]),
                    _canonical_json(payload["asset_provenance"]),
                    _canonical_json(payload["selected_ids"]),_canonical_json(payload["accepted_ids"]),
                    _canonical_json(payload["visible_ids"]),payload["generation"],payload["policy_version"],
                    payload["prompt_revision"],payload["reason_code"],payload["trimmed_reason"],
                    payload["budget_chars"],payload["budget_tokens"],payload["outcome"],payload["reply_id"],
                    _canonical_json(payload["diagnostics"]),payload["created_at"],
                ),
            )
            await db.commit()
        return LearningRetrievalEventMutation(True, False, event_id=event_id, idempotency_key=key)


__all__ = ["LearningRetrievalEventWriter"]
