# heartflow/utils/prompt_builder.py
# (v20.0 重构 - Humanizer: 去机器味、剧本化历史、沉浸式Prompt)
import datetime
import json
import time
import hashlib
import re # (v20.0)
from typing import TYPE_CHECKING, List, Dict, Any, Optional
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
import astrbot.api.message_components as Comp

# (v13.0) 导入
from ..datamodels import BrainActionPlan, ChatState, UserProfile
from ..config import HeartflowConfig
from ..core.state_manager import StateManager
from ..persistence import PersistenceManager


if TYPE_CHECKING:
    from ..features.persona_summarizer import PersonaSummarizer


class PromptBuilder:
    """
    (v20.0) Prompt 构建器
    职责：构建所有复杂的Prompt（规划、回复、摘要、主动），负责将数据转化为“剧本”
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 state_manager: StateManager
                 ):
        self.context = context
        self.config = config
        self.state_manager = state_manager
        # 已移除 persistence
        self.bot_name: str = None
        self.persona_summarizer: "PersonaSummarizer" = None

    # --- 辅助函数 ---
    def _get_image_ref(self, component: Comp.Image) -> str:
        try:
            source_str = component.url or component.file
            if not source_str: return "img_unknown"
            return "img_" + hashlib.md5(source_str.encode()).hexdigest()[:6]
        except Exception:
            return "img_error"

    async def _get_at_name(self, event: AstrMessageEvent, at_user_id: str) -> str:
        """
        (v4.13 优化) 获取被@用户的昵称
        策略：内存/DB缓存优先 -> API获取 -> 异步回写
        """
        # 1. 尝试从 StateManager 获取 (内存/DB)
        try:
            # 注意: StateManager 现已改为 async
            profile = await self.state_manager.get_user_profile(at_user_id)
            
            # 如果名字有效且不是默认的"未知用户"，直接返回
            if profile.name and profile.name != "未知用户":
                return profile.name
        except Exception as e:
            logger.warning(f"PromptBuilder: 获取用户缓存失败: {e}")

        # 2. 缓存未命中，调用 API 获取 (仅限群聊)
        at_name = None
        if (not event.is_private_chat() and 
            event.get_platform_name() == "aiocqhttp" and 
            hasattr(event, 'bot')):
            try:
                group_id = event.get_group_id()
                if group_id:
                    # 调用 OneBot API
                    member_info = await event.bot.api.call_action(
                        'get_group_member_info', 
                        group_id=int(group_id), 
                        user_id=int(at_user_id),
                        no_cache=True
                    )
                    at_name = member_info.get('card') or member_info.get('nickname')
                    
                    # 3. 获取成功，回写到 Profile
                    if at_name:
                        # 重新获取 profile (防止并发覆盖，虽然有锁)
                        profile = await self.state_manager.get_user_profile(at_user_id)
                        if profile.name != at_name:
                            profile.name = at_name
                            profile.is_dirty = True # 标记为脏数据，等待 MaintenanceTask 回写
                            logger.debug(f"更新用户昵称缓存: {at_user_id} -> {at_name}")
            except Exception as e:
                # logger.warning(f"API 获取昵称失败: {e}")
                pass
        
        # 4. 兜底
        if not at_name:
            at_name = f"用户{str(at_user_id)[-4:]}"
            
        return at_name
    
    def set_persona_summarizer(self, summarizer: "PersonaSummarizer"):
        self.persona_summarizer = summarizer
        logger.info("💖 PromptBuilder (v20.0)：已成功注入 PersonaSummarizer。")

    # --- (v20.0) Humanizer 核心：自然语言转换与剧本构建 ---

    def _convert_interaction_to_narrative(self, content: str) -> str:
        """
        [新增] 将技术标记转换为自然叙述/动作描写
        """
        if not content: return ""

        # 1. 戳一戳 (Interaction: A -> B) => *A 拍了拍 B*
        match = re.search(r"\(Interaction: (.*?) -> (.*?)\)", content)
        if match:
            s_name, t_name = match.groups()
            # 如果被戳的是机器人自己
            if self.bot_name and (t_name == self.bot_name or t_name == '我'):
                return f"[{s_name} 伸出手指戳了戳你的脸蛋]"
            return f"[{s_name} 伸出手指戳了戳 {t_name}]"
            
        # 2. 图片内容回填 (Ref:...) => [分享图片: xxx]
        # 如果有 VL 识别结果，优先使用
        if "[图片描述:" in content:
            desc_match = re.search(r"\[图片描述: (.*?) \(Ref:", content)
            if desc_match:
                return f"[分享了一张图片: {desc_match.group(1)}]"
        
        # 普通图片
        if "[图片" in content:
            return "[发了一张图片]"

        # 3. 引用回复 (回复 User: ...) => (回 User: ...)
        if "(回复消息)" in content:
            content = content.replace("(回复消息)", "[回复对方的话]")
        elif "(回复" in content:
            content = content.replace("(回复", "[指着话题回应")

        # 4. @提及 [@User] => @User
        if self.bot_name:
            # 匹配 [@BotName]
            content = content.replace(f"[@{self.bot_name}]", "[对你说]")
        
        # 处理其他 @ (正则匹配 [@任意字符])
        # 将 [@张三] 转换为 [望向 张三]
        content = re.sub(r"\[@(.*?)\]", r"[对\1说]", content)
        
        # 5. 去除多余的技术 Ref ID (兜底)
        content = re.sub(r"\(Ref:.*?\)", "", content).strip()
        
        return content
    
    def _normalize_content_to_str(self, content: Any) -> str:
        """
        将 content (str 或 list[dict]) 统一转换为字符串
        """
        if content is None:
            return ""
        
        if isinstance(content, str):
            return content
            
        if isinstance(content, list):
            # 处理 AstrBot 组件列表格式 (List[Dict])
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    t = item.get("type")
                    if t in ["plain", "text"]:
                        text_parts.append(item.get("text", ""))
                    elif t == "image":
                        text_parts.append("[图片]") # 简化图片显示
                    elif t == "at":
                        text_parts.append(f"[@{item.get('qq', 'User')}]")
                    else:
                        # 其他类型尝试取 text 字段
                        val = item.get("text", "")
                        if val: text_parts.append(val)
                else:
                    # 兜底：如果是字符串列表
                    text_parts.append(str(item))
            return "".join(text_parts)
            
        return str(content)    

    async def build_screenplay_history(self, umo: str, count: int) -> str:
        """
        [重构] 构建无时间感、合并连发、剧本式的历史记录
        """
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not curr_cid: return "（暂无聊天记录）"
            
            conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
            if not conversation or not conversation.history: return "（暂无聊天记录）"
            
            # history 可能是 JSON 字符串，也可能已经是对象
            if isinstance(conversation.history, str):
                try:
                    history_list = json.loads(conversation.history)
                except:
                    return "（历史解析错误）"
            else:
                history_list = conversation.history
            
            # 截取最近消息
            recent_msgs = history_list[-count:] if len(history_list) > count else history_list
            
            screenplay_lines = []
            last_sender = None

            for msg in recent_msgs:
                role = msg.get("role")
                raw_content_obj = msg.get("content", "")
                
                # [核心修复点] 先标准化为字符串，再处理
                raw_content = self._normalize_content_to_str(raw_content_obj)
                
                # --- 步骤A: 彻底剥离时间戳 ---
                sender_name = "未知"
                msg_body = raw_content

                # 尝试去除 [HH:MM:SS] 前缀
                if "] " in raw_content and raw_content.startswith("["): 
                    parts = raw_content.split("] ", 1)
                    if len(parts) > 1:
                        raw_content = parts[1]

                # 尝试分离名字
                if ": " in raw_content:
                    sender_name, msg_body = raw_content.split(": ", 1)
                else:
                    if role == "assistant":
                        sender_name = self.bot_name or "我"
                    else:
                        msg_body = raw_content

                # --- 步骤B: 自然语言转换 ---
                final_content = self._convert_interaction_to_narrative(msg_body)

                # --- 步骤C: 连续发言合并 ---
                if (sender_name == last_sender and 
                    "*" not in final_content and 
                    "[" not in final_content and
                    screenplay_lines): 
                    
                    screenplay_lines[-1] = f"{screenplay_lines[-1]} {final_content}"
                else:
                    screenplay_lines.append(f"{sender_name}: {final_content}")
                    last_sender = sender_name

            if not screenplay_lines:
                return "（暂无聊天记录）"

            return "\n".join(screenplay_lines)

        except Exception as e:
            logger.error(f"剧本构建失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return "（历史记录加载异常）"

    def extract_images_for_vision(self, event: AstrMessageEvent) -> List[Dict]:
        """
        (v4.13 F7) 从消息中提取图片组件，供 llm_generate 使用
        返回格式符合 AstrBot Context 标准 (e.g. [{"type": "image", "file": ...}])
        """
        images = []
        if event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, Comp.Image):
                    # 优先使用 url，其次 file
                    src = component.url or component.file
                    if src:
                        # 构造 llm_generate 兼容的 image payload
                        # 注意：具体格式取决于 adapter，通常传入组件对象即可，
                        # 但为了稳妥，我们让 ReplyEngine 直接传 urls 或者组件列表。
                        # 这里我们返回组件本身，让 ReplyEngine 处理
                        images.append(component) 
        return images

    # --- 1. (v21.0) 大脑规划 Prompt ---

    async def build_planner_prompt(self, 
                                     event: AstrMessageEvent, 
                                     chat_state: ChatState, 
                                     user_profile: UserProfile, 
                                     bonus_score: float,
                                     is_poke: bool
                                     ) -> (str, str):
        """
        (v22.0) 构建大脑规划器 Prompt
        新增：理性评分指令 (Relevance/Necessity/Confidence)
        """
        
        # 1. 获取人格与摘要
        _persona_key, persona_prompt_str = await self._get_persona_key_and_summary(event.unified_msg_origin)
        
        # 2. 获取当前消息内容 (处理节流摘要或单条消息)
        throttling_summary = event.get_extra("heartflow_throttling_summary")
        rich_content = throttling_summary if throttling_summary else await self._build_rich_content_string(event)
        
        # 3. 获取历史剧本
        recent_messages = await self.build_screenplay_history(event.unified_msg_origin, self.config.context_messages_count)
        
        # 4. 构建状态字符串
        internal_state_str = f"""
[我的内部状态]
- 精力: {chat_state.energy:.1f}/1.0
- 心情: {chat_state.mood:.2f} (-1.0=沮丧, 1.0=积极)
- 社交冷却: {chat_state.consecutive_reply_count}/{self.config.max_consecutive_replies}
"""
        social_perception_str = ""
        if self.config.enable_user_profiles and user_profile:
            social_perception_str = f"""
[我对发言者的感知]
- 用户: {user_profile.name}
- 身份: {user_profile.identity}
- 好感度: {user_profile.social_score:.1f}
"""
            if user_profile.persona_analysis:
                social_perception_str += f"- 印象记忆: {user_profile.persona_analysis}\n"
        
        special_event_str = ""
        if is_poke:
            special_event_str = f"[!!!] 特殊事件：{event.get_extra('heartflow_poke_sender_name') or '用户'} 刚刚戳了我一下！(建议 'REPLY')\n"
        elif bonus_score > 0:
            special_event_str = f"[!!!] 特殊事件：消息中提到了我的昵称！(建议 'REPLY')\n"

        history_str = f"""
[最近对话剧本]
{recent_messages}
"""
        message_str = f"""
[当前待处理消息]
发送者: {event.get_sender_name()}
内容: {rich_content}
"""
        
        # --- [修改核心] 引入理性评分机制 ---
        task_str = f"""
[任务：作为人格的你，请进行理性评估]
**不要被你“友好的性格”影响判断，请客观评估是否需要回复。**
你需要输出三个关键指标（1-10分），并基于此给出行动建议。

[评估指标定义]
1. **Relevance (话题相关性)**: 这条消息与你(AI)或你感兴趣的话题有多大关系？(1:完全无关, 10:直接点名/强相关)
2. **Necessity (必要性)**: 如果你不回，对话会冷场或显得无礼吗？(1:完全没必要, 10:必须回应)
3. **Confidence (回复信心)**: 你是否知道该怎么回？(1:不知道回啥, 10:有绝妙的回复点子)

[行动选项]
1. "REPLY": 综合分数较高。
2. "IGNORE": 综合分数较低，或者你可以单纯地做一个倾听者。
3. "SUMMARIZE_REPLY": 消息量巨大且适合总结时。

请严格按照以下JSON格式回复：
{{
    "thought": "（你的内心独白。**请自由发散你的想法**，即使最终决定不回复，你也可以在心里吐槽或思考。这个字段的内容**不**受行动影响。）",
    "relevance": (1-10的整数),
    "necessity": (1-10的整数),
    "confidence": (1-10的整数),
    "action": "（从[行动选项]中选择一个，参考上述打分）"
}}
"""
        
        planner_user_prompt = (
            f"{internal_state_str}\n"
            f"{social_perception_str}\n"
            f"{history_str}\n"
            f"{special_event_str}"
            f"{message_str}\n"
            f"{task_str}"
        )
        return planner_user_prompt, persona_prompt_str


    # --- 2. (v21.0 修改) 主回复 Prompt ---

    async def build_reply_prompt(self, event: AstrMessageEvent, 
                                 chat_state: ChatState, 
                                 user_profile: UserProfile, 
                                 brain_plan: BrainActionPlan, 
                                 prompt_override: str = None
                                 ) -> (str, str):
        """
        (v23.0 修复) 构建沉浸式剧本回复 Prompt
        (v4.14 优化) 适配消息聚合
        """
        
        # 1. 确保获取到 Bot 名字
        if self.bot_name is None: await self._fetch_bot_name_from_context()
        bot_name = self.bot_name or "我"

        # ... (获取人格设定 System Prompt 部分保持不变) ...
        # 获取摘要版人设
        _key, persona_prompt_text = await self._get_persona_key_and_summary(event.unified_msg_origin)
        if not persona_prompt_text:
            persona_prompt_text = f"你是一个名为 {bot_name} 的群聊成员，性格友好有趣。"

        # 构建 System Prompt
        max_len = self.config.humanization_word_count 
        system_prompt = f"""
[角色设定]
{persona_prompt_text}

[当前身份]
你现在的显示名称是 {bot_name}。你不是AI助手，而是一个真实的群聊群友。

[核心回复规则]
1. **沉浸式续写**：你正在参与群聊剧本续写，直接输出你的台词。
2. **去机器味**：绝对禁止带名字前缀(如"{bot_name}:")，绝对禁止带引号。
3. **口语化**：必须使用短句。字数严格控制在 {max_len} 字以内。
4. **拒绝泄露**：绝对禁止输出“[心理活动]”或“[决策]”等标签里的内容，只输出回复文本！
"""

        # ... (中间的状态描述部分保持不变) ...
        # A. 心理状态描述
        mood_val = chat_state.mood
        mood_desc = "平静"
        if mood_val > 0.5: mood_desc = "非常开心"
        elif mood_val > 0.2: mood_desc = "心情不错"
        elif mood_val < -0.5: mood_desc = "烦躁/生气"
        elif mood_val < -0.2: mood_desc = "有点郁闷"
        
        energy_val = chat_state.energy
        
        # B. 社交关系
        sender_name = event.get_sender_name() or "用户"
        social_score = 0.0
        identity_str = "群友" 
        memory_str = ""
        if user_profile:
            social_score = user_profile.social_score
            identity_str = user_profile.identity
            if user_profile.persona_analysis:
                memory_str = f"\n[关于 {sender_name} 的记忆]\n{user_profile.persona_analysis}"
        
        # C. 历史剧本
        clean_history = await self.build_screenplay_history(event.unified_msg_origin, self.config.context_messages_count)
        
        # --- [v4.14 核心修改] D. 当前消息 (优先使用聚合内容) ---
        aggregated_content = event.get_extra("heartflow_aggregated_content")
        if aggregated_content:
            # 如果有聚合内容，通常已经是 Rich Content 格式，直接进行拟人化转述
            # 或者，因为聚合内容已经是 "[图片] ... \n [图片] ..."，_convert_interaction_to_narrative 也能处理
            raw_input = aggregated_content
        else:
            raw_input = event.message_str

        current_content = self._convert_interaction_to_narrative(raw_input) or "[图片/表情]"
        
        # E. 大脑指令
        brain_instruction = brain_plan.thought
        if not brain_instruction or brain_instruction == "...":
            brain_instruction = "根据当前语境自然回复。"

        # 4. 组装 User Prompt
        if prompt_override:
            content_part = f"""
[当前任务]
{prompt_override}
"""
        else:
            content_part = f"""
[对话剧本]
{clean_history}

[当前]
{sender_name}: {current_content}

(请回复 {sender_name})
"""

        final_user_prompt = f"""
[当前状态]
心情: {mood_desc} ({mood_val:.2f}) | 精力: {energy_val:.2f}
关系: 发言者 {sender_name} (身份: {identity_str}) 与你的关系数值为 {social_score:.1f} (满分100){memory_str} 

[来自大脑的指令]
{brain_instruction}
(注意：这是你潜意识的想法，请基于此决定回复的语气，但不要把这句话说出来)

{content_part}
"""

        return system_prompt, final_user_prompt
    
    # --- 4. (v11.0) 主动话题 Prompt (微调) ---
    
    def build_proactive_idea_prompt(self, persona_prompt: str, minutes_silent: int) -> str:
        topic_prompt = f"""
群聊已经沉寂了{minutes_silent}分钟。
请基于你的角色，想出一个简短的、适合发起的新话题。
**重要：只回复话题本身，不要说任何其他内容！**
"""
        return topic_prompt

    def build_proactive_opening_prompt(self, persona_prompt: str, topic_idea: str) -> str:
        opening_prompt = f"""
你正在一个群聊中，群里已经安静了很长时间。
你决定基于以下“话题思路”发起一个自然的、符合你人设的开场白。

话题思路：{topic_idea}

请生成你的开场白。
**重要：你的回复必须自然，就像一个真实群友的“冒泡”，不要提及“话题思路”这个词！**
"""
        return opening_prompt
    
    async def build_resume_topic_prompt(self, umo: str) -> str:
        recent_history_str = await self.build_screenplay_history(umo, count=50) # 使用剧本格式
        if not recent_history_str or recent_history_str == "（暂无聊天记录）":
            return None
            
        resume_prompt = f"""
分析以下聊天记录：
{recent_history_str}
是否存在一个有趣但被意外中断的话题？
请严格按JSON格式回复：
{{
    "is_interesting": true/false,
    "was_interrupted": true/false,
    "topic_summary": "话题总结（如果有趣且被中断，请总结在20字以内）"
}}"""
        return resume_prompt

    # --- 5. (v11.0) 辅助函数 ---

    async def _fetch_bot_name_from_context(self):
        if self.bot_name is not None: return
        try:
            platform = self.context.get_platform("aiocqhttp")
            if platform and hasattr(platform, 'get_client'):
                client = platform.get_client()
                if client:
                    info = await client.api.call_action('get_login_info')
                    if info and info.get("nickname"):
                        self.bot_name = info["nickname"]
                        logger.info(f"💖 PromptBuilder：成功获取 Bot 昵称: {self.bot_name}")
                        return
        except Exception:
            pass
        self.bot_name = self.config.bot_nicknames[0] if self.config.bot_nicknames else "机器人"

    async def _build_rich_content_string(self, event: AstrMessageEvent) -> str:
        """
        (v22.1 修复) 构建用于持久化的丰富文本格式
        (v4.14 优化) 优先读取聚合消息
        """
        # 1. 优先检查聚合内容 (v4.14)
        aggregated = event.get_extra("heartflow_aggregated_content")
        if aggregated: 
            return aggregated

        # 2. 检查节流摘要
        throttling_summary = event.get_extra("heartflow_throttling_summary")
        if throttling_summary: return throttling_summary

        if self.bot_name is None: await self._fetch_bot_name_from_context()

        sender_name = event.get_sender_name() or "用户"
        
        # ... (以下保持原有逻辑不变)
        # 1. 处理真正的“戳一戳”事件
        if event.get_extra("heartflow_is_poke_event"):
            sender_name = event.get_extra("heartflow_poke_sender_name") or "用户"
            bot_name = self.bot_name or '我'
            return f"[{sender_name} 戳了你一下] (Interaction: {sender_name} -> {bot_name})"

        if not event.message_obj or not event.message_obj.message:
            return event.message_str

        parts = []

        try:
            for component in event.message_obj.message:
                if isinstance(component, Comp.Plain):
                    parts.append(component.text.strip())
                elif isinstance(component, Comp.Reply):
                    parts.append("(回复消息)")
                elif isinstance(component, Comp.At):
                    at_user_id = str(component.qq)
                    at_name = await self._get_at_name(event, at_user_id)
                    parts.append(f"[@{at_name}]")
                elif isinstance(component, Comp.Image):
                    image_ref = self._get_image_ref(component)
                    image_desc = event.get_extra("image_description")
                    if image_desc:
                        parts.append(f"[图片描述: {image_desc} (Ref:{image_ref})]")
                    else:
                        parts.append(f"[图片(Ref:{image_ref})]")

        except Exception as e:
            logger.error(f"构建 Rich Content String 失败: {e}")
            return event.message_str 
        
        content_str = " ".join(filter(None, parts))
        return content_str
    
    def _build_perception_info(self, event: AstrMessageEvent) -> (str, str):
        """
        (辅助) 构建感知信息 (正在回复/正在@)
        """
        reply_info = ""
        at_info = ""
        if event.message_obj and event.message_obj.message:
            for component in event.message_obj.message:
                if isinstance(component, Comp.Reply):
                    reply_info = "[正在回复某条消息]"
                elif isinstance(component, Comp.At):
                    at_info = f"[正在 @ 其他人]"
        return reply_info, at_info
    
    def _build_user_profile_info(self, event: AstrMessageEvent, user_profile: UserProfile) -> str:
        """
        (v20.0) 构建用户画像信息字符串 (移除 Tier，保留分数)
        通常用于日志或调试，或旧版 Prompt 兼容
        """
        user_profile_info = ""
        if self.config.enable_user_profiles and user_profile:
            user_profile_info = f"""
## 发言者信息
- 用户: {event.get_sender_name()}
- 好感度: {user_profile.social_score:.1f}
- 上次发言: {int((time.time() - user_profile.last_seen) / 60)} 分钟前
"""
        return user_profile_info

    async def _get_recent_messages(self, umo: str, count: int) -> str:
        """
        (v20.0 重构) 获取最近消息
        [重要] 为了保持全系统的一致性，现在这是 build_screenplay_history 的包装器。
        这意味着 BrainPlanner 和 Summary 都会看到“剧本式”的历史记录。
        """
        return await self.build_screenplay_history(umo, count)

    def _build_chat_context(self, chat_state: ChatState) -> str:
        """
        (辅助) 构建群聊上下文统计信息
        """
        context_info = f"""最近活跃度: {'高' if chat_state.total_messages > 100 else '中' if chat_state.total_messages > 20 else '低'}
历史回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%
当前时间: {datetime.datetime.now().strftime('%H:%M')}"""
        return context_info

    async def _get_last_bot_reply(self, event: AstrMessageEvent) -> str:
        """
        (辅助) 获取机器人上一次的回复内容 (用于防止重复复读)
        """
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid: return None
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if not conversation or not conversation.history: return None
            context = json.loads(conversation.history)
            
            # 倒序查找 assistant 的最后一条消息
            for msg in reversed(context):
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role == "assistant" and content.strip():
                    return content
            return None
        except Exception as e:
            logger.debug(f"获取上次bot回复失败: {e}")
            return None
            
    # --- 人格摘要获取核心函数 ---

    async def _get_persona_key_and_summary(self, umo: str) -> (str, str):
        """
        (核心) 获取当前群聊的人格 Key 和 摘要 Prompt
        1. 从 AstrBot 获取当前激活的 Persona (v3)
        2. 调用 PersonaSummarizer 获取缓存的摘要
        """
        try:
            if not self.persona_summarizer:
                logger.error("PromptBuilder: PersonaSummarizer 未被注入！无法获取人格。")
                return "error", ""

            persona_key_for_cache = "" 
            original_prompt = ""
            
            # 获取 AstrBot V3 默认人格
            default_persona_v3 = await self.context.persona_manager.get_default_persona_v3(umo=umo)
            
            if default_persona_v3:
                persona_key_for_cache = default_persona_v3.get("name")
                original_prompt = default_persona_v3.get("prompt")
                
                if not persona_key_for_cache or not original_prompt:
                     logger.warning("PromptBuilder: V3 默认人格对象无效（缺少 name 或 prompt）。")
                     return "error", ""
            else:
                logger.warning("PromptBuilder: 未能获取 (v3) 默认人格。")
                return "error", ""

            # 获取摘要 (如果缓存有，直接返缓存；否则生成)
            summarized_prompt = await self.persona_summarizer.get_or_create_summary(
                umo, 
                persona_key_for_cache,
                original_prompt
            )
            return persona_key_for_cache, summarized_prompt

        except Exception as e:
            logger.error(f"PromptBuilder: _get_persona_key_and_summary 失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return "error", ""

    async def _get_persona_system_prompt_by_umo(self, umo: str) -> str:
        """
        (核心) 对外接口：直接获取用于 System Prompt 的人设文本
        reply_engine.py 强依赖此函数
        """
        _key, summary = await self._get_persona_key_and_summary(umo)
        return summary
    
    def build_impulse_prompt(self, 
                             context_messages: list, 
                             persona_mutation: str, 
                             retrieved_memory: str,
                             current_goals: str) -> list:
        """
        (2.0) 构建冲动引擎的 ReAct Prompt (中文指令版)
        """
        # 1. 基础人设
        system_prompt = self._get_persona_prompt()
        
        # 2. 状态突变 (Persona Mutation)
        if persona_mutation:
            system_prompt += f"\n\n[当前状态/心情 (Current State)]\n{persona_mutation}"
            
        # 3. 记忆与目标 (Contextual Info)
        mem_text = retrieved_memory if retrieved_memory else "无 (None)"
        system_prompt += f"\n\n[检索到的记忆 (Retrieved Memories)]\n{mem_text}"
        system_prompt += f"\n\n[当前对话目标 (Current Goals)]\n{current_goals}"
        
        # 4. 思考指令 (ReAct Instructions) - 已汉化自然语言部分
        system_prompt += """
\n[决策指令 (Instruction)]
你是这个角色的“决策大脑”。请基于对话历史，分析当前局势并决定下一步行动。
**禁止**直接输出回复内容。你必须输出一个 **JSON 对象** 来描述你的思维过程和决策结果。

有效动作 (Valid Actions):
- REPLY: 生成回复（决定说话）。
- WAIT: 挂起/等待几秒（例如：你觉得用户还没说完，仍在输入中，或者你想假装思考/倾听）。
- COMPLETE_TALK: 结束对话（例如：用户说了“再见”、“晚安”、“哦”、“嗯”等终止性词汇，且没有新话题）。停止回复，进入休眠。
- IGNORE: 忽略（针对完全无关的内容、噪音或刷屏）。

JSON 格式要求 (JSON Format):
{
    "thought": "你的内心独白。你为什么选择这个动作？你现在心情如何？下一句回复应该是什么语气？（请用第一人称思考）",
    "action": "REPLY" | "WAIT" | "COMPLETE_TALK" | "IGNORE",
    "goals_update": [
        {"action": "add", "description": "新增目标的描述 (中文)"},
        {"action": "complete", "description": "已完成目标的描述 (中文)"},
        {"action": "clear"} 
    ],
    "params": {"wait_seconds": 3}
}
"""
        # 5. 构建消息列表
        messages = [
            {"role": "system", "content": system_prompt},
            *context_messages # 这里假设 context_messages 已经是 [{"role":..., "content":...}] 格式
        ]
        
        return messages
