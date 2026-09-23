from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


LATEST_ARCHITECTURE_SCHEMA_VERSION = 175

MESSAGELOG_REQUIRED_COLUMNS = (
    "event_id",
    "event_schema_version",
    "platform_message_id",
    "chat_kind",
    "role",
    "message_kind",
    "is_bot",
    "reply_target_event_id",
    "reply_target_actor_id",
    "reply_target_actor_name",
    "quote_event_id",
    "at_actor_ids",
    "topic_epoch",
    "causal_parent_event_id",
    "source_event_ids",
    "provenance",
    "image_refs",
    "interaction_kind",
    "recalled",
    "outcome",
)

CHAT_STATE_REQUIRED_COLUMNS = (
    "chat_kind",
    "last_real_user_activity_at",
    "last_committed_bot_reply_at",
    "next_proactive_due_at",
    "proactive_generation",
    "unanswered_proactive_count",
    "last_proactive_commit_id",
    "last_proactive_cancel_reason",
    "proactive_claim_token",
    "proactive_claimed_at",
)

REQUIRED_INDEXES = (
    "ix_messagelog_event_id",
    "ix_chat_states_next_proactive_due_at",
    "ix_reply_commit_outbox_next_retry_at",
    "ix_learning_mining_run_created_at",
    "ix_learning_mining_run_mining_run_id",
    "ix_memory_turn_checkpoint_updated_at",
    "ix_relationship_event_ledger_user_created",
    "ix_relationship_event_ledger_chat_created",
    "ix_relationship_event_ledger_turn_id",
    "ix_memory_turn_ledger_chat_updated",
    "ix_learning_ingest_outbox_due",
    "ix_background_task_ledger_scope_status",
    "ix_reflection_outbox_due",
    "ix_dream_completion_outbox_updated",
    "ix_dream_completion_outbox_lease",
    "ix_reply_commit_outbox_lease",
    "ix_memory_turn_ledger_lease",
    "ix_learning_candidate_due",
    "ix_learning_candidate_scope",
    "ix_learning_candidate_fingerprint",
    "ix_learning_source_batch_prefix",
    "ix_learning_attempt_due",
    "ix_learning_diagnostic_run",
    "ix_learning_circuit_due",
    "ix_learning_quality_candidate",
    "ux_learning_review_active_claim",
    "ix_learning_review_decision_candidate",
    "ix_learning_admission_blocked",
    "ix_learning_retrieval_turn",
    "ix_learning_retrieval_generation",
    "ix_learning_human_admission_candidate",
)

REQUIRED_TRIGGERS = (
    "trg_learning_review_decision_no_update",
    "trg_learning_review_decision_no_delete",
    "trg_learning_retrieval_event_no_update",
    "trg_learning_retrieval_event_no_delete",
    "trg_learning_human_admission_no_update",
    "trg_learning_human_admission_no_delete",
)


@dataclass(frozen=True, slots=True)
class ArchitectureMigrationAuditReport:
    schema_version: int
    expected_schema_version: int
    ready: bool
    table_row_counts: dict[str, int] = field(default_factory=dict)
    missing_tables: tuple[str, ...] = ()
    missing_columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    missing_indexes: tuple[str, ...] = ()
    missing_triggers: tuple[str, ...] = ()
    invalid_constraints: tuple[str, ...] = ()
    canonical_event_rows: int = 0
    duplicate_event_id_groups: int = 0
    conflicting_event_id_groups: int = 0
    unknown_actor_rows: int = 0
    unresolved_target_evidence_rows: int = 0
    unknown_chat_kind_rows: int = 0
    invalid_json_fields: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def _index_names(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }


def _trigger_names(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
    }


def _scalar(db: sqlite3.Connection, statement: str) -> int:
    row = db.execute(statement).fetchone()
    return int(row[0] if row else 0)


def _invalid_json_count(
    db: sqlite3.Connection,
    *,
    table: str,
    column: str,
) -> int:
    invalid = 0
    for row in db.execute(f'SELECT "{column}" FROM "{table}"'):
        value = row[0]
        if value in (None, ""):
            continue
        try:
            decoded = json.loads(str(value))
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid += 1
            continue
        if not isinstance(decoded, list):
            invalid += 1
    return invalid


def _event_id_conflicts(db: sqlite3.Connection) -> tuple[int, int]:
    duplicate_groups = 0
    conflicting_groups = 0
    rows = db.execute(
        """
        SELECT event_id
        FROM messagelog
        WHERE TRIM(COALESCE(event_id, '')) <> ''
        GROUP BY event_id
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for (event_id,) in rows:
        duplicate_groups += 1
        variants = {
            tuple(str(value or "") for value in item)
            for item in db.execute(
                """
                SELECT group_id, sender_id, role, message_kind, content
                FROM messagelog
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchall()
        }
        if len(variants) > 1:
            conflicting_groups += 1
    return duplicate_groups, conflicting_groups


def inspect_architecture_migration(
    db: sqlite3.Connection,
) -> ArchitectureMigrationAuditReport:
    """Inspect architecture migration readiness without mutating the database."""

    schema_version = _scalar(db, "PRAGMA user_version")
    tables = _table_names(db)
    required_tables = (
        "messagelog",
        "chat_states",
        "reply_commit_outbox",
        "memory_turn_checkpoint",
        "relationship_event_ledger",
        "memory_turn_ledger",
        "learning_ingest_outbox",
        "background_task_ledger",
        "reflection_outbox",
        "dream_completion_outbox",
        "diary_checkpoints",
        "learning_provider_circuit",
        "learning_provider_circuit_settlement",
        "learning_source_batch",
        "learning_candidate",
        "learning_candidate_evidence",
        "learning_candidate_attempt",
        "learning_stage_diagnostic",
        "learning_candidate_quality",
        "learning_review_attempt",
        "learning_review_decision",
        "learning_admission",
        "learning_human_admission",
        "learning_retrieval_event",
    )
    missing_tables = tuple(name for name in required_tables if name not in tables)
    table_row_counts = {
        name: _scalar(db, f'SELECT COUNT(*) FROM "{name}"')
        for name in required_tables
        if name in tables
    }

    requirements = {
        "messagelog": MESSAGELOG_REQUIRED_COLUMNS,
        "chat_states": CHAT_STATE_REQUIRED_COLUMNS,
        "dream_completion_outbox": ("lease_until", "lease_token"),
        "memory_turn_ledger": ("lease_until", "lease_token"),
        "learning_source_batch": (
            "source_ids_hash",
            "source_dispositions_json",
            "revision",
            "status",
        ),
        "learning_candidate": (
            "candidate_type",
            "fingerprint",
            "revision",
            "attempt",
            "lease_owner",
            "lease_token",
            "lease_until",
            "next_retry_at",
        ),
        "learning_candidate_evidence": (
            "candidate_id",
            "evidence_id",
            "batch_id",
            "is_generated",
            "eligible",
        ),
        "learning_candidate_attempt": (
            "attempt_id",
            "candidate_work_attempt",
            "provider_attempt",
            "provider_request_started",
            "result_digest",
            "settlement_payload_json",
        ),
        "learning_stage_diagnostic": (
            "diagnostic_id",
            "run_id",
            "stage",
            "status",
        ),
        "learning_candidate_quality": (
            "quality_id",
            "candidate_id",
            "candidate_revision",
            "profile_version",
            "profile_hash",
            "window_start",
            "window_end",
            "eligible_message_count",
            "unknown_message_count",
            "support_count",
            "speaker_support",
            "speaker_message_count",
            "group_support",
            "group_message_count",
            "other_support",
            "other_total",
            "distinct_turns",
            "distinct_turn_count",
            "distinct_day_count",
            "context_diversity",
            "g2",
            "log2_effect",
            "signed_log2_lift",
            "p_value",
            "fdr_q",
            "pmi",
            "left_entropy_bits",
            "right_entropy_bits",
            "burst_ratio",
            "first_seen_at",
            "last_seen_at",
            "feature_complete",
            "missing_reasons_json",
            "confidence_tier",
            "reasons_json",
            "created_at",
        ),
        "learning_review_attempt": (
            "attempt_id", "candidate_id", "candidate_revision",
            "review_work_attempt", "owner", "lease_token", "lease_until",
            "status", "reviewer_id", "reviewer_attempt_id", "revision",
            "provider_request_started", "provider_started_at",
        ),
        "learning_review_decision": (
            "decision_id", "candidate_id", "candidate_revision", "decision",
            "reviewer_id", "reviewer_attempt_id", "source_evidence_ids_json",
            "source_example_ids_json", "model_example_ids_json", "pair_order",
            "order_invariant",
            "source_decision_ids_json",
        ),
        "learning_admission": (
            "candidate_id", "candidate_revision", "review_decision_ids_json",
            "admission_revision", "pre_index_eligible", "post_publish_eligible",
            "index_blocked", "provenance_digest", "revision",
        ),
        "learning_human_admission": (
            "admission_id", "candidate_id", "candidate_revision", "admission_revision",
            "reviewer_identity", "decision", "reason", "provenance_digest",
            "publish_proof_digest", "created_at",
        ),
        "learning_retrieval_event": (
            "event_id", "idempotency_key", "turn_id", "correlation_id",
            "source_layer", "stage", "event_status", "scope_id", "sender_id",
            "query_fingerprint", "candidate_revision", "review_revision",
            "admission_revision", "asset_revision_ids_json", "asset_provenance_json", "selected_ids_json",
            "accepted_ids_json", "visible_ids_json", "generation", "policy_version",
            "prompt_revision", "reason_code", "trimmed_reason", "budget_chars",
            "budget_tokens", "outcome", "reply_id", "diagnostics_json", "created_at",
        ),
    }
    missing_columns: dict[str, tuple[str, ...]] = {}
    available_columns: dict[str, set[str]] = {}
    for table, required in requirements.items():
        if table not in tables:
            continue
        available = _columns(db, table)
        available_columns[table] = available
        missing = tuple(name for name in required if name not in available)
        if missing:
            missing_columns[table] = missing

    indexes = _index_names(db)
    index_table = {
        "ix_memory_turn_ledger_chat_updated": "memory_turn_ledger",
        "ix_learning_ingest_outbox_due": "learning_ingest_outbox",
        "ix_background_task_ledger_scope_status": "background_task_ledger",
        "ix_reflection_outbox_due": "reflection_outbox",
        "ix_dream_completion_outbox_updated": "dream_completion_outbox",
        "ix_dream_completion_outbox_lease": "dream_completion_outbox",
        "ix_reply_commit_outbox_lease": "reply_commit_outbox",
        "ix_memory_turn_ledger_lease": "memory_turn_ledger",
        "ix_learning_candidate_due": "learning_candidate",
        "ix_learning_candidate_scope": "learning_candidate",
        "ix_learning_candidate_fingerprint": "learning_candidate",
        "ix_learning_source_batch_prefix": "learning_source_batch",
        "ix_learning_attempt_due": "learning_candidate_attempt",
        "ix_learning_diagnostic_run": "learning_stage_diagnostic",
        "ix_learning_circuit_due": "learning_provider_circuit",
        "ix_learning_quality_candidate": "learning_candidate_quality",
        "ux_learning_review_active_claim": "learning_review_attempt",
        "ix_learning_review_decision_candidate": "learning_review_decision",
        "ix_learning_admission_blocked": "learning_admission",
        "ix_learning_retrieval_turn": "learning_retrieval_event",
        "ix_learning_retrieval_generation": "learning_retrieval_event",
        "ix_learning_human_admission_candidate": "learning_human_admission",
    }
    missing_indexes = tuple(
        name
        for name in REQUIRED_INDEXES
        if index_table.get(name) in (None, *tables) and name not in indexes
    )
    triggers = _trigger_names(db)
    missing_triggers = tuple(name for name in REQUIRED_TRIGGERS if name not in triggers)
    invalid_constraints: list[str] = []
    retrieval_sql = str(
        (
            db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='learning_retrieval_event'"
            ).fetchone()
            or ("",)
        )[0]
        or ""
    )
    for value in (
        "memory_injection", "react_retriever", "prompt_refiner", "reply_commit",
        "eligible", "selected", "accepted_for_prompt", "prompt_visible", "reply_outcome",
        "observed", "blocked", "unknown", "failed", "budget_zero", "fast_mode",
        "near_context", "priority_eviction", "policy", "error", "reply_sent",
        "reply_failed", "cancelled", "no_reply",
    ):
        if retrieval_sql and f"'{value}'" not in retrieval_sql:
            invalid_constraints.append(f"learning_retrieval_event:{value}")
    normalized_retrieval_sql = "".join(retrieval_sql.lower().split())
    if retrieval_sql and "json_valid(asset_provenance_json)" not in normalized_retrieval_sql:
        invalid_constraints.append("learning_retrieval_event:asset_provenance_json_valid")

    canonical_event_rows = 0
    duplicate_event_id_groups = 0
    conflicting_event_id_groups = 0
    unknown_actor_rows = 0
    unresolved_target_evidence_rows = 0
    unknown_chat_kind_rows = 0
    invalid_json_fields: dict[str, int] = {}

    message_columns = available_columns.get("messagelog", set())
    if "event_id" in message_columns:
        canonical_event_rows = _scalar(
            db,
            "SELECT COUNT(*) FROM messagelog "
            "WHERE TRIM(COALESCE(event_id, '')) <> ''",
        )
        duplicate_event_id_groups, conflicting_event_id_groups = _event_id_conflicts(db)
    if "sender_id" in message_columns:
        unknown_actor_rows = _scalar(
            db,
            "SELECT COUNT(*) FROM messagelog "
            "WHERE LOWER(TRIM(COALESCE(sender_id, ''))) IN ('', 'unknown')",
        )
    target_columns = {
        "reply_target_event_id",
        "reply_target_actor_id",
        "quote_event_id",
        "at_actor_ids",
    }
    if target_columns.issubset(message_columns):
        unresolved_target_evidence_rows = _scalar(
            db,
            """
            SELECT COUNT(*) FROM messagelog
            WHERE TRIM(COALESCE(reply_target_actor_id, '')) = ''
              AND (
                    TRIM(COALESCE(reply_target_event_id, '')) <> ''
                 OR TRIM(COALESCE(quote_event_id, '')) <> ''
                 OR COALESCE(at_actor_ids, '[]') NOT IN ('', '[]')
              )
            """,
        )
    if "chat_kind" in message_columns:
        unknown_chat_kind_rows = _scalar(
            db,
            "SELECT COUNT(*) FROM messagelog "
            "WHERE LOWER(TRIM(COALESCE(chat_kind, ''))) "
            "NOT IN ('group', 'private', 'friend', 'system')",
        )
    for column in ("at_actor_ids", "source_event_ids", "image_refs"):
        if column in message_columns:
            invalid = _invalid_json_count(db, table="messagelog", column=column)
            if invalid:
                invalid_json_fields[f"messagelog.{column}"] = invalid

    ready = schema_version >= LATEST_ARCHITECTURE_SCHEMA_VERSION and not any(
        (
            missing_tables,
            missing_columns,
            missing_indexes,
            missing_triggers,
            invalid_constraints,
            conflicting_event_id_groups,
            invalid_json_fields,
        )
    )
    return ArchitectureMigrationAuditReport(
        schema_version=schema_version,
        expected_schema_version=LATEST_ARCHITECTURE_SCHEMA_VERSION,
        ready=ready,
        table_row_counts=table_row_counts,
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        missing_indexes=missing_indexes,
        missing_triggers=missing_triggers,
        invalid_constraints=tuple(invalid_constraints),
        canonical_event_rows=canonical_event_rows,
        duplicate_event_id_groups=duplicate_event_id_groups,
        conflicting_event_id_groups=conflicting_event_id_groups,
        unknown_actor_rows=unknown_actor_rows,
        unresolved_target_evidence_rows=unresolved_target_evidence_rows,
        unknown_chat_kind_rows=unknown_chat_kind_rows,
        invalid_json_fields=invalid_json_fields,
    )


def audit_architecture_database(path: str | Path) -> ArchitectureMigrationAuditReport:
    """Open a SQLite file read-only and return a repeatable migration dry-run report."""

    resolved = Path(path).resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.execute("PRAGMA query_only = ON")
        return inspect_architecture_migration(db)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only AstrMai context architecture migration audit."
    )
    parser.add_argument("database", type=Path, help="SQLite database file to inspect")
    args = parser.parse_args()
    report = audit_architecture_database(args.database)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ready else 2


__all__ = [
    "ArchitectureMigrationAuditReport",
    "CHAT_STATE_REQUIRED_COLUMNS",
    "LATEST_ARCHITECTURE_SCHEMA_VERSION",
    "MESSAGELOG_REQUIRED_COLUMNS",
    "REQUIRED_INDEXES",
    "REQUIRED_TRIGGERS",
    "audit_architecture_database",
    "inspect_architecture_migration",
]


if __name__ == "__main__":
    raise SystemExit(main())
