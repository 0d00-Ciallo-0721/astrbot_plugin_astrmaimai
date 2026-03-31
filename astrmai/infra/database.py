import time
import sqlite3
import json
from typing import List, Optional
from sqlmodel import Session, select, desc
from .datamodels import ExpressionPattern, MessageLog, ChatState, Jargon, SocialRelation
from .persistence import PersistenceManager
import asyncio
from astrbot.api.event import AstrMessageEvent

class DatabaseService:
    """
    数据库服务层 (向下兼容代理)
    职责：包装 PersistenceManager，给 Memory/Evolution 等尚未重构的模块提供旧版同步接口
    """
    def __init__(self, persistence: PersistenceManager): # [修改]
        self.persistence = persistence
        # [彻底修复 Bug 1] 移除同步环境下的 asyncio.Lock() 实例化，改为惰性初始化占位符，避免 RuntimeError
        self._db_lock_instance = None

    @property # [新增]
    def _db_lock(self):
        """[新增] 惰性获取绑定到当前事件循环的 Lock"""
        import asyncio
        if self._db_lock_instance is None:
            self._db_lock_instance = asyncio.Lock()
        return self._db_lock_instance


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
            # 🟢 [深层修复 Bug 2] 跨线程脱水，完全切断延迟加载和线程绑定的 Session 属性
            return [MessageLog.model_validate(r.model_dump()) for r in reversed(results)]


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
            results = session.exec(statement).all()
            # 🟢 [深层修复 Bug 2] 跨线程脱水
            return [ExpressionPattern.model_validate(r.model_dump()) for r in results]


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
                target = existing
            else:
                session.add(jargon)
                target = jargon
                
            session.commit()
            session.refresh(target)
            
            # 【核心修复】脱水处理 (Detachment Fix)
            # 强制触发对象的属性加载，将数据固化到本地内存中。
            # 这样即使退出了 with 代码块 (Session 断开)，外部处理器仍能安全访问，不会引发 Not Bound to Session 错误。
            _ = target.is_jargon
            _ = target.is_complete
            _ = target.content
            _ = target.meaning
            
            # 将值同步回原始对象，确保外部直接引用 jargon 时不会报错
            jargon.is_jargon = target.is_jargon
            jargon.is_complete = target.is_complete
            jargon.content = target.content
            jargon.meaning = target.meaning
            

    def get_jargons(self, group_id: str, limit: int = 20, only_confirmed: bool = True) -> List[Jargon]:
        """[新增] 获取群组的黑话列表，供 Brain 检索使用"""
        with self.get_session() as session:
            statement = select(Jargon).where(Jargon.group_id == group_id)
            if only_confirmed:
                statement = statement.where(Jargon.is_jargon == True)
            statement = statement.order_by(desc(Jargon.updated_at)).limit(limit)
            results = session.exec(statement).all()
            # 🟢 [深层修复 Bug 2] 跨线程脱水
            return [Jargon.model_validate(r.model_dump()) for r in results]


    def get_recent_jargons(self, group_id: str, hours: int = 24) -> List[Jargon]:
        """[新增] 获取最近 N 小时内该群新学会的黑话"""
        with self.get_session() as session:
            cutoff_time = time.time() - (hours * 3600)
            statement = select(Jargon).where(
                Jargon.group_id == group_id,
                Jargon.is_jargon == True,
                Jargon.updated_at >= cutoff_time
            ).order_by(desc(Jargon.updated_at))
            results = session.exec(statement).all()
            # 🟢 [深层修复 Bug 2] 跨线程脱水
            return [Jargon.model_validate(r.model_dump()) for r in results]

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
            results = session.exec(statement).all()
            # 🟢 [深层修复 Bug 2] 跨线程脱水
            return [SocialRelation.model_validate(r.model_dump()) for r in results]


    async def resolve_entity_spatio_temporal(
        self, 
        target_name: str, 
        current_event, # AstrMessageEvent
        astr_ctx=None  # AstrBot Context, 用于阶段二的会话历史拉取
    ) -> Optional[tuple[str, str]]:
        """
        [修改] 时空双维度实体解析器 (增强正则提取版 + 当前事件 At 组件扫描兜底)
        支持直接提取大模型传入的带有 (QQ) 的格式，极大提升实体解析成功率。
        """
        if not target_name or not current_event:
            return None

        group_id = current_event.get_group_id()
        if not group_id:
            group_id = current_event.unified_msg_origin
        group_id = str(group_id)

        # 🟢 1. 预处理：剔除可能携带的 '@' 符号和两端空格
        target_name = target_name.strip().lstrip('@')
        clean_name = target_name

        import re

        # 🟢 2. 拦截模式 A：大模型直接传入了纯数字 ID
        if target_name.isdigit():
            return (target_name, group_id)

        # 🟢 3. 拦截模式 B：大模型传入了 "姓名(ID)" 或 "姓名（ID）" 格式
        # 正则匹配：任意字符开头，接着是半角/全角左括号，中间是纯数字，以半角/全角右括号结尾
        match = re.search(r'^(.*?)[\(（]([0-9]+)[\)）]$', target_name)
        if match:
            extracted_name = match.group(1).strip()
            extracted_id = match.group(2).strip()
            # 既然大模型已经把 ID 完整传过来了，直接采信，O(1) 返回，免去后续遍历查库
            return (extracted_id, group_id)
            
        # 🟢 [新增] 拦截模式 B.5：物理环境兜底检查 (扫描用户当前消息中的 @ 组件)
        # 解决群友主动要求机器人去戳另一个人的场景 (如 "帮我戳 @某某")，即使大模型变笨只传了名字，也能锁头定位。
        import astrbot.api.message_components as Comp
        if current_event.message_obj and hasattr(current_event.message_obj, 'message'):
            at_targets = []
            self_id = str(current_event.get_self_id())
            for comp in current_event.message_obj.message:
                if isinstance(comp, Comp.At):
                    at_qq = str(comp.qq)
                    # 排除机器人自己
                    if at_qq != self_id:
                        at_targets.append(at_qq)
            
            # 如果用户的消息里只明确 @ 了一个人，极大概率就是大模型想要锁定的目标
            if len(at_targets) == 1:
                return (at_targets[0], group_id)

        # 🟢 4. 拦截模式 C：如果只是单纯的姓名，继续走下方的时空搜索逻辑
        # 注意：后续的判断都要使用 clean_name 进行比对
        if current_event.get_sender_name() == clean_name:
            return (str(current_event.get_sender_id()), group_id)

        window_events = current_event.get_extra("astrmai_window_events", [])
        for w_event in reversed(window_events):
            if w_event.get_sender_name() == clean_name:
                return (str(w_event.get_sender_id()), group_id)

        if astr_ctx and hasattr(astr_ctx, 'conversation_manager'):
            try:
                conv_mgr = astr_ctx.conversation_manager
                uid = current_event.unified_msg_origin
                curr_cid = await conv_mgr.get_curr_conversation_id(uid)
                conversation = await conv_mgr.get_conversation(uid, curr_cid)
                
                if conversation and hasattr(conversation, "history") and conversation.history:
                    for msg_data in reversed(conversation.history):
                        sender_name = ""
                        sender_id = ""
                        if isinstance(msg_data, dict):
                            sender_name = msg_data.get("sender", {}).get("nickname", "") or msg_data.get("name", "")
                            sender_id = msg_data.get("sender", {}).get("user_id", "")
                        elif hasattr(msg_data, "sender"):
                            sender_name = getattr(msg_data.sender, "nickname", getattr(msg_data.sender, "name", ""))
                            sender_id = getattr(msg_data.sender, "user_id", "")
                            
                        # [修改] 使用 clean_name 比较
                        if sender_name == clean_name and sender_id:
                            return (str(sender_id), group_id)
            except Exception as e:
                pass

        # [修改] 数据库兜底也使用 clean_name
        def _sync_db_fallback():
            from sqlmodel import select
            with self.get_session() as session:
                statement = select(MessageLog.sender_id).where(
                    MessageLog.group_id == group_id,
                    MessageLog.sender_name == clean_name
                ).distinct()
                results = session.exec(statement).all()
                if len(results) == 1:
                    return (str(results[0]), group_id)
                return None

        import asyncio
        return await asyncio.to_thread(_sync_db_fallback)

    def update_nodes(self, nodes: List['MemoryNode']):
        with self.get_session() as session:
            from sqlmodel import select
            from .datamodels import MemoryNode  # [修复] 补充缺失的导入
            for node in nodes:
                statement = select(MemoryNode).where(MemoryNode.name == node.name)
                existing = session.exec(statement).first()
                if existing:
                    existing.type = node.type
                    existing.description = node.description
                    existing.last_updated = time.time()
                    session.add(existing)
                else:
                    session.add(node)
            session.commit()

    def search_nodes(self, query: str, limit: int = 3, include_description: bool = True) -> List['MemoryNode']:
        with self.get_session() as session:
            from sqlmodel import select, or_
            from .datamodels import MemoryNode  # [修复] 补充缺失的导入
            lower_query = f"%{query.lower()}%"
            # 优先匹配名字，可选匹配描述
            conditions = [MemoryNode.name.like(lower_query)]
            if include_description:
                conditions.append(MemoryNode.description.like(lower_query))
                
            statement = select(MemoryNode).where(or_(*conditions)).order_by(MemoryNode.last_updated.desc()).limit(limit)
            results = session.exec(statement).all()
            # 跨线程脱水处理
            return [MemoryNode.model_validate(r.model_dump()) for r in results]

    def save_reflection(self, date: str, reflection: str):
        with self.get_session() as session:
            from sqlmodel import select
            from .datamodels import DailyReflection  # [修复] 补充缺失的导入
            statement = select(DailyReflection).where(DailyReflection.date == date)
            existing = session.exec(statement).first()
            if existing:
                existing.reflection = reflection
            else:
                new_ref = DailyReflection(date=date, reflection=reflection)
                session.add(new_ref)
            session.commit()


    def get_reflection(self, date: str) -> Optional['DailyReflection']:
        with self.get_session() as session:
            from sqlmodel import select
            from .datamodels import DailyReflection  # [修复] 补充缺失的导入
            statement = select(DailyReflection).where(DailyReflection.date == date)
            res = session.exec(statement).first()
            if res:
                return DailyReflection.model_validate(res.model_dump())
            return None

    def save_event(self, event: 'MemoryEvent'):
        with self.get_session() as session:
            from sqlmodel import select
            from .datamodels import MemoryEvent  # [修复] 补充缺失的导入
            statement = select(MemoryEvent).where(MemoryEvent.event_id == event.event_id)
            existing = session.exec(statement).first()
            if existing:
                existing.narrative = event.narrative
                existing.emotion = event.emotion
                existing.importance = event.importance
                existing.emotional_intensity = event.emotional_intensity
                existing.reflection = event.reflection
                existing.tags = event.tags
                session.add(existing)
            else:
                session.add(event)
            session.commit()


    async def save_cron_snapshot(self, snapshot: 'CronSnapshot') -> None:
        """写入或更新 Cron 任务快照（在 CronAgent 成功创建任务后调用）"""
        import asyncio
        from .datamodels import CronSnapshot
        import time
        
        def _sync():
            with self.get_session() as session:
                existing = session.get(CronSnapshot, snapshot.job_id)
                if existing:
                    existing.is_active = snapshot.is_active
                    existing.updated_at = time.time()
                    session.add(existing)
                else:
                    snapshot.updated_at = time.time()
                    session.add(snapshot)
                session.commit()
        
        async with self._db_lock:
            await asyncio.to_thread(_sync)

    async def get_all_active_cron_snapshots(self) -> list:
        """获取所有 is_active=True 的快照（开机自愈时使用）"""
        import asyncio
        from .datamodels import CronSnapshot
        from sqlmodel import select
        
        def _sync():
            with self.get_session() as session:
                stmt = select(CronSnapshot).where(CronSnapshot.is_active == True)
                results = session.exec(stmt).all()
                return [CronSnapshot.model_validate(r.model_dump()) for r in results]
        
        return await asyncio.to_thread(_sync)

    async def deactivate_cron_snapshot(self, job_id: str) -> None:
        """注销快照（任务完成或被删除后调用）"""
        import asyncio
        from .datamodels import CronSnapshot
        import time
        
        def _sync():
            with self.get_session() as session:
                snap = session.get(CronSnapshot, job_id)
                if snap:
                    snap.is_active = False
                    snap.updated_at = time.time()
                    session.add(snap)
                    session.commit()
        
        async with self._db_lock:
            await asyncio.to_thread(_sync)


    async def save_jargon_async(self, jargon: Jargon):
        """[新增] 将同步写库操作推入线程池，释放主事件循环，防止高并发假死"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.save_jargon, jargon)            
    
    async def add_message_log_async(self, group_id: str, sender_id: str, sender_name: str, content: str):
        """[新增] 异步记录消息，防止高频聊天阻塞事件循环"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.add_message_log, group_id, sender_id, sender_name, content)

    async def mark_logs_processed_async(self, log_ids: List[int]):
        """[新增] 异步标记日志"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.mark_logs_processed, log_ids)

    async def save_pattern_async(self, pattern: ExpressionPattern):
        """[新增] 异步保存表达模式"""
        import asyncio
        async with self._db_lock:
                return await asyncio.to_thread(self.save_pattern, pattern)
        
    async def save_jargon_async(self, jargon: Jargon):
        """[新增] 将同步写库操作推入线程池，释放主事件循环"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.save_jargon, jargon)    
    
    async def get_recent_jargons_async(self, group_id: str, hours: int = 24) -> List[Jargon]:
        """[新增] 异步获取最近 N 小时内该群新学会的黑话，防止后台轮询卡死事件循环"""
        import asyncio
        return await asyncio.to_thread(self.get_recent_jargons, group_id, hours)
    
    async def get_unprocessed_logs_async(self, group_id: str, limit: int = 50) -> List[MessageLog]:
        """[新增] 异步获取未处理日志，防止高频聊天时触发挖掘检查卡死"""
        import asyncio
        return await asyncio.to_thread(self.get_unprocessed_logs, group_id, limit)

    async def get_patterns_async(self, group_id: str, limit: int = 5) -> List[ExpressionPattern]:
        """[新增] 异步获取高频句式"""
        import asyncio
        return await asyncio.to_thread(self.get_patterns, group_id, limit)

    async def get_jargons_async(self, group_id: str, limit: int = 20, only_confirmed: bool = True) -> List[Jargon]:
        """[新增] 异步获取群组黑话，供 ContextEngine 组装 Prompt 使用"""
        import asyncio
        return await asyncio.to_thread(self.get_jargons, group_id, limit, only_confirmed)

    async def update_social_relation_async(self, group_id: str, from_user: str, to_user: str, relation_type: str, strength_delta: float):
        """[新增] 异步更新成员互动图谱"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.update_social_relation, group_id, from_user, to_user, relation_type, strength_delta)

    async def get_user_relations_async(self, group_id: str, user_id: str) -> List[SocialRelation]:
        """[新增] 异步提取某用户相关的社交关系"""
        import asyncio
        return await asyncio.to_thread(self.get_user_relations, group_id, user_id)
        
    async def get_chat_state_async(self, chat_id: str) -> Optional[ChatState]:
        """[新增] 异步获取聊天状态的兜底兼容接口"""
        import asyncio
        return await asyncio.to_thread(self.get_chat_state, chat_id)    
    
    async def update_nodes_async(self, nodes: List['MemoryNode']):
        """[新增] 异步更新记忆节点"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.update_nodes, nodes)

    async def search_nodes_async(self, query: str, limit: int = 3, include_description: bool = True) -> List['MemoryNode']:
        """[新增] 异步搜索记忆节点"""
        import asyncio
        return await asyncio.to_thread(self.search_nodes, query, limit, include_description)

    async def save_reflection_async(self, date: str, reflection: str):
        """[新增] 异步保存每日感悟"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.save_reflection, date, reflection)

    async def get_reflection_async(self, date: str) -> Optional['DailyReflection']:
        """[新增] 异步获取每日感悟"""
        import asyncio
        return await asyncio.to_thread(self.get_reflection, date)

    async def save_event_async(self, event: 'MemoryEvent'):
        """[新增] 异步保存事件"""
        import asyncio
        async with self._db_lock:
            return await asyncio.to_thread(self.save_event, event)
