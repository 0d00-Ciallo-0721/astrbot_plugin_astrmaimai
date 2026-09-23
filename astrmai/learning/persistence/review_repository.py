from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from ...infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from ..review.contracts import (
    ReviewDecision,
    ReviewQuorumResult,
    is_reviewer_model_identity,
    sanitize_diagnostics,
)


_REVIEWABLE_CANDIDATE_STATUSES = frozenset({"enriched", "review_pending", "human_review_pending"})
_RETRY_ATTEMPT_SEPARATOR = ":retry:"
_STAGE07_SCHEMA_VERSION = 169


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewAttempt:
    attempt_id: str
    candidate_id: str
    candidate_revision: int
    review_work_attempt: int
    reviewer_id: str
    reviewer_kind: str
    reviewer_attempt_id: str
    owner: str
    lease_token: str
    lease_until: float
    status: str
    retry_at: float
    started_at: float
    finished_at: float | None
    failure_stage: str
    failure_kind: str
    retryable: bool
    diagnostics: dict[str, Any]
    result_digest: str
    revision: int
    provider_request_started: bool
    provider_started_at: float


@dataclass(frozen=True, slots=True)
class ReviewMutation:
    applied: bool
    conflict: bool
    idempotent: bool = False
    failure_kind: str = ""
    attempt: ReviewAttempt | None = None
    decision_id: str = ""


@dataclass(frozen=True, slots=True)
class ReviewRevisionResult:
    applied: bool
    conflict: bool
    candidate_id: str
    previous_revision: int
    current_revision: int
    previous_digest: str = ""
    current_digest: str = ""
    failure_kind: str = ""


class LearningReviewRepository:
    """Durable review lease and immutable decision boundary."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[Any]:
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def schema_ready(self) -> bool:
        required = {
            "learning_review_attempt", "learning_review_decision", "learning_admission",
            "ux_learning_review_active_claim", "ix_learning_review_decision_candidate",
            "ix_learning_admission_blocked",
            "trg_learning_review_decision_no_update",
            "trg_learning_review_decision_no_delete",
        }
        required_columns = {
            "learning_review_attempt": {
                "attempt_id", "candidate_id", "candidate_revision", "review_work_attempt",
                "reviewer_id", "reviewer_kind", "reviewer_attempt_id", "owner",
                "lease_token", "lease_until", "status", "retry_at", "result_digest",
                "revision", "provider_request_started", "provider_started_at",
            },
            "learning_review_decision": {
                "decision_id", "candidate_id", "candidate_revision", "decision",
                "reviewer_id", "reviewer_attempt_id", "model_identity",
                "source_evidence_ids_json", "source_example_ids_json",
                "model_example_ids_json", "source_decision_ids_json",
                "order_invariant",
            },
            "learning_admission": {
                "candidate_id", "candidate_revision", "review_decision_ids_json",
                "admission_revision", "pre_index_eligible", "post_publish_eligible",
                "index_blocked", "provenance_digest", "publish_proof_digest", "revision",
            },
        }
        required_table_sql = {
            "learning_review_attempt": (
                "check(statusin('running','completed','retry_wait','human_review_pending','blocked','quarantined','cancelled','expired'))",
                "unique(candidate_id,candidate_revision,review_work_attempt)",
                "unique(candidate_id,candidate_revision,reviewer_id,reviewer_attempt_id)",
            ),
            "learning_review_decision": (
                "check(expected_revision=candidate_revision)",
                "check(order_invariantin(0,1))",
                "unique(candidate_id,candidate_revision,reviewer_id,reviewer_attempt_id)",
            ),
            "learning_admission": (
                "primarykey(candidate_id,candidate_revision)",
                "check(pre_index_eligiblein(0,1))",
                "check(post_publish_eligiblein(0,1))",
            ),
        }
        async with self._db() as db:
            cursor = await db.execute("PRAGMA user_version")
            version_row = await cursor.fetchone()
            await cursor.close()
            if version_row is None or int(version_row[0]) < _STAGE07_SCHEMA_VERSION:
                return False
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
            )
            found = {str(row[0]) for row in await cursor.fetchall()}
            await cursor.close()
            if not required.issubset(found):
                return False
            for table, expected in required_columns.items():
                cursor = await db.execute(f"PRAGMA table_info({table})")
                columns = {str(row[1]) for row in await cursor.fetchall()}
                await cursor.close()
                if not expected.issubset(columns):
                    return False
                cursor = await db.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
                    (table,),
                )
                sql_row = await cursor.fetchone()
                await cursor.close()
                table_sql = str(sql_row[0] if sql_row else "").lower().replace(" ", "").replace("\n", "")
                if any(fragment not in table_sql for fragment in required_table_sql[table]):
                    return False
            cursor = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_learning_review_active_claim'"
            )
            index_row = await cursor.fetchone()
            await cursor.close()
            index_sql = str(index_row[0] if index_row else "").lower().replace(" ", "")
            if "uniqueindex" not in index_sql or "wherestatus='running'" not in index_sql:
                return False
            cursor = await db.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (?, ?)",
                ("trg_learning_review_decision_no_update", "trg_learning_review_decision_no_delete"),
            )
            triggers = {str(name): str(sql or "").lower() for name, sql in await cursor.fetchall()}
            await cursor.close()
            if len(triggers) != 2 or any("raise(abort" not in sql.replace(" ", "") for sql in triggers.values()):
                return False
        return True

    @staticmethod
    def _evidence_identity(row: Sequence[Any]) -> str:
        source_row_id, event_id, platform_message_id = row[0], row[1], row[2]
        event = str(event_id or "").strip()
        if event and not event.lower().startswith(("fallback_", "evt_")):
            return f"event_id:{event}"
        platform = str(platform_message_id or "").strip()
        if platform:
            return f"platform_message_id:{platform}"
        if type(source_row_id) is int and source_row_id > 0:
            return f"row:{source_row_id}"
        return ""

    async def _valid_source_evidence_ids(self, db: Any, candidate_id: str) -> set[str]:
        cursor = await db.execute(
            """SELECT source_row_id, event_id, platform_message_id, eligible, is_generated
               FROM learning_candidate_evidence WHERE candidate_id = ?""",
            (candidate_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            identity
            for row in rows
            if bool(row[3]) and not bool(row[4])
            for identity in (self._evidence_identity(row),)
            if identity
        }

    @staticmethod
    def _attempt(row: Sequence[Any] | None) -> ReviewAttempt | None:
        if row is None:
            return None
        try:
            diagnostics = json.loads(str(row[17] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            diagnostics = {"decode_error": True}
        return ReviewAttempt(
            attempt_id=str(row[0]), candidate_id=str(row[1]),
            candidate_revision=int(row[2]), review_work_attempt=int(row[3]),
            reviewer_id=str(row[4]), reviewer_kind=str(row[5]),
            reviewer_attempt_id=str(row[6]), owner=str(row[7]),
            lease_token=str(row[8]), lease_until=float(row[9]), status=str(row[10]),
            retry_at=float(row[11]), started_at=float(row[12]),
            finished_at=None if row[13] is None else float(row[13]),
            failure_stage=str(row[14]), failure_kind=str(row[15]),
            retryable=bool(row[16]), diagnostics=dict(diagnostics),
            result_digest=str(row[18]), revision=int(row[19]),
            provider_request_started=bool(row[20]), provider_started_at=float(row[21]),
        )

    _ATTEMPT_COLUMNS = """
        attempt_id, candidate_id, candidate_revision, review_work_attempt,
        reviewer_id, reviewer_kind, reviewer_attempt_id, owner, lease_token,
        lease_until, status, retry_at, started_at, finished_at, failure_stage,
        failure_kind, retryable, diagnostics_json, result_digest, revision,
        provider_request_started, provider_started_at
    """

    async def get_attempt(self, attempt_id: str) -> ReviewAttempt | None:
        async with self._db() as db:
            cursor = await db.execute(
                f"SELECT {self._ATTEMPT_COLUMNS} FROM learning_review_attempt WHERE attempt_id = ?",
                (str(attempt_id),),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._attempt(row)

    async def claim(
        self, *, candidate_id: str, expected_revision: int, owner: str,
        reviewer_id: str, reviewer_kind: str, reviewer_attempt_id: str,
        now: float, lease_seconds: float = 60.0,
    ) -> ReviewMutation:
        values = [candidate_id, owner, reviewer_id, reviewer_kind, reviewer_attempt_id]
        if any(not str(value or "").strip() for value in values):
            return ReviewMutation(False, True, failure_kind="invalid_claim_identity")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            return ReviewMutation(False, True, failure_kind="invalid_revision")
        if lease_seconds <= 0:
            return ReviewMutation(False, True, failure_kind="invalid_lease")
        if _RETRY_ATTEMPT_SEPARATOR in reviewer_attempt_id:
            return ReviewMutation(False, True, failure_kind="invalid_reviewer_attempt_root")
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cursor = await db.execute(
                    "SELECT revision, status FROM learning_candidate WHERE candidate_id = ?",
                    (candidate_id,),
                )
                candidate = await cursor.fetchone()
                await cursor.close()
                if candidate is None:
                    await db.rollback()
                    return ReviewMutation(False, True, failure_kind="candidate_missing")
                if int(candidate[0]) != expected_revision:
                    await db.rollback()
                    return ReviewMutation(False, True, failure_kind="candidate_revision_conflict")
                if str(candidate[1]) not in _REVIEWABLE_CANDIDATE_STATUSES:
                    await db.rollback()
                    return ReviewMutation(False, True, failure_kind="candidate_status_not_reviewable")
                cursor = await db.execute(
                    """SELECT 1 FROM learning_review_decision
                       WHERE candidate_id = ? AND candidate_revision = ?
                         AND reviewer_kind = 'rule' AND pair_order = 'quorum'
                       LIMIT 1""",
                    (candidate_id, expected_revision),
                )
                quorum_finalized = await cursor.fetchone()
                await cursor.close()
                if quorum_finalized is not None:
                    await db.rollback()
                    return ReviewMutation(False, True, failure_kind="review_quorum_finalized")
                if str(candidate[1]) == "enriched":
                    cursor = await db.execute(
                        """UPDATE learning_candidate SET status = 'review_pending', updated_at = ?
                           WHERE candidate_id = ? AND revision = ? AND status = 'enriched'""",
                        (now, candidate_id, expected_revision),
                    )
                    if cursor.rowcount != 1:
                        await cursor.close()
                        await db.rollback()
                        return ReviewMutation(False, True, failure_kind="candidate_status_conflict")
                    await cursor.close()
                cursor = await db.execute(
                    f"SELECT {self._ATTEMPT_COLUMNS} FROM learning_review_attempt "
                    "WHERE candidate_id = ? AND candidate_revision = ? AND reviewer_id = ? "
                    "ORDER BY review_work_attempt DESC",
                    (candidate_id, expected_revision, reviewer_id),
                )
                reviewer_attempts = [
                    item for item in (self._attempt(row) for row in await cursor.fetchall())
                    if item is not None and (
                        item.reviewer_attempt_id == reviewer_attempt_id
                        or item.reviewer_attempt_id.startswith(
                            reviewer_attempt_id + _RETRY_ATTEMPT_SEPARATOR
                        )
                    )
                ]
                await cursor.close()
                existing = reviewer_attempts[0] if reviewer_attempts else None
                if existing is not None:
                    same_claim = (
                        existing.status == "running"
                        and existing.owner == owner
                        and existing.reviewer_id == reviewer_id
                        and existing.reviewer_kind == reviewer_kind
                        and existing.candidate_revision == expected_revision
                        and existing.lease_until >= now
                    )
                    if same_claim:
                        await db.rollback()
                        return ReviewMutation(False, False, True, attempt=existing)
                    retry_due = (
                        existing.status in {"retry_wait", "expired"}
                        and existing.retryable
                        and existing.retry_at <= now
                    )
                    if not retry_due:
                        await db.rollback()
                        failure_kind = (
                            "review_retry_not_due"
                            if existing.status in {"retry_wait", "expired"} and existing.retryable
                            else "claim_replay_conflict"
                        )
                        return ReviewMutation(False, True, failure_kind=failure_kind, attempt=existing)
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(review_work_attempt), 0) FROM learning_review_attempt "
                    "WHERE candidate_id = ? AND candidate_revision = ?",
                    (candidate_id, expected_revision),
                )
                row = await cursor.fetchone()
                await cursor.close()
                work_attempt = int(row[0] if row else 0) + 1
                effective_reviewer_attempt_id = (
                    reviewer_attempt_id
                    if existing is None
                    else f"{reviewer_attempt_id}{_RETRY_ATTEMPT_SEPARATOR}{work_attempt}"
                )
                attempt_key = (
                    f"{candidate_id}:{expected_revision}:{reviewer_id}:"
                    f"{effective_reviewer_attempt_id}:{work_attempt}"
                )
                attempt_id = "review:" + hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()
                lease_token = secrets.token_urlsafe(32)
                await db.execute(
                    """INSERT INTO learning_review_attempt(
                        attempt_id, candidate_id, candidate_revision, review_work_attempt,
                        reviewer_id, reviewer_kind, reviewer_attempt_id, owner, lease_token,
                        lease_until, status, retry_at, started_at, created_at, updated_at, revision
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', 0, ?, ?, ?, 1)""",
                    (
                        attempt_id, candidate_id, expected_revision, work_attempt,
                        reviewer_id, reviewer_kind, effective_reviewer_attempt_id, owner, lease_token,
                        now + lease_seconds, now, now, now,
                    ),
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                if "ux_learning_review_active_claim" in str(exc) or "UNIQUE constraint failed" in str(exc):
                    return ReviewMutation(False, True, failure_kind="active_claim_conflict")
                raise
        return ReviewMutation(True, False, attempt=await self.get_attempt(attempt_id))

    async def renew(
        self, *, attempt_id: str, candidate_id: str, expected_candidate_revision: int,
        owner: str, lease_token: str, expected_revision: int, now: float,
        lease_seconds: float = 60.0,
    ) -> ReviewMutation:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """UPDATE learning_review_attempt
                   SET lease_until = ?, revision = revision + 1, updated_at = ?
                   WHERE attempt_id = ? AND candidate_id = ? AND candidate_revision = ?
                     AND owner = ? AND lease_token = ? AND revision = ?
                     AND status = 'running' AND lease_until >= ?
                      AND EXISTS(SELECT 1 FROM learning_candidate c
                                 WHERE c.candidate_id = learning_review_attempt.candidate_id
                                   AND c.revision = learning_review_attempt.candidate_revision
                                   AND c.status IN ('review_pending','human_review_pending'))""",
                (
                    now + lease_seconds, now, attempt_id, candidate_id,
                    expected_candidate_revision, owner, lease_token, expected_revision, now,
                ),
            )
            applied = cursor.rowcount == 1
            await cursor.close()
            if applied:
                await db.commit()
            else:
                await db.rollback()
        return ReviewMutation(applied, not applied, failure_kind="" if applied else "renew_cas_conflict", attempt=await self.get_attempt(attempt_id))

    async def mark_provider_started(
        self, *, attempt_id: str, candidate_id: str, candidate_revision: int,
        owner: str, lease_token: str, expected_revision: int, started_at: float,
    ) -> ReviewMutation:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"SELECT {self._ATTEMPT_COLUMNS} FROM learning_review_attempt WHERE attempt_id = ?",
                (attempt_id,),
            )
            existing = self._attempt(await cursor.fetchone())
            await cursor.close()
            if existing and existing.provider_request_started:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="provider_request_already_started", attempt=existing)
            cursor = await db.execute(
                """UPDATE learning_review_attempt
                   SET provider_request_started = 1, provider_started_at = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE attempt_id = ? AND candidate_id = ? AND candidate_revision = ?
                     AND owner = ? AND lease_token = ? AND revision = ?
                     AND status = 'running' AND lease_until >= ?
                      AND EXISTS(SELECT 1 FROM learning_candidate c
                                 WHERE c.candidate_id = learning_review_attempt.candidate_id
                                   AND c.revision = learning_review_attempt.candidate_revision
                                   AND c.status IN ('review_pending','human_review_pending'))""",
                (
                    started_at, started_at, attempt_id, candidate_id, candidate_revision,
                    owner, lease_token, expected_revision, started_at,
                ),
            )
            applied = cursor.rowcount == 1
            await cursor.close()
            if applied:
                await db.commit()
            else:
                await db.rollback()
        return ReviewMutation(
            applied, not applied,
            failure_kind="" if applied else "provider_start_cas_conflict",
            attempt=await self.get_attempt(attempt_id),
        )

    @staticmethod
    def _decision_payload(decision: ReviewDecision) -> dict[str, Any]:
        return {
            "decision_id": decision.decision_id, "candidate_id": decision.candidate_id,
            "candidate_revision": decision.candidate_revision, "decision": decision.decision,
            "reason": decision.reason, "reviewer_id": decision.reviewer_id,
            "reviewer_kind": decision.reviewer_kind,
            "reviewer_attempt_id": decision.reviewer_attempt_id,
            "rubric_version": decision.rubric_version,
            "prompt_version": decision.prompt_version,
            "model_identity": decision.model_identity,
            "source_evidence_ids": decision.source_evidence_ids,
            "source_example_ids": decision.source_example_ids,
            "model_example_ids": decision.model_example_ids,
            "source_decision_ids": decision.source_decision_ids,
            "confidence": decision.confidence, "expected_revision": decision.expected_revision,
            "pair_order": decision.pair_order, "diagnostics": dict(decision.diagnostics),
            "order_invariant": decision.order_invariant,
            "created_at": decision.created_at,
        }

    async def settle(
        self, *, attempt_id: str, owner: str, lease_token: str,
        expected_attempt_revision: int, finished_at: float, decision: ReviewDecision,
    ) -> ReviewMutation:
        decision.__post_init__()
        payload = self._decision_payload(decision)
        digest = _digest(payload)
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"SELECT {self._ATTEMPT_COLUMNS} FROM learning_review_attempt WHERE attempt_id = ?",
                (attempt_id,),
            )
            attempt = self._attempt(await cursor.fetchone())
            await cursor.close()
            if attempt is None:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="attempt_missing")
            identity_matches = (
                attempt.candidate_id == decision.candidate_id
                and attempt.candidate_revision == decision.candidate_revision
                and attempt.reviewer_id == decision.reviewer_id
                and attempt.reviewer_kind == decision.reviewer_kind
                and attempt.reviewer_attempt_id == decision.reviewer_attempt_id
                and attempt.owner == owner and attempt.lease_token == lease_token
            )
            cursor = await db.execute(
                "SELECT revision, status FROM learning_candidate WHERE candidate_id = ?",
                (decision.candidate_id,),
            )
            candidate = await cursor.fetchone()
            await cursor.close()
            if candidate is None:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="candidate_missing", attempt=attempt)
            if int(candidate[0]) != decision.candidate_revision:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="candidate_revision_conflict", attempt=attempt)
            if str(candidate[1]) not in _REVIEWABLE_CANDIDATE_STATUSES:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="candidate_status_not_reviewable", attempt=attempt)
            valid_evidence_ids = await self._valid_source_evidence_ids(db, decision.candidate_id)
            if set(decision.source_evidence_ids) - valid_evidence_ids:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="review_evidence_missing", attempt=attempt)
            if attempt.result_digest:
                idempotent = (
                    identity_matches and attempt.result_digest == digest
                    and attempt.revision == expected_attempt_revision + 1
                    and attempt.finished_at == finished_at
                )
                await db.rollback()
                return ReviewMutation(
                    False, not idempotent, idempotent,
                    "" if idempotent else "settlement_replay_conflict",
                    attempt, decision.decision_id if idempotent else "",
                )
            cursor = await db.execute(
                """SELECT 1 FROM learning_review_decision
                   WHERE candidate_id = ? AND candidate_revision = ?
                     AND reviewer_kind = 'rule' AND pair_order = 'quorum'
                   LIMIT 1""",
                (decision.candidate_id, decision.candidate_revision),
            )
            quorum_finalized = await cursor.fetchone()
            await cursor.close()
            if quorum_finalized is not None:
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="review_quorum_finalized", attempt=attempt)
            if (
                not identity_matches or attempt.status != "running"
                or attempt.revision != expected_attempt_revision
                or attempt.lease_until < finished_at
            ):
                await db.rollback()
                return ReviewMutation(False, True, failure_kind="settlement_cas_conflict", attempt=attempt)
            try:
                await db.execute(
                    """INSERT INTO learning_review_decision(
                        decision_id, candidate_id, candidate_revision, decision, reason,
                        reviewer_id, reviewer_kind, reviewer_attempt_id, rubric_version,
                        prompt_version, model_identity, source_evidence_ids_json,
                        source_example_ids_json, model_example_ids_json, confidence,
                        source_decision_ids_json, expected_revision, pair_order,
                        diagnostics_json, created_at, order_invariant
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        decision.decision_id, decision.candidate_id, decision.candidate_revision,
                        decision.decision, decision.reason, decision.reviewer_id,
                        decision.reviewer_kind, decision.reviewer_attempt_id,
                        decision.rubric_version, decision.prompt_version, decision.model_identity,
                        _json(decision.source_evidence_ids), _json(decision.source_example_ids),
                        _json(decision.model_example_ids), decision.confidence,
                        _json(decision.source_decision_ids),
                        decision.expected_revision, decision.pair_order,
                        _json(dict(decision.diagnostics)), decision.created_at,
                        int(decision.order_invariant),
                    ),
                )
                cursor = await db.execute(
                    """UPDATE learning_review_attempt
                       SET status = 'completed', finished_at = ?, result_digest = ?,
                           diagnostics_json = ?, revision = revision + 1, updated_at = ?
                       WHERE attempt_id = ? AND revision = ? AND status = 'running'
                         AND owner = ? AND lease_token = ? AND lease_until >= ?""",
                    (
                        finished_at, digest, _json(dict(decision.diagnostics)), finished_at,
                        attempt_id, expected_attempt_revision, owner, lease_token, finished_at,
                    ),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    await db.rollback()
                    return ReviewMutation(False, True, failure_kind="settlement_cas_conflict")
                await cursor.close()
                await db.commit()
            except Exception as exc:
                await db.rollback()
                if "UNIQUE constraint failed" in str(exc):
                    return ReviewMutation(False, True, failure_kind="decision_identity_conflict")
                raise
        return ReviewMutation(True, False, attempt=await self.get_attempt(attempt_id), decision_id=decision.decision_id)

    async def abort(
        self, *, attempt_id: str, candidate_id: str, candidate_revision: int,
        owner: str, lease_token: str,
        expected_revision: int, now: float, status: str = "retry_wait",
        failure_stage: str = "review", failure_kind: str = "cancelled",
        retryable: bool = True, retry_at: float = 0.0,
        diagnostics: dict[str, Any] | None = None,
    ) -> ReviewMutation:
        if status not in {"retry_wait", "blocked", "quarantined", "cancelled", "expired", "human_review_pending"}:
            return ReviewMutation(False, True, failure_kind="invalid_abort_status")
        safe_diagnostics = dict(sanitize_diagnostics(diagnostics))
        async with self._db() as db:
            cursor = await db.execute(
                """UPDATE learning_review_attempt SET status = ?, retry_at = ?, finished_at = ?,
                   failure_stage = ?, failure_kind = ?, retryable = ?, diagnostics_json = ?,
                   revision = revision + 1, updated_at = ?
                   WHERE attempt_id = ? AND candidate_id = ? AND candidate_revision = ?
                     AND owner = ? AND lease_token = ?
                     AND revision = ? AND status = 'running' AND lease_until >= ?
                     AND EXISTS(SELECT 1 FROM learning_candidate c
                                WHERE c.candidate_id = learning_review_attempt.candidate_id
                                  AND c.revision = learning_review_attempt.candidate_revision)""",
                (
                    status, retry_at, now, failure_stage, failure_kind, int(retryable),
                    _json(safe_diagnostics), now, attempt_id, candidate_id,
                    candidate_revision, owner, lease_token, expected_revision, now,
                ),
            )
            applied = cursor.rowcount == 1
            await cursor.close()
            if applied:
                await db.commit()
            else:
                await db.rollback()
        return ReviewMutation(applied, not applied, failure_kind="" if applied else "abort_cas_conflict", attempt=await self.get_attempt(attempt_id))

    async def recover_expired(self, *, now: float) -> int:
        async with self._db() as db:
            cursor = await db.execute(
                """UPDATE learning_review_attempt
                   SET status = 'expired', finished_at = ?, failure_stage = 'review_lease',
                       failure_kind = 'lease_expired', retryable = 1, retry_at = ?,
                       revision = revision + 1, updated_at = ?
                   WHERE status = 'running' AND (
                       lease_until < ? OR NOT EXISTS(
                           SELECT 1 FROM learning_candidate c
                           WHERE c.candidate_id = learning_review_attempt.candidate_id
                             AND c.revision = learning_review_attempt.candidate_revision
                       )
                   )""",
                (now, now, now, now),
            )
            count = int(cursor.rowcount)
            await cursor.close()
            await db.commit()
        return count

    async def settle_running_for_shutdown(self, *, now: float) -> int:
        async with self._db() as db:
            cursor = await db.execute(
                """UPDATE learning_review_attempt
                   SET status = 'retry_wait', retry_at = ?, finished_at = ?,
                       failure_stage = 'review_shutdown', failure_kind = 'shutdown',
                       retryable = 1, lease_token = '', lease_until = 0,
                       revision = revision + 1, updated_at = ?
                   WHERE status = 'running'""",
                (now, now, now),
            )
            count = int(cursor.rowcount)
            await cursor.close()
            await db.commit()
        return count

    async def create_revision(
        self, *, candidate_id: str, expected_revision: int,
        edited_payload: dict[str, Any], source_evidence_ids: tuple[str, ...],
        editor_id: str, reason: str, now: float,
    ) -> ReviewRevisionResult:
        if not candidate_id or not editor_id or not reason or not source_evidence_ids:
            return ReviewRevisionResult(False, True, candidate_id, expected_revision, expected_revision, failure_kind="revision_contract_invalid")
        if any(
            not str(value).startswith(("row:", "event_id:", "platform_message_id:"))
            or "fallback" in str(value).lower()
            or "synthetic" in str(value).lower()
            or str(value).lower().startswith("event_id:evt_")
            for value in source_evidence_ids
        ):
            return ReviewRevisionResult(False, True, candidate_id, expected_revision, expected_revision, failure_kind="revision_evidence_invalid")
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT revision, source_payload_json FROM learning_candidate WHERE candidate_id = ?",
                (candidate_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or int(row[0]) != expected_revision:
                await db.rollback()
                return ReviewRevisionResult(False, True, candidate_id, expected_revision, int(row[0]) if row else expected_revision, failure_kind="candidate_revision_conflict")
            try:
                previous_payload = json.loads(str(row[1] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                previous_payload = {}
            if not isinstance(previous_payload, dict):
                previous_payload = {}
            previous_digest = _digest(previous_payload)
            current_digest = _digest(edited_payload)
            history = previous_payload.get("review_revision_history")
            history = list(history) if isinstance(history, list) else []
            history.append({
                "previous_revision": expected_revision,
                "current_revision": expected_revision + 1,
                "previous_digest": previous_digest,
                "current_digest": current_digest,
                "source_evidence_ids": list(source_evidence_ids),
                "editor_id": editor_id,
                "reason": reason,
                "created_at": now,
            })
            next_payload = dict(previous_payload)
            next_payload["review_edit"] = dict(edited_payload)
            next_payload["review_revision_history"] = history[-32:]
            cursor = await db.execute(
                """UPDATE learning_candidate
                   SET revision = revision + 1, status = 'review_pending',
                       lease_owner = '', lease_token = '', lease_until = 0,
                       source_payload_json = ?, updated_at = ?
                   WHERE candidate_id = ? AND revision = ?""",
                (_json(next_payload), now, candidate_id, expected_revision),
            )
            if cursor.rowcount != 1:
                await cursor.close()
                await db.rollback()
                return ReviewRevisionResult(False, True, candidate_id, expected_revision, expected_revision, failure_kind="candidate_revision_conflict")
            await cursor.close()
            await db.execute(
                """UPDATE learning_review_attempt
                   SET status = 'expired', finished_at = ?, failure_stage = 'review_revision',
                       failure_kind = 'candidate_revised', retryable = 0,
                       revision = revision + 1, updated_at = ?
                   WHERE candidate_id = ? AND candidate_revision = ? AND status = 'running'""",
                (now, now, candidate_id, expected_revision),
            )
            await db.execute(
                """UPDATE learning_admission
                   SET post_publish_eligible = 0, index_blocked = 1,
                       blocked_reason = 'candidate_revised',
                       blocked_stage = 'candidate_revision',
                       blocked_kind = 'candidate_revision_conflict',
                       revision = revision + 1, updated_at = ?
                   WHERE candidate_id = ? AND candidate_revision = ?""",
                (now, candidate_id, expected_revision),
            )
            await db.commit()
        return ReviewRevisionResult(
            True, False, candidate_id, expected_revision, expected_revision + 1,
            previous_digest, current_digest,
        )

    _DECISION_COLUMNS = """
        decision_id, candidate_id, candidate_revision, decision, reason,
        reviewer_id, reviewer_kind, reviewer_attempt_id, rubric_version,
        prompt_version, model_identity, source_evidence_ids_json,
        source_example_ids_json, model_example_ids_json, source_decision_ids_json,
        confidence, expected_revision, pair_order, diagnostics_json, created_at,
        order_invariant
    """

    @staticmethod
    def _decision(row: Sequence[Any]) -> ReviewDecision:
        return ReviewDecision(
            decision_id=str(row[0]), candidate_id=str(row[1]), candidate_revision=int(row[2]),
            decision=str(row[3]), reason=str(row[4]), reviewer_id=str(row[5]),
            reviewer_kind=str(row[6]), reviewer_attempt_id=str(row[7]),
            rubric_version=str(row[8]), prompt_version=str(row[9]), model_identity=str(row[10]),
            source_evidence_ids=tuple(json.loads(str(row[11]))),
            source_example_ids=tuple(json.loads(str(row[12]))),
            model_example_ids=tuple(json.loads(str(row[13]))),
            source_decision_ids=tuple(json.loads(str(row[14]))), confidence=row[15],
            expected_revision=int(row[16]), pair_order=str(row[17]),
            diagnostics=json.loads(str(row[18] or "{}")), created_at=float(row[19]),
            order_invariant=bool(row[20]),
        )

    async def _list_decisions(
        self, db: Any, candidate_id: str, candidate_revision: int,
    ) -> tuple[ReviewDecision, ...]:
        cursor = await db.execute(
            f"SELECT {self._DECISION_COLUMNS} FROM learning_review_decision "
            "WHERE candidate_id = ? AND candidate_revision = ? ORDER BY created_at, decision_id",
            (candidate_id, candidate_revision),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return tuple(self._decision(row) for row in rows)

    async def list_decisions(self, candidate_id: str, candidate_revision: int) -> tuple[ReviewDecision, ...]:
        async with self._db() as db:
            return await self._list_decisions(db, candidate_id, candidate_revision)

    @staticmethod
    def _quorum_from_decisions(
        candidate_id: str, candidate_revision: int,
        decisions: tuple[ReviewDecision, ...], expected_reviewer_ids: tuple[str, ...],
    ) -> ReviewQuorumResult:
        configured = tuple(dict.fromkeys(
            str(value).strip() for value in expected_reviewer_ids if str(value).strip()
        ))
        if len(configured) < 2:
            return ReviewQuorumResult(
                candidate_id, candidate_revision, "human_review_pending", None,
                tuple(), tuple(), "reviewer_configuration_missing",
            )
        by_reviewer: dict[str, list[ReviewDecision]] = {}
        for decision in decisions:
            if decision.reviewer_kind == "rule":
                continue
            if decision.decision not in {"approved", "rejected"}:
                continue
            if decision.pair_order not in {"ab", "ba"}:
                continue
            by_reviewer.setdefault(decision.reviewer_id, []).append(decision)

        selected: list[ReviewDecision] = []
        reviewer_votes: list[str] = []
        missing_reviewer = False
        pair_order_incomplete = False
        pair_order_unstable = False
        input_conflict = False
        identity_conflict = False
        reviewer_models: dict[str, str] = {}
        common_input_signatures: set[tuple[Any, ...]] = set()
        for reviewer_id in configured:
            rows = by_reviewer.get(reviewer_id, [])
            if not rows:
                missing_reviewer = True
                continue
            by_order = {
                order: sorted(
                    (row for row in rows if row.pair_order == order),
                    key=lambda row: (row.created_at, row.decision_id),
                )
                for order in ("ab", "ba")
            }
            if any(not by_order[order] for order in ("ab", "ba")):
                pair_order_incomplete = True
                continue
            all_rows = tuple(by_order["ab"] + by_order["ba"])
            selected.extend((by_order["ab"][0], by_order["ba"][0]))
            reviewer_kinds = {row.reviewer_kind for row in all_rows}
            if len(reviewer_kinds) != 1:
                identity_conflict = True
                continue
            reviewer_kind = next(iter(reviewer_kinds))
            model_identities = {row.model_identity for row in all_rows}
            if reviewer_kind == "model":
                if (
                    len(model_identities) != 1
                    or not is_reviewer_model_identity(next(iter(model_identities), ""))
                ):
                    identity_conflict = True
                    continue
                reviewer_models[reviewer_id] = next(iter(model_identities))
            elif any(model_identities):
                identity_conflict = True
                continue

            row_input_signatures = {
                (
                    row.candidate_revision,
                    row.reviewer_kind,
                    row.rubric_version,
                    (
                        row.prompt_version[: -(len(row.pair_order) + 1)]
                        if row.prompt_version.endswith(f":{row.pair_order}")
                        else row.prompt_version
                    ),
                    tuple(sorted(row.source_evidence_ids)),
                )
                for row in all_rows
            }
            if len(row_input_signatures) != 1:
                input_conflict = True
                continue
            common_input_signatures.update(row_input_signatures)
            votes = {row.decision for row in all_rows}
            if len(votes) != 1:
                pair_order_unstable = True
                continue
            reviewer_votes.append(next(iter(votes)))

        decision_ids = tuple(row.decision_id for row in selected)
        reviewer_ids = tuple(dict.fromkeys(row.reviewer_id for row in selected))
        if (
            identity_conflict
            or len(set(reviewer_models.values())) != len(reviewer_models)
            or len({signature[1] for signature in common_input_signatures}) > 1
        ):
            return ReviewQuorumResult(candidate_id, candidate_revision, "human_review_pending", None, decision_ids, reviewer_ids, "reviewer_identity_conflict")
        if input_conflict or len(common_input_signatures) > 1:
            return ReviewQuorumResult(candidate_id, candidate_revision, "human_review_pending", None, decision_ids, reviewer_ids, "review_input_mismatch")
        if pair_order_unstable:
            return ReviewQuorumResult(candidate_id, candidate_revision, "human_review_pending", None, decision_ids, reviewer_ids, "pair_order_unstable")
        if pair_order_incomplete:
            return ReviewQuorumResult(candidate_id, candidate_revision, "human_review_pending", None, decision_ids, reviewer_ids, "pair_order_incomplete")
        if missing_reviewer or len(reviewer_ids) != len(configured):
            return ReviewQuorumResult(candidate_id, candidate_revision, "human_review_pending", None, decision_ids, reviewer_ids, "insufficient_reviewers")
        if len(set(reviewer_votes)) != 1:
            return ReviewQuorumResult(candidate_id, candidate_revision, "human_review_pending", None, decision_ids, reviewer_ids, "reviewer_disagreement")
        return ReviewQuorumResult(
            candidate_id, candidate_revision, "completed", reviewer_votes[0],
            decision_ids, reviewer_ids, "quorum_reached", True,
        )

    async def quorum(
        self, candidate_id: str, candidate_revision: int, *,
        expected_reviewer_ids: tuple[str, ...] = (),
    ) -> ReviewQuorumResult:
        decisions = await self.list_decisions(candidate_id, candidate_revision)
        return self._quorum_from_decisions(
            candidate_id, candidate_revision, decisions, expected_reviewer_ids,
        )

    async def finalize_quorum(
        self, candidate_id: str, candidate_revision: int, *, created_at: float,
        expected_reviewer_ids: tuple[str, ...] = (),
    ) -> ReviewDecision | None:
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT revision, status FROM learning_candidate WHERE candidate_id = ?",
                (candidate_id,),
            )
            candidate = await cursor.fetchone()
            await cursor.close()
            if (
                candidate is None
                or int(candidate[0]) != candidate_revision
                or str(candidate[1]) not in _REVIEWABLE_CANDIDATE_STATUSES
            ):
                await db.rollback()
                return None
            decisions = await self._list_decisions(db, candidate_id, candidate_revision)
            existing = next(
                (item for item in decisions if item.reviewer_kind == "rule" and item.pair_order == "quorum"),
                None,
            )
            if existing is not None:
                await db.rollback()
                return existing
            quorum = self._quorum_from_decisions(
                candidate_id, candidate_revision, decisions, expected_reviewer_ids,
            )
            if (
                quorum.status != "completed"
                or quorum.decision not in {"approved", "rejected"}
                or not quorum.order_invariant
            ):
                await db.rollback()
                return None
            selected = [item for item in decisions if item.decision_id in quorum.decision_ids]
            if len(selected) != len(quorum.decision_ids):
                await db.rollback()
                return None
            identity = _digest({
                "candidate_id": candidate_id,
                "candidate_revision": candidate_revision,
                "decision": quorum.decision,
                "source_decision_ids": tuple(sorted(quorum.decision_ids)),
                "order_invariant": True,
                "version": 2,
            })
            final = ReviewDecision(
                decision_id=f"review-quorum:{identity}",
                candidate_id=candidate_id,
                candidate_revision=candidate_revision,
                decision=quorum.decision,
                reason="evidence_supported" if quorum.decision == "approved" else "evidence_conflict",
                reviewer_id="review-quorum-v2",
                reviewer_kind="rule",
                reviewer_attempt_id=f"quorum:{identity}",
                rubric_version="review-quorum-v2",
                prompt_version="deterministic",
                model_identity="",
                source_evidence_ids=tuple(dict.fromkeys(value for item in selected for value in item.source_evidence_ids)),
                source_example_ids=tuple(dict.fromkeys(value for item in selected for value in item.source_example_ids)),
                model_example_ids=tuple(dict.fromkeys(value for item in selected for value in item.model_example_ids)),
                source_decision_ids=tuple(sorted(quorum.decision_ids)),
                confidence=min((float(item.confidence) for item in selected if item.confidence is not None), default=0.0),
                expected_revision=candidate_revision,
                pair_order="quorum",
                order_invariant=True,
                diagnostics={
                    "source_decision_count": len(quorum.decision_ids),
                    "reviewer_ids": list(quorum.reviewer_ids),
                    "order_invariant": True,
                },
                created_at=created_at,
            )
            try:
                await db.execute(
                    """INSERT INTO learning_review_decision(
                        decision_id, candidate_id, candidate_revision, decision, reason,
                        reviewer_id, reviewer_kind, reviewer_attempt_id, rubric_version,
                        prompt_version, model_identity, source_evidence_ids_json,
                        source_example_ids_json, model_example_ids_json, source_decision_ids_json,
                        confidence, expected_revision, pair_order, diagnostics_json, created_at,
                        order_invariant
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        final.decision_id, final.candidate_id, final.candidate_revision,
                        final.decision, final.reason, final.reviewer_id, final.reviewer_kind,
                        final.reviewer_attempt_id, final.rubric_version, final.prompt_version,
                        final.model_identity, _json(final.source_evidence_ids),
                        _json(final.source_example_ids), _json(final.model_example_ids),
                        _json(final.source_decision_ids), final.confidence,
                        final.expected_revision, final.pair_order,
                        _json(dict(final.diagnostics)), final.created_at,
                        int(final.order_invariant),
                    ),
                )
                await db.execute(
                    """UPDATE learning_review_attempt
                       SET status = 'blocked', finished_at = ?, failure_stage = 'review_quorum',
                           failure_kind = 'review_quorum_finalized', retryable = 0,
                           revision = revision + 1, updated_at = ?
                       WHERE candidate_id = ? AND candidate_revision = ? AND status = 'running'""",
                    (created_at, created_at, candidate_id, candidate_revision),
                )
                await db.commit()
            except Exception as exc:
                await db.rollback()
                if "UNIQUE constraint failed" not in str(exc):
                    raise
        return final


ReviewRepository = LearningReviewRepository

__all__ = [
    "LearningReviewRepository", "ReviewRepository", "ReviewAttempt", "ReviewMutation",
    "ReviewRevisionResult",
]
