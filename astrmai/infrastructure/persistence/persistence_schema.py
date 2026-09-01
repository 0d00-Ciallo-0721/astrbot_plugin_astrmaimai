from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
from astrbot.api import logger
from sqlmodel import SQLModel
from ...shared.helpers.plugin_helpers import safe_create_task
from .sqlite_helpers import connect_aiosqlite, connect_sqlite


# ── Schema migration versions (PRAGMA user_version) ──────────────────
# Each tuple: (version_number, ddl_statement)
# New migrations MUST be appended with incrementing version numbers.
_MIGRATIONS: list[tuple[int, str]] = [
    ( 1, "ALTER TABLE chat_states ADD COLUMN last_reply_time REAL DEFAULT 0"),
    ( 2, "ALTER TABLE chat_states ADD COLUMN last_passive_decay_time REAL DEFAULT 0"),
    ( 3, "ALTER TABLE chat_states ADD COLUMN last_energy_recovery_time REAL DEFAULT 0"),
    ( 4, "ALTER TABLE chat_states ADD COLUMN total_messages INTEGER DEFAULT 0"),
    ( 5, "ALTER TABLE chat_states ADD COLUMN judgment_mode TEXT DEFAULT 'single'"),
    ( 6, "ALTER TABLE chat_states ADD COLUMN last_msg_info TEXT DEFAULT '{}'"),
    ( 7, "ALTER TABLE chat_states ADD COLUMN last_access_time REAL DEFAULT 0"),
    ( 8, "ALTER TABLE chat_states ADD COLUMN next_wakeup_timestamp REAL DEFAULT 0"),
    ( 9, "ALTER TABLE chat_states ADD COLUMN is_dirty INTEGER DEFAULT 0"),
    (10, "ALTER TABLE user_profiles ADD COLUMN tags TEXT DEFAULT '[]'"),
    (11, "ALTER TABLE user_profiles ADD COLUMN nickname TEXT DEFAULT ''"),
    (12, "ALTER TABLE user_profiles ADD COLUMN nickname_reason TEXT DEFAULT ''"),
    (13, "ALTER TABLE user_profiles ADD COLUMN know_times INTEGER DEFAULT 0"),
    (14, "ALTER TABLE user_profiles ADD COLUMN is_known INTEGER DEFAULT 0"),
    (15, "ALTER TABLE user_profiles ADD COLUMN memory_points TEXT DEFAULT '[]'"),
    (16, "ALTER TABLE user_profiles ADD COLUMN identity_points TEXT DEFAULT '[]'"),
    (17, "ALTER TABLE user_profiles ADD COLUMN preference_points TEXT DEFAULT '[]'"),
    (18, "ALTER TABLE user_profiles ADD COLUMN relationship_points TEXT DEFAULT '[]'"),
    (19, "ALTER TABLE user_profiles ADD COLUMN speech_style_points TEXT DEFAULT '[]'"),
    (20, "ALTER TABLE user_profiles ADD COLUMN message_count_for_profiling INTEGER DEFAULT 0"),
    (21, "ALTER TABLE user_profiles ADD COLUMN last_persona_gen_time REAL DEFAULT 0"),
    (22, "ALTER TABLE user_profiles ADD COLUMN profile_metadata TEXT DEFAULT '{}'"),
    (23, "ALTER TABLE expressionpattern ADD COLUMN style TEXT DEFAULT ''"),
    (24, "ALTER TABLE expressionpattern ADD COLUMN content_list TEXT DEFAULT '[]'"),
    (25, "ALTER TABLE expressionpattern ADD COLUMN count INTEGER DEFAULT 1"),
    (26, "ALTER TABLE expressionpattern ADD COLUMN checked INTEGER DEFAULT 0"),
    (27, "ALTER TABLE expressionpattern ADD COLUMN rejected INTEGER DEFAULT 0"),
    (28, "ALTER TABLE expressionpattern ADD COLUMN modified_by TEXT DEFAULT ''"),
    (29, "ALTER TABLE expressionpattern ADD COLUMN source TEXT DEFAULT 'learning'"),
    (30, "ALTER TABLE expressionpattern ADD COLUMN shared_scope TEXT DEFAULT ''"),
    (31, "ALTER TABLE expressionpattern ADD COLUMN think_level INTEGER DEFAULT 0"),
    (32, "ALTER TABLE expressionpattern ADD COLUMN review_status TEXT DEFAULT 'pending'"),
    (33, "ALTER TABLE expressionpattern ADD COLUMN review_reason TEXT DEFAULT ''"),
    (34, "ALTER TABLE expressionpattern ADD COLUMN review_suggestion TEXT DEFAULT ''"),
    (35, "ALTER TABLE expressionpattern ADD COLUMN last_review_time REAL DEFAULT 0"),
    (36, "ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''"),
    (37, "ALTER TABLE messagelog ADD COLUMN event_id TEXT DEFAULT ''"),
    (38, "ALTER TABLE messagelog ADD COLUMN event_schema_version INTEGER DEFAULT 0"),
    (39, "ALTER TABLE messagelog ADD COLUMN platform_message_id TEXT DEFAULT ''"),
    (40, "ALTER TABLE messagelog ADD COLUMN chat_kind TEXT DEFAULT ''"),
    (41, "ALTER TABLE messagelog ADD COLUMN role TEXT DEFAULT ''"),
    (42, "ALTER TABLE messagelog ADD COLUMN message_kind TEXT DEFAULT ''"),
    (43, "ALTER TABLE messagelog ADD COLUMN is_bot INTEGER DEFAULT 0"),
    (44, "ALTER TABLE messagelog ADD COLUMN reply_target_event_id TEXT DEFAULT ''"),
    (45, "ALTER TABLE messagelog ADD COLUMN reply_target_actor_id TEXT DEFAULT ''"),
    (46, "ALTER TABLE messagelog ADD COLUMN reply_target_actor_name TEXT DEFAULT ''"),
    (47, "ALTER TABLE messagelog ADD COLUMN quote_event_id TEXT DEFAULT ''"),
    (48, "ALTER TABLE messagelog ADD COLUMN at_actor_ids TEXT DEFAULT '[]'"),
    (49, "ALTER TABLE messagelog ADD COLUMN topic_epoch INTEGER DEFAULT 0"),
    (50, "ALTER TABLE messagelog ADD COLUMN causal_parent_event_id TEXT DEFAULT ''"),
    (51, "ALTER TABLE messagelog ADD COLUMN source_event_ids TEXT DEFAULT '[]'"),
    (52, "ALTER TABLE messagelog ADD COLUMN provenance TEXT DEFAULT 'legacy'"),
    (53, "ALTER TABLE messagelog ADD COLUMN image_refs TEXT DEFAULT '[]'"),
    (54, "ALTER TABLE messagelog ADD COLUMN interaction_kind TEXT DEFAULT ''"),
    (55, "ALTER TABLE messagelog ADD COLUMN recalled INTEGER DEFAULT 0"),
    (56, "ALTER TABLE messagelog ADD COLUMN outcome TEXT DEFAULT ''"),
    (57, "CREATE INDEX IF NOT EXISTS ix_messagelog_event_id ON messagelog(event_id)"),
    (58, "ALTER TABLE chat_states ADD COLUMN chat_kind TEXT DEFAULT ''"),
    (59, "ALTER TABLE chat_states ADD COLUMN last_real_user_activity_at REAL DEFAULT 0"),
    (60, "ALTER TABLE chat_states ADD COLUMN last_committed_bot_reply_at REAL DEFAULT 0"),
    (61, "ALTER TABLE chat_states ADD COLUMN next_proactive_due_at REAL DEFAULT 0"),
    (62, "ALTER TABLE chat_states ADD COLUMN proactive_generation INTEGER DEFAULT 0"),
    (63, "ALTER TABLE chat_states ADD COLUMN unanswered_proactive_count INTEGER DEFAULT 0"),
    (64, "ALTER TABLE chat_states ADD COLUMN last_proactive_commit_id TEXT DEFAULT ''"),
    (65, "ALTER TABLE chat_states ADD COLUMN last_proactive_cancel_reason TEXT DEFAULT ''"),
    (66, "ALTER TABLE chat_states ADD COLUMN proactive_claim_token TEXT DEFAULT ''"),
    (67, "ALTER TABLE chat_states ADD COLUMN proactive_claimed_at REAL DEFAULT 0"),
    (68, "UPDATE chat_states SET last_real_user_activity_at = last_reply_time WHERE last_real_user_activity_at = 0 AND last_reply_time > 0"),
    (69, "UPDATE chat_states SET last_committed_bot_reply_at = last_reply_time WHERE last_committed_bot_reply_at = 0 AND last_reply_time > 0"),
    (70, "UPDATE chat_states SET next_proactive_due_at = next_wakeup_timestamp WHERE next_proactive_due_at = 0 AND next_wakeup_timestamp > 0"),
    (71, "CREATE INDEX IF NOT EXISTS ix_chat_states_next_proactive_due_at ON chat_states(next_proactive_due_at)"),
    (72, """CREATE TABLE IF NOT EXISTS reply_commit_outbox (
        commit_id TEXT PRIMARY KEY,
        committed_turn_json TEXT NOT NULL,
        repair_context_json TEXT NOT NULL DEFAULT '{}',
        consumer_status_json TEXT NOT NULL DEFAULT '{}',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (73, "CREATE INDEX IF NOT EXISTS ix_reply_commit_outbox_next_retry_at ON reply_commit_outbox(next_retry_at)"),
    (74, """CREATE TABLE IF NOT EXISTS learning_pipeline_checkpoint (
        pipeline TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        cursor_log_id INTEGER NOT NULL DEFAULT 0,
        last_batch_id TEXT NOT NULL DEFAULT '',
        last_status TEXT NOT NULL DEFAULT '',
        failure_count INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (pipeline, chat_id)
    )"""),
    (75, "CREATE INDEX IF NOT EXISTS ix_learning_pipeline_checkpoint_retry ON learning_pipeline_checkpoint(pipeline, retry_at)"),
    (76, """CREATE TABLE IF NOT EXISTS learning_mining_run (
        run_id TEXT PRIMARY KEY,
        pipeline TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        batch_id TEXT NOT NULL DEFAULT '',
        raw_count INTEGER NOT NULL DEFAULT 0,
        normalized_count INTEGER NOT NULL DEFAULT 0,
        required_count INTEGER NOT NULL DEFAULT 0,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        saved_count INTEGER NOT NULL DEFAULT 0,
        deduplicated_count INTEGER NOT NULL DEFAULT 0,
        cursor_before INTEGER NOT NULL DEFAULT 0,
        cursor_after INTEGER NOT NULL DEFAULT 0,
        retained_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        duration_ms REAL NOT NULL DEFAULT 0,
        model_id TEXT NOT NULL DEFAULT '',
        retryable INTEGER NOT NULL DEFAULT 0,
        error_type TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL DEFAULT 0
    )"""),
    (77, "CREATE INDEX IF NOT EXISTS ix_learning_mining_run_pipeline_chat_created ON learning_mining_run(pipeline, chat_id, created_at)"),
    (78, "CREATE INDEX IF NOT EXISTS ix_learning_mining_run_created_at ON learning_mining_run(created_at)"),
    (79, """CREATE TABLE IF NOT EXISTS memory_turn_checkpoint (
        chat_id TEXT PRIMARY KEY,
        session_json TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (80, "CREATE INDEX IF NOT EXISTS ix_memory_turn_checkpoint_updated_at ON memory_turn_checkpoint(updated_at)"),
    (81, """CREATE TABLE IF NOT EXISTS cross_session_handoff (
        handoff_id TEXT PRIMARY KEY,
        platform_id TEXT NOT NULL,
        source_umo TEXT NOT NULL DEFAULT '',
        source_sender_id TEXT NOT NULL DEFAULT '',
        source_sender_name TEXT NOT NULL DEFAULT '',
        target_umo TEXT NOT NULL DEFAULT '',
        target_id TEXT NOT NULL,
        target_name TEXT NOT NULL DEFAULT '',
        outbound_message TEXT NOT NULL DEFAULT '',
        context_summary TEXT NOT NULL DEFAULT '',
        delivery_mode TEXT NOT NULL DEFAULT 'relay',
        observed_turns INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at REAL NOT NULL DEFAULT 0,
        expires_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (82, "CREATE INDEX IF NOT EXISTS ix_cross_session_handoff_target_active ON cross_session_handoff(platform_id, target_id, status, expires_at)"),
    (83, """CREATE TABLE IF NOT EXISTS proactive_daily_plan (
        plan_date TEXT PRIMARY KEY,
        plan_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT 'fallback',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (84, """CREATE TABLE IF NOT EXISTS proactive_scenario_delivery (
        delivery_key TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        scenario TEXT NOT NULL,
        local_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'claimed',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (85, "CREATE INDEX IF NOT EXISTS ix_proactive_scenario_delivery_retry ON proactive_scenario_delivery(status, next_retry_at)"),
    (86, """CREATE TABLE IF NOT EXISTS visualasset (
        asset_id TEXT PRIMARY KEY,
        blob_hash TEXT DEFAULT '',
        pixel_hash TEXT DEFAULT '',
        perceptual_hash TEXT DEFAULT '',
        prompt_version TEXT DEFAULT 'v1',
        type TEXT DEFAULT 'image',
        description TEXT DEFAULT '',
        emotion_tags TEXT DEFAULT '[]',
        model_id TEXT DEFAULT '',
        mime_type TEXT DEFAULT '',
        width INTEGER DEFAULT 0,
        height INTEGER DEFAULT 0,
        frame_count INTEGER DEFAULT 1,
        byte_size INTEGER DEFAULT 0,
        initial_recognition_elapsed_ms REAL DEFAULT 0,
        storage_path TEXT DEFAULT '',
        status TEXT DEFAULT 'ready',
        hit_count INTEGER DEFAULT 0,
        reuse_count INTEGER DEFAULT 0,
        last_error TEXT DEFAULT '',
        created_at REAL DEFAULT 0,
        updated_at REAL DEFAULT 0,
        last_access_at REAL DEFAULT 0
    )"""),
    (87, "ALTER TABLE visualasset ADD COLUMN initial_recognition_elapsed_ms REAL DEFAULT 0"),
    (88, "ALTER TABLE visualasset ADD COLUMN reuse_count INTEGER DEFAULT 0"),
    (89, """CREATE TABLE IF NOT EXISTS relationship_event_ledger (
        event_id TEXT PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        user_id TEXT NOT NULL,
        chat_id TEXT NOT NULL DEFAULT '',
        turn_id TEXT NOT NULL DEFAULT '',
        source_event_ids_json TEXT NOT NULL DEFAULT '[]',
        actor_kind TEXT NOT NULL DEFAULT 'user',
        target_kind TEXT NOT NULL DEFAULT 'bot',
        event_type TEXT NOT NULL,
        confidence REAL NOT NULL DEFAULT 0,
        intensity REAL NOT NULL DEFAULT 1,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        policy_version TEXT NOT NULL DEFAULT 'relationship-v1',
        source TEXT NOT NULL DEFAULT 'deterministic_rule',
        mood_tag TEXT NOT NULL DEFAULT '',
        before_vector_json TEXT NOT NULL DEFAULT '{}',
        delta_vector_json TEXT NOT NULL DEFAULT '{}',
        after_vector_json TEXT NOT NULL DEFAULT '{}',
        disposition TEXT NOT NULL DEFAULT 'rejected',
        created_at REAL NOT NULL DEFAULT 0
    )"""),
    (90, "CREATE INDEX IF NOT EXISTS ix_relationship_event_ledger_user_created ON relationship_event_ledger(user_id, created_at)"),
    (91, "CREATE INDEX IF NOT EXISTS ix_relationship_event_ledger_chat_created ON relationship_event_ledger(chat_id, created_at)"),
    (92, "CREATE INDEX IF NOT EXISTS ix_relationship_event_ledger_turn_id ON relationship_event_ledger(turn_id)"),
    (93, "ALTER TABLE proactive_scenario_delivery ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''"),
    (94, """CREATE TABLE IF NOT EXISTS proactive_dispatch_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        intent_id TEXT NOT NULL,
        created_at REAL NOT NULL DEFAULT 0,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )"""),
    (95, "CREATE INDEX IF NOT EXISTS ix_proactive_dispatch_history_created ON proactive_dispatch_history(created_at DESC, id DESC)"),
    (96, """CREATE TABLE IF NOT EXISTS dream_run (
        run_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 1,
        seed_event_ids_json TEXT NOT NULL DEFAULT '[]',
        maintenance_summary TEXT NOT NULL DEFAULT '',
        maintenance_actions_json TEXT NOT NULL DEFAULT '[]',
        dream_text_hash TEXT NOT NULL DEFAULT '',
        promotion_report_json TEXT NOT NULL DEFAULT '{}',
        stage_status_json TEXT NOT NULL DEFAULT '{}',
        error TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL DEFAULT 0,
        completed_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (97, "CREATE INDEX IF NOT EXISTS ix_dream_run_session_started ON dream_run(session_id, started_at DESC)"),
    (98, """CREATE TABLE IF NOT EXISTS user_profile_revision (
        revision_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        tags_json TEXT NOT NULL DEFAULT '[]',
        memory_points_json TEXT NOT NULL DEFAULT '[]',
        changed_fields_json TEXT NOT NULL DEFAULT '[]',
        model_id TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0
    )"""),
    (99, "CREATE INDEX IF NOT EXISTS ix_user_profile_revision_user_created ON user_profile_revision(user_id, created_at DESC)"),
    (100, """CREATE TABLE IF NOT EXISTS memory_turn_ledger (
        turn_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'recorded',
        first_seen_at REAL NOT NULL DEFAULT 0,
        committed_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (101, "CREATE INDEX IF NOT EXISTS ix_memory_turn_ledger_chat_updated ON memory_turn_ledger(chat_id, updated_at DESC)"),
    (102, """CREATE TABLE IF NOT EXISTS learning_ingest_outbox (
        event_id TEXT PRIMARY KEY,
        envelope_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (103, "CREATE INDEX IF NOT EXISTS ix_learning_ingest_outbox_due ON learning_ingest_outbox(status, next_retry_at)"),
    (104, """CREATE TABLE IF NOT EXISTS background_task_ledger (
        task_id TEXT PRIMARY KEY,
        task_family TEXT NOT NULL,
        scope_id TEXT NOT NULL DEFAULT '',
        scheduled_at REAL NOT NULL DEFAULT 0,
        started_at REAL NOT NULL DEFAULT 0,
        finished_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT NOT NULL DEFAULT '',
        input_fingerprint TEXT NOT NULL DEFAULT '',
        checkpoint_before TEXT NOT NULL DEFAULT '{}',
        checkpoint_after TEXT NOT NULL DEFAULT '{}',
        llm_call_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'queued',
        retry_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0,
        UNIQUE(task_family, scope_id, input_fingerprint)
    )"""),
    (105, "CREATE INDEX IF NOT EXISTS ix_background_task_ledger_scope_status ON background_task_ledger(task_family, scope_id, status, lease_until)"),
    (106, """CREATE TABLE IF NOT EXISTS reflection_outbox (
        reflection_id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL DEFAULT '',
        pattern_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL NOT NULL DEFAULT 0,
        lease_until REAL NOT NULL DEFAULT 0,
        lease_token TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (107, "CREATE INDEX IF NOT EXISTS ix_reflection_outbox_due ON reflection_outbox(status, next_retry_at, chat_id)"),
    (108, """CREATE TABLE IF NOT EXISTS dream_completion_outbox (
        request_key TEXT PRIMARY KEY,
        run_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL DEFAULT 0
    )"""),
    (109, "CREATE INDEX IF NOT EXISTS ix_dream_completion_outbox_updated ON dream_completion_outbox(status, updated_at)"),
    (110, "ALTER TABLE reply_commit_outbox ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"),
    (111, "ALTER TABLE reply_commit_outbox ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''"),
    (112, "CREATE INDEX IF NOT EXISTS ix_reply_commit_outbox_lease ON reply_commit_outbox(next_retry_at, lease_until)"),
    (113, "ALTER TABLE dream_completion_outbox ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"),
    (114, "ALTER TABLE dream_completion_outbox ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''"),
    (115, "CREATE INDEX IF NOT EXISTS ix_dream_completion_outbox_lease ON dream_completion_outbox(status, lease_until)"),
    (116, "ALTER TABLE memory_turn_ledger ADD COLUMN lease_until REAL NOT NULL DEFAULT 0"),
    (117, "ALTER TABLE memory_turn_ledger ADD COLUMN lease_token TEXT NOT NULL DEFAULT ''"),
    (118, "CREATE INDEX IF NOT EXISTS ix_memory_turn_ledger_lease ON memory_turn_ledger(status, lease_until)"),
    (119, "ALTER TABLE learning_mining_run ADD COLUMN mining_run_id TEXT NOT NULL DEFAULT ''"),
    (120, "CREATE INDEX IF NOT EXISTS ix_learning_mining_run_mining_run_id ON learning_mining_run(mining_run_id, pipeline, chat_id)"),
]


def _run_migrations(db: sqlite3.Connection) -> None:
    """Execute unapplied schema migrations based on PRAGMA user_version."""
    current = db.execute("PRAGMA user_version").fetchone()[0]
    if current != 0 and current not in range(1, len(_MIGRATIONS) + 1):
        logger.warning(
            f"[AstrMai-DB] PRAGMA user_version={current}, expected 0..{len(_MIGRATIONS)}. "
            f"Proceeding with catch-up migration."
        )
    for version, ddl in _MIGRATIONS:
        if version <= current:
            continue
        if version in (110, 111, 112):
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_commit_outbox'"
            ).fetchone()
            if not table:
                db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(reply_commit_outbox table absent)"
                )
                continue
        if version in (113, 114, 115):
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dream_completion_outbox'"
            ).fetchone()
            if not table:
                db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(dream_completion_outbox table absent)"
                )
                continue
        if version in (116, 117, 118):
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_turn_ledger'"
            ).fetchone()
            if not table:
                db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(memory_turn_ledger table absent)"
                )
                continue
        if version in (119, 120):
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_mining_run'"
            ).fetchone()
            if not table:
                db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(learning_mining_run table absent)"
                )
                continue
        if version == 93:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_scenario_delivery'"
            ).fetchone()
            if not table:
                db.execute(f"PRAGMA user_version = {version}")
                logger.info("[AstrMai-DB] migration v93 skipped (delivery table absent)")
                continue
            columns = {
                str(row[1])
                for row in db.execute("PRAGMA table_info(proactive_scenario_delivery)").fetchall()
            }
            if "claim_token" in columns:
                db.execute(f"PRAGMA user_version = {version}")
                logger.info("[AstrMai-DB] migration v93 already applied (column exists)")
                continue
        try:
            db.execute(ddl)
            db.execute(f"PRAGMA user_version = {version}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                db.execute(f"PRAGMA user_version = {version}")
                logger.info(f"[AstrMai-DB] migration v{version} already applied (column exists)")
            else:
                logger.error(f"[AstrMai-DB] migration v{version} failed: {exc}")
                raise


async def _run_migrations_async(db: aiosqlite.Connection) -> None:
    """Async variant of schema migrations based on PRAGMA user_version."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    await cursor.close()
    current = int(row[0] if row else 0)
    if current != 0 and current not in range(1, len(_MIGRATIONS) + 1):
        logger.warning(
            f"[AstrMai-DB] PRAGMA user_version={current}, expected 0..{len(_MIGRATIONS)}. "
            f"Proceeding with catch-up migration."
        )
    for version, ddl in _MIGRATIONS:
        if version <= current:
            continue
        if version in (110, 111, 112):
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reply_commit_outbox'"
            )
            table = await cursor.fetchone()
            await cursor.close()
            if not table:
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(reply_commit_outbox table absent)"
                )
                continue
        if version in (113, 114, 115):
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dream_completion_outbox'"
            )
            table = await cursor.fetchone()
            await cursor.close()
            if not table:
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(dream_completion_outbox table absent)"
                )
                continue
        if version in (116, 117, 118):
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_turn_ledger'"
            )
            table = await cursor.fetchone()
            await cursor.close()
            if not table:
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(memory_turn_ledger table absent)"
                )
                continue
        if version in (119, 120):
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_mining_run'"
            )
            table = await cursor.fetchone()
            await cursor.close()
            if not table:
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info(
                    f"[AstrMai-DB] migration v{version} skipped "
                    "(learning_mining_run table absent)"
                )
                continue
        if version == 93:
            cursor = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='proactive_scenario_delivery'"
            )
            table = await cursor.fetchone()
            await cursor.close()
            if not table:
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info("[AstrMai-DB] migration v93 skipped (delivery table absent)")
                continue
            cursor = await db.execute("PRAGMA table_info(proactive_scenario_delivery)")
            columns = {str(row[1]) for row in await cursor.fetchall()}
            await cursor.close()
            if "claim_token" in columns:
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info("[AstrMai-DB] migration v93 already applied (column exists)")
                continue
        try:
            await db.execute(ddl)
            await db.execute(f"PRAGMA user_version = {version}")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                await db.execute(f"PRAGMA user_version = {version}")
                logger.info(f"[AstrMai-DB] migration v{version} already applied (column exists)")
            else:
                logger.error(f"[AstrMai-DB] migration v{version} failed: {exc}")
                raise


def _is_duplicate_column_error(exc: sqlite3.OperationalError) -> bool:
    return "duplicate column name" in str(exc).lower()


def _dedupe_sqlmodel_metadata_indexes() -> None:
    """Remove duplicate SQLModel metadata indexes after reloads."""
    for table in SQLModel.metadata.tables.values():
        indexes = list(table.indexes)
        if len(indexes) <= 1:
            continue

        seen_signatures = set()
        deduped_indexes = []
        changed = False
        for index in indexes:
            signature = (
                index.name,
                bool(index.unique),
                tuple(column.name for column in index.columns),
            )
            if signature in seen_signatures:
                changed = True
                continue
            seen_signatures.add(signature)
            deduped_indexes.append(index)

        if changed:
            table.indexes.clear()
            table.indexes.update(deduped_indexes)


class PersistenceSchemaMixin:
    # ponytail: lazy init ready event, avoids MRO issues with mixins
    _init_ready_event: asyncio.Event | None = None

    @property
    def _init_ready(self) -> asyncio.Event:
        if self._init_ready_event is None:
            self._init_ready_event = asyncio.Event()
            self._init_ready_event.set()  # default to ready (sync init or not yet scheduled)
        return self._init_ready_event
    @staticmethod
    def _schema_patch_error_message(scope: str, statement: str, exc: Exception) -> str:
        return f"{scope} failed for {statement!r}: {exc}"

    def _apply_schema_patch_sync(self, db, statement: str, *, scope: str) -> None:
        try:
            db.execute(statement)
        except sqlite3.OperationalError as exc:
            if _is_duplicate_column_error(exc):
                return
            raise sqlite3.OperationalError(self._schema_patch_error_message(scope, statement, exc)) from exc

    async def _apply_schema_patch_async(self, db, statement: str, *, scope: str) -> None:
        try:
            await db.execute(statement)
        except sqlite3.OperationalError as exc:
            if _is_duplicate_column_error(exc):
                return
            raise sqlite3.OperationalError(self._schema_patch_error_message(scope, statement, exc)) from exc

    def _apply_schema_patch_batch_sync(self, db, statements: list[str], *, scope: str) -> None:
        for statement in statements:
            self._apply_schema_patch_sync(db, statement, scope=scope)

    async def _apply_schema_patch_batch_async(self, db, statements: list[str], *, scope: str) -> None:
        for statement in statements:
            await self._apply_schema_patch_async(db, statement, scope=scope)

    def _schedule_init_db(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._init_db_sync()
            self._init_ready.set()
            return
        # ponytail: clear event before fire-and-forget, set after init completes
        self._init_ready.clear()
        self._init_task = safe_create_task(self._init_db())
        register_owner = getattr(self, "_register_init_task_owner", None)
        if callable(register_owner):
            register_owner()
        self._init_task.add_done_callback(self._on_init_db_done)

    def _on_init_db_done(self, task):
        if task.cancelled():
            self._init_error = asyncio.CancelledError()
            logger.warning("[AstrMai-DB] async init task cancelled")
            return
        exc = task.exception()
        if exc is not None:
            self._init_error = exc
            logger.error(f"[AstrMai-DB] async init failed: {exc}", exc_info=(type(exc), exc, exc.__traceback__))
            return
        self._init_error = None
        self._init_ready.set()

    def _init_db_sync(self):
        try:
            with connect_sqlite(self.db_path) as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS chat_states (
                        chat_id TEXT PRIMARY KEY,
                        energy REAL,
                        mood REAL,
                        group_config TEXT,
                        last_reset_date TEXT,
                        total_replies INTEGER,
                        last_reply_time REAL DEFAULT 0,
                        last_passive_decay_time REAL DEFAULT 0,
                        last_energy_recovery_time REAL DEFAULT 0,
                        total_messages INTEGER DEFAULT 0,
                        judgment_mode TEXT DEFAULT 'single',
                        last_msg_info TEXT DEFAULT '{}',
                        last_access_time REAL DEFAULT 0,
                        next_wakeup_timestamp REAL DEFAULT 0,
                        chat_kind TEXT DEFAULT '',
                        last_real_user_activity_at REAL DEFAULT 0,
                        last_committed_bot_reply_at REAL DEFAULT 0,
                        next_proactive_due_at REAL DEFAULT 0,
                        proactive_generation INTEGER DEFAULT 0,
                        unanswered_proactive_count INTEGER DEFAULT 0,
                        last_proactive_commit_id TEXT DEFAULT '',
                        last_proactive_cancel_reason TEXT DEFAULT '',
                        proactive_claim_token TEXT DEFAULT '',
                        proactive_claimed_at REAL DEFAULT 0,
                        is_dirty INTEGER DEFAULT 0,
                        updated_at REAL
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS user_profiles (
                        user_id TEXT PRIMARY KEY,
                        name TEXT,
                        social_score REAL,
                        last_seen REAL,
                        persona_analysis TEXT,
                        message_count_for_profiling INTEGER DEFAULT 0,
                        last_persona_gen_time REAL DEFAULT 0,
                        group_footprints TEXT,
                        profile_metadata TEXT DEFAULT '{}',
                        identity TEXT,
                        tags TEXT DEFAULT '[]',
                        nickname TEXT DEFAULT '',
                        nickname_reason TEXT DEFAULT '',
                        know_times INTEGER DEFAULT 0,
                        is_known INTEGER DEFAULT 0,
                        memory_points TEXT DEFAULT '[]',
                        identity_points TEXT DEFAULT '[]',
                        preference_points TEXT DEFAULT '[]',
                        relationship_points TEXT DEFAULT '[]',
                        speech_style_points TEXT DEFAULT '[]',
                        updated_at REAL
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS profile_generation_claims (
                        user_id TEXT PRIMARY KEY,
                        claim_token TEXT NOT NULL,
                        claimed_until REAL NOT NULL
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS expressionpattern (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        situation TEXT,
                        expression TEXT,
                        weight REAL DEFAULT 1.0,
                        last_active_time REAL DEFAULT 0,
                        create_time REAL DEFAULT 0,
                        group_id TEXT DEFAULT ''
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS memoryevent (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT,
                        date TEXT,
                        narrative TEXT DEFAULT '',
                        emotion TEXT DEFAULT '',
                        importance INTEGER DEFAULT 5,
                        emotional_intensity INTEGER DEFAULT 5,
                        reflection TEXT DEFAULT '',
                        memory_kind TEXT DEFAULT 'event',
                        source_layer TEXT DEFAULT 'fact',
                        tags TEXT DEFAULT '[]',
                        created_at REAL DEFAULT 0
                    )
                """)
                _run_migrations(db)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS cronsnapshot (
                        job_id      TEXT PRIMARY KEY,
                        name        TEXT DEFAULT '',
                        cron_expression TEXT,
                        run_at      REAL,
                        run_once    INTEGER DEFAULT 0,
                        target_origin TEXT DEFAULT '',
                        payload     TEXT DEFAULT '{}',
                        note        TEXT DEFAULT '',
                        is_active   INTEGER DEFAULT 1,
                        created_at  REAL,
                        updated_at  REAL
                    )
                """)
                db.execute("""
                    CREATE TABLE IF NOT EXISTS lastmessagemetadatadb (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        sender_id TEXT,
                        has_image INTEGER DEFAULT 0,
                        image_urls TEXT DEFAULT '[]',
                        vl_executed INTEGER DEFAULT 0,
                        timestamp REAL
                    )
                """)
                self._apply_schema_patch_sync(
                    db,
                    "ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''",
                    scope="sync memoryevent schema patch",
                )
                db.execute("""
                    CREATE INDEX IF NOT EXISTS ix_memoryevent_session_id
                    ON memoryevent (session_id)
                """)
                db.commit()
        except Exception as e:
            logger.error(f"[AstrMai-Infra] init db failed: {e}")

    # ==========================================
    # Cache I/O (Persona Summarizer)
    # ==========================================

    async def _init_db(self):
        """Initialize async database tables required by the plugin."""
        try:
            async with connect_aiosqlite(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS chat_states (
                        chat_id TEXT PRIMARY KEY,
                        energy REAL,
                        mood REAL,
                        group_config TEXT,
                        last_reset_date TEXT,
                        total_replies INTEGER,
                        last_reply_time REAL DEFAULT 0,
                        last_passive_decay_time REAL DEFAULT 0,
                        last_energy_recovery_time REAL DEFAULT 0,
                        total_messages INTEGER DEFAULT 0,
                        judgment_mode TEXT DEFAULT 'single',
                        last_msg_info TEXT DEFAULT '{}',
                        last_access_time REAL DEFAULT 0,
                        next_wakeup_timestamp REAL DEFAULT 0,
                        chat_kind TEXT DEFAULT '',
                        last_real_user_activity_at REAL DEFAULT 0,
                        last_committed_bot_reply_at REAL DEFAULT 0,
                        next_proactive_due_at REAL DEFAULT 0,
                        proactive_generation INTEGER DEFAULT 0,
                        unanswered_proactive_count INTEGER DEFAULT 0,
                        last_proactive_commit_id TEXT DEFAULT '',
                        last_proactive_cancel_reason TEXT DEFAULT '',
                        proactive_claim_token TEXT DEFAULT '',
                        proactive_claimed_at REAL DEFAULT 0,
                        is_dirty INTEGER DEFAULT 0,
                        updated_at REAL
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_profiles (
                        user_id TEXT PRIMARY KEY,
                        name TEXT,
                        social_score REAL,
                        last_seen REAL,
                        persona_analysis TEXT,
                        message_count_for_profiling INTEGER DEFAULT 0,
                        last_persona_gen_time REAL DEFAULT 0,
                        group_footprints TEXT,
                        profile_metadata TEXT DEFAULT '{}',
                        identity TEXT,
                        tags TEXT DEFAULT '[]',
                        nickname TEXT DEFAULT '',
                        nickname_reason TEXT DEFAULT '',
                        know_times INTEGER DEFAULT 0,
                        is_known INTEGER DEFAULT 0,
                        memory_points TEXT DEFAULT '[]',
                        identity_points TEXT DEFAULT '[]',
                        preference_points TEXT DEFAULT '[]',
                        relationship_points TEXT DEFAULT '[]',
                        speech_style_points TEXT DEFAULT '[]',
                        updated_at REAL
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS profile_generation_claims (
                        user_id TEXT PRIMARY KEY,
                        claim_token TEXT NOT NULL,
                        claimed_until REAL NOT NULL
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS expressionpattern (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        situation TEXT,
                        expression TEXT,
                        weight REAL DEFAULT 1.0,
                        last_active_time REAL DEFAULT 0,
                        create_time REAL DEFAULT 0,
                        group_id TEXT DEFAULT ''
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS memoryevent (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT,
                        date TEXT,
                        narrative TEXT DEFAULT '',
                        emotion TEXT DEFAULT '',
                        importance INTEGER DEFAULT 5,
                        emotional_intensity INTEGER DEFAULT 5,
                        reflection TEXT DEFAULT '',
                        memory_kind TEXT DEFAULT 'event',
                        source_layer TEXT DEFAULT 'fact',
                        tags TEXT DEFAULT '[]',
                        created_at REAL DEFAULT 0
                    )
                """)
                await _run_migrations_async(db)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS cronsnapshot (
                        job_id      TEXT PRIMARY KEY,
                        name        TEXT DEFAULT '',
                        cron_expression TEXT,
                        run_at      REAL,
                        run_once    INTEGER DEFAULT 0,
                        target_origin TEXT DEFAULT '',
                        payload     TEXT DEFAULT '{}',
                        note        TEXT DEFAULT '',
                        is_active   INTEGER DEFAULT 1,
                        created_at  REAL,
                        updated_at  REAL
                    )
                """)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS lastmessagemetadatadb (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id TEXT,
                        sender_id TEXT,
                        has_image INTEGER DEFAULT 0,
                        image_urls TEXT DEFAULT '[]',
                        vl_executed INTEGER DEFAULT 0,
                        timestamp REAL
                    )
                """)
                await self._apply_schema_patch_async(
                    db,
                    "ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''",
                    scope="async memoryevent schema patch",
                )
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS ix_memoryevent_session_id
                    ON memoryevent (session_id)
                """)
                await db.commit()
            return
        except Exception as e:
            logger.error(f"[AstrMai-Infra] init db failed: {e}")
            raise
