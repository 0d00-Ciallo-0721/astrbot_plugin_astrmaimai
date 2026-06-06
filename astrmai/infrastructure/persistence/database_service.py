import asyncio
import json
import sqlite3
import time
from typing import Any, Callable, List, Optional, TypeVar

from sqlmodel import Session, select

from .database_cron import CronPersistenceMixin
from .database_jargon import JargonPersistenceMixin
from .database_memory import MemoryPersistenceMixin
from .database_profile_relation import ProfileRelationPersistenceMixin
from .database_review import ReviewPersistenceMixin
from .orm_models import ChatState, LastMessageMetadata, MessageLog
from .persistence_manager import PersistenceManager

T = TypeVar("T")

# Cache PRAGMA table_info(chat_states) result to avoid repeated schema queries
_CHAT_STATES_COLUMNS: list[str] | None = None


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

    def get_recent_message_logs(
        self,
        group_id: str,
        limit: int = 8,
        max_age_seconds: Optional[float] = None,
        include_processed: bool = True,
    ) -> List[MessageLog]:
        cutoff_timestamp = None
        if max_age_seconds is not None and max_age_seconds > 0:
            cutoff_timestamp = time.time() - float(max_age_seconds)

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
        with sqlite3.connect(self.persistence.db_path) as conn:
            cursor = conn.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if not row:
                return None
            global _CHAT_STATES_COLUMNS
            if _CHAT_STATES_COLUMNS is None:
                cols_cursor = conn.execute("PRAGMA table_info(chat_states)")
                _CHAT_STATES_COLUMNS = [col[1] for col in cols_cursor.fetchall()]
            col_names = _CHAT_STATES_COLUMNS
            row_dict = dict(zip(col_names, row))
            state = ChatState(
                chat_id=str(row_dict.get("chat_id", chat_id) or chat_id),
                energy=float(row_dict.get("energy", 0.5) or 0.5),
                mood=float(row_dict.get("mood", 0.0) or 0.0),
            )
            state.group_config = json.loads(row_dict.get("group_config") or "{}")
            state.last_reset_date = str(row_dict.get("last_reset_date", "") or "")
            state.total_replies = int(row_dict.get("total_replies") or 0)
            state.last_reply_time = float(row_dict.get("last_reply_time") or 0.0)
            state.last_passive_decay_time = float(row_dict.get("last_passive_decay_time") or 0.0)
            state.total_messages = int(row_dict.get("total_messages") or 0)
            state.judgment_mode = str(row_dict.get("judgment_mode", "single") or "single")
            last_msg_info_raw = json.loads(row_dict.get("last_msg_info") or "{}")
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
