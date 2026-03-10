# astrmai/evolution/proactive_task.py
import asyncio
import time
import random
from typing import List
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.event import MessageChain

from ..Heart.state_engine import StateEngine
from ..infra.gateway import GlobalModelGateway
from ..infra.persistence import PersistenceManager

class ProactiveTask:
    """
    主动任务与生命周期管理器 (Phase 6: Subconscious & Lifecycle)
    职责:
    1. 代谢 (Metabolism): 随时间流逝恢复精力、平复情绪。
    2. 唤醒 (Wakeup): 在冷场时主动发起话题。
    3. 侧写 (Profiling): 对高频互动用户进行深度心理画像。
    """
    def __init__(self, 
                 context: Context, 
                 state_engine: StateEngine, 
                 gateway: GlobalModelGateway,
                 persistence: PersistenceManager,
                 memory_engine = None,  # [新增参数] 注入记忆引擎
                 config=None):
        self.context = context
        self.state_engine = state_engine
        self.gateway = gateway
        self.persistence = persistence
        self.memory_engine = memory_engine
        self.config = config if config else gateway.config
        
        self._is_running = False
        self._task = None
        self._last_profile_run = 0

    async def start(self):
        """[修改] 启动多维后台主动任务循环 (心跳机制)"""
        if self._is_running:
            return
        self._is_running = True
        logger.info("[Life] 🌱 潜意识与生命周期循环已启动...")
        
        self._task = asyncio.create_task(self._loop())

    async def stop(self):
        """停止后台循环"""
        self._is_running = False
        if self._task:
            self._task.cancel()
            logger.info("[AstrMai-Life] 🛑 生命循环已停止")

    async def _loop(self):
        """[修改] 维持后台心跳与任务调度"""
        while self._is_running:
            try:
                # 心跳间隔 60 秒
                await asyncio.sleep(60)
                
                # 1. 执行自然代谢 (Decay)
                await self._run_decay_task()
                
                # 2. 执行主动唤醒 (Wakeup)
                await self._run_wakeup_task()
                
                # 3. 深度侧写任务 (Profiling)
                now = time.time()
                if now - self._last_profile_run > 3600: # 每小时巡检一次侧写
                    await self._run_profiling_task()
                    self._last_profile_run = now

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ProactiveTask] 循环异常: {e}")
                await asyncio.sleep(60)

    async def _run_decay_task(self):
        """整合版代谢任务：包含精力恢复、情绪平复、好感度衰减与长期记忆物理衰减"""
        now = time.time()
        
        # 1. 群组级状态自然代谢 (精力恢复、情绪平复等)
        active_states = self.state_engine.get_active_states()
        for state in active_states:
            self.state_engine.apply_natural_decay(state)
            
        # 2. 用户级好感度缓慢衰减 (向趋中值 0 回落)
        active_profiles = self.state_engine.get_active_profiles()
        for profile in active_profiles:
            # 假设每 24 小时自然衰减 1 点好感度（仅对绝对值大于10的生效）
            if now - profile.last_access_time > 86400: # 一天未交互
                old_score = profile.social_score
                if old_score > 10:
                    profile.social_score -= 1
                elif old_score < -10:
                    profile.social_score += 1
                    
                if old_score != profile.social_score:
                    profile.is_dirty = True
                    profile.last_access_time = now
                    logger.debug(f"[Life] 🍂 时间流逝: 用户 {profile.name} 的好感度自然衰减至 {profile.social_score}")            

        # 3. 长期记忆物理衰减 (带错过补偿机制)
        if not self.memory_engine:
            return
            
        decay_interval = 86400  # 物理衰减周期：24小时
        
        import os
        import json
        from pathlib import Path
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path
        
        # 借鉴 LivingMemory：使用文件记录上次衰减时间，防止重启丢失状态
        state_file = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "decay_state.json"
        last_decay_time = now
        
        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    last_decay_time = data.get("last_decay_time", now)
            except Exception as e:
                logger.error(f"[Life] 读取衰减状态文件失败: {e}")
        else:
            # 第一次运行，初始化时间
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"last_decay_time": now}, f)
                
        elapsed_seconds = now - last_decay_time
        days_missed = int(elapsed_seconds / decay_interval)
        
        # 如果距离上次执行已经超过 1 天（含错过多天的情况）
        if days_missed >= 1:
            decay_rate = getattr(self.config.memory, 'time_decay_rate', 0.01)
            logger.info(f"[Life] 🥀 触发长期记忆物理衰减，补偿天数: {days_missed}，衰减率: {decay_rate}")
            
            # 批量执行底层 SQL 衰减
            affected_rows = await self.memory_engine.apply_daily_decay(decay_rate=decay_rate, days=days_missed)
            logger.info(f"[Life] 🥀 物理衰减完成，重要度降低，共影响了 {affected_rows} 条记忆。")
            
            # 更新状态文件，推进最后执行时间（保留余数）
            try:
                new_decay_time = last_decay_time + days_missed * decay_interval
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump({"last_decay_time": new_decay_time}, f)
            except Exception as e:
                logger.error(f"[Life] 保存衰减状态失败: {e}")

    async def _run_wakeup_task(self):
        """唤醒任务：检测冷场并尝试发言"""
        active_states = self.state_engine.get_active_states()
        now = time.time()
        
        # 配置阈值 (接入 Config)
        SILENCE_THRESHOLD_MIN = self.config.life.silence_threshold
        ENERGY_THRESHOLD = self.config.life.wakeup_min_energy
        WAKEUP_COST = self.config.life.wakeup_cost
        WAKEUP_COOLDOWN = self.config.life.wakeup_cooldown
        
        for state in active_states:
            # 基础过滤
            if hasattr(state, "lock") and state.lock.locked(): continue # 正在处理消息
            if not state.chat_id: continue
            
            # 计算静默时间
            minutes_silent = 999
            if state.last_reply_time > 0:
                minutes_silent = (now - state.last_reply_time) / 60
                
            # 判定条件
            if (minutes_silent > SILENCE_THRESHOLD_MIN and 
                state.energy > ENERGY_THRESHOLD and 
                minutes_silent != 999):
                
                # 冷却检查 (防止频繁唤醒，利用 next_wakeup_timestamp)
                if now < state.next_wakeup_timestamp:
                    continue
                
                logger.info(f"[Life] 💤 发现群 {state.chat_id} 冷场 {int(minutes_silent)} 分钟，尝试主动发起话题...")
                
                # 生成开场白
                opening = await self._generate_opening_line(state.chat_id)
                if opening:
                    # 发送消息
                    try:
                        from astrbot.api.event import MessageChain
                        chain = MessageChain().message(opening)
                        await self.context.send_message(state.chat_id, chain)
                        
                        # 消耗精力并设置冷却 (接入 Config)
                        await self.state_engine.consume_energy(state.chat_id, amount=WAKEUP_COST)
                        state.next_wakeup_timestamp = now + WAKEUP_COOLDOWN
                        logger.info(f"[Life] 🗣️ 主动破冰成功: {opening}")
                    except Exception as e:
                        logger.error(f"[Life] 发送主动消息失败: {e}")

    async def _generate_opening_line(self, chat_id: str) -> str:
        """调用主动任务模型生成有趣的开场白，并注入人设防止 OC"""
        
        # [新增] 从持久化缓存中读取人设压缩摘要
        persona_id = getattr(self.config.persona, 'persona_id', "") or "global"
        cache = self.persistence.load_persona_cache()
        persona_data = cache.get(persona_id, {})
        summary = persona_data.get("summary", "")
        style = persona_data.get("style", "")
        
        persona_injection = ""
        if summary:
            persona_injection = f"\n[你的核心人设]: {summary}\n[回复风格]: {style}\n"

        prompt = f"""
你是一个群聊活跃气氛的群友。这个群已经冷场很久了（超过2小时没人说话）。
请你完全沉浸于以下设定中：{persona_injection}

请你根据你的设定，生成一个简短、有趣、自然的开场白，试图引起大家的讨论。
可以是分享一个生活小事、问一个无厘头的问题，或者发一个简短的感慨。
不要太生硬，不要像个机器人客服。
长度限制：20字以内。
直接输出内容，不要带引号。
"""
        # [修改点] 调用主动任务专项模型接口 (Phase 4 重构)
        return await self.gateway.call_proactive_task(prompt)

    # [修改] 具体位置：类 ProactiveTask 中
    async def _generate_persona_analysis(self, profile):
        """生成并保存画像，并注入人设防止 OC"""
        
        # [新增] 从持久化缓存中读取人设压缩摘要
        persona_id = getattr(self.config.persona, 'persona_id', "") or "global"
        cache = self.persistence.load_persona_cache()
        persona_data = cache.get(persona_id, {})
        summary = persona_data.get("summary", "")
        
        persona_injection = f"\n[你的核心人设]: {summary}\n" if summary else ""

        prompt = f"""{persona_injection}
请基于用户 "{profile.name}" 与你的历史交互，构建深度人物画像。
他已经与你互动了 {profile.message_count_for_profiling} 次。

[任务]
请以“我”（符合你的人设设定）的视角，生成一段 100 字以内的**深度印象侧写**。
- 重点提取：具体的行为习惯、性格底色、对你的态度。
- 输出为一段流畅的自然语言文本，像老朋友的私密备注。
- 不要使用 Markdown 列表。

(由于当前无法获取全量历史，请基于你对他的一贯印象进行创作)
"""
        # [修改点] 调用主动任务专项模型接口 (Phase 4 重构)
        analysis = await self.gateway.call_proactive_task(prompt)
        if analysis:
            profile.persona_analysis = analysis.strip()
            profile.message_count_for_profiling = 0 # 重置计数器
            profile.last_persona_gen_time = time.time()
            profile.is_dirty = True
            
            # 立即保存
            await self.persistence.save_user_profile(profile)
            logger.info(f"[Life] ✅ 画像生成完成: {analysis[:20]}...")


    async def _run_profiling_task(self):
        """深度侧写任务：筛选互动频次达标的用户，更新其心理画像"""
        # 获取所有活跃的用户档案
        active_profiles = self.state_engine.get_active_profiles()
        
        # 从配置中获取侧写触发阈值，默认设为 50 条消息
        threshold = getattr(self.config.life, 'profiling_msg_threshold', 200)
        
        for profile in active_profiles:
            # 如果该用户自上次侧写以来的互动次数达到阈值
            if getattr(profile, 'message_count_for_profiling', 0) >= threshold:
                logger.info(f"[Life] 🕵️ 用户 {profile.name} 互动频次达标，开始进行深度心理侧写...")
                try:
                    await self._generate_persona_analysis(profile)
                except Exception as e:
                    logger.error(f"[Life] 侧写生成失败 ({profile.name}): {e}")            