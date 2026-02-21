import os
import time
from typing import Optional, List
from pathlib import Path
from sqlmodel import SQLModel, Field, create_engine, Session, select, desc
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# ==========================================
# 1. Data Models (数据模型)
# ==========================================

class ChatState(SQLModel, table=True):
    """群聊/私聊心流状态表 (System 1 生理状态)"""
    __table_args__ = {"extend_existing": True} 
    chat_id: str = Field(primary_key=True)
    energy: float = Field(default=0.5)         # 精力值 (0.0 - 1.0)
    mood: float = Field(default=0.0)           # 情绪值 (-1.0 - 1.0)
    last_reply_time: float = Field(default=0.0) # 上次回复时间戳
    last_reset_date: str = Field(default="")   # 上次重置日期 (ISO)
    total_replies: int = Field(default=0)

class UserProfile(SQLModel, table=True):
    """用户画像与好感度表"""
    __table_args__ = {"extend_existing": True}
    user_id: str = Field(primary_key=True)
    name: str = Field(default="Unknown")
    social_score: float = Field(default=0.0)   # 社交好感度 (-100 to 100)
    last_seen: float = Field(default_factory=time.time)

class ExpressionPattern(SQLModel, table=True):
    """表达模式表 (潜意识挖掘的黑话与句式)"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    situation: str = Field(index=True)  # 场景描述
    expression: str                     # 表达方式
    weight: float = Field(default=1.0)  # 权重/频率
    last_active_time: float = Field(default_factory=time.time)
    create_time: float = Field(default_factory=time.time)
    group_id: str = Field(index=True)

class MessageLog(SQLModel, table=True):
    """短期滚动消息日志 (用于后台离线挖掘)"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    sender_id: str
    sender_name: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    processed: bool = Field(default=False) # 是否已被挖掘过

# ==========================================
# 2. Database Service (持久化基座)
# ==========================================

class DatabaseService:
    def __init__(self):
        # 统一存储路径: data/plugin_data/astrmai/astrmai.db
        base_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai"
        os.makedirs(base_path, exist_ok=True)
            
        if not os.path.exists(base_path):
            os.makedirs(base_path, exist_ok=True)
            
        db_url = f"sqlite:///{base_path}/astrmai.db"
        self.engine = create_engine(db_url)
        
        # 初始化建表
        SQLModel.metadata.create_all(self.engine)
        logger.info(f"[AstrMai] 💾 Database connected at {db_url}")

    def get_session(self) -> Session:
        return Session(self.engine)

    # --- State & Profile API ---
    def get_chat_state(self, chat_id: str) -> Optional[ChatState]:
        with self.get_session() as session:
            return session.get(ChatState, chat_id)

    def save_chat_state(self, state: ChatState):
        with self.get_session() as session:
            session.add(state)
            session.commit()
            session.refresh(state)

    def get_user_profile(self, user_id: str) -> Optional[UserProfile]:
        with self.get_session() as session:
            return session.get(UserProfile, user_id)

    def save_user_profile(self, profile: UserProfile):
        with self.get_session() as session:
            session.add(profile)
            session.commit()
            session.refresh(profile)

    # --- Subconscious Evolution API ---
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

    def get_unprocessed_logs(self, group_id: str, limit: int = 50) -> List[MessageLog]:
        with self.get_session() as session:
            statement = select(MessageLog).where(
                MessageLog.group_id == group_id,
                MessageLog.processed == False
            ).order_by(MessageLog.timestamp.desc()).limit(limit)
            results = session.exec(statement).all()
            return list(reversed(results)) # 返回按时间正序

    def mark_logs_processed(self, log_ids: List[int]):
        with self.get_session() as session:
            for lid in log_ids:
                log = session.get(MessageLog, lid)
                if log:
                    log.processed = True
                    session.add(log)
            session.commit()

    def save_pattern(self, pattern: ExpressionPattern):
        with self.get_session() as session:
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
                target = existing
            else:
                session.add(pattern)
                target = pattern
                
            session.commit()
            # 【核心修复】在 Session 关闭前刷新对象，并访问属性以强制加载到内存
            session.refresh(target)
            _ = target.situation 
            _ = target.expression

    def get_patterns(self, group_id: str, limit: int = 5) -> List[ExpressionPattern]:
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(
                ExpressionPattern.group_id == group_id
            ).order_by(desc(ExpressionPattern.weight)).limit(limit)
            return session.exec(statement).all()