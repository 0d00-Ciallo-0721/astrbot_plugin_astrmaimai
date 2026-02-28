import time
import sqlite3
import json
from typing import List, Optional
from sqlmodel import Session, select, desc
from .datamodels import ExpressionPattern, MessageLog, ChatState, Jargon, SocialRelation
from .persistence import PersistenceManager

class DatabaseService:
    """
    数据库服务层 (向下兼容代理)
    职责：包装 PersistenceManager，给 Memory/Evolution 等尚未重构的模块提供旧版同步接口
    """
    def __init__(self, persistence: PersistenceManager):
        self.persistence = persistence

    def get_session(self) -> Session:
        return self.persistence.get_session()

    # ==========================================
    # 兼容 API: 供 Evolution / Memory 模块使用
    # ==========================================
    def add_message_log(self, group_id: str, sender_id: str, sender_name: str, content: str):
        with self.get_session() as session:
            log = MessageLog(group_id=group_id, sender_id=sender_id, sender_name=sender_name, content=content)
            session.add(log)
            session.commit()

    def get_unprocessed_logs(self, group_id: str, limit: int = 50) -> List[MessageLog]:
        with self.get_session() as session:
            statement = select(MessageLog).where(
                MessageLog.group_id == group_id,
                MessageLog.processed == False
            ).order_by(MessageLog.timestamp.desc()).limit(limit)
            results = session.exec(statement).all()
            return list(reversed(results))

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
            session.refresh(target)
            _ = target.situation 
            _ = target.expression

    def get_patterns(self, group_id: str, limit: int = 5) -> List[ExpressionPattern]:
        with self.get_session() as session:
            statement = select(ExpressionPattern).where(
                ExpressionPattern.group_id == group_id
            ).order_by(desc(ExpressionPattern.weight)).limit(limit)
            return session.exec(statement).all()

    # ==========================================
    # 临时过渡 API: 供 ContextEngine 使用
    # (这部分将在阶段四全面改写为异步缓存读取)
    # ==========================================
    def get_chat_state(self, chat_id: str) -> Optional[ChatState]:
        """使用 sqlite3 同步读取持久化文件，供老模块兼容读取"""
        with sqlite3.connect(self.persistence.db_path) as conn:
            cursor = conn.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if row:
                state = ChatState(chat_id=row[0], energy=row[1], mood=row[2])
                state.group_config = json.loads(row[3]) if row[3] else {}
                state.last_reset_date = row[4]
                state.total_replies = row[5]
                return state
        return None
    


    def save_jargon(self, jargon: Jargon):
        """[新增] 保存或更新黑话，基于内容和群组防重"""
        with self.get_session() as session:
            statement = select(Jargon).where(
                Jargon.group_id == jargon.group_id,
                Jargon.content == jargon.content
            )
            existing = session.exec(statement).first()
            if existing:
                existing.count += 1
                existing.updated_at = time.time()
                # 如果传入了新的推断含义，则更新它
                if jargon.meaning:
                    existing.meaning = jargon.meaning
                    existing.is_complete = jargon.is_complete
                    existing.is_jargon = jargon.is_jargon
                session.add(existing)
            else:
                session.add(jargon)
            session.commit()

    def get_jargons(self, group_id: str, limit: int = 20, only_confirmed: bool = True) -> List[Jargon]:
        """[新增] 获取群组的黑话列表，供 Brain 检索使用"""
        with self.get_session() as session:
            statement = select(Jargon).where(Jargon.group_id == group_id)
            if only_confirmed:
                statement = statement.where(Jargon.is_jargon == True)
            statement = statement.order_by(desc(Jargon.updated_at)).limit(limit)
            return session.exec(statement).all()

    def update_social_relation(self, group_id: str, from_user: str, to_user: str, relation_type: str, strength_delta: float):
        """[新增] 更新成员间的互动图谱强度"""
        with self.get_session() as session:
            statement = select(SocialRelation).where(
                SocialRelation.group_id == group_id,
                SocialRelation.from_user == from_user,
                SocialRelation.to_user == to_user,
                SocialRelation.relation_type == relation_type
            )
            existing = session.exec(statement).first()
            if existing:
                existing.strength = min(1.0, max(0.0, existing.strength + strength_delta))
                existing.frequency += 1
                existing.last_interaction = time.time()
                session.add(existing)
            else:
                new_relation = SocialRelation(
                    group_id=group_id,
                    from_user=from_user,
                    to_user=to_user,
                    relation_type=relation_type,
                    strength=min(1.0, max(0.0, strength_delta)),
                    frequency=1
                )
                session.add(new_relation)
            session.commit()

    def get_user_relations(self, group_id: str, user_id: str) -> List[SocialRelation]:
        """[新增] 提取某用户相关的社交关系（双向）"""
        with self.get_session() as session:
            statement = select(SocialRelation).where(
                (SocialRelation.group_id == group_id) & 
                ((SocialRelation.from_user == user_id) | (SocialRelation.to_user == user_id))
            ).order_by(desc(SocialRelation.strength))
            return session.exec(statement).all()