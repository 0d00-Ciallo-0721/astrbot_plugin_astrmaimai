from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..contracts.memory_query import MemoryCandidate, MemoryWriteRequest
from .v2_store import MemoryV2Store


@dataclass(slots=True)
class ExpressionPatternRecord:
    id: str
    group_id: str
    situation: str
    expression: str
    style: str = ""
    content_list: str = "[]"
    count: int = 1
    checked: bool = False
    rejected: bool = False
    modified_by: str = ""
    source: str = "learning_expression_pattern"
    shared_scope: str = ""
    think_level: int = 0
    review_status: str = "pending"
    review_reason: str = ""
    review_suggestion: str = ""
    last_review_time: float = 0.0
    weight: float = 1.0
    last_active_time: float = 0.0
    create_time: float = 0.0
    status: str = "review_pending"
    visibility: str = "maintenance_only"
    metadata: dict[str, Any] = field(default_factory=dict)


class ExpressionPatternService:
    def __init__(self, store: MemoryV2Store, write_service):
        self.store = store
        self.write_service = write_service

    @staticmethod
    def normalize_text(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "").strip().lower())

    @classmethod
    def build_dedup_key(cls, group_id: str, situation: str, expression: str, shared_scope: str = "") -> str:
        return "expression_pattern:{group}:{scope}:{situation}:{expression}".format(
            group=str(group_id or ""),
            scope=cls.normalize_text(shared_scope or ""),
            situation=cls.normalize_text(situation or ""),
            expression=cls.normalize_text(expression or ""),
        )

    @staticmethod
    def _canonical_status(review_status: str) -> str:
        normalized = str(review_status or "pending").strip().lower()
        if normalized == "approved":
            return "active"
        if normalized == "rejected":
            return "rejected"
        if normalized == "stale":
            return "stale"
        return "review_pending"

    @staticmethod
    def _visibility_for_status(status: str) -> str:
        return "auto_and_tool" if status == "active" else "maintenance_only"

    @staticmethod
    def _merge_review_status(existing: str, incoming: str) -> str:
        old = str(existing or "").strip().lower()
        new = str(incoming or "").strip().lower()
        if new in {"approved", "rejected", "pending_human", "revision_needed"}:
            return new
        if old in {"approved", "rejected", "pending_human", "revision_needed"}:
            return old
        return new or old or "pending"

    @staticmethod
    def _source_requires_review(source: str) -> bool:
        normalized = str(source or "").strip().lower()
        return normalized in {
            "learning_expression_pattern",
            "learning_expression_backfill",
            "legacy_expression_write",
        }

    @classmethod
    def _normalize_incoming_review_status(cls, review_status: str, *, source: str) -> str:
        normalized = str(review_status or "pending").strip().lower()
        if cls._source_requires_review(source) and normalized == "approved":
            return "pending"
        if normalized in {"approved", "pending", "pending_human", "revision_needed", "rejected", "stale"}:
            return normalized
        return "pending"

    @staticmethod
    def _json_samples(value: Any) -> str:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
            except Exception:
                value = [value]
        if not isinstance(value, list):
            value = []
        return json.dumps(list(dict.fromkeys([str(item).strip() for item in value if str(item).strip()]))[:12], ensure_ascii=False)

    @staticmethod
    def _sample_list(value: Any) -> list[str]:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    value = parsed
                else:
                    value = [value]
            except Exception:
                value = [value]
        if not isinstance(value, list):
            value = []
        return [str(item).strip() for item in value if str(item).strip()][:12]

    @staticmethod
    def _candidate_to_record(candidate: MemoryCandidate) -> ExpressionPatternRecord:
        metadata = dict(candidate.metadata or {})
        review_status = str(metadata.get("review_status") or candidate.status or "pending")
        content_samples = ExpressionPatternService._sample_list(metadata.get("content_samples", []))
        return ExpressionPatternRecord(
            id=str(candidate.id or ""),
            group_id=str(candidate.session_id or ""),
            situation=str(metadata.get("situation") or ""),
            expression=str(candidate.content or ""),
            style=str(metadata.get("style") or ""),
            content_list=json.dumps(content_samples, ensure_ascii=False),
            count=int(metadata.get("count") or 1),
            checked=review_status == "approved",
            rejected=review_status == "rejected",
            modified_by=str(metadata.get("modified_by") or ""),
            source=str(candidate.source or ""),
            shared_scope=str(metadata.get("shared_scope") or ""),
            think_level=int(metadata.get("think_level") or 0),
            review_status=review_status,
            review_reason=str(metadata.get("review_reason") or ""),
            review_suggestion=str(metadata.get("review_suggestion") or ""),
            last_review_time=float(metadata.get("last_review_time") or 0.0),
            weight=ExpressionPatternService._safe_float(metadata.get("weight"), 1.0),
            last_active_time=float(metadata.get("last_active_time") or candidate.last_access_time or candidate.updated_at or candidate.created_at or 0.0),
            create_time=float(candidate.created_at or 0.0),
            status=str(candidate.status or "review_pending"),
            visibility=str(candidate.visibility or ""),
            metadata=metadata,
        )

    async def get_pattern(self, pattern_id: str) -> ExpressionPatternRecord | None:
        candidate = await self.store.get_canonical(str(pattern_id or ""), include_inactive=True)
        if not candidate or candidate.kind != "expression_pattern":
            return None
        return self._candidate_to_record(candidate)

    async def list_patterns(
        self,
        group_id: str,
        *,
        limit: int = 20,
        only_checked: bool = False,
        include_rejected: bool = False,
        shared_scope: str | None = None,
        think_level: int | None = None,
        review_status: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[ExpressionPatternRecord]:
        rows = await self.store.list_candidates(
            session_id=str(group_id or ""),
            kinds=["expression_pattern"],
            statuses=statuses or ["active", "review_pending", "rejected", "stale"],
            limit=max(int(limit or 20) * 6, 60),
            include_inactive=True,
        )
        results: list[ExpressionPatternRecord] = []
        for candidate in rows:
            record = self._candidate_to_record(candidate)
            if only_checked and not record.checked:
                continue
            if not include_rejected and record.rejected:
                continue
            if shared_scope is not None:
                requested_scope = str(shared_scope or "").strip()
                # New records are speaker-scoped; legacy group-scoped records remain
                # readable as a compatibility fallback for the same chat only.
                allowed_scopes = {requested_scope, str(group_id or "").strip()}
                if record.shared_scope not in allowed_scopes:
                    continue
            if think_level is not None and int(record.think_level or 0) > int(think_level or 0):
                continue
            if review_status and str(record.review_status or "").strip().lower() != str(review_status).strip().lower():
                continue
            results.append(record)
        results.sort(
            key=lambda item: (
                float(item.weight or 1.0),
                int(item.count or 1),
                float(item.last_active_time or 0.0),
            ),
            reverse=True,
        )
        return results[: max(int(limit or 20), 1)]

    async def list_reviewable_patterns(self, group_id: str | None = None, limit: int = 20) -> list[ExpressionPatternRecord]:
        rows = await self.list_patterns(
            str(group_id or ""),
            limit=max(int(limit or 20), 1),
            only_checked=False,
            include_rejected=False,
            statuses=["review_pending"],
        )
        return [
            item
            for item in rows
            if str(item.review_status or "").strip().lower() in {"pending", "pending_human", "revision_needed"}
        ][: max(int(limit or 20), 1)]

    async def list_governance_groups(self, *, limit: int = 500) -> list[str]:
        rows = await self.store.list_candidates(
            kinds=["expression_pattern"],
            statuses=["active", "review_pending", "rejected", "stale"],
            limit=max(int(limit or 500), 1),
            include_inactive=True,
        )
        groups: list[str] = []
        for candidate in rows:
            metadata = dict(candidate.metadata or {})
            review_status = str(metadata.get("review_status") or "").strip().lower()
            if candidate.status not in {"active", "review_pending", "rejected", "stale"} and review_status not in {
                "approved",
                "pending",
                "pending_human",
                "revision_needed",
                "rejected",
            }:
                continue
            scope_id = str(candidate.session_id or metadata.get("shared_scope") or "GLOBAL").strip() or "GLOBAL"
            groups.append(scope_id)
        return list(dict.fromkeys(groups))

    async def update_review(
        self,
        pattern_id: str,
        *,
        checked: bool | None = None,
        rejected: bool | None = None,
        modified_by: str | None = None,
        review_status: str | None = None,
        review_reason: str | None = None,
        review_suggestion: str | None = None,
        weight_delta: float = 0.0,
        replacement_expression: str | None = None,
        apply_replacement: bool = False,
        situation: str | None = None,
        shared_scope: str | None = None,
        style: str | None = None,
    ) -> ExpressionPatternRecord | None:
        current = await self.get_pattern(pattern_id)
        if not current:
            return None
        metadata = dict(current.metadata or {})
        requested_situation = situation
        requested_shared_scope = shared_scope
        expression = str(current.expression or "")
        situation = str(current.situation or "")
        shared_scope = str(current.shared_scope or "")
        old_dedup_key = self.build_dedup_key(current.group_id, situation, expression, shared_scope)
        replacement_applied = False
        identity_changed = False
        if replacement_expression and apply_replacement:
            replacement = str(replacement_expression or "").strip()
            if replacement and replacement != expression:
                expression = replacement
                replacement_applied = True
        if requested_situation is not None:
            next_situation = str(requested_situation or "").strip()
            if next_situation and next_situation != situation:
                situation = next_situation
                metadata["situation"] = situation
                identity_changed = True
        if requested_shared_scope is not None:
            next_shared_scope = str(requested_shared_scope or "").strip()
            if next_shared_scope and next_shared_scope != shared_scope:
                shared_scope = next_shared_scope
                metadata["shared_scope"] = shared_scope
                identity_changed = True
        if style is not None:
            metadata["style"] = str(style or "").strip()
        if modified_by is not None:
            metadata["modified_by"] = str(modified_by or "")
        if review_reason is not None:
            metadata["review_reason"] = str(review_reason or "")
        if review_suggestion is not None:
            metadata["review_suggestion"] = str(review_suggestion or "")
        explicit_review_status = (
            str(review_status or "").strip().lower()
            if review_status is not None
            else None
        )
        checked_true = checked is not None and checked
        rejected_true = rejected is not None and rejected
        if explicit_review_status is not None:
            metadata["review_status"] = explicit_review_status
        elif checked_true and rejected_true:
            metadata["review_status"] = str(metadata.get("review_status") or "pending").strip().lower()
        elif checked_true:
            metadata["review_status"] = "approved"
        elif rejected_true:
            metadata["review_status"] = "rejected"
        metadata["last_review_time"] = time.time()
        metadata["weight"] = max(0.0, min(3.0, self._safe_float(metadata.get("weight"), self._safe_float(current.weight, 1.0)) + float(weight_delta or 0.0)))
        history = metadata.get("manual_revision_history")
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "action": str(review_status or ("replace" if replacement_applied else "update") or "update"),
                "modified_by": str(modified_by or "human:webui"),
                "modified_at": metadata["last_review_time"],
                "changes": {
                    "expression": expression,
                    "situation": situation,
                    "shared_scope": shared_scope,
                    "style": metadata.get("style", ""),
                    "review_reason": metadata.get("review_reason", ""),
                    "review_status": metadata.get("review_status", ""),
                    "weight": metadata.get("weight", 1.0),
                },
            }
        )
        metadata["manual_revision_history"] = history[-20:]
        next_review_status = str(metadata.get("review_status") or current.review_status or "pending").strip().lower()
        status = self._canonical_status(next_review_status)
        visibility = self._visibility_for_status(status)
        metadata["content_samples"] = self._sample_list(metadata.get("content_samples") or current.content_list)
        summary = str(metadata.get("summary") or expression[:240])
        if replacement_applied or identity_changed:
            new_dedup_key = self.build_dedup_key(current.group_id, situation, expression, shared_scope)
            conflict = await self.store.get_by_dedup_key(new_dedup_key, include_inactive=True)
            if conflict and str(conflict.id) != str(pattern_id):
                conflict_metadata = dict(conflict.metadata or {})
                metadata["content_samples"] = self._sample_list(
                    [
                        *self._sample_list(metadata.get("content_samples", [])),
                        *self._sample_list(conflict_metadata.get("content_samples", [])),
                    ]
                )
                metadata["count"] = int(metadata.get("count") or 0) + int(conflict_metadata.get("count") or 0)
                metadata["weight"] = max(
                    self._safe_float(metadata.get("weight"), 1.0),
                    self._safe_float(conflict_metadata.get("weight"), 1.0),
                )
            metadata["summary"] = expression
            updated = await self.store.replace_dedup_identity(
                str(pattern_id),
                old_dedup_key=old_dedup_key,
                new_dedup_key=new_dedup_key,
                content=expression,
                summary=expression,
                metadata=metadata,
                status=status,
                visibility=visibility,
            )
        else:
            updated = await self.store.update_memory(
                str(pattern_id),
                content=expression,
                summary=summary,
                metadata=metadata,
                status=status,
                visibility=visibility,
            )
        if not updated:
            return None
        return await self.get_pattern(pattern_id)

    async def adjust_weight(self, pattern_id: str, delta: float) -> ExpressionPatternRecord | None:
        current = await self.get_pattern(pattern_id)
        if not current:
            return None
        metadata = dict(current.metadata or {})
        metadata["weight"] = max(0.0, min(3.0, self._safe_float(metadata.get("weight"), self._safe_float(current.weight, 1.0)) + float(delta or 0.0)))
        metadata["last_active_time"] = time.time()
        changed = await self.store.update_memory(str(pattern_id), metadata=metadata)
        if not changed:
            return None
        return await self.get_pattern(pattern_id)

    async def adjust_weight_once(
        self,
        pattern_id: str,
        delta: float,
        *,
        operation_id: str,
    ) -> ExpressionPatternRecord | None:
        operation_key = str(operation_id or "").strip()
        if not operation_key:
            return await self.adjust_weight(pattern_id, delta)
        current = await self.get_pattern(pattern_id)
        if not current:
            return None
        metadata = dict(current.metadata or {})
        applied = metadata.get("applied_weight_operation_ids", [])
        if not isinstance(applied, list):
            applied = []
        applied_ids = [str(item) for item in applied if str(item or "").strip()]
        if operation_key in applied_ids:
            return current
        metadata["weight"] = max(
            0.0,
            min(
                3.0,
                self._safe_float(metadata.get("weight"), self._safe_float(current.weight, 1.0)) + float(delta or 0.0),
            ),
        )
        metadata["last_active_time"] = time.time()
        metadata["applied_weight_operation_ids"] = [*applied_ids, operation_key][-128:]
        changed = await self.store.update_memory(str(pattern_id), metadata=metadata)
        if not changed:
            return None
        return await self.get_pattern(pattern_id)

    @staticmethod
    def _safe_float(value, default: float) -> float:
        """Parse float without falsy-or-default trap (0.0 is valid)."""
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def write_pattern(self, group_id: str, payload: dict[str, Any], *, source: str = "learning_expression_pattern") -> str:
        expression = str(payload.get("expression") or "").strip()
        situation = str(payload.get("situation") or "").strip()
        if not expression or not situation:
            return ""
        shared_scope = str(payload.get("shared_scope") or group_id or "").strip()
        dedup_key = self.build_dedup_key(group_id, situation, expression, shared_scope)
        existing = await self.store.get_by_dedup_key(dedup_key, include_inactive=True)
        resolver = getattr(self.store, "resolve_dedup_key", None)
        resolved_dedup_key = await resolver(dedup_key) if callable(resolver) else dedup_key
        if existing and resolved_dedup_key != dedup_key:
            expression = str(existing.content or expression)
            dedup_key = resolved_dedup_key
        incoming_samples = self._sample_list(payload.get("content_samples", []))
        now = time.time()
        existing_metadata = dict(existing.metadata or {}) if existing else {}
        incoming_evidence_ids = [
            str(item)
            for item in (payload.get("evidence_message_ids") or [])
            if str(item or "").strip()
        ]
        existing_evidence_ids = [
            str(item)
            for item in (existing_metadata.get("evidence_message_ids") or [])
            if str(item or "").strip()
        ]
        existing_evidence_set = set(existing_evidence_ids)
        unseen_evidence_ids = [item for item in incoming_evidence_ids if item not in existing_evidence_set]
        mining_batch_id = str(payload.get("mining_batch_id") or "").strip()
        applied_batch_ids = [
            str(item)
            for item in (existing_metadata.get("applied_mining_batch_ids") or [])
            if str(item or "").strip()
        ]
        if existing and mining_batch_id and mining_batch_id in applied_batch_ids and not unseen_evidence_ids:
            return str(existing.id)
        incoming_review_status = self._normalize_incoming_review_status(payload.get("review_status", "pending"), source=source)
        review_status = self._merge_review_status(existing_metadata.get("review_status", ""), incoming_review_status)
        merged_samples = self._sample_list([*self._sample_list(existing_metadata.get("content_samples", [])), *incoming_samples])
        incoming_count = max(int(payload.get("count") or 1), 1)
        if existing and incoming_evidence_ids:
            count_increment = len(unseen_evidence_ids)
        elif existing and mining_batch_id in applied_batch_ids:
            count_increment = 0
        else:
            count_increment = incoming_count
        merged_count = int(existing_metadata.get("count") or 0) + count_increment
        merged_weight = max(
            0.0,
            min(
                3.0,
                max(
                    self._safe_float(existing_metadata.get("weight"), 0.0),
                    max(self._safe_float(payload.get("weight"), 1.0), 0.1),
                ),
            ),
        )
        summary = str(payload.get("summary") or expression).strip()
        confidence = float(payload.get("confidence") or payload.get("activation_score") or existing_metadata.get("confidence") or 0.6)
        importance = max(0.2, min(1.0, float(payload.get("activation_score") or confidence or 0.6)))
        status = self._canonical_status(review_status)
        metadata = {
            **existing_metadata,
            "situation": situation,
            "style": str(payload.get("style") or existing_metadata.get("style") or "").strip(),
            "habit_type": str(payload.get("habit_type") or existing_metadata.get("habit_type") or "sentence_pattern").strip(),
            "content_kind": str(payload.get("content_kind") or existing_metadata.get("content_kind") or "expression").strip(),
            "normalized_pattern": str(payload.get("normalized_pattern") or existing_metadata.get("normalized_pattern") or "").strip(),
            "speaker_id": str(payload.get("speaker_id") or existing_metadata.get("speaker_id") or "").strip(),
            "speaker_name": str(payload.get("speaker_name") or existing_metadata.get("speaker_name") or "").strip(),
            "scope_kind": str(payload.get("scope_kind") or existing_metadata.get("scope_kind") or "group").strip(),
            "distinct_turn_count": max(
                int(existing_metadata.get("distinct_turn_count") or 0),
                int(payload.get("distinct_turn_count") or len(incoming_evidence_ids) or 0),
            ),
            "distinct_day_count": max(
                int(existing_metadata.get("distinct_day_count") or 0),
                int(payload.get("distinct_day_count") or 0),
            ),
            "content_samples": merged_samples,
            "shared_scope": shared_scope,
            "think_level": int(payload.get("think_level") or existing_metadata.get("think_level") or 0),
            "review_status": review_status,
            "review_reason": str(payload.get("review_reason") or existing_metadata.get("review_reason") or "").strip(),
            "review_suggestion": str(payload.get("review_suggestion") or existing_metadata.get("review_suggestion") or "").strip(),
            "weight": merged_weight,
            "count": merged_count,
            "last_active_time": now,
            "confidence": confidence,
            "summary": summary,
            "candidate_id": str(payload.get("candidate_id") or existing_metadata.get("candidate_id") or ""),
            "evidence_message_ids": list(dict.fromkeys([*existing_evidence_ids, *incoming_evidence_ids]))[-256:],
            "applied_mining_batch_ids": list(
                dict.fromkeys([*applied_batch_ids, *([mining_batch_id] if mining_batch_id else [])])
            )[-128:],
        }
        if payload.get("legacy_pattern_id"):
            metadata["legacy_pattern_id"] = payload.get("legacy_pattern_id")
        memory_id = await self.write_service.write(
            MemoryWriteRequest(
                source=source,
                kind="expression_pattern",
                session_id=str(group_id or ""),
                content=expression,
                summary=summary,
                importance=importance,
                confidence=max(0.0, min(confidence, 1.0)),
                metadata=metadata,
                dedup_key=dedup_key,
                source_ref=str(payload.get("source_ref") or f"{source}:{dedup_key}"),
                visibility=self._visibility_for_status(status),
                status=status,
            )
        )
        if memory_id:
            return str(memory_id)
        stored = await self.store.get_by_dedup_key(dedup_key, include_inactive=True)
        return str(stored.id) if stored else ""

    async def render_active_patterns(self, group_id: str, *, limit: int = 5) -> str:
        patterns = await self.list_patterns(
            group_id,
            limit=limit,
            only_checked=True,
            include_rejected=False,
            review_status="approved",
            statuses=["active"],
        )
        if not patterns:
            return "暂无特殊语言风格记录。"
        return "\n".join(
            f"- 当「{pattern.situation}」时 -> 习惯使用表达/风格：「{pattern.expression}」"
            for pattern in patterns
        )


__all__ = ["ExpressionPatternRecord", "ExpressionPatternService"]
