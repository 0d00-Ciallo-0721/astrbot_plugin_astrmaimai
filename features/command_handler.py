### 📄 features/command_handler.py
import time
import json
import math
from typing import TYPE_CHECKING
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.event import AstrMessageEvent, filter as event_filter
from astrbot.api.message_components import At

from ..config import HeartflowConfig
from ..core.state_manager import StateManager
from ..features.persona_summarizer import PersonaSummarizer
from ..core.brain_planner import BrainPlanner
from ..utils.db_migrate import migrate_legacy_data

if TYPE_CHECKING:
    from ..features.proactive_task import ProactiveTask

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..config import HeartflowConfig
from ..core.state_manager import StateManager
from ..core.impulse_engine import ImpulseEngine
from ..core.memory_glands import MemoryGlands
from ..core.evolution_cortex import EvolutionCortex

class CommandHandler:
    """
    (v2.0) 指令处理器
    职责：处理管理指令，调试 2.0 组件状态
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 state_manager: StateManager,
                 impulse_engine: ImpulseEngine,
                 memory_glands: MemoryGlands,
                 evolution_cortex: EvolutionCortex
                 ):
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.impulse = impulse_engine
        self.memory = memory_glands
        self.evolution = evolution_cortex
        # 参数别名映射表 (中文指令 -> 配置键名)
        self.ALIAS_MAP = {
            "回复阈值": "reply_composite_threshold",
            "评分门槛": "reply_composite_threshold",
            "精力恢复": "energy_recovery_rate",
            "精力消耗": "energy_decay_rate",
            "精力过滤": "energy_soft_filter_limit",
            "加分权重": "score_positive_interaction",
            "扣分权重": "score_negative_interaction"
        }
    async def cmd_reset_memory(self, event: AstrMessageEvent):
        """/遗忘"""
        if not self._check_admin(event): return
        
        session_id = event.unified_msg_origin
        # 调用 MemoryGlands 清除 (需实现该接口，或直接操作 underlying engine)
        # 这里暂时只清除 ChatState 中的短期缓存
        state = await self.state_manager.get_chat_state(session_id)
        state.accumulation_pool.clear()
        state.background_buffer.clear()
        
        # 若需清除向量库，需在 MemoryGlands 增加 clear_session 接口
        # await self.memory.clear_session(session_id)
        
        yield event.plain_result("✅ 短期记忆缓冲区已清空。")

    async def cmd_force_mutation(self, event: AstrMessageEvent):
        """/突变"""
        if not self._check_admin(event): return
        
        session_id = event.unified_msg_origin
        if self.evolution:
            # 强制刷新状态
            # 这里的逻辑取决于 EvolutionCortex 的实现，假设它有 force_refresh
            # 简单起见，我们直接清除缓存让其下次自动生成
            if session_id in self.evolution.active_mutations:
                del self.evolution.active_mutations[session_id]
            
            yield event.plain_result("🧬 人格状态缓存已清除，下次对话将触发新突变。")
        else:
            yield event.plain_result("❌ 进化皮层未启用。")

    def _check_admin(self, event: AstrMessageEvent) -> bool:
        sender = event.get_sender_id()
        if sender in self.config.super_admin_id or sender == self.config.super_admin_id:
            return True
        return False

    # =================================================================
    # Level 1: 普通用户指令
    # =================================================================

    async def cmd_menu(self, event: AstrMessageEvent):
        """
        (v4.14 新增) 动态帮助菜单
        根据权限展示不同内容
        """
        is_super = await self._check_permission(event, level=3)
        is_admin = await self._check_permission(event, level=2)
        
        help_text = "✨ **HeartCore 核心指令** ✨\n\n"
        
        # Level 1: 基础指令
        help_text += "👤 **通用指令**\n"
        help_text += "- `/heartcore` : 查看当前状态\n"
        help_text += "- `/人物画像` : 查看AI眼中的你\n"
        help_text += "- `/修改身份 [身份]` : 修改你在AI眼中的设定\n" 
        help_text += "- `/生成画像` : 立即刷新人物画像 (24h/次)\n"
        help_text += "- `/菜单` : 显示此帮助\n"
        
        # Level 2: 群管指令
        if is_admin:
            help_text += "\n🛡️ **群管指令**\n"
            help_text += "- `/设置阈值 [参数] [数值]` : 调整群内参数\n"
            help_text += "  (支持: 评分门槛, 精力恢复, 精力消耗...)\n"
            
        # Level 3: 超管指令
        if is_super:
            help_text += "\n⚡ **超管指令**\n"
            help_text += "- `/生成画像 @某人` : 强制刷新指定用户\n"
            help_text += "- `/查看人格` : 查看当前生效的人格摘要\n" # [新增]
            help_text += "- `/重载人格` : 强制重新生成人格摘要\n" # [新增]
            help_text += "- `/全量修改身份 [身份]` : 强制重置所有人身份\n" 
            help_text += "- `/一键设置群管` : 同步群管理员权限\n"
            help_text += "- `/设置群管 @某人` : 手动授权\n"
            help_text += "- `/数据迁移` : 升级数据库结构\n"
            
        yield event.plain_result(help_text)

    async def cmd_generate_persona(self, event: AstrMessageEvent, target: At = None):
        """
        (v4.14.3 修复) 主动生成人物画像
        修复：target 参数类型兼容性 (At/str)
        """
        if not self.proactive_task:
            yield event.plain_result("❌ 模块未就绪：ProactiveTask 未加载。")
            return

        sender_id = event.get_sender_id()
        is_super = await self._check_permission(event, level=3)
        
        target_id = sender_id
        target_name = event.get_sender_name()
        
        # 1. 确定目标与权限检查
        if target:
            if not is_super:
                yield event.plain_result("❌ 权限不足：只有超级管理员可以为他人生成画像。")
                return
            
            # [关键修复] 类型安全检查
            # AstrBot 有时可能传入 At 对象，有时可能是字符串（如果解析未命中）
            if isinstance(target, At):
                target_id = str(target.qq)
            elif isinstance(target, str):
                # 尝试清洗字符串 (去除 at: 等前缀，提取数字)
                # 简单处理：如果是纯数字字符串，直接用；否则报错
                if target.isdigit():
                    target_id = target
                else:
                    # 尝试从字符串中提取数字 (兼容情况)
                    import re
                    match = re.search(r'\d+', target)
                    if match:
                        target_id = match.group()
                    else:
                        yield event.plain_result("❌ 参数格式错误：无法解析目标用户 ID。请直接 @用户。")
                        return
            else:
                # 兜底：如果是其他类型 (如 int)
                target_id = str(target)

            target_name = f"用户{target_id}"
        
        # 2. 获取 Profile 检查冷却
        profile = await self.state_manager.get_user_profile(target_id)
        
        if not is_super:
            now = time.time()
            if now - profile.last_persona_gen_time < 24 * 3600:
                remaining = (profile.last_persona_gen_time + 24 * 3600) - now
                hours = int(remaining / 3600)
                minutes = int((remaining % 3600) / 60)
                yield event.plain_result(f"⏳ 画像生成冷却中...\n请等待 {hours}小时 {minutes}分 后再试。")
                return

        # 3. 发送提示并执行
        yield event.plain_result(f"🎨 正在为 {target_name}生成最新的人物画像 (检查最近500条发言)... \n这可能需要几十秒，请稍候。")
        
        # 调用公共方法
        analysis_result = await self.proactive_task.generate_persona_for_user(target_id, limit=500)
        
        if analysis_result == "NOT_ENOUGH_MESSAGES":
            yield event.plain_result(f"⚠️ 生成中止：{target_name} 的近期有效发言不足 100 条，数据量过少无法生成准确画像。")
        
        elif analysis_result:
            # 4. 成功反馈
            profile = await self.state_manager.get_user_profile(target_id)
            result_text = f"✅ 画像更新成功！\n\n【我对 {profile.name} 的最新印象】\n{profile.persona_analysis}"
            yield event.plain_result(result_text)
            
        else:
            yield event.plain_result("⚠️ 生成失败：AI 暂时繁忙或发生未知错误。")

    async def get_my_persona(self, event: AstrMessageEvent):
        """查看自己在Bot眼中的印象"""
        user_id = event.get_sender_id()
        profile = await self.state_manager.get_user_profile(user_id) # [Fix] await
        
        # [修改] 增加身份和好感度的格式化展示
        msg = f"【我对 {profile.name} 的印象】\n"
        msg += f"🏷️ 身份: {profile.identity}\n"
        msg += f"❤️ 好感: {profile.social_score:.1f}\n"
        
        if not profile.persona_analysis:
            msg += f"📝 画像: (暂无深度画像，请多聊聊或发送 /生成画像)"
        else:
            msg += f"📝 画像: {profile.persona_analysis}"
            
        yield event.plain_result(msg)

    async def update_user_identity(self, event: AstrMessageEvent, new_identity: str):
        """
        (v4.14) 修改用户身份
        """
        if not new_identity:
            yield event.plain_result("❌ 请输入具体身份，例如：/修改身份 魔法少女")
            return

        # 长度限制
        if len(new_identity) > 20:
             yield event.plain_result("❌ 身份设定太长了，请控制在 20 字以内。")
             return

        user_id = event.get_sender_id()
        profile = await self.state_manager.get_user_profile(user_id)
        
        old_identity = profile.identity
        profile.identity = new_identity
        profile.is_dirty = True # 触发保存
        
        yield event.plain_result(f"✅ 身份更新成功！\n从「{old_identity}」变更为「{new_identity}」。\nAI 稍后会根据新身份重新审视对你的印象。")

    async def heartflow_status(self, event: AstrMessageEvent):
        """查看当前状态 (开放给所有人，但脱敏)"""
        chat_id = event.unified_msg_origin
        state = await self.state_manager.get_chat_state(chat_id) # [Fix] await
        
        # ... (构建基础状态信息，逻辑同旧版，省略部分非关键字段) ...
        status_info = f"""
📊 **群聊状态**
- 精力: {state.energy:.2f} | 心情: {state.mood:.2f}
- 冷却: {state.consecutive_reply_count}/{self.config.max_consecutive_replies}
- 积压: {len(state.background_buffer)} 条 (User B)
"""
        # 如果是管理员，显示更多调试信息
        if await self._check_permission(event, level=2):
            status_info += f"\n🔧 **管理员面板**\n- 独立配置: {json.dumps(state.group_config, ensure_ascii=False)}"
            
        yield event.plain_result(status_info)

    # =================================================================
    # Level 2: 群管理员指令 (Group Admin)
    # =================================================================

    async def set_threshold(self, event: AstrMessageEvent, key: str, value: float):
        """
        修改本群参数 (支持中文别名)
        用法: /设置阈值 评分门槛 80
        """
        if not await self._check_permission(event, level=2):
            yield event.plain_result("❌ 权限不足：仅群管理员或超级管理员可用。")
            return

        # 1. 别名解析
        real_key = self.ALIAS_MAP.get(key, key) # 如果不在映射表，尝试直接用英文
        
        # 2. 合法性检查 (白名单)
        valid_keys = set(self.ALIAS_MAP.values()) 
        if real_key not in valid_keys:
            yield event.plain_result(f"❌ 未知参数 '{key}'。支持参数：\n" + "、".join(self.ALIAS_MAP.keys()))
            return

        # 3. 数值范围检查 (简单示例)
        if "rate" in real_key or ("limit" in real_key and "segment" not in real_key):
            if not (0.0 <= value <= 1.0) and "score" not in real_key: 
                 yield event.plain_result("❌ 数值超出范围 (0.0 - 1.0)")
                 return
        
        if "threshold" in real_key:
            if not (0 <= value <= 100):
                 yield event.plain_result("❌ 数值超出范围 (0 - 100)")
                 return
        
        # [新增] 针对长文阈值的检查
        if "no_segment_limit" in real_key:
            if value < 10:
                yield event.plain_result("❌ 长文阈值不能小于 10")
                return

        # 4. 执行更新
        chat_id = event.unified_msg_origin
        # [Fix] async await
        state = await self.state_manager.get_chat_state(chat_id)
        
        state.group_config[real_key] = value
        
        yield event.plain_result(f"✅ 已更新本群配置：{key} -> {value}")

    # =================================================================
    # Level 3: 超级管理员指令 (Super Admin)
    # =================================================================

    async def auto_set_admin(self, event: AstrMessageEvent):
        """读取群成员列表，将管理员和群主自动加入 HeartCore 群管"""
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足：需要超级管理员权限。")
            return

        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("⚠️ 目前仅支持 OneBot/NapCat 协议自动获取群管。")
            return

        try:
            client = event.bot
            group_id = event.get_group_id()
            if not group_id: return

            # 调用 API 获取群成员列表
            member_list = await client.api.call_action('get_group_member_list', group_id=int(group_id))
            
            new_admins = []
            for m in member_list:
                role = m.get('role')
                if role in ['owner', 'admin']:
                    uid = str(m.get('user_id'))
                    new_admins.append(uid)
            
            # 更新状态
            # [Fix] async await
            state = await self.state_manager.get_chat_state(event.unified_msg_origin)
            state.admin_list = list(set(state.admin_list + new_admins)) # 去重合并
             
            yield event.plain_result(f"✅ 操作成功！已添加 {len(new_admins)} 名群管。")

        except Exception as e:
            logger.error(f"Auto Set Admin Failed: {e}")
            yield event.plain_result(f"❌ 获取群成员列表失败: {e}")

    async def manual_set_admin(self, event: AstrMessageEvent, target: At):
        """手动设置某人为群管 (需要 @用户)"""
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足。")
            return
            
        target_qq = str(target.qq)
        # [Fix] async await
        state = await self.state_manager.get_chat_state(event.unified_msg_origin)
        
        if target_qq not in state.admin_list:
            state.admin_list.append(target_qq)
            yield event.plain_result(f"✅ 已将 {target_qq} 设为本群 HeartCore 管理员。")
        else:
            yield event.plain_result("ℹ️ 该用户已经是管理员了。")


    async def remove_admin(self, event: AstrMessageEvent, target: At):
        """取消某人的群管权限"""
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足。")
            return
            
        target_qq = str(target.qq)
        # [Fix] async await
        state = await self.state_manager.get_chat_state(event.unified_msg_origin)
        
        if target_qq in state.admin_list:
            state.admin_list.remove(target_qq)
            yield event.plain_result(f"✅ 已移除 {target_qq} 的管理员权限。")
        else:
            yield event.plain_result("ℹ️ 该用户不是管理员。")

    async def run_data_migration(self, event: AstrMessageEvent):
        """
        [新增] 执行数据迁移指令
        仅 Super Admin 可用
        """
        # 1. 严格鉴权 (Level 3)
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足：此操作仅限超级管理员执行。")
            return

        yield event.plain_result("🚀 正在后台执行数据迁移 (JSON -> SQLite)，请稍候...")

        try:
            # 2. 调用迁移逻辑
            # 通过 state_manager 获取 persistence 实例，它包含了正确的路径配置
            pm = self.state_manager.persistence
            report = await migrate_legacy_data(pm)
            
            yield event.plain_result(f"📊 **迁移完成报告**\n\n{report}")
            
        except Exception as e:
            logger.error(f"Data Migration Failed: {e}")
            yield event.plain_result(f"❌ 迁移过程中发生未捕获异常: {e}")

    # =================================================================
    # 鉴权核心逻辑
    # =================================================================

    async def _check_permission(self, event: AstrMessageEvent, level: int) -> bool:
        """
        权限检查器
        Level 1: User (Always True)
        Level 2: Group Admin (Config Owner OR Group Admin List OR API Role)
        Level 3: Super Admin (Config Super Admin)
        """
        sender_id = event.get_sender_id()
        
        # 1. 检查 Super Admin (Level 3 总是包含 Level 2)
        if str(sender_id) == str(self.config.super_admin_id):
            return True
            
        if level == 3:
            return False # 不是超管，直接拒

        # 2. 检查 Level 2 (Group Admin)
        if not self.config.enable_group_admin:
            return False # 未开启群管功能

        # [Fix] async await
        state = await self.state_manager.get_chat_state(event.unified_msg_origin)
        # 2a. 检查本地缓存列表
        if str(sender_id) in state.admin_list:
            return True
            
        # 2b. 兜底：实时 API 检查 (针对未执行过“一键设置”的情况)
        # 仅在 aiocqhttp 下生效
        if event.get_platform_name() == "aiocqhttp":
            try:
                group_id = event.get_group_id()
                if group_id:
                    info = await event.bot.api.call_action(
                        'get_group_member_info', 
                        group_id=int(group_id), 
                        user_id=int(sender_id)
                    )
                    if info.get('role') in ['owner', 'admin']:
                        return True
            except Exception:
                pass
        
        return False


    async def cmd_refresh_commands(self, event: AstrMessageEvent):
        """
        (v4.14.2) 手动刷新指令防火墙 (仅限超管)
        用于在系统完全启动后，补充漏网的指令，并输出完整列表。
        """
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足：仅限超级管理员。")
            return
            
        if not self.pre_filters:
            yield event.plain_result("❌ 内部错误：PreFilters 模块未注入。")
            return

        yield event.plain_result("🔄 正在重新扫描所有插件的注册指令...")

        # 调用加载逻辑 (它是累加的，不会丢失内置名单)
        await self.pre_filters.load_foreign_commands()
        
        # 获取统计信息
        total_count = len(self.pre_filters.foreign_commands)
        # 排序以保证输出整洁
        cmd_list = sorted(list(self.pre_filters.foreign_commands))
        
        # [修改] 构建完整展示文本 (移除 [:50] 限制)
        # 将列表连接成字符串
        full_list_str = ", ".join(cmd_list)
            
        msg = (
            f"✅ **指令库刷新完毕！**\n"
            f"🛡️ 当前已隔离防护 {total_count} 个指令词：\n"
            f"```\n{full_list_str}\n```\n"
            f"现在 HeartCore 不会对这些词进行闲聊回复了。"
        )
        yield event.plain_result(msg)


    async def cmd_bulk_update_identity(self, event: AstrMessageEvent, new_identity: str):
        """
        (v4.14) 超管指令：全量修改身份
        """
        # 1. 严格鉴权
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足：仅限超级管理员。")
            return

        if not new_identity:
            yield event.plain_result("❌ 请输入目标身份，例如：/全量修改身份 群友")
            return

        yield event.plain_result(f"⚠️ 正在将数据库中所有用户的身份重置为「{new_identity}」...")

        try:
            # 2. 数据库全量更新
            db_count = await self.state_manager.persistence.update_all_user_identities(new_identity)

            # 3. 内存缓存同步更新 (防止旧内存数据覆盖数据库)
            # 直接操作 state_manager 的缓存字典
            cached_profiles = self.state_manager.get_all_user_profiles_unsafe()
            mem_count = 0
            for uid, profile in cached_profiles.items():
                if profile.identity != new_identity:
                    profile.identity = new_identity
                    # 此时不需要设为 is_dirty=True，因为 DB 已经是最新的了。
                    # 且如果设为 Dirty，MaintenanceTask 稍后会执行一次无意义的写操作。
                    # 但为了逻辑一致性，不设 Dirty 是安全的，前提是内存已经变了。
                    mem_count += 1
            
            yield event.plain_result(f"✅ 全量更新完成！\n💾 数据库受影响行数: {db_count}\n🧠 内存同步更新: {mem_count} 个活跃用户")
            
        except Exception as e:
            logger.error(f"Bulk identity update failed: {e}")
            yield event.plain_result(f"❌ 操作失败: {e}")        


    # [新增] 查看人格
    async def cmd_view_persona(self, event: AstrMessageEvent):
        """
        (v4.14) 超管指令：查看当前人格
        """
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足。")
            return

        umo = event.unified_msg_origin
        # 1. 获取当前会话对应的人格 (v3 兼容格式)
        persona_v3 = await self.context.persona_manager.get_default_persona_v3(umo=umo)
        
        if not persona_v3:
            yield event.plain_result("⚠️ 当前会话未绑定任何有效人格 (Persona V3)。")
            return

        pid = persona_v3.get("name", "Unknown")
        prompt = persona_v3.get("prompt", "")
        
        # 2. 获取缓存中的摘要
        cached_data = self.persona_summarizer.cache.get(pid)
        
        info = f"🎭 **当前人格信息**\n\n"
        info += f"🆔ID/名称: {pid}\n"
        
        if cached_data:
            summary = cached_data.get("summarized", "（数据缺失）")
            style = cached_data.get("dynamic_style_guide", "（无）")
            info += f"📝 **当前摘要**:\n{summary}\n\n"
            info += f"🎨 **风格指南**:\n{style}"
        else:
            info += "⚠️ 缓存未命中：该人格尚未经过 HeartCore 摘要处理。\n(发送消息或使用 /重载人格 可触发生成)"
            
        yield event.plain_result(info)

    # [新增] 重载人格
    async def cmd_reload_persona(self, event: AstrMessageEvent):
        """
        (v4.14) 超管指令：强制重载人格
        """
        if not await self._check_permission(event, level=3):
            yield event.plain_result("❌ 权限不足。")
            return

        yield event.plain_result("🔄 正在请求小模型重新生成人格摘要，请稍候...")

        umo = event.unified_msg_origin
        persona_v3 = await self.context.persona_manager.get_default_persona_v3(umo=umo)
        
        if not persona_v3:
            yield event.plain_result("❌ 失败：当前会话未绑定有效人格。")
            return

        pid = persona_v3.get("name", "Unknown")
        original_prompt = persona_v3.get("prompt", "")

        try:
            # 调用强制刷新
            new_summary = await self.persona_summarizer.force_regenerate_summary(umo, pid, original_prompt)
            
            yield event.plain_result(f"✅ 人格 [{pid}] 重载成功！\n\n**最新摘要**:\n{new_summary}")
            
        except Exception as e:
            logger.error(f"Reload persona failed: {e}")
            yield event.plain_result(f"❌ 重载失败: {e}")            