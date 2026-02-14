### 📄 persistence.py
import json
import time
import datetime
import asyncio
import aiosqlite
from pathlib import Path
from typing import Dict, List, Any, Optional
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .datamodels import ChatState, UserProfile
from .config import HeartflowConfig

class PersistenceManager:
    """
    SQLite 持久化管理器
    负责 ChatState 和 UserProfile 的数据库 I/O
    """
    
    def __init__(self, context: Context, config: HeartflowConfig):
        self.context = context
        self.config = config
        
        base_path = Path(get_astrbot_data_path())
        self.data_dir = base_path / "plugin_data" / "heartcore"
        self.data_dir.mkdir(parents=True, exist_ok=True) 

        self.db_path = self.data_dir / "heartflow.db"
        self.persona_cache_file = self.data_dir / "persona_cache.json"
        
        asyncio.create_task(self._init_db())

    async def _init_db(self):
        async with aiosqlite.connect(self.db_path) as db:
            # 状态表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS chat_states (
                    chat_id TEXT PRIMARY KEY,
                    energy REAL,
                    mood REAL,
                    group_config TEXT,
                    last_reset_date TEXT,
                    updated_at REAL
                )
            """)
            # 用户画像表
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_profiles (
                    user_id TEXT PRIMARY KEY,
                    name TEXT,
                    social_score REAL,
                    last_seen REAL,
                    persona_analysis TEXT,
                    group_footprints TEXT,
                    updated_at REAL,
                    identity TEXT,
                    last_persona_gen_time REAL
                )
            """)
            await db.commit()

    # --- 1. ChatState ---
    async def save_chat_state(self, chat_id: str, state: ChatState):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO chat_states (chat_id, energy, mood, group_config, last_reset_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    chat_id, 
                    state.energy, 
                    state.mood, 
                    json.dumps(state.group_config, ensure_ascii=False),
                    state.last_reset_date,
                    time.time()
                ))
                await db.commit()
        except Exception as e:
            logger.error(f"Save ChatState Error: {e}")

    async def load_chat_state(self, chat_id: str) -> Optional[ChatState]:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        state = ChatState()
                        state.energy = row[1]
                        state.mood = row[2]
                        state.group_config = json.loads(row[3]) if row[3] else {}
                        state.last_reset_date = row[4]
                        return state
        except Exception as e:
            logger.error(f"Load ChatState Error: {e}")
        return None

    # --- 2. UserProfile ---
    async def save_user_profile(self, profile: UserProfile):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO user_profiles 
                    (user_id, name, social_score, last_seen, persona_analysis, group_footprints, updated_at, identity, last_persona_gen_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    profile.user_id,
                    profile.name,
                    profile.social_score,
                    profile.last_seen,
                    profile.persona_analysis,
                    json.dumps(profile.group_footprints, ensure_ascii=False),
                    time.time(),
                    profile.identity,
                    profile.last_persona_gen_time
                ))
                await db.commit()
        except Exception as e:
            logger.error(f"Save UserProfile Error: {e}")

    async def load_user_profile(self, user_id: str) -> Optional[UserProfile]:
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        return UserProfile(
                            user_id=row[0],
                            name=row[1],
                            social_score=row[2],
                            last_seen=row[3],
                            persona_analysis=row[4],
                            group_footprints=json.loads(row[5]) if row[5] else {},
                            identity=row[7] if len(row) > 7 else "",
                            last_persona_gen_time=row[8] if len(row) > 8 else 0.0
                        )
        except Exception as e:
            logger.error(f"Load UserProfile Error: {e}")
        return None

    # --- 3. 缓存管理 ---
    def load_persona_cache(self) -> Dict[str, Any]:
        if self.persona_cache_file.exists():
            try:
                with open(self.persona_cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                pass
        return {}

    def save_persona_cache(self, cache: Dict[str, Any]):
        try:
            with open(self.persona_cache_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Save Persona Cache Error: {e}")

    # [已废弃] 以下方法在 HeartCore 2.0 中由 MemoryGlands (VectorDB) 接管
    # async def save_message(self, ...): pass
    # async def save_history_message(self, ...): pass