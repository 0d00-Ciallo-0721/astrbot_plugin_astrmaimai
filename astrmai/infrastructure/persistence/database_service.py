import asyncio
import json
import time
import uuid
from typing import Any, Callable, List, Optional, TypeVar

from sqlmodel import Session, select

from .database_cron import CronPersistenceMixin
from .database_jargon import JargonPersistenceMixin
from .database_memory import MemoryPersistenceMixin
from .database_profile_relation import ProfileRelationPersistenceMixin
from .database_review import ReviewPersistenceMixin
from .orm_models import (
    ChatState,
    LastMessageMetadata,
    MessageLog,
    VisualAsset,
    VisualMemory,
    VisualMessageBinding,
)
from .sqlite_helpers import connect_sqlite
from astrbot.api import logger

from .persistence_manager import PersistenceManager

T = TypeVar("T")

# TTL for PRAGMA table_info column caches (seconds) — avoids stale schema after runtime DDL
_COL_CACHE_TTL_SEC = 300


class DatabaseService(
    JargonPersistenceMixin,
    ProfileRelationPersistenceMixin,
    MemoryPersistenceMixin,
    ReviewPersistenceMixin,
    CronPersistenceMixin,
):
    """Persistence facade used by state, memory, learning, and runtime domains."""

    def __init__(self, persistence: PersistenceManager):
        self.persistence = persistence
        self._db_lock_instance = None
        self.memory_engine = None
        self._chat_state_cols_cache: list[str] | None = None
        self._chat_state_cols_ts: float = 0.0
        from .repositories.chat_repository import ChatRepository
        from .repositories.memory_repository import MemoryRepository
        from .repositories.profile_repository import ProfileRepository
        from .repositories.review_repository import ReviewRepository

        self.chat_repository = ChatRepository(self)
        self.profile_repository = ProfileRepository(self)
        self.memory_repository = MemoryRepository(self)
        self.review_repository = ReviewRepository(self)
        if hasattr(self.persistence, "bind_database_service"):
            self.persistence.bind_database_service(self)

    @staticmethod
    def _safe_json_loads(value: Any, default: Any = None):
        """Safe JSON deserialization with fallback for dirty/corrupted data."""
        raw = str(value or "").strip()
        if not raw:
            return default if default is not None else {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            from astrbot.api import logger

            logger.warning(
                f"[AstrMai-DB] safe_json_loads failed, falling back to default: {exc} | "
                f"raw_preview={raw[:120]!r}"
            )
            return default if default is not None else {}

    @property
    def _db_lock(self):
        if self._db_lock_instance is None:
            self._db_lock_instance = asyncio.Lock()
        return self._db_lock_instance

    def get_session(self) -> Session:
        return self.persistence.get_session()

    @property
    def engine(self):
        return self.persistence.engine

    @property
    def db_path(self):
        return self.persistence.db_path

    @property
    def orm_models(self):
        return getattr(self.persistence, "orm_models", None)

    @property
    def repositories(self) -> dict[str, Any]:
        return {
            "chat": self.chat_repository,
            "profile": self.profile_repository,
            "memory": self.memory_repository,
            "review": self.review_repository,
        }

    def _clone_model(self, model: T) -> T:
        return model.__class__.model_validate(model.model_dump())

    def _run_with_session(self, callback: Callable[[Session], T]) -> T:
        with self.get_session() as session:
            return callback(session)

    async def _run_blocking(self, callback: Callable[..., T], *args, with_lock: bool = False, **kwargs) -> T:
        if with_lock:
            async with self._db_lock:
                return await asyncio.to_thread(callback, *args, **kwargs)
        return await asyncio.to_thread(callback, *args, **kwargs)

    @staticmethod
    def _conversation_event_log_fields(conversation_event: Any) -> dict[str, Any]:
        if conversation_event is None:
            return {}

        def _value(name: str, default: Any = "") -> Any:
            if isinstance(conversation_event, dict):
                return conversation_event.get(name, default)
            return getattr(conversation_event, name, default)

        def _json_list(name: str) -> str:
            values = _value(name, ()) or ()
            if isinstance(values, str):
                try:
                    parsed = json.loads(values)
                    values = parsed if isinstance(parsed, list) else [values]
                except (TypeError, ValueError, json.JSONDecodeError):
                    values = [values]
            return json.dumps(
                [str(item) for item in values if str(item or "").strip()],
                ensure_ascii=False,
            )

        return {
            "event_id": str(_value("event_id") or ""),
            "event_schema_version": int(_value("schema_version", 0) or 0),
            "platform_message_id": str(_value("platform_message_id") or ""),
            "chat_kind": str(_value("chat_kind") or ""),
            "role": str(_value("role") or ""),
            "message_kind": str(_value("message_kind") or ""),
            "is_bot": bool(_value("is_bot", False)),
            "reply_target_event_id": str(_value("reply_target_event_id") or ""),
            "reply_target_actor_id": str(_value("reply_target_actor_id") or ""),
            "reply_target_actor_name": str(_value("reply_target_actor_name") or ""),
            "quote_event_id": str(_value("quote_event_id") or ""),
            "at_actor_ids": _json_list("at_actor_ids"),
            "topic_epoch": max(0, int(_value("topic_epoch", 0) or 0)),
            "causal_parent_event_id": str(_value("causal_parent_event_id") or ""),
            "source_event_ids": _json_list("source_event_ids"),
            "provenance": str(_value("provenance", "legacy") or "legacy"),
            "image_refs": _json_list("image_refs"),
            "interaction_kind": str(_value("interaction_kind") or ""),
            "recalled": bool(_value("recalled", False)),
            "outcome": str(_value("outcome") or ""),
            "timestamp": float(_value("timestamp", time.time()) or time.time()),
        }

    def add_message_log(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        *,
        conversation_event: Any = None,
    ):
        event_fields = self._conversation_event_log_fields(conversation_event)

        def _sync(session: Session) -> None:
            session.add(
                MessageLog(
                    group_id=group_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content=content,
                    **event_fields,
                )
            )
            session.commit()

        self._run_with_session(_sync)

    def get_unprocessed_logs(self, group_id: str, limit: int = 50) -> List[MessageLog]:
        def _sync(session: Session) -> List[MessageLog]:
            statement = (
                select(MessageLog)
                .where(MessageLog.group_id == group_id, MessageLog.processed == False)
                .order_by(MessageLog.timestamp.desc())
                .limit(limit)
            )
            results = session.exec(statement).all()
            return [self._clone_model(item) for item in reversed(results)]

        return self._run_with_session(_sync)

    def get_learning_visual_context(
        self,
        chat_id: str,
        message_ids: List[str],
    ) -> dict[str, list[dict[str, str]]]:
        normalized_ids = list(dict.fromkeys(str(item or "").strip() for item in message_ids if str(item or "").strip()))
        if not normalized_ids:
            return {}

        def _sync(session: Session) -> dict[str, list[dict[str, str]]]:
            statement = (
                select(VisualMessageBinding)
                .where(
                    VisualMessageBinding.chat_id == str(chat_id or ""),
                    VisualMessageBinding.message_id.in_(normalized_ids),
                )
                .order_by(VisualMessageBinding.message_id, VisualMessageBinding.image_index)
            )
            result: dict[str, list[dict[str, str]]] = {}
            for binding in session.exec(statement).all():
                description = ""
                kind = "image"
                if binding.asset_id:
                    asset = session.get(VisualAsset, binding.asset_id)
                    if asset and str(asset.status or "").lower() == "ready":
                        description = str(asset.description or "").strip()
                        kind = str(asset.type or "image").strip() or "image"
                if not description and binding.legacy_picid:
                    legacy = session.get(VisualMemory, binding.legacy_picid)
                    if legacy:
                        description = str(legacy.description or "").strip()
                        kind = str(legacy.type or "image").strip() or "image"
                if description:
                    result.setdefault(str(binding.message_id), []).append(
                        {"description": description[:800], "type": kind}
                    )
            return result

        return self._run_with_session(_sync)

    def list_unprocessed_log_groups(self, *, min_count: int = 1, limit: int = 20) -> list[dict[str, Any]]:
        safe_min_count = max(1, int(min_count or 1))
        safe_limit = max(1, min(int(limit or 20), 200))
        with connect_sqlite(self.persistence.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT group_id, COUNT(*) AS count, MIN(timestamp) AS oldest_timestamp, MAX(timestamp) AS latest_timestamp
                FROM messagelog
                WHERE processed = 0
                GROUP BY group_id
                HAVING COUNT(*) >= ?
                ORDER BY count DESC, latest_timestamp ASC
                LIMIT ?
                """,
                (safe_min_count, safe_limit),
            )
            return [
                {
                    "group_id": str(row[0] or ""),
                    "count": int(row[1] or 0),
                    "oldest_timestamp": float(row[2] or 0.0),
                    "latest_timestamp": float(row[3] or 0.0),
                }
                for row in cursor.fetchall()
                if str(row[0] or "")
            ]

    def get_recent_message_logs(
        self,
        group_id: str,
        limit: int = 8,
        max_age_seconds: Optional[float] = None,
        include_processed: bool = True,
    ) -> List[MessageLog]:
        cutoff_timestamp = None
        if max_age_seconds is not None and max_age_seconds > 0:
            now = time.time()
            cutoff_timestamp = now - float(max_age_seconds)
            if cutoff_timestamp > now:  # ponytail: NTP backward jump guard
                logger.warning(f"[AstrMai-db] NTP backward jump: cutoff={cutoff_timestamp} > now={now}, clamping")
                cutoff_timestamp = 0.0

        def _sync(session: Session) -> List[MessageLog]:
            filters = [MessageLog.group_id == group_id]
            if not include_processed:
                filters.append(MessageLog.processed == False)
            if cutoff_timestamp is not None:
                filters.append(MessageLog.timestamp >= cutoff_timestamp)

            statement = (
                select(MessageLog)
                .where(*filters)
                .order_by(MessageLog.timestamp.desc())
                .limit(limit)
            )
            results = session.exec(statement).all()
            clones = [
                self._clone_model(item)
                for item in results
                if float(getattr(item, "timestamp", 0) or 0) > 0
            ]
            clones.sort(key=lambda item: float(getattr(item, "timestamp", 0) or 0))
            return clones

        return self._run_with_session(_sync)

    def mark_logs_processed(self, log_ids: List[int]):
        def _sync(session: Session) -> None:
            for log_id in log_ids:
                log = session.get(MessageLog, log_id)
                if log:
                    log.processed = True
                    session.add(log)
            session.commit()

        self._run_with_session(_sync)

    @staticmethod
    def _normalize_learning_pipeline(pipeline: str) -> str:
        value = str(pipeline or "").strip().lower()
        if value not in {"expression", "jargon"}:
            raise ValueError(f"unsupported learning pipeline: {pipeline!r}")
        return value

    def ensure_learning_checkpoint(
        self,
        pipeline: str,
        chat_id: str,
        *,
        replay_recent: int = 0,
    ) -> dict[str, Any]:
        pipeline_name = self._normalize_learning_pipeline(pipeline)
        normalized_chat_id = str(chat_id or "").strip()
        if not normalized_chat_id:
            raise ValueError("chat_id is required")
        now = time.time()
        with connect_sqlite(self.persistence.db_path) as conn:
            row = conn.execute(
                """
                SELECT pipeline, chat_id, cursor_log_id, last_batch_id, last_status,
                       failure_count, retry_at, last_error, created_at, updated_at
                FROM learning_pipeline_checkpoint
                WHERE pipeline = ? AND chat_id = ?
                """,
                (pipeline_name, normalized_chat_id),
            ).fetchone()
            if row is None:
                cursor_log_id = 0
                safe_replay = max(0, int(replay_recent or 0))
                if safe_replay > 0:
                    boundary = conn.execute(
                        """
                        SELECT id FROM messagelog
                        WHERE group_id = ?
                        ORDER BY id DESC
                        LIMIT 1 OFFSET ?
                        """,
                        (normalized_chat_id, safe_replay - 1),
                    ).fetchone()
                    cursor_log_id = max(0, int(boundary[0] or 0) - 1) if boundary else 0
                else:
                    processed_row = conn.execute(
                        """
                        SELECT COALESCE(MAX(id), 0)
                        FROM messagelog
                        WHERE group_id = ? AND processed = 1
                        """,
                        (normalized_chat_id,),
                    ).fetchone()
                    cursor_log_id = int(processed_row[0] or 0) if processed_row else 0
                conn.execute(
                    """
                    INSERT INTO learning_pipeline_checkpoint (
                        pipeline, chat_id, cursor_log_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (pipeline_name, normalized_chat_id, cursor_log_id, now, now),
                )
                conn.commit()
                row = (
                    pipeline_name,
                    normalized_chat_id,
                    cursor_log_id,
                    "",
                    "",
                    0,
                    0.0,
                    "",
                    now,
                    now,
                )
        keys = (
            "pipeline",
            "chat_id",
            "cursor_log_id",
            "last_batch_id",
            "last_status",
            "failure_count",
            "retry_at",
            "last_error",
            "created_at",
            "updated_at",
        )
        return dict(zip(keys, row))

    def ensure_learning_checkpoints_for_groups(
        self,
        pipeline: str,
        *,
        replay_recent: int = 0,
    ) -> int:
        """Initialize missing group checkpoints in one transaction."""

        pipeline_name = self._normalize_learning_pipeline(pipeline)
        safe_replay = max(0, int(replay_recent or 0))
        now = time.time()
        with connect_sqlite(self.persistence.db_path) as conn:
            before = conn.total_changes
            if safe_replay == 0:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO learning_pipeline_checkpoint (
                        pipeline, chat_id, cursor_log_id, created_at, updated_at
                    )
                    SELECT ?, group_id,
                           COALESCE(MAX(CASE WHEN processed = 1 THEN id ELSE 0 END), 0),
                           ?, ?
                    FROM messagelog
                    WHERE group_id != ''
                    GROUP BY group_id
                    """,
                    (pipeline_name, now, now),
                )
            else:
                existing = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT chat_id FROM learning_pipeline_checkpoint WHERE pipeline = ?",
                        (pipeline_name,),
                    ).fetchall()
                }
                chat_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "SELECT DISTINCT group_id FROM messagelog WHERE group_id != ''"
                    ).fetchall()
                    if str(row[0] or "") and str(row[0]) not in existing
                ]
                for chat_id in chat_ids:
                    boundary = conn.execute(
                        """
                        SELECT id FROM messagelog
                        WHERE group_id = ?
                        ORDER BY id DESC
                        LIMIT 1 OFFSET ?
                        """,
                        (chat_id, safe_replay - 1),
                    ).fetchone()
                    cursor_log_id = max(0, int(boundary[0] or 0) - 1) if boundary else 0
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO learning_pipeline_checkpoint (
                            pipeline, chat_id, cursor_log_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (pipeline_name, chat_id, cursor_log_id, now, now),
                    )
            initialized = max(0, conn.total_changes - before)
            conn.commit()
        return initialized

    def get_learning_logs(
        self,
        pipeline: str,
        chat_id: str,
        limit: int = 100,
        *,
        replay_recent: int = 0,
    ) -> List[MessageLog]:
        checkpoint = self.ensure_learning_checkpoint(
            pipeline,
            chat_id,
            replay_recent=replay_recent,
        )
        if float(checkpoint.get("retry_at", 0.0) or 0.0) > time.time():
            return []
        cursor_log_id = int(checkpoint.get("cursor_log_id", 0) or 0)
        safe_limit = max(1, min(int(limit or 100), 1000))

        def _sync(session: Session) -> List[MessageLog]:
            statement = (
                select(MessageLog)
                .where(
                    MessageLog.group_id == str(chat_id),
                    MessageLog.id > cursor_log_id,
                )
                .order_by(MessageLog.id.asc())
                .limit(safe_limit)
            )
            return [self._clone_model(item) for item in session.exec(statement).all()]

        return self._run_with_session(_sync)

    def list_learning_checkpoints(
        self,
        *,
        pipeline: str = "",
        chat_id: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if str(pipeline or "").strip():
            filters.append("pipeline = ?")
            params.append(self._normalize_learning_pipeline(pipeline))
        if str(chat_id or "").strip():
            filters.append("chat_id = ?")
            params.append(str(chat_id))
        if str(status or "").strip():
            filters.append("last_status = ?")
            params.append(str(status).strip())
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(int(limit or 100), 1000)))
        params.append(max(0, int(offset or 0)))
        with connect_sqlite(self.persistence.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT pipeline, chat_id, cursor_log_id, last_batch_id,
                       last_status, failure_count, retry_at, last_error,
                       created_at, updated_at
                FROM learning_pipeline_checkpoint
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        keys = (
            "pipeline", "chat_id", "cursor_log_id", "last_batch_id",
            "last_status", "failure_count", "retry_at", "last_error",
            "created_at", "updated_at",
        )
        return [dict(zip(keys, row)) for row in rows]

    def count_learning_checkpoints(
        self,
        *,
        pipeline: str = "",
        chat_id: str = "",
        status: str = "",
    ) -> int:
        filters: list[str] = []
        params: list[Any] = []
        if str(pipeline or "").strip():
            filters.append("pipeline = ?")
            params.append(self._normalize_learning_pipeline(pipeline))
        if str(chat_id or "").strip():
            filters.append("chat_id = ?")
            params.append(str(chat_id))
        if str(status or "").strip():
            filters.append("last_status = ?")
            params.append(str(status).strip())
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        with connect_sqlite(self.persistence.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM learning_pipeline_checkpoint {where_sql}",
                params,
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def list_learning_log_groups(
        self,
        pipeline: str,
        *,
        min_count: int = 1,
        limit: int = 20,
        replay_recent: int = 0,
    ) -> list[dict[str, Any]]:
        pipeline_name = self._normalize_learning_pipeline(pipeline)
        safe_min_count = max(1, int(min_count or 1))
        safe_limit = max(1, min(int(limit or 20), 200))
        self.ensure_learning_checkpoints_for_groups(
            pipeline_name,
            replay_recent=replay_recent,
        )
        with connect_sqlite(self.persistence.db_path) as conn:
            rows = conn.execute(
                """
                SELECT m.group_id, COUNT(*) AS count,
                       MIN(m.timestamp) AS oldest_timestamp,
                       MAX(m.timestamp) AS latest_timestamp
                FROM messagelog AS m
                JOIN learning_pipeline_checkpoint AS c
                  ON c.chat_id = m.group_id AND c.pipeline = ?
                WHERE m.id > c.cursor_log_id
                  AND c.retry_at <= ?
                GROUP BY m.group_id
                HAVING COUNT(*) >= ?
                ORDER BY count DESC, latest_timestamp ASC
                LIMIT ?
                """,
                (pipeline_name, time.time(), safe_min_count, safe_limit),
            ).fetchall()
        return [
            {
                "group_id": str(row[0] or ""),
                "count": int(row[1] or 0),
                "oldest_timestamp": float(row[2] or 0.0),
                "latest_timestamp": float(row[3] or 0.0),
            }
            for row in rows
            if str(row[0] or "")
        ]

    def advance_learning_checkpoint(
        self,
        pipeline: str,
        chat_id: str,
        cursor_log_id: int,
        *,
        batch_id: str = "",
        status: str = "",
        failure_count: int = 0,
        retry_at: float = 0.0,
        last_error: str = "",
    ) -> dict[str, Any]:
        checkpoint = self.ensure_learning_checkpoint(pipeline, chat_id)
        pipeline_name = str(checkpoint["pipeline"])
        normalized_chat_id = str(checkpoint["chat_id"])
        next_cursor = max(int(checkpoint.get("cursor_log_id", 0) or 0), int(cursor_log_id or 0))
        now = time.time()
        with connect_sqlite(self.persistence.db_path) as conn:
            conn.execute(
                """
                UPDATE learning_pipeline_checkpoint
                SET cursor_log_id = ?, last_batch_id = ?, last_status = ?,
                    failure_count = ?, retry_at = ?, last_error = ?, updated_at = ?
                WHERE pipeline = ? AND chat_id = ?
                """,
                (
                    next_cursor,
                    str(batch_id or ""),
                    str(status or ""),
                    max(0, int(failure_count or 0)),
                    max(0.0, float(retry_at or 0.0)),
                    str(last_error or "")[:1000],
                    now,
                    pipeline_name,
                    normalized_chat_id,
                ),
            )
            conn.commit()
        return self.ensure_learning_checkpoint(pipeline_name, normalized_chat_id)

    def reset_learning_checkpoint(
        self,
        pipeline: str,
        chat_id: str,
        *,
        status: str = "manual_retry",
    ) -> dict[str, Any]:
        checkpoint = self.ensure_learning_checkpoint(pipeline, chat_id)
        now = time.time()
        with connect_sqlite(self.persistence.db_path) as conn:
            conn.execute(
                """
                UPDATE learning_pipeline_checkpoint
                SET last_status = ?, failure_count = 0, retry_at = 0,
                    last_error = '', updated_at = ?
                WHERE pipeline = ? AND chat_id = ?
                """,
                (
                    str(status or "manual_retry"),
                    now,
                    str(checkpoint["pipeline"]),
                    str(checkpoint["chat_id"]),
                ),
            )
            conn.commit()
        return self.ensure_learning_checkpoint(
            str(checkpoint["pipeline"]),
            str(checkpoint["chat_id"]),
        )

    def record_learning_mining_run(self, payload: dict[str, Any]) -> str:
        run_id = str(payload.get("run_id") or uuid.uuid4().hex)
        now = float(payload.get("created_at") or time.time())
        details = payload.get("details")
        if details is None:
            details = payload.get("details_json", {})
        if isinstance(details, str):
            details_json = details
        else:
            details_json = json.dumps(details or {}, ensure_ascii=False, default=str)
        with connect_sqlite(self.persistence.db_path) as conn:
            conn.execute(
                """
                INSERT INTO learning_mining_run (
                    run_id, pipeline, chat_id, batch_id, raw_count,
                    normalized_count, required_count, candidate_count,
                    saved_count, deduplicated_count, cursor_before,
                    cursor_after, retained_count, status, reason, duration_ms,
                    model_id, retryable, error_type, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    self._normalize_learning_pipeline(str(payload.get("pipeline") or "")),
                    str(payload.get("chat_id") or ""),
                    str(payload.get("batch_id") or ""),
                    int(payload.get("raw_count") or 0),
                    int(payload.get("normalized_count") or 0),
                    int(payload.get("required_count") or 0),
                    int(payload.get("candidate_count") or 0),
                    int(payload.get("saved_count") or 0),
                    int(payload.get("deduplicated_count") or 0),
                    int(payload.get("cursor_before") or 0),
                    int(payload.get("cursor_after") or 0),
                    int(payload.get("retained_count") or 0),
                    str(payload.get("status") or ""),
                    str(payload.get("reason") or ""),
                    float(payload.get("duration_ms") or 0.0),
                    str(payload.get("model_id") or ""),
                    int(bool(payload.get("retryable", False))),
                    str(payload.get("error_type") or ""),
                    details_json,
                    now,
                ),
            )
            conn.commit()
        return run_id

    def list_learning_mining_runs(
        self,
        *,
        pipeline: str = "",
        chat_id: str = "",
        status: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if str(pipeline or "").strip():
            filters.append("pipeline = ?")
            params.append(self._normalize_learning_pipeline(pipeline))
        if str(chat_id or "").strip():
            filters.append("chat_id = ?")
            params.append(str(chat_id))
        if str(status or "").strip():
            filters.append("status = ?")
            params.append(str(status).strip())
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(max(1, min(int(limit or 100), 1000)))
        params.append(max(0, int(offset or 0)))
        with connect_sqlite(self.persistence.db_path) as conn:
            cursor = conn.execute(
                f"""
                SELECT run_id, pipeline, chat_id, batch_id, raw_count,
                       normalized_count, required_count, candidate_count,
                       saved_count, deduplicated_count, cursor_before,
                       cursor_after, retained_count, status, reason, duration_ms,
                       model_id, retryable, error_type, details_json, created_at
                FROM learning_mining_run
                {where_sql}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                params,
            )
            rows = cursor.fetchall()
        keys = (
            "run_id", "pipeline", "chat_id", "batch_id", "raw_count",
            "normalized_count", "required_count", "candidate_count",
            "saved_count", "deduplicated_count", "cursor_before",
            "cursor_after", "retained_count", "status", "reason",
            "duration_ms", "model_id", "retryable", "error_type",
            "details_json", "created_at",
        )
        return [dict(zip(keys, row)) for row in rows]

    def count_learning_mining_runs(
        self,
        *,
        pipeline: str = "",
        chat_id: str = "",
        status: str = "",
    ) -> int:
        filters: list[str] = []
        params: list[Any] = []
        if str(pipeline or "").strip():
            filters.append("pipeline = ?")
            params.append(self._normalize_learning_pipeline(pipeline))
        if str(chat_id or "").strip():
            filters.append("chat_id = ?")
            params.append(str(chat_id))
        if str(status or "").strip():
            filters.append("status = ?")
            params.append(str(status).strip())
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        with connect_sqlite(self.persistence.db_path) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM learning_mining_run {where_sql}",
                params,
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def purge_learning_mining_runs(
        self,
        *,
        retention_days: int = 30,
        max_per_pipeline_chat: int = 500,
    ) -> dict[str, int | float]:
        safe_days = max(1, int(retention_days or 30))
        safe_max = max(1, int(max_per_pipeline_chat or 500))
        cutoff = time.time() - safe_days * 86400
        with connect_sqlite(self.persistence.db_path) as conn:
            before_row = conn.execute("SELECT COUNT(*) FROM learning_mining_run").fetchone()
            before = int(before_row[0] or 0) if before_row else 0
            age_result = conn.execute(
                "DELETE FROM learning_mining_run WHERE created_at < ?",
                (cutoff,),
            )
            limit_result = conn.execute(
                """
                DELETE FROM learning_mining_run
                WHERE run_id IN (
                    SELECT run_id FROM (
                        SELECT run_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY pipeline, chat_id
                                   ORDER BY created_at DESC, rowid DESC
                               ) AS rank_in_scope
                        FROM learning_mining_run
                    )
                    WHERE rank_in_scope > ?
                )
                """,
                (safe_max,),
            )
            after_row = conn.execute("SELECT COUNT(*) FROM learning_mining_run").fetchone()
            after = int(after_row[0] or 0) if after_row else 0
            conn.commit()
        return {
            "before": before,
            "after": after,
            "deleted_by_age": max(0, int(age_result.rowcount or 0)),
            "deleted_by_limit": max(0, int(limit_result.rowcount or 0)),
            "retention_days": safe_days,
            "max_per_pipeline_chat": safe_max,
            "cutoff": cutoff,
        }

    def mark_logs_processed_through_learning_checkpoints(
        self,
        chat_id: str,
        *,
        pipelines: tuple[str, ...] = ("expression", "jargon"),
    ) -> int:
        normalized = tuple(self._normalize_learning_pipeline(item) for item in pipelines)
        placeholders = ",".join("?" for _ in normalized)
        with connect_sqlite(self.persistence.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT cursor_log_id FROM learning_pipeline_checkpoint
                WHERE chat_id = ? AND pipeline IN ({placeholders})
                """,
                (str(chat_id), *normalized),
            ).fetchall()
            if len(rows) != len(normalized):
                return 0
            safe_cursor = min(int(row[0] or 0) for row in rows)
            result = conn.execute(
                """
                UPDATE messagelog SET processed = 1
                WHERE group_id = ? AND id <= ? AND processed = 0
                """,
                (str(chat_id), safe_cursor),
            )
            conn.commit()
            return max(0, int(result.rowcount or 0))

    def get_chat_state(self, chat_id: str) -> Optional[ChatState]:
        with connect_sqlite(self.persistence.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if not row:
                return None
            actual_col_names = [col[0] for col in (cursor.description or [])]
            now = time.time()
            if (
                not actual_col_names
                or len(actual_col_names) != len(row)
            ):
                cols_cursor = conn.execute("PRAGMA table_info(chat_states)")
                actual_col_names = [col[1] for col in cols_cursor.fetchall()]
            if (
                self._chat_state_cols_cache is None
                or (now - self._chat_state_cols_ts) > _COL_CACHE_TTL_SEC
                or len(self._chat_state_cols_cache) != len(actual_col_names)
                or self._chat_state_cols_cache != actual_col_names
            ):
                self._chat_state_cols_cache = list(actual_col_names)
                self._chat_state_cols_ts = now
            row_dict = dict(zip(actual_col_names, row))
            state = ChatState(
                chat_id=str(row_dict.get("chat_id", chat_id) or chat_id),
                energy=float(row_dict.get("energy", 0.5) or 0.5),
                mood=float(row_dict.get("mood", 0.0) or 0.0),
            )
            state.group_config = self._safe_json_loads(row_dict.get("group_config"))
            state.last_reset_date = str(row_dict.get("last_reset_date", "") or "")
            state.total_replies = int(row_dict.get("total_replies") or 0)
            state.last_reply_time = float(row_dict.get("last_reply_time") or 0.0)
            state.last_passive_decay_time = float(row_dict.get("last_passive_decay_time") or 0.0)
            state.last_energy_recovery_time = float(row_dict.get("last_energy_recovery_time") or 0.0)
            state.total_messages = int(row_dict.get("total_messages") or 0)
            state.judgment_mode = str(row_dict.get("judgment_mode", "single") or "single")
            last_msg_info_raw = self._safe_json_loads(row_dict.get("last_msg_info"), {"sender_id": "", "has_image": False, "image_urls": [], "vl_executed": False})
            state.last_msg_info = LastMessageMetadata(
                sender_id=last_msg_info_raw.get("sender_id", ""),
                has_image=bool(last_msg_info_raw.get("has_image", False)),
                image_urls=last_msg_info_raw.get("image_urls", []),
                vl_executed=bool(last_msg_info_raw.get("vl_executed", False)),
            )
            state.last_access_time = float(row_dict.get("last_access_time") or 0.0)
            state.next_wakeup_timestamp = float(row_dict.get("next_wakeup_timestamp") or 0.0)
            state.chat_kind = str(row_dict.get("chat_kind", "") or "")
            state.last_real_user_activity_at = float(
                row_dict.get("last_real_user_activity_at") or state.last_reply_time or 0.0
            )
            state.last_committed_bot_reply_at = float(
                row_dict.get("last_committed_bot_reply_at") or state.last_reply_time or 0.0
            )
            state.next_proactive_due_at = float(
                row_dict.get("next_proactive_due_at") or state.next_wakeup_timestamp or 0.0
            )
            state.proactive_generation = int(row_dict.get("proactive_generation") or 0)
            state.unanswered_proactive_count = int(row_dict.get("unanswered_proactive_count") or 0)
            state.last_proactive_commit_id = str(row_dict.get("last_proactive_commit_id", "") or "")
            state.last_proactive_cancel_reason = str(row_dict.get("last_proactive_cancel_reason", "") or "")
            state.proactive_claim_token = str(row_dict.get("proactive_claim_token", "") or "")
            state.proactive_claimed_at = float(row_dict.get("proactive_claimed_at") or 0.0)
            state.is_dirty = bool(row_dict.get("is_dirty") or False)
            return state

    async def add_message_log_async(
        self,
        group_id: str,
        sender_id: str,
        sender_name: str,
        content: str,
        *,
        conversation_event: Any = None,
    ):
        return await self._run_blocking(
            self.add_message_log,
            group_id,
            sender_id,
            sender_name,
            content,
            conversation_event=conversation_event,
            with_lock=True,
        )

    async def mark_logs_processed_async(self, log_ids: List[int]):
        return await self._run_blocking(self.mark_logs_processed, log_ids, with_lock=True)

    async def get_unprocessed_logs_async(self, group_id: str, limit: int = 50):
        return await self._run_blocking(self.get_unprocessed_logs, group_id, limit)

    async def get_learning_visual_context_async(
        self,
        chat_id: str,
        message_ids: List[str],
    ) -> dict[str, list[dict[str, str]]]:
        return await self._run_blocking(self.get_learning_visual_context, chat_id, message_ids)

    async def get_learning_logs_async(
        self,
        pipeline: str,
        chat_id: str,
        limit: int = 100,
        *,
        replay_recent: int = 0,
    ):
        return await self._run_blocking(
            self.get_learning_logs,
            pipeline,
            chat_id,
            limit,
            replay_recent=replay_recent,
        )

    async def ensure_learning_checkpoint_async(self, *args, **kwargs):
        return await self._run_blocking(
            self.ensure_learning_checkpoint,
            *args,
            with_lock=True,
            **kwargs,
        )

    async def ensure_learning_checkpoints_for_groups_async(self, *args, **kwargs):
        return await self._run_blocking(
            self.ensure_learning_checkpoints_for_groups,
            *args,
            with_lock=True,
            **kwargs,
        )

    async def list_learning_log_groups_async(
        self,
        pipeline: str,
        *,
        min_count: int = 1,
        limit: int = 20,
        replay_recent: int = 0,
    ):
        return await self._run_blocking(
            self.list_learning_log_groups,
            pipeline,
            min_count=min_count,
            limit=limit,
            replay_recent=replay_recent,
        )

    async def list_learning_checkpoints_async(self, **kwargs):
        return await self._run_blocking(self.list_learning_checkpoints, **kwargs)

    async def count_learning_checkpoints_async(self, **kwargs):
        return await self._run_blocking(self.count_learning_checkpoints, **kwargs)

    async def advance_learning_checkpoint_async(self, *args, **kwargs):
        return await self._run_blocking(
            self.advance_learning_checkpoint,
            *args,
            with_lock=True,
            **kwargs,
        )

    async def reset_learning_checkpoint_async(self, *args, **kwargs):
        return await self._run_blocking(
            self.reset_learning_checkpoint,
            *args,
            with_lock=True,
            **kwargs,
        )

    async def record_learning_mining_run_async(self, payload: dict[str, Any]):
        return await self._run_blocking(
            self.record_learning_mining_run,
            payload,
            with_lock=True,
        )

    async def list_learning_mining_runs_async(self, **kwargs):
        return await self._run_blocking(self.list_learning_mining_runs, **kwargs)

    async def count_learning_mining_runs_async(self, **kwargs):
        return await self._run_blocking(self.count_learning_mining_runs, **kwargs)

    async def purge_learning_mining_runs_async(self, **kwargs):
        return await self._run_blocking(
            self.purge_learning_mining_runs,
            with_lock=True,
            **kwargs,
        )

    async def mark_logs_processed_through_learning_checkpoints_async(self, chat_id: str, **kwargs):
        return await self._run_blocking(
            self.mark_logs_processed_through_learning_checkpoints,
            chat_id,
            with_lock=True,
            **kwargs,
        )

    async def list_unprocessed_log_groups_async(self, *, min_count: int = 1, limit: int = 20):
        return await self._run_blocking(
            self.list_unprocessed_log_groups,
            min_count=min_count,
            limit=limit,
        )

    async def get_recent_message_logs_async(
        self,
        group_id: str,
        limit: int = 8,
        max_age_seconds: Optional[float] = None,
        include_processed: bool = True,
    ):
        return await self._run_blocking(
            self.get_recent_message_logs,
            group_id,
            limit,
            max_age_seconds,
            include_processed,
        )

    async def get_chat_state_async(self, chat_id: str):
        return await asyncio.to_thread(self.get_chat_state, chat_id)
