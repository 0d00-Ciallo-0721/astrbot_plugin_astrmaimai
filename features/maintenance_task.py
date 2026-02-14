# heartflow/features/maintenance_task.py
# (v4.13 - New Feature)
import asyncio
import time
from typing import List
from astrbot.api import logger
from ..core.state_manager import StateManager
from ..persistence import PersistenceManager

class MaintenanceTask:
    """
    维护任务管理器
    职责：
    1. 定时将脏数据 (Dirty Data) 回写到 SQLite (Write-Behind)
    2. 清理长期不活跃的内存缓存 (LRU Eviction)
    3. 定期同步活跃用户的昵称 (Identity Sync)
    """

    def __init__(self, state_manager: StateManager, persistence: PersistenceManager, context):
        self.state_manager = state_manager
        self.persistence = persistence
        self.context = context
        self._is_running = False

    async def run(self):
        """启动维护循环"""
        if self._is_running: return
        self._is_running = True
        logger.info("🛠️ HeartCore: 维护任务已启动 (缓存/持久化 + 身份同步)")
        
        # 启动两个独立的循环任务
        asyncio.create_task(self._cache_loop())
        asyncio.create_task(self._identity_loop())
    
    async def _cache_loop(self):
        """
        Loop 1: 缓存维护 (高频: 60s)
        负责数据回写和内存释放
        """
        while self._is_running:
            try:
                await asyncio.sleep(60)
                await self._process_cache_maintenance()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Maintenance Cache Loop Error: {e}")
                await asyncio.sleep(5)

    async def _identity_loop(self):
        """
        Loop 2: 身份同步 (低频: 3天)
        负责更新活跃用户的昵称，防止因改名导致的记忆错乱
        """
        # 初次启动等待 30 秒，错开启动高峰
        await asyncio.sleep(30)
        
        while self._is_running:
            try:
                logger.info("🔄 HeartCore: 开始执行活跃用户昵称同步任务...")
                
                # 1. 获取适配器 (目前仅支持 aiocqhttp/OneBot)
                platform = self.context.get_platform("aiocqhttp")
                if not platform: 
                    logger.debug("未找到 aiocqhttp 适配器，跳过昵称同步。")
                    await asyncio.sleep(3600) # 没适配器，睡一小时再试
                    continue
                
                client = platform.get_client()
                if not client:
                    await asyncio.sleep(60)
                    continue

                # 2. 从 DB 获取 3 天内活跃的用户 ID
                active_users = await self.persistence.get_active_users(days=3)
                logger.info(f"📊 发现 {len(active_users)} 个活跃用户，准备检查昵称...")

                synced_count = 0
                for user_id in active_users:
                    # 3. 获取用户 Profile
                    # 注意：这里我们只检查"已有足迹"的用户
                    profile = await self.state_manager.get_user_profile(user_id)
                    if not profile.group_footprints: 
                        continue
                    
                    # 4. 寻找最近活跃的群作为查询锚点
                    # group_footprints 结构: {gid: {"last_active_time": 12345, ...}}
                    try:
                        target_group = max(
                            profile.group_footprints.items(), 
                            key=lambda x: x[1].get('last_active_time', 0)
                        )[0]
                    except ValueError:
                        continue
                    
                    # 5. 调用 API 获取最新信息
                    try:
                        info = await client.api.call_action(
                            'get_group_member_info', 
                            group_id=int(target_group), 
                            user_id=int(user_id),
                            no_cache=True # 强制刷新
                        )
                        new_name = info.get('card') or info.get('nickname')
                        
                        # 6. 对比并更新
                        if new_name and new_name != profile.name:
                            logger.info(f"👤 [Identity] 更新昵称: {user_id} ({profile.name} -> {new_name})")
                            profile.name = new_name
                            profile.is_dirty = True
                            synced_count += 1
                            
                    except Exception:
                        # 可能是退群了或者 API 失败，忽略
                        pass
                    
                    # 7. 限流保护 (每秒 1 个)
                    await asyncio.sleep(1.0)
                
                logger.info(f"✅ 昵称同步完成。更新了 {synced_count} 个用户的名字。")

            except Exception as e:
                logger.error(f"Identity Loop Error: {e}")
            
            # 等待 3 天 (3 * 24 * 3600 = 259200 秒)
            await asyncio.sleep(259200)

    async def _process_cache_maintenance(self):
        """执行缓存回写与淘汰"""
        now = time.time()
        eviction_ttl = 600 # 10分钟无访问则淘汰
        
        # ==========================
        # 1. ChatState 维护
        # ==========================
        chats_to_remove = []
        # 获取 keys 副本，防止迭代时字典大小改变
        chat_ids = list(self.state_manager.get_all_states_unsafe().keys())
        
        for cid in chat_ids:
            state = self.state_manager.chat_states.get(cid)
            if not state: continue
            
            # A. 回写脏数据
            if state.is_dirty:
                try:
                    await self.persistence.save_chat_state(cid, state)
                    state.is_dirty = False
                    # logger.debug(f"Saved dirty chat state: {cid}")
                except Exception as e:
                    logger.error(f"Failed to save chat state {cid}: {e}")
            
            # B. 检查过期淘汰
            # 条件：超时 + 非脏数据 + 无锁 + 双池为空 (确保没有正在处理的消息)
            if (now - state.last_access_time > eviction_ttl) and (not state.is_dirty):
                # 深度检查：确保没有活跃任务
                is_busy = state.lock.locked() or state.accumulation_pool or state.background_buffer
                if not is_busy:
                    chats_to_remove.append(cid)

        # 执行淘汰
        if chats_to_remove:
            for cid in chats_to_remove:
                # 二次检查，防止在处理过程中状态发生了变化
                if cid in self.state_manager.chat_states:
                    self.state_manager.chat_states.pop(cid, None)
            logger.info(f"🧹 HeartCore: 已淘汰 {len(chats_to_remove)} 个不活跃群聊缓存。")

        # ==========================
        # 2. UserProfile 维护
        # ==========================
        users_to_remove = []
        user_ids = list(self.state_manager.get_all_user_profiles_unsafe().keys())
        
        for uid in user_ids:
            profile = self.state_manager.user_profiles.get(uid)
            if not profile: continue
            
            # A. 回写脏数据
            if profile.is_dirty:
                try:
                    await self.persistence.save_user_profile(profile)
                    profile.is_dirty = False
                except Exception as e:
                    logger.error(f"Failed to save user profile {uid}: {e}")
            
            # B. 检查过期淘汰
            if (now - profile.last_access_time > eviction_ttl) and (not profile.is_dirty):
                users_to_remove.append(uid)
        
        # 执行淘汰
        if users_to_remove:
            for uid in users_to_remove:
                if uid in self.state_manager.user_profiles:
                    self.state_manager.user_profiles.pop(uid, None)
            # logger.debug(f"🧹 Evicted {len(users_to_remove)} user profiles.")