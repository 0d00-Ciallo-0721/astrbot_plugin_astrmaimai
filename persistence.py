# heartflow/persistence.py
# (v4.13 - SQLite Upgrade)
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
from astrbot.api.event import AstrMessageEvent 
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
        
        # 数据目录
        base_path = Path(get_astrbot_data_path())
        self.data_dir = base_path / "plugin_data" / "heartcore"
        self.data_dir.mkdir(parents=True, exist_ok=True) 

        # 数据库路径
        self.db_path = self.data_dir / "heartflow.db"
        # 缓存文件仍保留 JSON 格式（也可用 DB 存，但 JSON 查看方便）
        self.persona_cache_file = self.data_dir / "persona_cache.json"
        
        # 初始化任务
        asyncio.create_task(self._init_db())


    async def _init_db(self):
        """初始化数据库表结构 (v4.14 自动迁移适配)"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # ... (前略: chat_states 表)
                
                # 2. 用户画像表 (新增 identity 字段)
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_profiles (
                        user_id TEXT PRIMARY KEY,
                        name TEXT,
                        social_score REAL,
                        last_seen REAL,
                        persona_analysis TEXT,
                        group_footprints TEXT,
                        updated_at REAL,
                        last_persona_gen_time REAL DEFAULT 0.0,
                        identity TEXT DEFAULT ''
                    )
                """)

                # [自动迁移 1] last_persona_gen_time
                try:
                    await db.execute("ALTER TABLE user_profiles ADD COLUMN last_persona_gen_time REAL DEFAULT 0.0")
                except Exception:
                    pass 

                # [新增] [自动迁移 2] identity
                try:
                    await db.execute("ALTER TABLE user_profiles ADD COLUMN identity TEXT DEFAULT ''")
                    logger.info("💖 HeartCore: 数据库字段迁移成功 (Added identity)")
                except Exception:
                    pass 

                await db.commit()
            logger.info("💖 HeartCore: SQLite 数据库初始化完成。")
        except Exception as e:
            logger.error(f"💖 HeartCore: 数据库初始化失败: {e}")
    # --- ChatState CRUD ---

    async def load_chat_state(self, chat_id: str) -> Optional[Dict[str, Any]]:
        """从 DB 加载单个群状态"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT * FROM chat_states WHERE chat_id = ?", (chat_id,))
                row = await cursor.fetchone()
                if row:
                    # row: (chat_id, energy, mood, group_config, last_reset_date, updated_at)
                    return {
                        "energy": row[1],
                        "mood": row[2],
                        "group_config": json.loads(row[3]) if row[3] else {},
                        "last_reset_date": row[4]
                    }
        except Exception as e:
            logger.error(f"Load ChatState Error ({chat_id}): {e}")
        return None

    async def save_chat_state(self, chat_id: str, state: ChatState):
        """保存单个群状态到 DB"""
        try:
            config_json = json.dumps(state.group_config, ensure_ascii=False)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO chat_states 
                    (chat_id, energy, mood, group_config, last_reset_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (chat_id, state.energy, state.mood, config_json, state.last_reset_date, time.time()))
                await db.commit()
        except Exception as e:
            logger.error(f"Save ChatState Error ({chat_id}): {e}")

    # --- UserProfile CRUD ---

    async def load_user_profile(self, user_id: str) -> Optional[Dict[str, Any]]:
        """从 DB 加载单个用户画像 (v4.14 适配)"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # [修改] 增加 identity 查询
                cursor = await db.execute("""
                    SELECT user_id, name, social_score, last_seen, 
                           persona_analysis, group_footprints, last_persona_gen_time, identity 
                    FROM user_profiles WHERE user_id = ?
                """, (user_id,))
                row = await cursor.fetchone()
                
                if row:
                    # [新增] 处理身份逻辑：如果数据库为空，则使用默认配置
                    raw_identity = row[7] if len(row) > 7 else ""
                    final_identity = raw_identity if raw_identity else self.config.default_user_identity

                    return {
                        "user_id": row[0],
                        "name": row[1],
                        "social_score": row[2],
                        "last_seen": row[3],
                        "persona_analysis": row[4],
                        "group_footprints": json.loads(row[5]) if row[5] else {},
                        "last_persona_gen_time": row[6] if len(row) > 6 and row[6] is not None else 0.0,
                        "identity": final_identity
                    }
        except Exception as e:
            logger.error(f"Load UserProfile Error ({user_id}): {e}")
        return None

    async def save_user_profile(self, profile: UserProfile):
        """保存单个用户画像到 DB (v4.14 适配)"""
        try:
            footprints_json = json.dumps(profile.group_footprints, ensure_ascii=False)
            async with aiosqlite.connect(self.db_path) as db:
                # [修改] 增加 identity 字段保存
                await db.execute("""
                    INSERT OR REPLACE INTO user_profiles 
                    (user_id, name, social_score, last_seen, persona_analysis, group_footprints, updated_at, last_persona_gen_time, identity)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (profile.user_id, profile.name, profile.social_score, profile.last_seen, 
                      profile.persona_analysis, footprints_json, time.time(), profile.last_persona_gen_time, profile.identity))
                await db.commit()
        except Exception as e:
            logger.error(f"Save UserProfile Error ({profile.user_id}): {e}")

    # [新增] 全量更新用户身份
    async def update_all_user_identities(self, new_identity: str) -> int:
        """
        全量更新数据库中的用户身份字段
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("UPDATE user_profiles SET identity = ?", (new_identity,))
                await db.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Bulk Update Identity Error: {e}")
            return 


    async def get_active_users(self, days: int) -> List[str]:
        """获取最近 N 天活跃的用户 ID (用于昵称同步)"""
        limit_time = time.time() - (days * 86400)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("SELECT user_id FROM user_profiles WHERE last_seen > ?", (limit_time,))
                rows = await cursor.fetchall()
                return [row[0] for row in rows]
        except Exception as e:
            logger.error(f"Get Active Users Error: {e}")
            return []

    # --- 兼容旧版接口 (Persona Cache) ---
    def load_persona_cache(self) -> Dict[str, Any]:
        """加载人格缓存 (保留 JSON 文件方式)"""
        if self.persona_cache_file.exists():
            try:
                with open(self.persona_cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Load Persona Cache Error: {e}")
        return {}

    def save_persona_cache(self, cache: Dict[str, Any]):
        """保存人格缓存"""
        try:
            with open(self.persona_cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Save Persona Cache Error: {e}")

    async def save_history_message(self, chat_id: str, role: str, content: str, bot_name: str, sender_name: str = None, event: AstrMessageEvent = None):
        """
        手动保存单条消息到 AstrBot 上下文
        [修复] 增加 event 参数，用于检查指令标记，防止指令污染历史记录
        """
        # --- [核心] 指令熔断机制 ---
        if event and event.get_extra("heartflow_is_command"):
            logger.debug(f"Persistence: 检测到指令标记，已阻止写入上下文历史。")
            return
        # ---------------------------

        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(chat_id)
            history = []
            if curr_cid:
                conv = await self.context.conversation_manager.get_conversation(chat_id, curr_cid)
                if conv and conv.history: 
                    history = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
            
            time_str = datetime.datetime.now().strftime("[%H:%M:%S]")
            
            formatted_content = ""
            if role == "user":
                formatted_content = f"{time_str} {sender_name or '用户'}: {content}"
            else:
                formatted_content = f"{time_str} {bot_name or '我'}: {content}"

            history.append({"role": role, "content": formatted_content})
            
            user_configured_count = getattr(self.config, 'context_messages_count', 20)
            actual_max_history = max(user_configured_count, 100)
            
            if len(history) > actual_max_history:
                history = history[-actual_max_history:]
            
            await self.context.conversation_manager.update_conversation(
                unified_msg_origin=chat_id,
                conversation_id=None, 
                history=history
            )
        except Exception as e:
            logger.error(f"[{chat_id[:10]}] 手动保存历史失败: {e}")