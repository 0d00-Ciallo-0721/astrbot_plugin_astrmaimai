# astrmai/infra/persistence.py
import json
import time
import asyncio
import aiosqlite
from pathlib import Path
from typing import Dict, List, Any, Optional
from sqlmodel import SQLModel, create_engine, Session
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .datamodels import ChatState, UserProfile, LastMessageMetadataDB, ExpressionPattern, MessageLog, CronSnapshot

class PersistenceManager:
    """
    底层持久化管理器
    混合模式：对高频 State 走 aiosqlite，对关系数据走 SQLModel
    """
    def __init__(self):
        base_path = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai"
        base_path.mkdir(parents=True, exist_ok=True)
        
        self.db_path = base_path / "astrmai.db"
        self.db_url = f"sqlite:///{self.db_path}"
        
        # 缓存文件路径
        self.cache_dir = base_path / "cache"
        self.cache_dir.mkdir(exist_ok=True)
        self.persona_cache_path = self.cache_dir / "persona_cache.json"
        
        # 兼容旧逻辑的同步 Engine (用于 Vector Store 等)
        self.engine = create_engine(self.db_url)
        SQLModel.metadata.create_all(self.engine)
        
        # 异步初始化表结构
        asyncio.create_task(self._init_db())
        logger.info(f"[AstrMai-Infra] 💾 Database connected & mounted at {self.db_path}")

    def get_session(self) -> Session:
        """提供给 Memory 和 Evolution 的兼容接口"""
        return Session(self.engine)

    async def _init_db(self):
        """初始化异步高频表 (绕过 SQLModel 开销)"""
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
                        updated_at REAL
                    )
                """)
                await db.commit()
        except Exception as e:
            logger.error(f"[AstrMai-Infra] 数据库异步表初始化失败: {e}")

    # ==========================================
    # Cache I/O (Persona Summarizer)
    # ==========================================
    def load_persona_cache(self) -> Dict[str, Any]:
        """[修改] 加载人设摘要缓存 (Key: Persona ID / Session ID)"""
        if not self.persona_cache_path.exists():
            return {}
        try:
            with open(self.persona_cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Persistence] 加载人设缓存失败: {e}")
            return {}

    def save_persona_cache(self, cache_data: Dict[str, Any]):
        """[修改] 保存人设摘要缓存 (持久化为 {'persona_id': {...}} 结构)"""
        try:
            with open(self.persona_cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Persistence] 保存人设缓存失败: {e}")


    # ==========================================
    # State & Profile 异步高频 I/O
    # ==========================================
    
    async def load_chat_state(self, chat_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            if row:
                return {
                    "chat_id": row[0],
                    "energy": row[1],
                    "mood": row[2],
                    "group_config": json.loads(row[3]) if row[3] else {},
                    "last_reset_date": row[4],
                    "total_replies": row[5]
                }
        return None

    async def save_chat_state(self, chat_id: str, state: ChatState):
        config_json = json.dumps(state.group_config, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO chat_states 
                (chat_id, energy, mood, group_config, last_reset_date, total_replies, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (chat_id, state.energy, state.mood, config_json, state.last_reset_date, state.total_replies, time.time()))
            await db.commit()

    async def load_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            if row:
                # 训多列名山不同版本字段顺序，用 PRAGMA 获取列名并建立映射
                cursor2 = await db.execute("PRAGMA table_info(user_profiles)")
                cols_info = await cursor2.fetchall()
                col_names = [c[1] for c in cols_info]
                row_dict = dict(zip(col_names, row))
                return {
                    "user_id": row_dict.get("user_id", ""),
                    "name": row_dict.get("name", "Unknown"),
                    "social_score": row_dict.get("social_score", 0.0),
                    "last_seen": row_dict.get("last_seen", 0.0),
                    "persona_analysis": row_dict.get("persona_analysis", ""),
                    "group_footprints": json.loads(row_dict.get("group_footprints") or "{}"),
                    "identity": row_dict.get("identity", ""),
                    "tags": json.loads(row_dict.get("tags") or "[]"),
                    # Phase 8.1: 取名系统
                    "nickname": row_dict.get("nickname", ""),
                    "nickname_reason": row_dict.get("nickname_reason", ""),
                    "know_times": int(row_dict.get("know_times") or 0),
                    "is_known": bool(row_dict.get("is_known") or False),
                    # Phase 8.2: 分类记忆点
                    "memory_points": json.loads(row_dict.get("memory_points") or "[]"),
                }
        return None

    async def save_user_profile(self, profile: 'UserProfile'):
        footprints_json = json.dumps(profile.group_footprints, ensure_ascii=False)
        tags_json = json.dumps(profile.tags, ensure_ascii=False)
        memory_points_json = json.dumps(profile.memory_points, ensure_ascii=False)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT OR REPLACE INTO user_profiles 
                (user_id, name, social_score, last_seen, persona_analysis, group_footprints,
                 identity, tags, nickname, nickname_reason, know_times, is_known,
                 memory_points, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (profile.user_id, profile.name, profile.social_score, profile.last_seen,
                  profile.persona_analysis, footprints_json, profile.identity,
                  tags_json,
                  profile.nickname, profile.nickname_reason,
                  profile.know_times, int(profile.is_known),
                  memory_points_json, time.time()))
            await db.commit()
            
    async def add_last_message_meta(self, chat_id: str, sender_id: str, has_image: bool, image_urls: list):
        """记录多模态元数据"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO lastmessagemetadatadb 
                (chat_id, sender_id, has_image, image_urls, vl_executed, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, sender_id, has_image, json.dumps(image_urls), False, time.time()))
            await db.commit()


    async def load_persona_cache_async(self) -> Dict[str, Any]:
        """[新增] 异步加载人设摘要缓存"""
        import asyncio
        return await asyncio.to_thread(self.load_persona_cache)

    async def save_persona_cache_async(self, cache_data: Dict[str, Any]):
        """[新增] 异步保存人设摘要缓存"""
        import asyncio
        return await asyncio.to_thread(self.save_persona_cache, cache_data)
    


    async def _init_db(self):
        """初始化异步高频表 (绕过 SQLModel 开销)"""
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
                        updated_at REAL
                    )
                """)
                # 兼容旧数据库字段迁移 (ALTER TABLE 内指，已存列失败不中断)
                for col_def in [
                    "ALTER TABLE user_profiles ADD COLUMN tags TEXT DEFAULT '[]'",
                    "ALTER TABLE user_profiles ADD COLUMN nickname TEXT DEFAULT ''",
                    "ALTER TABLE user_profiles ADD COLUMN nickname_reason TEXT DEFAULT ''",
                    "ALTER TABLE user_profiles ADD COLUMN know_times INTEGER DEFAULT 0",
                    "ALTER TABLE user_profiles ADD COLUMN is_known INTEGER DEFAULT 0",
                    "ALTER TABLE user_profiles ADD COLUMN memory_points TEXT DEFAULT '[]'",
                ]:
                    try:
                        await db.execute(col_def)
                    except Exception:
                        pass  # 列已存在时忽略
                # [新增] CronSnapshot 快照表的原生 SQL 兜底建表
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
                await db.commit()
        except Exception as e:
            logger.error(f"[AstrMai-Infra] 数据库异步表初始化失败: {e}")