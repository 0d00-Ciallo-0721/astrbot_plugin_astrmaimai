import asyncio
import json
import time
from typing import Any, Callable, List, Optional, TypeVar

from sqlmodel import Session, select

from .database_cron import CronPersistenceMixin
from .database_jargon import JargonPersistenceMixin
from .database_memory import MemoryPersistenceMixin
from .database_profile_relation import ProfileRelationPersistenceMixin
from .database_review import ReviewPersistenceMixin
from .orm_models import ChatState, LastMessageMetadata, MessageLog
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

    def add_message_log(self, group_id: str, sender_id: str, sender_name: str, content: str):
        def _sync(session: Session) -> None:
            session.add(
                MessageLog(
                    group_id=group_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    content=content,
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
            state.is_dirty = bool(row_dict.get("is_dirty") or False)
            return state

    async def add_message_log_async(self, group_id: str, sender_id: str, sender_name: str, content: str):
        return await self._run_blocking(self.add_message_log, group_id, sender_id, sender_name, content, with_lock=True)

    async def mark_logs_processed_async(self, log_ids: List[int]):
        return await self._run_blocking(self.mark_logs_processed, log_ids, with_lock=True)

    async def get_unprocessed_logs_async(self, group_id: str, limit: int = 50):
        return await self._run_blocking(self.get_unprocessed_logs, group_id, limit)

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
