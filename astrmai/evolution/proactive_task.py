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
        self._last_diary_date = ""

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

    # [新增]
    def _fire_background_task(self, coro):
        """安全触发后台任务，接管游离 Task 防止 GC 销毁与静默崩溃"""
        if not hasattr(self, '_background_tasks'):
            self._background_tasks = set()
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

    # [新增]
    def _handle_task_result(self, task: asyncio.Task):
        """清理已完成的任务并暴露异常"""
        if hasattr(self, '_background_tasks'):
            self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[Proactive Task Error] 生命周期后台任务发生异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass

    # [新增] 将带抖动的耗时任务独立出去
    async def _run_daily_diary_task_with_jitter(self):
        """异步执行午夜日记任务，包含随机睡眠防熔断"""
        # 增加睡眠抖动 (Jitter)，打散多群组并发请求，防止熔断
        await asyncio.sleep(random.randint(1, 300))
        await self._run_daily_diary_task()

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

                # 4. 🟢 [核心修复 Bug 2] 午夜记忆日记任务 (凌晨 3-4 点执行)
                current_time_struct = time.localtime(now)
                current_hour = current_time_struct.tm_hour
                current_date = time.strftime("%Y-%m-%d", current_time_struct)
                
                if 3 <= current_hour < 4 and self._last_diary_date != current_date:
                    self._last_diary_date = current_date
                    # 将极其耗时的抖动休眠和日记任务抛入后台执行，绝不阻塞当前的心跳轮询！
                    self._fire_background_task(self._run_daily_diary_task_with_jitter())

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ProactiveTask] 循环异常: {e}")
                await asyncio.sleep(60)

    async def _run_decay_task(self):
        """[修改] 整合版代谢任务：异步化长时记忆物理衰减的状态文件读取与保存"""
        now = time.time()
        
        # 1. 群组级状态自然代谢 (精力恢复、情绪平复等)
        active_states = self.state_engine.get_active_states()
        for state in active_states:
            self.state_engine.apply_natural_decay(state)
            
        # 2. 用户级好感度缓慢衰减 (向趋中值 0 回落)
        active_profiles = self.state_engine.get_active_profiles()
        for profile in active_profiles:
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
        
        state_file = Path(get_astrbot_data_path()) / "plugin_data" / "astrmai" / "decay_state.json"
        last_decay_time = now
        
        # [修改点] 将同步文件读取放入线程池
        def _read_decay_state():
            with open(state_file, "r", encoding="utf-8") as f:
                return json.load(f)
                
        def _write_decay_state(data):
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        
        if state_file.exists():
            try:
                data = await asyncio.to_thread(_read_decay_state)
                last_decay_time = data.get("last_decay_time", now)
            except Exception as e:
                logger.error(f"[Life] 读取衰减状态文件失败: {e}")
        else:
            await asyncio.to_thread(_write_decay_state, {"last_decay_time": now})
                
        elapsed_seconds = now - last_decay_time
        days_missed = int(elapsed_seconds / decay_interval)
        
        if days_missed >= 1:
            decay_rate = getattr(self.config.memory, 'time_decay_rate', 0.01)
            logger.info(f"[Life] 🥀 触发长期记忆物理衰减，补偿天数: {days_missed}，衰减率: {decay_rate}")
            
            affected_rows = await self.memory_engine.apply_daily_decay(decay_rate=decay_rate, days=days_missed)
            logger.info(f"[Life] 🥀 物理衰减完成，重要度降低，共影响了 {affected_rows} 条记忆。")
            
            try:
                new_decay_time = last_decay_time + days_missed * decay_interval
                await asyncio.to_thread(_write_decay_state, {"last_decay_time": new_decay_time})
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
        """[修改] 生成并保存画像，支持增量更新与标签提取"""
        
        persona_id = getattr(self.config.persona, 'persona_id', "") or "global"
        
        if hasattr(self.persistence, 'load_persona_cache_async'):
            cache = await self.persistence.load_persona_cache_async()
        else:
            cache = self.persistence.load_persona_cache()
            
        persona_data = cache.get(persona_id, {})
        summary = persona_data.get("summary", "")
        
        persona_injection = f"\n[你的核心人设]: {summary}\n" if summary else ""

        # 提取旧画像和标签
        old_analysis = getattr(profile, "persona_analysis", "")
        if not old_analysis:
            old_analysis = "暂无旧画像"
            
        old_tags = getattr(profile, "tags", [])
        if isinstance(old_tags, list):
            old_tags_str = ", ".join(old_tags) if old_tags else "暂无标签"
        else:
            old_tags_str = str(old_tags)

        prompt = f"""{persona_injection}
请基于用户 "{profile.name}" 与你的历史交互，构建深度人物画像。
他近期已经在私聊中与你互动了 {profile.message_count_for_profiling} 次。

【该用户旧的画像】：{old_analysis}
【该用户旧的标签】：{old_tags_str}

[任务]
请结合以上旧的画像和标签，对该用户进行**增量更新**。
- 重点提取：具体的行为习惯、性格底色、近期的偏好、对你的态度。
- 请强制按 JSON 格式输出结果。必须包含 `tags`（字符串数组，提取3-5个偏好标签，如极客、二次元等）和 `analysis`（一段100字以内的深度印象侧写文本）。

严格返回格式示例：
{{
    "tags": ["标签1", "标签2"],
    "analysis": "这里是深度侧写文本..."
}}
"""
        result = await self.gateway.call_proactive_task(prompt)
        if result:
            import json
            import re
            
            tags = []
            analysis = ""
            
            try:
                # 尝试解析 JSON
                match = re.search(r'\{.*\}', result, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    tags = data.get("tags", [])
                    analysis = data.get("analysis", "")
            except Exception as e:
                from astrbot.api import logger
                logger.error(f"[Life] 解析增量画像 JSON 失败: {e}")
                
            if not analysis:
                analysis = result.strip()
                
            if analysis:
                profile.persona_analysis = analysis.strip()
            if tags:
                profile.tags = tags
                
            profile.message_count_for_profiling = 0 
            profile.last_persona_gen_time = time.time()
            profile.is_dirty = True
            
            await self.persistence.save_user_profile(profile)
            from astrbot.api import logger
            logger.info(f"[Life] ✅ 私聊画像增量挖掘完成: {analysis[:20]}... 标签: {tags}")


    async def _run_profiling_task(self):
        """深度侧写任务：筛选互动频次达标的用户，更新其心理画像"""
        # 获取所有活跃的用户档案
        active_profiles = self.state_engine.get_active_profiles()
        
        # 从配置中获取侧写触发阈值，默认设为 50 条消息
        threshold = getattr(self.config.life, 'profiling_msg_threshold', 200)
        
        for profile in active_profiles:
            # 如果该用户自上次侧写以来的互动次数达到阈值
            if getattr(profile, 'message_count_for_profiling', 0) >= threshold:
                from astrbot.api import logger
                logger.info(f"[Life] 🕵️ 用户 {profile.name} 私聊互动频次达标，开始进行增量心理侧写...")
                try:
                    await self._generate_persona_analysis(profile)
                except Exception as e:
                    from astrbot.api import logger
                    logger.error(f"[Life] 侧写生成失败 ({profile.name}): {e}")

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

                # 4. [新增] 午夜记忆日记任务 (凌晨 3-4 点执行)
                current_time_struct = time.localtime(now)
                current_hour = current_time_struct.tm_hour
                current_date = time.strftime("%Y-%m-%d", current_time_struct)
                
                if 3 <= current_hour < 4 and self._last_diary_date != current_date:
                    # 增加睡眠抖动 (Jitter)，打散多群组并发请求，防止熔断
                    await asyncio.sleep(random.randint(1, 300))
                    await self._run_daily_diary_task()
                    self._last_diary_date = current_date

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ProactiveTask] 循环异常: {e}")
                await asyncio.sleep(60)

    async def _run_daily_diary_task(self):
        """[修改] 午夜记忆日记撰写任务：不仅生成文本日记，还要触发深度的事件结构化、反思生成和节点提取。"""
        logger.info("[Life] 🌙 夜深了，机器大脑开始为各个群聊撰写每日内部日记并进行深度记忆归档...")
        active_states = self.state_engine.get_active_states()
        
        from ..infra.database import DatabaseService
        db = DatabaseService(self.persistence)
        
        persona_id = getattr(self.config.persona, 'persona_id', "") or "global"
        
        if hasattr(self.persistence, 'load_persona_cache_async'):
            cache = await self.persistence.load_persona_cache_async()
        else:
            cache = self.persistence.load_persona_cache()
            
        persona_data = cache.get(persona_id, {})
        summary = persona_data.get("summary", "")
        persona_injection = f"\n[你的核心人设]: {summary}\n" if summary else ""
        
        for state in active_states:
            group_id = state.chat_id
            if not group_id: continue
            
            # 🟢 1. 挂接自动触发系统：启动群组历史记忆批量回溯与节点提取
            if self.memory_engine and hasattr(self.memory_engine, 'summarizer'):
                logger.info(f"[Life] 启动群 {group_id} 的长对话历史回溯与多维节点打点...")
                if hasattr(self.memory_engine.summarizer, 'extract_and_summarize_history'):
                    await self.memory_engine.summarizer.extract_and_summarize_history(group_id, days=1)

            # 🟢 2. 生成文本日记和每日自由感悟
            recent_memories = []
            if self.memory_engine and hasattr(self.memory_engine, 'get_recent_memories'):
                recent_memories = await self.memory_engine.get_recent_memories(group_id, hours=24)
                
            recent_jargons = []
            if hasattr(db, 'get_recent_jargons_async'):
                recent_jargons = await db.get_recent_jargons_async(group_id, hours=24)
            elif hasattr(db, 'get_recent_jargons'):
                recent_jargons = db.get_recent_jargons(group_id, hours=24)
                
            mood_val = state.mood
            energy_val = state.energy
            
            if not recent_memories and not recent_jargons and abs(mood_val) < 0.2:
                logger.debug(f"[Life] 📖 群 {group_id} 今天非常安静且情绪平稳，跳过日记撰写。")
                continue
                
            memory_context = "\n".join([f"- {m}" for m in recent_memories]) if recent_memories else "无特别事实发生。"
            jargon_context = "\n".join([f"- {j.content} (含义: {j.meaning})" for j in recent_jargons]) if recent_jargons else "无新学词汇。"
            
            import time
            prompt = f"""你现在进入了深度睡眠的“潜意识反思模式”。
今天是 {time.strftime("%Y年%m月%d日")}。夜深人静，你需要静下心来，写一篇属于你自己的“内部私人日记”。
你正在回顾群聊【{group_id}】今天发生的事情。

{persona_injection}

以下是你脑海中提取出的今天关于这个群的“记忆残片”和“新知识”：
【今日事实记忆】：
{memory_context}

【今日新学词汇/黑话】：
{jargon_context}

【入睡前的心情指数】：
当前心情指数为 {mood_val:.2f} (-1.0 到 1.0)。精力剩余 {energy_val:.2f}。

[日记撰写要求]
1. 视角：必须以第一人称（“我”）的语气撰写，完全沉浸于你的人设。
2. 内容结构：
   - 概括：今天在这个群里，我都经历了什么？（把零散的事实串联起来）
   - 沉淀：今天我有什么新收获？（比如新黑话，或者对某些群友的新看法）
   - 情绪：结合今天发生的事和入睡前的心情指数，解释一下“我为什么会有这种心情”。
3. 格式：纯文本内心独白，分段落，150字到300字左右。绝对不要使用 Markdown 列表，要像真正的人类日记一样自然连贯。
"""
            try:
                diary_content = await self.gateway.call_proactive_task(prompt)
                
                if diary_content and self.memory_engine:
                    import datetime
                    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
                    diary_entry = f"【主观情景记忆/私人日记】 {time.strftime('%Y年%m月%d日')} 关于群 {group_id} 的回忆：\n{diary_content}"
                    
                    await self.memory_engine.add_memory(
                        content=diary_entry,
                        session_id=str(group_id),
                        importance=0.95  
                    )
                    
                    # 🟢 [新增] 存入 DailyReflection 表供长期调取查阅
                    plugin = getattr(self.gateway.context, 'astrmai_plugin', None)
                    if plugin and hasattr(plugin, 'db_service') and hasattr(plugin.db_service, 'save_reflection_async'):
                        await plugin.db_service.save_reflection_async(date_str, diary_content)

                    logger.info(f"[Life] 📖 为群 {group_id} 撰写日记并生成反思完成: {diary_content[:20]}...")
            except Exception as e:
                logger.error(f"[Life] 生成群 {group_id} 的日记失败: {e}")