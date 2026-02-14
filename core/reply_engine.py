# heartflow/core/reply_engine.py
# (v22.1 - Formatting & Segmentation Update)

import json
import random
import re   # [新增] 正则处理
import asyncio # [新增] 异步延迟
from typing import List, Dict, Any, Optional
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import LLMResponse
import astrbot.api.message_components as Comp

# 导入内部模块
from ..datamodels import BrainActionPlan, ChatState, UserProfile
from ..config import HeartflowConfig
from ..utils.prompt_builder import PromptBuilder
from ..persistence import PersistenceManager
from ..core.state_manager import StateManager
from ..core.mood_manager import MoodManager
from ..meme_engine.meme_config import MEMES_DIR
from ..meme_engine.meme_sender import send_meme

class ReplyEngine:
    """
    (v22.1) 回复引擎
    职责：
    1. 接收 BrainPlan
    2. 生成回复
    3. [新增] 清洗回复格式 (去除时间戳和名字前缀)
    4. [新增] 拟人化分段发送
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 prompt_builder: PromptBuilder, 
                 state_manager: StateManager, 
                 persistence: PersistenceManager,
                 mood_manager: MoodManager
                 ):
        self.context = context
        self.config = config
        self.prompt_builder = prompt_builder
        self.state_manager = state_manager
        self.persistence = persistence
        self.mood_manager = mood_manager
        self.bot_name: str = None

    async def fetch_bot_name(self):
        """获取机器人昵称"""
        await self.prompt_builder._fetch_bot_name_from_context()
        self.bot_name = self.prompt_builder.bot_name

    async def send_plain_text(self, event: AstrMessageEvent, content: str, history_hint: str = None):
        """
        跳过 LLM 直接发送文本 (用于复读机等)
        """
        if not content: return

        # 1. 直接发送
        await event.send(event.plain_result(content))
        logger.info(f"ReplyEngine: 直发消息 (复读): {content}")

        # 2. 保存历史
        save_content = content
        if history_hint:
            save_content = f"{content} ({history_hint})"
        
        await self.persistence.save_history_message(
            event.unified_msg_origin, "assistant", save_content, self.bot_name
        )
        
        # 3. 发送表情
        await self._send_meme(event, self.config.emotions_probability)

    # =================================================================
    # [新增] 文本清洗与分段工具函数
    # =================================================================
    
    def _clean_reply_content(self, text: str) -> str:
        """
        清洗 LLM 输出的日志格式污染
        1. 去除开头的 [HH:MM:SS]
        2. 去除开头的 BotName:
        """
        if not text: return ""
        
        # 1. 过滤开头的 [时间戳] (e.g., [21:43:03])
        text = re.sub(r'^\[.*?\]\s*', '', text)
        
        # 2. 过滤开头的 机器人名字: (e.g., 萝卜子: )
        names_to_filter = []
        if self.bot_name: 
            names_to_filter.append(re.escape(self.bot_name))
        if self.config.bot_nicknames:
            names_to_filter.extend([re.escape(n) for n in self.config.bot_nicknames if n])
        
        if names_to_filter:
            # [修复] 将 (?i) 移到最前面，放在 ^ 之前
            # 错误: f"^(?i)..."
            # 正确: f"(?i)^..."
            pattern_str = f"(?i)^({'|'.join(names_to_filter)})[：:]\s*"
            
            try:
                text = re.sub(pattern_str, '', text)
            except re.error as e:
                logger.error(f"正则清洗失败: {e}")
            
        return text.strip()

    def _segment_reply_content(self, text: str) -> List[str]:
        """
        (v4.13) 拟人化分段算法
        新增：颜文字保护、长文不分段阈值、表情包过滤
        """
        if not self.config.enable_segmentation:
            return [text]
            
        # 1. 长文阈值检查 (F5)
        if len(text) > self.config.no_segment_limit:
            logger.debug(f"回复长度 {len(text)} 超过阈值 {self.config.no_segment_limit}，跳过分段。")
            return [text]

        text = re.sub(r'[\U0001F600-\U0001F64F]', '', text) 

        threshold = self.config.segmentation_threshold
        segments = []
        
        # 3. 颜文字保护 (F5) - [修正] 使用非贪婪匹配 .*?
        kaomoji_pattern = r'(\(.*?\)|（.*?）|¯\\_.*_/¯)' 
        kaomojis = []
        
        def replace_kaomoji(match):
            k = match.group(0)
            # 简单启发式：如果括号里包含非汉字且长度较短，视为颜文字
            if len(k) < 15 and not re.search(r'[\u4e00-\u9fa5]{3,}', k):
                kaomojis.append(k)
                return f"__KAOMOJI_{len(kaomojis)-1}__"
            return k

        protected_text = re.sub(kaomoji_pattern, replace_kaomoji, text)

        # 4. 标准分段逻辑
        split_pattern = r'([。！？；!?;~]+)'
        parts = re.split(split_pattern, protected_text)
        
        current_segment = ""
        for part in parts:
            if not part: continue
            
            if re.match(split_pattern, part):
                if len(current_segment) >= threshold:
                    segments.append(current_segment.strip())
                    current_segment = "" 
                else:
                    current_segment += part 
            else:
                current_segment += part
        
        if current_segment.strip():
            segments.append(current_segment.strip())
            
        # 5. 还原颜文字
        final_segments = []
        for seg in segments:
            for i, k in enumerate(kaomojis):
                seg = seg.replace(f"__KAOMOJI_{i}__", k)
            final_segments.append(seg)
            
        return final_segments

    # =================================================================
    # [修改] 核心回复处理逻辑
    # =================================================================

    async def handle_reply(self, event: AstrMessageEvent, 
                           plan: object, # 兼容 BrainActionPlan 和 ImpulseDecision
                           is_poke_or_nickname: bool = False):
        """
        统一回复入口 (HeartCore 2.0 适配版)
        职责：指令提取 -> 生成回复 -> 反AI腔调 -> 动作解析 -> 发送 -> 副作用
        """
        # 1. 获取指令
        # 兼容 dataclass 和 dict (ImpulseDecision)
        if isinstance(plan, dict):
            thought = plan.get("thought", "")
        else:
            thought = getattr(plan, "thought", "")
        
        # 2. 生成回复
        reply_text = await self._generate_reply(event, thought)
        if not reply_text: return

        # 3. 🛡️ Anti-AI Slop (反 AI 腔调防御)
        if self._is_ai_slop(reply_text):
            logger.warning(f"🛡️ [Anti-Slop] Detected AI-like text: {reply_text[:20]}... (Consider retrying)")
            # 简单策略：降级为短语或重试 (这里简化为打个日志，生产环境可触发重生成)

        # 4. 💪 Physical Actions (肢体动作解析)
        # 提取 (poke), (sigh) 等标记
        actions = self._extract_actions(reply_text)
        
        # 5. 清洗动作标记，得到纯净文本
        clean_text = self._clean_actions(reply_text)
        
        # --- (原逻辑) 状态更新副作用 ---
        chat_state = await self.state_manager.get_chat_state(event.unified_msg_origin)
        user_profile = await self.state_manager.get_user_profile(event.get_sender_id())
        
        # 情绪分析
        (reply_mood_tag, new_mood_value) = await self.mood_manager.analyze_text_mood(clean_text, chat_state)
        event.set_extra("heartcore_mood_tag", reply_mood_tag)
        self._update_state_after_reply(chat_state, new_mood_value, is_poke_or_nickname, user_profile)

        # 6. 发送文本 (调用分段逻辑)
        if clean_text.strip():
            await self._send_segmented(event, clean_text)

        # 7. 执行动作 (异步执行，不阻塞后续流程)
        if actions:
            asyncio.create_task(self._execute_actions(event, actions))

        # 8. 保存历史 & 发送常规表情
        if hasattr(plan, "action"):
            self.state_manager._update_active_state(event, plan, is_poke_or_nickname)
        
        await self.persistence.save_history_message(event.unified_msg_origin, "assistant", clean_text, self.bot_name)
        
        # 强制 Poke 或高概率发送表情
        prob = 100 if is_poke_or_nickname else self.config.emotions_probability
        await self._send_meme(event, prob)
        
        event.stop_event()

    def _extract_actions(self, text: str) -> list:
        """提取 (action) 格式的动作"""
        # 匹配英文圆括号或中文圆括号，支持 poke/戳一戳/sigh 等
        return re.findall(r'[\(\（](poke|戳一戳|摸摸|sigh|wink)[\)\）]', text)

    def _clean_actions(self, text: str) -> str:
        """清除动作标记，只保留对话内容"""
        return re.sub(r'[\(\（](poke|戳一戳|摸摸|sigh|wink)[\)\）]', '', text).strip()

    async def _execute_actions(self, event: AstrMessageEvent, actions: list):
        """执行物理动作 API"""
        for act in actions:
            if act in ["poke", "戳一戳"]:
                # 调用 OneBot 戳一戳 API
                try:
                    target_id = event.get_sender_id()
                    client = getattr(event, 'bot', None)
                    if client:
                        group_id = event.get_group_id()
                        if group_id:
                            await client.api.call_action('send_poke', user_id=int(target_id), group_id=int(group_id))
                        else:
                            await client.api.call_action('send_poke', user_id=int(target_id))
                        logger.info(f"💪 [Action] Executing physical poke -> {target_id}")
                except Exception as e:
                    logger.warning(f"Execute action 'poke' failed: {e}")
            
            elif act == "sigh":
                # 叹气：发送 'sad' 表情
                await self._send_meme(event, 100, tag="sad")

    def _is_ai_slop(self, text: str) -> bool:
        """检测是否包含典型的 AI 客套话"""
        slop_keywords = [
            "作为人工智能", "As an AI", "我无法", "抱歉", "cannot fulfill",
            "语言模型", "language model", "I am an AI"
        ]
        return any(kw in text for kw in slop_keywords)

    async def _send_segmented(self, event: AstrMessageEvent, text: str):
        """(Refactor) 发送分段消息"""
        segments = self._segment_reply_content(text)
        for i, segment in enumerate(segments):
            if not segment.strip(): continue
            await event.send(event.plain_result(segment))
            # 模拟打字延迟
            if i < len(segments) - 1:
                delay = min(2.0, max(0.5, len(segment) * 0.1))
                await asyncio.sleep(delay)

    async def _send_meme(self, event: AstrMessageEvent, probability: int, tag: str = None):
        """
        (Modified) 发送表情包，支持 tag 覆盖
        """
        if not self.config.enable_emotion_sending:
            return
        
        try:
            # 优先使用传入的 tag，否则从 event 获取
            emotion_tag = tag if tag else event.get_extra("heartcore_mood_tag")
            
            if not emotion_tag or emotion_tag == "none":
                return
            
            # 导入放在这里避免循环引用
            from ..meme_engine.meme_sender import send_meme 
            await send_meme(
                self.context, 
                event, 
                emotion_tag,
                probability,
                MEMES_DIR
            )
        except Exception as e:
            logger.error(f"ReplyEngine: _send_meme 失败: {e}")

    async def _generate_reply(self, 
                              event: AstrMessageEvent, 
                              chat_state: ChatState, 
                              user_profile: UserProfile, 
                              plan: Any = None, 
                              contexts_to_add: list = None, 
                              prompt_override: str = None) -> str:
        """
        生成回复文本 (Core Generation)
        (v4.13 适配) 核心 LLM 请求
        新增: F7 原生视觉 (Native Vision) 支持
        """
        try:
            # [修改] 获取 Provider ID 而不是实例 
            # get_current_chat_provider_id 是 v4.5.7+ 引入的标准方法
            provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            
            if not provider_id:
                logger.warning(f"MainLLM: 未找到 {event.unified_msg_origin} 的主回复模型ID")
                return ""

            # 构建 Prompt (逻辑不变)
            system_prompt, final_user_prompt = await self.prompt_builder.build_reply_prompt(
                event, chat_state, user_profile, plan, prompt_override=prompt_override
            )
            
            # [修改] 构造符合 AstrBot 规范的 contexts
            # llm_generate 的 contexts 参数通常接受 List[dict] 或 List[BaseMessageComponent]
            req_contexts = []
            
            # 1. 注入 System Prompt
            if system_prompt:
                req_contexts.append({"role": "system", "content": system_prompt})
            
            # 2. 注入额外的上下文 (如瞬时想法、图片描述)
            if contexts_to_add:
                req_contexts.extend(contexts_to_add)

            # 3. [F7 新增] 原生视觉支持
            # 如果配置开启，尝试提取图片组件并注入上下文
            if self.config.use_native_vision:
                images = self.prompt_builder.extract_images_for_vision(event)
                if images:
                    # vvvvvvvvvvvvvv 修改开始 vvvvvvvvvvvvvv
                    # [修复] 必须包裹在 dict 中，因为 llm_generate/provider 期望的是消息对象字典
                    # 将提取到的所有图片组件作为一个 User 消息注入
                    req_contexts.append({
                        "role": "user",
                        "content": images  # images 是 List[Comp.Image]
                    })
                    # ^^^^^^^^^^^^^^ 修改结束 ^^^^^^^^^^^^^^
                    
                    logger.debug(f"MainLLM: 已注入 {len(images)} 张图片用于原生视觉识别。")
            # [核心修改] 使用 llm_generate
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=final_user_prompt,
                contexts=req_contexts
            )
            
            if llm_resp and llm_resp.completion_text:
                return llm_resp.completion_text
            else:
                return ""
            
        except Exception as e:
            logger.error(f"MainLLM: _generate_reply 异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    async def handle_summary_reply(self, event: AstrMessageEvent, plan: BrainActionPlan, is_poke_or_nickname: bool, summary_context: List[str] = None):
        """
        总结回复 (同样应用清洗和分段)
        (v4.13) 支持传入 summary_context，移除副作用清理，集成精力正反馈
        """
        try:
            # 1. 准备上下文数据
            if summary_context:
                # 如果调用方直接传了积压消息列表（通常来自 Background Buffer 溢出）
                # 我们直接拼接这些消息作为 Prompt 上下文
                msgs_block = "\n".join([f"- {msg}" for msg in summary_context])
                context_str = f"【积压的未读消息摘要】\n{msgs_block}"
            else:
                # 否则走默认流程，拉取最近历史
                message_count_to_summarize = self.config.context_messages_count
                context_str = await self.prompt_builder._get_recent_messages(event.unified_msg_origin, message_count_to_summarize)
            
            summary_reply_prompt = f"""
以下是最近的 {message_count_to_summarize} 条群聊摘要：
{context_str}
请你针对上述**所有**内容，发表一句总结性的、符合人设的回复。
**重要：你的回复必须自然，就像一个真实群友的“冒泡”，不要暴露你是机器人！**
"""
            # [Fix] async await
            chat_state = await self.state_manager.get_chat_state(event.unified_msg_origin)
            user_profile = await self.state_manager.get_user_profile(event.get_sender_id())

            # 2. 调用 LLM
            # 使用 prompt_override 覆盖默认的 "回复+剧本" 结构
            llm_response, _ = await self._get_main_llm_reply(
                event, chat_state, user_profile, 
                plan=plan,
                prompt_override=summary_reply_prompt
            )
            
            if llm_response is None or not llm_response.completion_text:
                logger.warning(f"[{event.unified_msg_origin}] 总结回复失败。")
                self.state_manager._update_passive_state(event, plan, batch_size=1)
                event.stop_event()
                return
            
            raw_text = llm_response.completion_text.strip()
            
            # --- [步骤 3] 清洗 ---
            clean_text = self._clean_reply_content(raw_text)
            if not clean_text: return
            
            # 情绪分析
            (reply_mood_tag, new_mood_value) = await self.mood_manager.analyze_text_mood(clean_text, chat_state)
            chat_state.mood = new_mood_value 
            event.set_extra("heartcore_mood_tag", reply_mood_tag)
            # --- [核心修改] F3 精力正反馈与状态更新 ---
            # 替换旧的 _update_active_state，且不执行任何 buffer.clear()
            self._update_state_after_reply(chat_state, new_mood_value, is_poke_or_nickname, user_profile)
            
            event.set_extra("heartcore_mood_tag", reply_mood_tag)

            # --- [步骤 4] 智能分段 (F5) ---
            segments = self._segment_reply_content(clean_text)
            
            # --- [步骤 5] 发送 ---
            for i, segment in enumerate(segments):
                if not segment.strip(): continue
                await event.send(event.plain_result(segment))
                if i < len(segments) - 1:
                    await asyncio.sleep(min(2.0, max(0.5, len(segment) * 0.1)))

            # 持久化 Bot 回复
            await self.persistence.save_history_message(event.unified_msg_origin, "assistant", clean_text, self.bot_name)
            
            # 发送表情
            await self._send_meme(event, self.config.emotions_probability)
            
            event.stop_event()

        except Exception as e:
            logger.error(f"handle_summary_reply 异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
    # --- 2. 核心 LLM 调用 ---

    async def _get_main_llm_reply(self, event: AstrMessageEvent, 
                                  chat_state: ChatState, 
                                  user_profile: UserProfile, 
                                  plan: BrainActionPlan = None, 
                                  contexts_to_add: list = None, 
                                  prompt_override: str = None
                                  ) -> (LLMResponse, list):
        """
        (v4.13 适配) 核心 LLM 请求
        新增: F7 原生视觉 (Native Vision) 支持
        """
        try:
            # [修改] 获取 Provider ID 而不是实例 
            # get_current_chat_provider_id 是 v4.5.7+ 引入的标准方法
            provider_id = await self.context.get_current_chat_provider_id(umo=event.unified_msg_origin)
            
            if not provider_id:
                logger.warning(f"MainLLM: 未找到 {event.unified_msg_origin} 的主回复模型ID")
                return LLMResponse(role="assistant", completion_text="..."), []

            # 构建 Prompt (逻辑不变)
            system_prompt, final_user_prompt = await self.prompt_builder.build_reply_prompt(
                event, chat_state, user_profile, plan, prompt_override=prompt_override
            )
            
            # [修改] 构造符合 AstrBot 规范的 contexts
            # llm_generate 的 contexts 参数通常接受 List[dict] 或 List[BaseMessageComponent]
            req_contexts = []
            
            # 1. 注入 System Prompt
            if system_prompt:
                req_contexts.append({"role": "system", "content": system_prompt})
            
            # 2. 注入额外的上下文 (如瞬时想法、图片描述)
            if contexts_to_add:
                req_contexts.extend(contexts_to_add)

            # 3. [F7 新增] 原生视觉支持
            # 如果配置开启，尝试提取图片组件并注入上下文
            if self.config.use_native_vision:
                images = self.prompt_builder.extract_images_for_vision(event)
                if images:
                    # vvvvvvvvvvvvvv 修改开始 vvvvvvvvvvvvvv
                    # [修复] 必须包裹在 dict 中，因为 llm_generate/provider 期望的是消息对象字典
                    # 将提取到的所有图片组件作为一个 User 消息注入
                    req_contexts.append({
                        "role": "user",
                        "content": images  # images 是 List[Comp.Image]
                    })
                    # ^^^^^^^^^^^^^^ 修改结束 ^^^^^^^^^^^^^^
                    
                    logger.debug(f"MainLLM: 已注入 {len(images)} 张图片用于原生视觉识别。")
            # [核心修改] 使用 llm_generate
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=final_user_prompt,
                contexts=req_contexts
            )
            
            return llm_resp, req_contexts 
            
        except Exception as e:
            logger.error(f"MainLLM: _get_main_llm_reply 异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None, []
    # --- 3. 辅助功能 ---

    async def _send_meme(self, event: AstrMessageEvent, probability: int):
        """发送表情包"""
        if not self.config.enable_emotion_sending:
            return
        
        try:
            emotion_tag = event.get_extra("heartcore_mood_tag")
            
            if not emotion_tag or emotion_tag == "none":
                logger.debug("表情引擎：无情绪标签 (none)，跳过发送。")
                return
            
            await send_meme(
                self.context, 
                event, 
                emotion_tag,
                probability,
                MEMES_DIR
            )
        
        except Exception as e:
            logger.error(f"ReplyEngine: _send_meme 失败: {e}")

    def _update_state_after_reply(self, chat_state: ChatState, new_mood: float, is_force: bool, user_profile: UserProfile = None):
        """
        (F3 & F4) 执行精力正反馈、好感度更新与基础状态维护
        """
        
        # 2. 更新心情
        old_mood = chat_state.mood
        chat_state.mood = new_mood
        
        # 3. 精力计算 (F3)
        if is_force:
            pass # 强制回复不消耗也不恢复
        else:
            if new_mood > 0.5 or (new_mood - old_mood) > 0.2:
                chat_state.energy = min(1.0, chat_state.energy + 0.05)
                logger.debug(f"Energy Bonus! Mood: {new_mood:.2f}, Energy +0.05 -> {chat_state.energy:.2f}")
            else:
                chat_state.energy = max(0.0, chat_state.energy - self.config.energy_decay_rate)

        # 4. 更新好感度 (F4 上限 100) (保留，StateManager中的已移除)
        if self.config.enable_user_profiles and user_profile:
            user_profile.social_score += self.config.score_positive_interaction
            if user_profile.social_score > 100.0:
                user_profile.social_score = 100.0
            logger.debug(f"Social Score: {user_profile.name} -> {user_profile.social_score:.1f} (Max 100)")

        chat_state.judgment_mode = "single"       