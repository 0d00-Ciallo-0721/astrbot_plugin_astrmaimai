# heartflow/features/proactive_task.py
# (v4.14 - Refactored: Decoupled Persona Generation Logic)

import asyncio
import json
import time
import random
from typing import List, Dict, Optional
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.event import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from json.decoder import JSONDecodeError

from ..config import HeartflowConfig
from ..core.state_manager import StateManager
from ..utils.prompt_builder import PromptBuilder
from ..features.persona_summarizer import PersonaSummarizer
from ..utils.api_utils import elastic_simple_text_chat
from ..persistence import PersistenceManager

class ProactiveTask:
    """
    (v4.14) 主动话题与画像任务管理器
    职责：
    1. 状态被动衰减 (情绪/精力)
    2. 主动发起话题 (Proactive Chat)
    3. 用户画像生成 (后台任务 + 主动调用)
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 state_manager: StateManager,
                 prompt_builder: PromptBuilder,
                 persona_summarizer: PersonaSummarizer,
                 persistence: PersistenceManager
                 ):
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.prompt_builder = prompt_builder
        self.persona_summarizer = persona_summarizer
        self.persistence = persistence
        self._last_profiling_time = 0.0

    async def run_task(self):
        """
        后台任务主循环
        """
        logger.info("💖 HeartFlow (v4.14): 主动话题/状态衰减任务已启动。")
        while True:
            try:
                check_interval = self.config.proactive_check_interval_seconds
                await asyncio.sleep(max(60, check_interval))
                
                if not self.config.enable_heartflow:
                    continue

                energy_threshold = self.config.proactive_energy_threshold
                silence_threshold = self.config.proactive_silence_threshold_minutes
                global_cooldown = self.config.proactive_global_cooldown_seconds
                
                chat_ids = list(self.state_manager.get_all_states().keys())
                
                # 尝试执行后台画像生成 (F11)
                await self._run_persona_profiling_task()
                
                for chat_id in chat_ids:
                    # --- 1. 状态被动衰减 ---
                    
                    # 精力被动恢复
                    self.state_manager._apply_passive_decay(chat_id)
                    
                    # 情绪被动平复
                    chat_state = self.state_manager.get_chat_state_readonly(chat_id)
                    if not chat_state:
                        continue 

                    now = time.time()
                    decay_interval_sec = self.config.emotion_decay_interval_hours * 3600
                    if now - chat_state.last_passive_decay_time > decay_interval_sec:
                        chat_state.last_passive_decay_time = now 
                        
                        original_mood = chat_state.mood
                        if chat_state.mood > 0:
                            chat_state.mood = max(0.0, chat_state.mood - self.config.mood_decay)
                        elif chat_state.mood < 0:
                            chat_state.mood = min(0.0, chat_state.mood + self.config.mood_decay)
                        
                        if original_mood != chat_state.mood:
                            logger.debug(f"[{chat_id[:10]}] (ProactiveTask) 情绪平复，心情 -> {chat_state.mood:.2f}")

                    # --- 2. 主动话题检查 ---
                    if not self.config.proactive_enabled:
                        continue
                    
                    if chat_state.lock.locked():
                        continue

                    if self.config.whitelist_enabled and chat_id not in self.config.chat_whitelist:
                        continue
                    
                    minutes_silent = 999
                    if chat_state.last_reply_time != 0:
                        minutes_silent = (time.time() - chat_state.last_reply_time) / 60
                    
                    if (chat_state.energy > energy_threshold and 
                        minutes_silent > silence_threshold and 
                        minutes_silent != 999):
                        
                        logger.info(f"[群聊] 心流：{chat_id[:20]}... 满足主动冒泡条件。")
                        
                        original_prompt = await self.prompt_builder._get_persona_system_prompt_by_umo(chat_id)
                        summarized_prompt = await self.persona_summarizer.get_or_create_summary(chat_id, "default", original_prompt)
                        
                        topic_idea_text = None

                        # A. 尝试恢复旧话题
                        try:
                            resume_prompt = await self.prompt_builder.build_resume_topic_prompt(chat_id)
                            if resume_prompt:
                                providers_to_try = []
                                if self.config.summarize_provider_name:
                                    providers_to_try.append(self.config.summarize_provider_name)
                                else:
                                    providers_to_try.extend(self.config.general_pool)
                                
                                if providers_to_try:
                                    max_retries = 2
                                    for attempt in range(max_retries + 1):
                                        try:
                                            decision_text = await elastic_simple_text_chat(
                                                self.context, 
                                                providers_to_try, 
                                                resume_prompt,
                                                system_prompt=""
                                            )
                                            if not decision_text: continue

                                            content = decision_text.strip()
                                            if content.startswith("```json"): content = content[7:-3].strip()
                                            elif content.startswith("```"): content = content[3:-3].strip()
                                            
                                            data = json.loads(content)
                                            if data.get("is_interesting") and data.get("was_interrupted") and data.get("topic_summary"):
                                                topic_idea_text = f"继续我们之前聊到的 “{data.get('topic_summary')}”"
                                            break
                                        except (json.JSONDecodeError, JSONDecodeError):
                                            pass
                        except Exception:
                            pass
                        
                        # B. 弹性生成新话题
                        opening_line_text = None
                        
                        providers_to_try = []
                        if self.config.summarize_provider_name:
                            providers_to_try.append(self.config.summarize_provider_name)
                        else:
                            providers_to_try.extend(self.config.general_pool)
                        
                        if not providers_to_try:
                             continue

                        if not topic_idea_text:
                            topic_idea_prompt = self.prompt_builder.build_proactive_idea_prompt(summarized_prompt, int(minutes_silent))
                            topic_idea_text = await elastic_simple_text_chat(
                                self.context,
                                providers_to_try,
                                topic_idea_prompt,
                                system_prompt=summarized_prompt
                            )

                        if topic_idea_text:
                            opening_line_prompt = self.prompt_builder.build_proactive_opening_prompt(summarized_prompt, topic_idea_text)
                            opening_line_text = await elastic_simple_text_chat(
                                self.context,
                                providers_to_try,
                                opening_line_prompt,
                                system_prompt=summarized_prompt
                            )

                            if opening_line_text:
                                message_chain = MessageChain().message(opening_line_text)
                                await self.context.send_message(chat_id, message_chain)
                                self.state_manager._consume_energy_for_proactive_reply(chat_id)
                                chat_state.mood = 0.0
                                logger.info(f"💖 [群聊] 心流：已向 {chat_id[:20]}... 发送主动话题 (情绪已重置)。")
                                await asyncio.sleep(global_cooldown)
                                
            except asyncio.CancelledError:
                logger.info("💖 心流：主动话题任务被取消。")
                break
            except Exception as e:
                logger.error(f"心流：主动话题任务异常: {e}")

    # =================================================================
    # 画像生成核心逻辑 (v4.14 重构)
    # =================================================================

    async def generate_persona_for_user(self, user_id: str, limit: int = 500) -> Optional[str]:
        """
        (v4.14 优化) 为指定用户生成画像
        新增：
        1. 最小消息检查 (>100条)
        2. 最大消息限制 (默认500条)
        3. 成功后重置计数器
        """
        try:
            # 1. 获取 Profile
            target_profile = await self.state_manager.get_user_profile(user_id)
            
            # 2. 跨群数据拉取
            raw_messages = []
            valid_groups = []
            now = time.time()
            for gid, data in target_profile.group_footprints.items():
                last_active = data.get("last_active_time", 0)
                if now - last_active < 3 * 86400: # 3天内
                    valid_groups.append((gid, data.get("message_weight", 0)))
            
            valid_groups.sort(key=lambda x: x[1], reverse=True)
            top_groups = valid_groups[:3]
            
            for gid, _ in top_groups:
                try:
                    curr_cid = await self.context.conversation_manager.get_curr_conversation_id(gid)
                    if not curr_cid: continue
                    
                    conv = await self.context.conversation_manager.get_conversation(gid, curr_cid)
                    if not conv or not conv.history: continue
                    
                    history = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
                    
                    for msg in history:
                        content = msg.get("content", "")
                        if target_profile.name in content: 
                            raw_messages.append(f"[{gid}] {content}")
                except Exception as e:
                    logger.warning(f"拉取群 {gid} 历史失败: {e}")

            # --- [新增] 消息数量检查 ---
            msg_count = len(raw_messages)
            if msg_count < 100:
                logger.warning(f"画像生成中止: 用户 {target_profile.name} 消息过少 ({msg_count} < 100)。")
                return "NOT_ENOUGH_MESSAGES" # 返回特殊标记

            # 3. 截断与排序 (使用新的 limit 默认 500)
            raw_messages = raw_messages[-limit:] 
            context_str = "\n".join(raw_messages)

            # 4. 调用 LLM 生成画像
            profiling_prompt = f"""
请基于以下聊天记录，深度构建用户 "{target_profile.name}" 的人物画像。
你需要透过聊天记录，看到一个“活生生的人”，而不仅仅是性格标签的堆砌。

[用户自我认知身份]
**{target_profile.identity}**
(请结合此身份解读他的行为)

[聊天记录片段 ({len(raw_messages)}条)]
{context_str}

[分析核心：从性格到事实]
请重点提取**具体的行为习惯**，而不仅仅是抽象的性格形容词。
1. **常态行为（重点）**：他经常在群里具体干什么？
   - (例如：不是简单的“幽默”，而是“经常发地狱笑话图”)
   - (例如：不是简单的“热心”，而是“喜欢在深夜帮新人解答代码问题”)
2. **性格底色**：基于上述行为表现出的内在性格，他是外向还是内向？幽默还是严肃？傲娇还是直球？。
3. **兴趣爱好**：基于上述聊记录总结他喜欢什么话题？(游戏、技术、二次元等)
4. **交互惯性**：他习惯怎么对待“我”？（调戏、依赖、把它当工具人、还是当朋友？）

[输出要求]
请以“我”的视角，生成一段 100 字以内的**深度印象侧写**。
- **必须包含具体的行为细节**。
- 输出为一段流畅的自然语言文本，**不要**使用 Markdown 列表或标题。
- 让这段描述看起来像是你对老朋友的私密备注。
"""
            # 使用 summarizer 模型或全局池
            providers = []
            if self.config.summarize_provider_name:
                providers.append(self.config.summarize_provider_name)
            else:
                providers.extend(self.config.general_pool)
            
            if not providers: return None

            analysis = await elastic_simple_text_chat(
                self.context, providers, profiling_prompt, 
                system_prompt="你是一位敏锐的心理侧写师。"
            )
            
            if analysis:
                # 5. 保存结果 & 清零计数器
                target_profile.persona_analysis = analysis.strip()
                # [核心逻辑] 主动调用也会清零计数，防止后台任务重复跑
                target_profile.message_count_for_profiling = 0 
                target_profile.last_persona_gen_time = time.time()
                
                await self.persistence.save_user_profile(target_profile)
                
                logger.info(f"💖 [画像生成] 完成: {target_profile.name} -> {analysis[:20]}...")
                self._last_profiling_time = time.time()
                return analysis
            
            return None

        except Exception as e:
            logger.error(f"Generate Persona Error: {e}", exc_info=True)
            return None

    async def _run_persona_profiling_task(self):
        """
        (F11) 后台自动画像总结任务
        条件: Bot空闲 & 间隔>5min & 用户数据达标
        """
        if not self.config.enable_user_profiles:
            return

        # 1. 频率与空闲检查
        if time.time() - self._last_profiling_time < 300: # 5分钟间隔
            return
        
        # 检查是否所有群都空闲
        for state in self.state_manager.get_all_states().values():
            if state.lock.locked():
                return 

        # 2. 寻找目标用户
        # 遍历所有用户，找到满足条件的 (Msg > 200, Score > 80)
        candidates = []
        for uid, profile in self.state_manager.get_all_user_profiles().items():
            if (profile.message_count_for_profiling > 200 and 
                profile.social_score > 80):
                candidates.append(profile)
        
        if not candidates:
            return
            
        # 按消息数倒序，取第一个
        candidates.sort(key=lambda p: p.message_count_for_profiling, reverse=True)
        target_profile = candidates[0]
        
        logger.info(f"💖 [后台画像任务] 选中用户 {target_profile.name} (Msg: {target_profile.message_count_for_profiling})")

        # 3. 调用核心生成逻辑 (复用)
        # 后台任务使用默认的消息拉取数量 (代码里目前是硬编码的, 但 generate_persona_for_user 默认 200)
        await self.generate_persona_for_user(target_profile.user_id, limit=200)