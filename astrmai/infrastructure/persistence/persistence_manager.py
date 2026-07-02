# astrmai/infra/persistence.py
from pathlib import Path
from typing import Any
from sqlmodel import SQLModel, create_engine, Session
from astrbot.api import logger



from .persona_cache import PersonaCacheMixin
from .persistence_schema import PersistenceSchemaMixin, _dedupe_sqlmodel_metadata_indexes
from .state_profile_persistence import StateProfilePersistenceMixin


class PersistenceManager(PersistenceSchemaMixin, PersonaCacheMixin, StateProfilePersistenceMixin):
    """Persistence manager for refactored local storage."""
    def __init__(self):
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            base_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai"
        except ImportError:
            base_path = Path("data") / "plugin_data" / "astrmai"
            logger.warning("[AstrMai] get_astrbot_data_path not available, using relative path: " + str(base_path))
        base_path.mkdir(parents=True, exist_ok=True)
        
        self.db_path = base_path / "astrmai.db"
        self.db_url = f"sqlite:///{self.db_path}"
        
        # 
        self.cache_dir = base_path / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.persona_cache_path = self.cache_dir / "persona_cache.json"
        
        # ?Engine ( Vector Store ?
        self.engine = create_engine(self.db_url)
        _dedupe_sqlmodel_metadata_indexes()
        SQLModel.metadata.create_all(self.engine)
        
        # 
        self._init_task = None
        self.database_service = None
        from . import orm_models
        self.orm_models = orm_models
        self._schedule_init_db()
        logger.info(f"[AstrMai-Infra]  Database connected & mounted at {self.db_path}")

    def get_session(self) -> Session:
        """Return a SQLModel session for compatibility callers."""
        return Session(self.engine)

    def bind_database_service(self, database_service: Any) -> None:
        self.database_service = database_service

    # ponytail: release SQLAlchemy connection pool
    def dispose(self):
        self.engine.dispose()

