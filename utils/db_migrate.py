### 📄 utils/db_migrate.py
import json
import time
import aiosqlite
import asyncio
from pathlib import Path
from astrbot.api import logger

async def migrate_legacy_data(persistence) -> str:
    """
    执行旧版 JSON 数据到 SQLite 的迁移
    :param persistence: PersistenceManager 实例 (用于获取路径)
    :return: 迁移结果报告文本
    """
    data_dir = persistence.data_dir
    db_path = persistence.db_path
    
    json_states_path = data_dir / "heartflow_states.json"
    json_profiles_path = data_dir / "heartflow_user_profiles.json"
    
    report = []
    
    # --- 1. 迁移群聊状态 ---
    if json_states_path.exists():
        try:
            # 使用 to_thread 防止大文件读取阻塞 Event Loop
            def _read_states():
                with open(json_states_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await asyncio.to_thread(_read_states)
            count = 0
            
            async with aiosqlite.connect(db_path) as db:
                for chat_id, state in data.items():
                    # 提取字段 (与 datamodels.py 保持一致)
                    energy = state.get("energy", 0.5)
                    mood = state.get("mood", 0.0)
                    group_config = json.dumps(state.get("group_config", {}), ensure_ascii=False)
                    last_reset_date = state.get("last_reset_date", "")
                    updated_at = time.time()

                    await db.execute("""
                        INSERT OR REPLACE INTO chat_states 
                        (chat_id, energy, mood, group_config, last_reset_date, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (chat_id, energy, mood, group_config, last_reset_date, updated_at))
                    count += 1
                await db.commit()
            report.append(f"✅ 群聊状态迁移成功: {count} 条")
        except Exception as e:
            logger.error(f"Migrate ChatStates Error: {e}")
            report.append(f"❌ 群聊状态迁移失败: {e}")
    else:
        report.append("⚠️ 未找到旧版群聊数据 (heartflow_states.json)，跳过。")

    # --- 2. 迁移用户画像 ---
    if json_profiles_path.exists():
        try:
            def _read_profiles():
                with open(json_profiles_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            
            data = await asyncio.to_thread(_read_profiles)
            count = 0
            
            async with aiosqlite.connect(db_path) as db:
                for user_id, profile in data.items():
                    # 提取字段
                    name = profile.get("name", "未知用户")
                    social_score = profile.get("social_score", 0.0)
                    last_seen = profile.get("last_seen", 0.0)
                    persona_analysis = profile.get("persona_analysis", "")
                    group_footprints = json.dumps(profile.get("group_footprints", {}), ensure_ascii=False)
                    updated_at = time.time()
                    
                    # [v4.14 修正] 迁移旧数据时，last_persona_gen_time 默认为 0.0
                    last_persona_gen_time = 0.0

                    await db.execute("""
                        INSERT OR REPLACE INTO user_profiles 
                        (user_id, name, social_score, last_seen, persona_analysis, group_footprints, updated_at, last_persona_gen_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (user_id, name, social_score, last_seen, persona_analysis, group_footprints, updated_at, last_persona_gen_time))
                    count += 1
                await db.commit()
            report.append(f"✅ 用户画像迁移成功: {count} 条")
        except Exception as e:
            logger.error(f"Migrate UserProfiles Error: {e}")
            report.append(f"❌ 用户画像迁移失败: {e}")
    else:
        report.append("⚠️ 未找到旧版用户数据 (heartflow_user_profiles.json)，跳过。")

    return "\n".join(report)