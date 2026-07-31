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
