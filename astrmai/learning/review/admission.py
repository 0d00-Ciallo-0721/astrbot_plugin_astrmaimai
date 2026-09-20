from __future__ import annotations

import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

from ...infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from ..persistence.review_repository import LearningReviewRepository


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    candidate_id: str
    candidate_revision: int
    review_decision_ids: tuple[str, ...]
    admission_revision: int
    pre_index_eligible: bool
    post_publish_eligible: bool
    index_blocked: bool
    blocked_reason: str
    blocked_stage: str
    blocked_kind: str
    index_generation: str
    mapping_digest: str
    index_hash: str
    provenance_digest: str
    publish_proof_digest: str
    revision: int
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class AdmissionMutation:
    applied: bool
    conflict: bool
    idempotent: bool = False
    failure_kind: str = ""
    admission: AdmissionRecord | None = None


@dataclass(frozen=True, slots=True)
class PublishProofVerification:
    verified: bool
    failure_kind: str = ""
    verification_digest: str = ""


class PublishProofVerifier(Protocol):
    async def verify(
        self, proof: VectorPublishProof, *, submitting_owner_id: str,
    ) -> PublishProofVerification: ...


@dataclass(frozen=True, slots=True)
class AdmissionHandoff:
    candidate_id: str
    candidate_revision: int
    candidate_family: str
    scope_id: str
    speaker_scope_id: str
    fingerprint: str
    approved_review_decision_ids: tuple[str, ...]
    source_evidence_ids: tuple[str, ...]
    source_example_ids: tuple[str, ...]
    model_example_ids: tuple[str, ...]
    quality_features: dict[str, Any]
    pre_index_eligible: bool
    admission_revision: int
    index_blocked: bool
    blocked_reason: str
    provenance_digest: str


@dataclass(frozen=True, slots=True)
class VectorPublishProof:
    candidate_id: str
    candidate_revision: int
    admission_revision: int
    review_decision_ids: tuple[str, ...]
    canonical_revision: int
    owner_id: str
    asset_id: str
    asset_revision: int
    membership_id: str
    index_generation: str
    provider_id: str
    model_id: str
    vector_dimension: int
    vector_count: int
    mapping_digest: str
    index_hash: str
    provenance_digest: str
    publish_proof: str

    def proof_payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_revision": self.candidate_revision,
            "admission_revision": self.admission_revision,
            "review_decision_ids": tuple(self.review_decision_ids),
            "canonical_revision": self.canonical_revision,
            "owner_id": self.owner_id,
            "asset_id": self.asset_id,
            "asset_revision": self.asset_revision,
            "membership_id": self.membership_id,
            "index_generation": self.index_generation,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "vector_dimension": self.vector_dimension,
            "vector_count": self.vector_count,
            "mapping_digest": self.mapping_digest,
            "index_hash": self.index_hash,
            "provenance_digest": self.provenance_digest,
            "proof_version": 1,
        }

    @classmethod
    def signed(cls, **values: Any) -> VectorPublishProof:
        unsigned = cls(publish_proof="0" * 64, **values)
        return cls(publish_proof=_digest(unsigned.proof_payload()), **values)

    def valid(self, trusted_owner_ids: frozenset[str]) -> bool:
        strict_ints = (
            self.candidate_revision, self.admission_revision, self.canonical_revision,
            self.asset_revision, self.vector_dimension, self.vector_count,
        )
        digests = (
            self.mapping_digest, self.index_hash, self.provenance_digest, self.publish_proof,
        )
        return bool(
            all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in strict_ints)
            and self.canonical_revision == self.candidate_revision
            and self.asset_revision == self.canonical_revision
            and self.review_decision_ids
            and all(str(value).strip() for value in self.review_decision_ids)
            and all(str(value or "").strip() for value in (
                self.candidate_id, self.owner_id, self.asset_id, self.membership_id,
                self.index_generation, self.provider_id, self.model_id,
            ))
            and self.owner_id in trusted_owner_ids
            and all(re.fullmatch(r"[0-9a-f]{64}", value or "") for value in digests)
            and hmac.compare_digest(self.publish_proof, _digest(self.proof_payload()))
        )


class AdmissionRepository:
    def __init__(
        self, db_path: str | Path, *, mark_published_enabled: bool = False,
        trusted_publish_owner_ids: frozenset[str] = frozenset(),
        publish_proof_verifier: PublishProofVerifier | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.mark_published_enabled = bool(mark_published_enabled)
        self.trusted_publish_owner_ids = frozenset(trusted_publish_owner_ids)
        self.publish_proof_verifier = publish_proof_verifier

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[Any]:
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    @staticmethod
    def _record(row: Any) -> AdmissionRecord | None:
        if row is None:
            return None
        return AdmissionRecord(
            candidate_id=str(row[0]), candidate_revision=int(row[1]),
            review_decision_ids=tuple(json.loads(str(row[2]))), admission_revision=int(row[3]),
            pre_index_eligible=bool(row[4]), post_publish_eligible=bool(row[5]),
            index_blocked=bool(row[6]), blocked_reason=str(row[7]),
            blocked_stage=str(row[8]), blocked_kind=str(row[9]),
            index_generation=str(row[10]), mapping_digest=str(row[11]),
            index_hash=str(row[12]), provenance_digest=str(row[13]),
            publish_proof_digest=str(row[14]), revision=int(row[15]),
            created_at=float(row[16]), updated_at=float(row[17]),
        )

    _COLUMNS = """
        candidate_id, candidate_revision, review_decision_ids_json,
        admission_revision, pre_index_eligible, post_publish_eligible,
        index_blocked, blocked_reason, blocked_stage, blocked_kind,
        index_generation, mapping_digest, index_hash, provenance_digest,
        publish_proof_digest, revision, created_at, updated_at
    """

    async def get(self, candidate_id: str, candidate_revision: int) -> AdmissionRecord | None:
        async with self._db() as db:
            cursor = await db.execute(
                f"SELECT {self._COLUMNS} FROM learning_admission WHERE candidate_id = ? AND candidate_revision = ?",
                (candidate_id, candidate_revision),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return self._record(row)

    async def save_evaluation(
        self, *, candidate_id: str, candidate_revision: int,
        review_decision_ids: tuple[str, ...], pre_index_eligible: bool,
        blocked_reason: str, blocked_stage: str, blocked_kind: str,
        provenance_digest: str, now: float,
    ) -> AdmissionMutation:
        decision_ids = tuple(dict.fromkeys(review_decision_ids))
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT revision, status FROM learning_candidate WHERE candidate_id = ?",
                (candidate_id,),
            )
            candidate = await cursor.fetchone()
            await cursor.close()
            if candidate is None:
                await db.rollback()
                return AdmissionMutation(False, True, failure_kind="candidate_missing")
            if int(candidate[0]) != candidate_revision:
                await db.rollback()
                return AdmissionMutation(False, True, failure_kind="candidate_revision_conflict")
            if str(candidate[1]) not in {"review_pending", "human_review_pending"}:
                await db.rollback()
                return AdmissionMutation(False, True, failure_kind="candidate_status_not_reviewable")
            cursor = await db.execute(
                f"SELECT {self._COLUMNS} FROM learning_admission WHERE candidate_id = ? AND candidate_revision = ?",
                (candidate_id, candidate_revision),
            )
            existing = self._record(await cursor.fetchone())
            await cursor.close()
            if existing and existing.provenance_digest == provenance_digest:
                await db.rollback()
                return AdmissionMutation(False, False, True, admission=existing)
            if existing is None:
                admission_revision = 1
                await db.execute(
                    """INSERT INTO learning_admission(
                        candidate_id, candidate_revision, review_decision_ids_json,
                        admission_revision, pre_index_eligible, post_publish_eligible,
                        index_blocked, blocked_reason, blocked_stage, blocked_kind,
                        provenance_digest, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        candidate_id, candidate_revision, _json(decision_ids), admission_revision,
                        int(pre_index_eligible), blocked_reason, blocked_stage, blocked_kind,
                        provenance_digest, now, now,
                    ),
                )
            else:
                admission_revision = existing.admission_revision + 1
                cursor = await db.execute(
                    """UPDATE learning_admission SET review_decision_ids_json = ?,
                       admission_revision = ?, pre_index_eligible = ?,
                       post_publish_eligible = 0, index_blocked = 1,
                       blocked_reason = ?, blocked_stage = ?, blocked_kind = ?,
                       index_generation = '', mapping_digest = '', index_hash = '',
                       publish_proof_digest = '', provenance_digest = ?,
                       revision = revision + 1, updated_at = ?
                       WHERE candidate_id = ? AND candidate_revision = ? AND revision = ?""",
                    (
                        _json(decision_ids), admission_revision, int(pre_index_eligible),
                        blocked_reason, blocked_stage, blocked_kind, provenance_digest, now,
                        candidate_id, candidate_revision, existing.revision,
                    ),
                )
                if cursor.rowcount != 1:
                    await cursor.close()
                    await db.rollback()
                    return AdmissionMutation(False, True, failure_kind="admission_cas_conflict")
                await cursor.close()
            await db.commit()
        return AdmissionMutation(True, False, admission=await self.get(candidate_id, candidate_revision))

    async def mark_published(
        self, proof: VectorPublishProof, *, submitting_owner_id: str,
        expected_record_revision: int, now: float,
    ) -> AdmissionMutation:
        if not self.mark_published_enabled:
            return AdmissionMutation(
                False, True, failure_kind="mark_published_disabled",
                admission=await self.get(proof.candidate_id, proof.candidate_revision),
            )
        if (
            not str(submitting_owner_id or "").strip()
            or submitting_owner_id != proof.owner_id
            or not proof.valid(self.trusted_publish_owner_ids)
        ):
            return AdmissionMutation(False, True, failure_kind="invalid_publish_proof", admission=await self.get(proof.candidate_id, proof.candidate_revision))
        if self.publish_proof_verifier is None:
            return AdmissionMutation(
                False, True, failure_kind="publish_verifier_unavailable",
                admission=await self.get(proof.candidate_id, proof.candidate_revision),
            )
        try:
            verification = await self.publish_proof_verifier.verify(
                proof, submitting_owner_id=submitting_owner_id,
            )
        except Exception:
            return AdmissionMutation(
                False, True, failure_kind="publish_verification_error",
                admission=await self.get(proof.candidate_id, proof.candidate_revision),
            )
        if (
            not verification.verified
            or re.fullmatch(r"[0-9a-f]{64}", verification.verification_digest or "") is None
        ):
            return AdmissionMutation(
                False, True,
                failure_kind=verification.failure_kind or "publish_verification_failed",
                admission=await self.get(proof.candidate_id, proof.candidate_revision),
            )
        proof_digest = verification.verification_digest
        async with self._db() as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                f"SELECT {self._COLUMNS} FROM learning_admission WHERE candidate_id = ? AND candidate_revision = ?",
                (proof.candidate_id, proof.candidate_revision),
            )
            current = self._record(await cursor.fetchone())
            await cursor.close()
            cursor = await db.execute(
                "SELECT revision FROM learning_candidate WHERE candidate_id = ?",
                (proof.candidate_id,),
            )
            candidate = await cursor.fetchone()
            await cursor.close()
            if candidate is None or int(candidate[0]) != proof.candidate_revision:
                await db.rollback()
                return AdmissionMutation(False, True, failure_kind="candidate_revision_conflict", admission=current)
            if current and current.publish_proof_digest:
                idempotent = (
                    current.publish_proof_digest == proof_digest
                    and current.post_publish_eligible
                    and not current.index_blocked
                )
                await db.rollback()
                return AdmissionMutation(False, not idempotent, idempotent, "" if idempotent else "publish_replay_conflict", current)
            if (
                current is None or not current.pre_index_eligible or not current.index_blocked
                or current.admission_revision != proof.admission_revision
                or current.revision != expected_record_revision
                or current.review_decision_ids != tuple(proof.review_decision_ids)
                or current.provenance_digest != proof.provenance_digest
            ):
                await db.rollback()
                return AdmissionMutation(False, True, failure_kind="publish_cas_conflict", admission=current)
            cursor = await db.execute(
                """UPDATE learning_admission SET post_publish_eligible = 1,
                   index_blocked = 0, blocked_reason = '', blocked_stage = '', blocked_kind = '',
                   index_generation = ?, mapping_digest = ?, index_hash = ?,
                   publish_proof_digest = ?, revision = revision + 1, updated_at = ?
                   WHERE candidate_id = ? AND candidate_revision = ?
                     AND admission_revision = ? AND revision = ? AND index_blocked = 1""",
                (
                    proof.index_generation, proof.mapping_digest, proof.index_hash, proof_digest,
                    now, proof.candidate_id, proof.candidate_revision,
                    proof.admission_revision, expected_record_revision,
                ),
            )
            if cursor.rowcount != 1:
                await cursor.close()
                await db.rollback()
                return AdmissionMutation(False, True, failure_kind="publish_cas_conflict")
            await cursor.close()
            await db.commit()
        return AdmissionMutation(True, False, admission=await self.get(proof.candidate_id, proof.candidate_revision))


class AdmissionService:
    def __init__(
        self, db_path: str | Path, *, evaluation_enabled: bool = False,
        automatic_quorum_enabled: bool = False,
        mark_published_enabled: bool = False,
        trusted_publish_owner_ids: frozenset[str] = frozenset(),
        expected_reviewer_ids: tuple[str, ...] = (),
        publish_proof_verifier: PublishProofVerifier | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.evaluation_enabled = bool(evaluation_enabled)
        self.automatic_quorum_enabled = bool(automatic_quorum_enabled)
        self.expected_reviewer_ids = tuple(expected_reviewer_ids)
        self.repository = AdmissionRepository(
            db_path,
            mark_published_enabled=mark_published_enabled,
            trusted_publish_owner_ids=trusted_publish_owner_ids,
            publish_proof_verifier=publish_proof_verifier,
        )
        self.reviews = LearningReviewRepository(db_path)

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[Any]:
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db

    async def evaluate(self, candidate_id: str, expected_revision: int, *, now: float) -> AdmissionMutation:
        if not self.evaluation_enabled:
            return AdmissionMutation(
                False, True, failure_kind="admission_evaluation_disabled",
                admission=await self.repository.get(candidate_id, expected_revision),
            )
        async with self._db() as db:
            cursor = await db.execute(
                """SELECT candidate_type, scope_id, speaker_id, speaker_scope_id,
                   fingerprint, quality_profile_version, revision, status
                   FROM learning_candidate WHERE candidate_id = ?""",
                (candidate_id,),
            )
            candidate = await cursor.fetchone()
            await cursor.close()
            if candidate is None:
                return AdmissionMutation(False, True, failure_kind="candidate_missing")
            if int(candidate[6]) != expected_revision:
                return AdmissionMutation(False, True, failure_kind="candidate_revision_conflict")
            if str(candidate[7]) not in {"review_pending", "human_review_pending"}:
                return AdmissionMutation(False, True, failure_kind="candidate_status_not_reviewable")
            cursor = await db.execute(
                """SELECT evidence_id, source_row_id, event_id, platform_message_id,
                   scope_id, speaker_scope_id, source_type, evidence_quality, eligible,
                   payload_json FROM learning_candidate_evidence WHERE candidate_id = ?""",
                (candidate_id,),
            )
            evidence = await cursor.fetchall()
            await cursor.close()
            cursor = await db.execute(
                """SELECT quality_id, profile_version, profile_hash, feature_complete,
                   missing_reasons_json, confidence_tier, support_count, eligible_message_count
                   FROM learning_candidate_quality
                   WHERE candidate_id = ? AND candidate_revision = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (candidate_id, expected_revision),
            )
            quality = await cursor.fetchone()
            await cursor.close()
        final_quorum = None
        if self.automatic_quorum_enabled:
            final_quorum = await self.reviews.finalize_quorum(
                candidate_id, expected_revision, created_at=now,
                expected_reviewer_ids=self.expected_reviewer_ids,
            )
        quorum = await self.reviews.quorum(
            candidate_id, expected_revision,
            expected_reviewer_ids=self.expected_reviewer_ids,
        )
        reasons: list[str] = []
        if not evidence:
            reasons.append("source_evidence_missing")
        valid_evidence_ids: list[str] = []
        for row in evidence:
            identity = ""
            if str(row[2] or "").strip() and not str(row[2]).lower().startswith(("fallback_", "evt_")):
                identity = f"event_id:{str(row[2]).strip()}"
            elif str(row[3] or "").strip():
                identity = f"platform_message_id:{str(row[3]).strip()}"
            elif isinstance(row[1], int) and not isinstance(row[1], bool) and int(row[1]) > 0:
                identity = f"row:{int(row[1])}"
            if not identity or not bool(row[8]) or str(row[6]) == "unknown" or str(row[7]) == "unknown":
                reasons.append("source_evidence_unverifiable")
            else:
                valid_evidence_ids.append(identity)
            try:
                payload = json.loads(str(row[9] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if payload.get("sensitive") or payload.get("conflict"):
                reasons.append("evidence_policy_blocked")
        if not str(candidate[1] or "").strip():
            reasons.append("scope_missing")
        if str(candidate[2] or "").strip() and not str(candidate[3] or "").strip():
            reasons.append("speaker_scope_missing")
        if quality is None:
            reasons.append("quality_snapshot_missing")
        elif (
            str(quality[1]) != str(candidate[5]) or not bool(quality[3])
        ):
            reasons.append("quality_snapshot_mismatch")
        elif str(quality[5]) != "high":
            reasons.append("quality_gate_not_met")
        if quorum.status != "completed" or quorum.decision != "approved":
            reasons.append("review_quorum_incomplete" if quorum.decision is None else "review_not_approved")
        if self.automatic_quorum_enabled and final_quorum is None:
            reasons.append("review_quorum_not_finalized")
        if quorum.status == "completed" and not self.automatic_quorum_enabled:
            reasons.append("automatic_quorum_disabled")
        if self.automatic_quorum_enabled and len(self.expected_reviewer_ids) < 2:
            reasons.append("reviewer_configuration_missing")
        if quorum.decision_ids:
            decisions = await self.reviews.list_decisions(candidate_id, expected_revision)
            selected = [item for item in decisions if item.decision_id in quorum.decision_ids]
            if any(set(item.source_evidence_ids) - set(valid_evidence_ids) for item in selected):
                reasons.append("review_evidence_mismatch")
            if any(set(item.source_example_ids) & set(item.model_example_ids) for item in selected):
                reasons.append("source_model_example_overlap")
        reasons = list(dict.fromkeys(reasons))
        pre_index_eligible = not reasons
        provenance = {
            "candidate_id": candidate_id, "candidate_revision": expected_revision,
            "candidate_facts": tuple(candidate or ()), "evidence_ids": tuple(sorted(valid_evidence_ids)),
            "quality_id": "" if quality is None else str(quality[0]),
            "quality_profile": "" if quality is None else str(quality[1]),
            "decision_ids": quorum.decision_ids, "reasons": tuple(reasons),
        }
        admission_decision_ids = (
            (final_quorum.decision_id, *quorum.decision_ids)
            if final_quorum is not None
            else quorum.decision_ids
        )
        return await self.repository.save_evaluation(
            candidate_id=candidate_id, candidate_revision=expected_revision,
            review_decision_ids=admission_decision_ids,
            pre_index_eligible=pre_index_eligible,
            blocked_reason="vector_identity_unverified" if pre_index_eligible else reasons[0],
            blocked_stage="index_identity" if pre_index_eligible else "admission",
            blocked_kind="vector_identity_unverified" if pre_index_eligible else reasons[0],
            provenance_digest=_digest(provenance), now=now,
        )

    async def handoff(self, candidate_id: str, candidate_revision: int) -> AdmissionHandoff | None:
        admission = await self.repository.get(candidate_id, candidate_revision)
        if admission is None:
            return None
        decisions = await self.reviews.list_decisions(candidate_id, candidate_revision)
        selected = [item for item in decisions if item.decision_id in admission.review_decision_ids]
        async with self._db() as db:
            cursor = await db.execute(
                "SELECT candidate_type, scope_id, speaker_scope_id, fingerprint FROM learning_candidate WHERE candidate_id = ? AND revision = ?",
                (candidate_id, candidate_revision),
            )
            candidate = await cursor.fetchone()
            await cursor.close()
            cursor = await db.execute(
                "SELECT quality_id, profile_version, profile_hash, confidence_tier, support_count, eligible_message_count FROM learning_candidate_quality WHERE candidate_id = ? AND candidate_revision = ? ORDER BY created_at DESC LIMIT 1",
                (candidate_id, candidate_revision),
            )
            quality = await cursor.fetchone()
            await cursor.close()
        if candidate is None:
            return None
        return AdmissionHandoff(
            candidate_id=candidate_id, candidate_revision=candidate_revision,
            candidate_family=str(candidate[0]), scope_id=str(candidate[1]),
            speaker_scope_id=str(candidate[2]), fingerprint=str(candidate[3]),
            approved_review_decision_ids=admission.review_decision_ids,
            source_evidence_ids=tuple(dict.fromkeys(v for item in selected for v in item.source_evidence_ids)),
            source_example_ids=tuple(dict.fromkeys(v for item in selected for v in item.source_example_ids)),
            model_example_ids=tuple(dict.fromkeys(v for item in selected for v in item.model_example_ids)),
            quality_features={} if quality is None else {
                "quality_id": quality[0], "profile_version": quality[1],
                "profile_hash": quality[2], "confidence_tier": quality[3],
                "support_count": quality[4], "eligible_message_count": quality[5],
            },
            pre_index_eligible=admission.pre_index_eligible,
            admission_revision=admission.admission_revision,
            index_blocked=admission.index_blocked, blocked_reason=admission.blocked_reason,
            provenance_digest=admission.provenance_digest,
        )


__all__ = [
    "AdmissionHandoff", "AdmissionMutation", "AdmissionRecord", "AdmissionRepository",
    "AdmissionService", "PublishProofVerification", "PublishProofVerifier",
    "VectorPublishProof",
]
