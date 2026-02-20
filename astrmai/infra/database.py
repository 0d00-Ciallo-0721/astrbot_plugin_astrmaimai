from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session, select
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
import os

# --- Data Models (Inferred from HeartCore/state_manager.py) ---

class ChatState(SQLModel, table=True):
    """群聊心流状态表"""
    chat_id: str = Field(primary_key=True)
    energy: float = Field(default=0.5)         # 精力值 (0.0 - 1.0)
    mood: float = Field(default=0.0)           # 情绪值 (-1.0 - 1.0)
    last_reply_time: float = Field(default=0.0) # 上次回复时间戳
    last_reset_date: str = Field(default="")   # 上次重置日期 (ISO)
    total_replies: int = Field(default=0)
    # 运行时字段 (is_dirty, lock) 不入库

class UserProfile(SQLModel, table=True):
    """用户画像表"""
    user_id: str = Field(primary_key=True)
    name: str = Field(default="Unknown")
    social_score: float = Field(default=0.0)   # 社交好感度
    last_seen: float = Field(default=0.0)

# --- Database Service ---

class DatabaseService:
    """
    统一数据库服务 (Infrastructure Layer)
    使用 SQLModel (SQLAlchemy) 管理 SQLite 连接
    """
    def __init__(self):
        # 存储路径: data/plugin_data/astrmai/astrmai.db
        base_path = get_astrbot_data_path() / "plugin_data" / "astrmai"
        if not os.path.exists(base_path):
            os.makedirs(base_path)
            
        db_url = f"sqlite:///{base_path}/astrmai.db"
        self.engine = create_engine(db_url)
        
        # 初始化建表
        SQLModel.metadata.create_all(self.engine)
        logger.info(f"[AstrMai] Database connected at {db_url}")

    def get_session(self) -> Session:
        return Session(self.engine)

    # 简单的 CRUD 封装，供 StateManager 使用
    def get_chat_state(self, chat_id: str) -> Optional[ChatState]:
        with self.get_session() as session:
            return session.get(ChatState, chat_id)

    def save_chat_state(self, state: ChatState):
        with self.get_session() as session:
            session.add(state)
            session.commit()
            session.refresh(state)


# ... (Previous imports)
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session, select, desc
import time

# --- Existing Models (ChatState, UserProfile) ---
# ... (Keep existing models)

# --- New Models for Evolution ---

class ExpressionPattern(SQLModel, table=True):
    """
    表达模式表 (Source: Self_Learning)
    存储挖掘出的说话习惯，如 "当[场景]时，使用[表达]"
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    situation: str = Field(index=True)  # 场景描述
    expression: str                     # 表达方式
    weight: float = Field(default=1.0)  # 权重/频率
    last_active_time: float = Field(default_factory=time.time)
    create_time: float = Field(default_factory=time.time)
    group_id: str = Field(index=True)

class MessageLog(SQLModel, table=True):
    """
    短期消息日志 (用于风格挖掘的 Raw Data)
    注意：这不同于 Memory 层的长期记忆，这是滚动的短期窗口。
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    sender_id: str
    sender_name: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    processed: bool = Field(default=False) # 是否已被挖掘过

# --- Updated Database Service ---

class DatabaseService:
    # ... (Previous __init__ and basic methods)

    # --- Evolution Methods ---

    def add_message_log(self, group_id: str, sender_id: str, sender_name: str, content: str):
        with self.get_session() as session:
            log = MessageLog(
                group_id=group_id, 
                sender_id=sender_id, 
                sender_name=sender_name, 
                content=content
            )
            session.add(log)
            session.commit()

    def get_unprocessed_logs(self, group_id: str, limit: int = 50) -> list[MessageLog]:
        with self.get_session() as session:
            statement = select(MessageLog).where(
                MessageLog.group_id == group_id,
                MessageLog.processed == False
            ).order_by(MessageLog.timestamp.desc()).limit(limit)
            results = session.exec(statement).all()
            return list(reversed(results)) # 返回按时间正序

    def mark_logs_processed(self, log_ids: list[int]):
        with self.get_session() as session:
            for lid in log_ids:
                log = session.get(MessageLog, lid)
                if log:
                    log.processed = True
                    session.add(log)
            session.commit()

    def save_pattern(self, pattern: ExpressionPattern):
        with self.get_session() as session:
            # 查重逻辑: 同群组下相同的 situation 和 expression
            statement = select(ExpressionPattern).where(
                ExpressionPattern.group_id == pattern.group_id,
                ExpressionPattern.situation == pattern.situation,
                ExpressionPattern.expression == pattern.expression
            )
            existing = session.exec(statement).first()
            
            if existing:
                existing.weight += 1.0
                existing.last_active_time = time.time()
                session.add(existing)
            else:
                session.add(pattern)
            session.commit()

    def get_patterns(self, group_id: str, limit: int = 5) -> list[ExpressionPattern]:
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(
                ExpressionPattern.group_id == group_id
            ).order_by(desc(ExpressionPattern.weight)).limit(limit)
            return session.exec(statement).all()














            