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
                 config=None):
        self.context = context
        self.state_engine = state_engine
        self.gateway = gateway
        self.persistence = persistence
        self.config = config if config else gateway.config
        
        self._is_running = False
        self._task = None
        self._last_profile_run = 0


    async def start(self):
        """启动后台循环"""
        if self._is_running: return
        self._is_running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[AstrMai-Life] 🌱 生命循环已启动 (Proactive Task)")

    async def stop(self):
        """停止后台循环"""
        self._is_running = False
        if self._task:
            self._task.cancel()
            logger.info("[AstrMai-Life] 🛑 生命循环已停止")

    async def _loop(self):
        """主心跳循环"""
        while self._is_running:
            try:
                # 心跳间隔 60 秒
                await asyncio.sleep(60)
                
                # 1. 执行自然代谢 (Decay)
                await self._run_decay_task()
                
                # 2. 执行主动唤醒 (Wakeup)
                await self._run_wakeup_task()
                
                # 3. 执行深度侧写 (Profiling) - 低频 (每5分钟检查一次)
                if time.time() - self._last_profile_run > 300:
                    await self._run_profiling_task()
                    self._last_profile_run = time.time()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ProactiveTask] 循环异常: {e}")
                await asyncio.sleep(5)

    async def _run_decay_task(self):
        """代谢任务：遍历活跃状态执行衰减"""
        active_states = self.state_engine.get_active_states()
        for state in active_states:
            self.state_engine.apply_natural_decay(state)
            # 如果有脏数据，PersistenceManager 的定时任务会处理，这里不强制落盘

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
            if state.lock.locked(): continue # 正在处理消息
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
        """调用 System 2 生成有趣的开场白"""
        prompt = f"""
你是一个群聊活跃气氛的群友。这个群已经冷场很久了（超过2小时没人说话）。
请你根据你的设定，生成一个简短、有趣、自然的开场白，试图引起大家的讨论。
可以是分享一个生活小事、问一个无厘头的问题，或者发一个简短的感慨。
不要太生硬，不要像个机器人客服。
长度限制：20字以内。
直接输出内容，不要带引号。
"""
        # 使用 Gateway 调用 Planner (System 2)
        return await self.gateway.call_planner(prompt)

    async def _run_profiling_task(self):
        """侧写任务：扫描并生成用户画像"""
        # 阈值配置 (动态兼容，若 config 中未配则默认200)
        MSG_THRESHOLD = getattr(self.config.evolution, 'profile_threshold', 200) 
        
        profiles = self.state_engine.get_active_profiles()
        candidates = [
            p for p in profiles 
            if p.message_count_for_profiling > MSG_THRESHOLD
        ]
        
        if not candidates: return
        
        # 每次只处理一个，避免拥塞
        target = candidates[0]
        logger.info(f"[Life] 🕵️‍♂️ 触发深度侧写: 用户 {target.name} (Msg: {target.message_count_for_profiling})")
        
        await self._generate_persona_analysis(target)

    async def _generate_persona_analysis(self, profile):
        """生成并保存画像"""
        prompt = f"""
请基于用户 "{profile.name}" 与你的历史交互，构建深度人物画像。
他已经与你互动了 {profile.message_count_for_profiling} 次。

[任务]
请以“我”的视角，生成一段 100 字以内的**深度印象侧写**。
- 重点提取：具体的行为习惯、性格底色、对你的态度。
- 输出为一段流畅的自然语言文本，像老朋友的私密备注。
- 不要使用 Markdown 列表。

(由于当前无法获取全量历史，请基于你对他的一贯印象进行创作)
"""
        analysis = await self.gateway.call_planner(prompt)
        if analysis:
            profile.persona_analysis = analysis.strip()
            profile.message_count_for_profiling = 0 # 重置计数器
            profile.last_persona_gen_time = time.time()
            profile.is_dirty = True
            
            # 立即保存
            await self.persistence.save_user_profile(profile)
            logger.info(f"[Life] ✅ 画像生成完成: {analysis[:20]}...")