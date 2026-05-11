from __future__ import annotations

import asyncio
import sqlite3

import aiosqlite
from astrbot.api import logger
from sqlmodel import SQLModel


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
    def _schedule_init_db(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._init_db_sync()
            return
        self._init_task = loop.create_task(self._init_db())

    def _init_db_sync(self):
        try:
            with sqlite3.connect(self.db_path) as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS chat_states (
                        chat_id TEXT PRIMARY KEY,
                        energy REAL,
                        mood REAL,
                        group_config TEXT,
                        last_reset_date TEXT,
                        total_replies INTEGER,
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
                        group_footprints TEXT,
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
                for col_def in [
                    "ALTER TABLE user_profiles ADD COLUMN tags TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN nickname TEXT DEFAULT ''",
                    "ALTER TABLE user_profiles ADD COLUMN nickname_reason TEXT DEFAULT ''",
                    "ALTER TABLE user_profiles ADD COLUMN know_times INTEGER DEFAULT 0",
                    "ALTER TABLE user_profiles ADD COLUMN is_known INTEGER DEFAULT 0",
                    "ALTER TABLE user_profiles ADD COLUMN memory_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN identity_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN preference_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN relationship_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN speech_style_points TEXT DEFAULT '[]'",
                ]:
                    try:
                        db.execute(col_def)
                    except Exception:
                        pass
                for col_def in [
                    "ALTER TABLE expressionpattern ADD COLUMN style TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN content_list TEXT DEFAULT '[]'",
                    "ALTER TABLE expressionpattern ADD COLUMN count INTEGER DEFAULT 1",
                    "ALTER TABLE expressionpattern ADD COLUMN checked INTEGER DEFAULT 0",
                    "ALTER TABLE expressionpattern ADD COLUMN rejected INTEGER DEFAULT 0",
                    "ALTER TABLE expressionpattern ADD COLUMN modified_by TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN source TEXT DEFAULT 'learning'",
                    "ALTER TABLE expressionpattern ADD COLUMN shared_scope TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN think_level INTEGER DEFAULT 0",
                    "ALTER TABLE expressionpattern ADD COLUMN review_status TEXT DEFAULT 'pending'",
                    "ALTER TABLE expressionpattern ADD COLUMN review_reason TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN review_suggestion TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN last_review_time REAL DEFAULT 0",
                ]:
                    try:
                        db.execute(col_def)
                    except Exception:
                        pass
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
                try:
                    db.execute("ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''")
                except Exception:
                    pass
                db.execute("""
                    CREATE INDEX IF NOT EXISTS ix_memoryevent_session_id
                    ON memoryevent (session_id)
                """)
                db.commit()
        except Exception as e:
            logger.error(f"[AstrMai-Infra] ? {e}")

    # ==========================================
    # Cache I/O (Persona Summarizer)
    # ==========================================

    async def _init_db(self):
        """Initialize async database tables required by the plugin."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS chat_states (
                        chat_id TEXT PRIMARY KEY,
                        energy REAL,
                        mood REAL,
                        group_config TEXT,
                        last_reset_date TEXT,
                        total_replies INTEGER,
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
                        group_footprints TEXT,
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
                #  (ALTER TABLE ?
                for col_def in [
                    "ALTER TABLE user_profiles ADD COLUMN tags TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN nickname TEXT DEFAULT ''",
                    "ALTER TABLE user_profiles ADD COLUMN nickname_reason TEXT DEFAULT ''",
                    "ALTER TABLE user_profiles ADD COLUMN know_times INTEGER DEFAULT 0",
                    "ALTER TABLE user_profiles ADD COLUMN is_known INTEGER DEFAULT 0",
                    "ALTER TABLE user_profiles ADD COLUMN memory_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN identity_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN preference_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN relationship_points TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN speech_style_points TEXT DEFAULT '[]'",
                ]:
                    try:
                        await db.execute(col_def)
                    except Exception:
                        pass  # ?                # [] CronSnapshot  SQL 
                for col_def in [
                    "ALTER TABLE expressionpattern ADD COLUMN style TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN content_list TEXT DEFAULT '[]'",
                    "ALTER TABLE expressionpattern ADD COLUMN count INTEGER DEFAULT 1",
                    "ALTER TABLE expressionpattern ADD COLUMN checked INTEGER DEFAULT 0",
                    "ALTER TABLE expressionpattern ADD COLUMN rejected INTEGER DEFAULT 0",
                    "ALTER TABLE expressionpattern ADD COLUMN modified_by TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN source TEXT DEFAULT 'learning'",
                    "ALTER TABLE expressionpattern ADD COLUMN shared_scope TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN think_level INTEGER DEFAULT 0",
                    "ALTER TABLE expressionpattern ADD COLUMN review_status TEXT DEFAULT 'pending'",
                    "ALTER TABLE expressionpattern ADD COLUMN review_reason TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN review_suggestion TEXT DEFAULT ''",
                    "ALTER TABLE expressionpattern ADD COLUMN last_review_time REAL DEFAULT 0",
                ]:
                    try:
                        await db.execute(col_def)
                    except Exception:
                        pass
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
                try:
                    await db.execute("ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''")
                except Exception:
                    pass
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS ix_memoryevent_session_id
                    ON memoryevent (session_id)
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"[AstrMai-Infra] ? {e}")
