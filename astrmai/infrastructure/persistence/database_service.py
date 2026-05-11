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
from .orm_models import ChatState, MessageLog
from .persistence_manager import PersistenceManager

T = TypeVar("T")


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
            state = ChatState(chat_id=row[0], energy=row[1], mood=row[2])
            state.group_config = json.loads(row[3]) if row[3] else {}
            state.last_reset_date = row[4]
            state.total_replies = row[5]
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
