import re
import asyncio
import random
from typing import List
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
# [阶段四新增] 引入情绪归因路由器
from ..Heart.affection_router import AffectionRouter
# 引入依赖模块
from ..infra.datamodels import ChatState
from ..Heart.state_engine import StateEngine
from ..Heart.mood_manager import MoodManager
from ..meme_engine.meme_config import MEMES_DIR
from ..meme_engine.meme_sender import send_meme

class ReplyEngine:
    """
    回复引擎 (Expression Layer)
    职责: 清洗 LLM 输出、拟人化分段、情绪后处理与表情包发送
    """
    def __init__(self, state_engine: StateEngine, mood_manager: MoodManager, config=None):
        self.state_engine = state_engine
        self.mood_manager = mood_manager
        self.config = config if config else state_engine.config
        
        # 接入 Config (不再硬编码)
        self.segmentation_threshold = self.config.reply.segment_min_len # 分段阈值
        self.no_segment_limit = self.config.reply.no_segment_max_len      # 长文不分段阈值
        self.meme_probability = self.config.reply.meme_probability       # 表情包概率
        
        # [新增] 引入独立的智能分段器，挂载至引擎实例
        from .text_segmenter import TextSegmenter
        self.segmenter = TextSegmenter(
            min_length=self.segmentation_threshold,
            max_length=self.no_segment_limit
        )

    def _clean_reply_content(self, text: str) -> str:
        """
        清洗 LLM 输出的幻觉前缀
        """
        if not text: return ""
        # 去除 [HH:MM:SS] 时间戳
        text = re.sub(r'^\[.*?\]\s*', '', text)
        # 去除 BotName: 前缀 (简单正则，匹配常见的 名字: 格式)
        text = re.sub(r'(?i)^[a-zA-Z0-9_\u4e00-\u9fa5]+[：:]\s*', '', text)
        return text.strip()

    def _segment_reply_content(self, text: str) -> List[str]:
        """
        [修改] 拟人化分段算法 (安全闭环版，彻底解决颜文字切片错位与正则冲突)
        代理调用独立的 TextSegmenter 核心，解决正则切割太粗暴与换行符逃逸的问题。
        """
        if len(text) > self.no_segment_limit:
            # 即使触发不分段机制，也必须净化首尾换行符，斩杀导致气泡错位的幽灵字符
            cleaned = re.sub(r'^\n+|\n+$', '', text.strip())
            return [cleaned] if cleaned else []

        # 直接调用外置的智能状态机分段器，其内部已经妥善处理了片段粘连、标点吞噬和换行符逃逸
        return self.segmenter.segment(text)

    async def _fetch_history(self, chat_id: str, anchor_text: str, anchor_event: AstrMessageEvent = None) -> list:
        """
        [终极修复版] 历史记录获取：完全适配底层的 Dict 与 List 多模态数据结构，放弃无效的 message_id。
        """
        history_list = []
        fetch_count = getattr(self.config.attention, 'bg_pool_size', 20) if self.config else 20
        
        try:
            context = getattr(self.state_engine.gateway, 'context', None)
            if not context: return []
            
            conv_mgr = context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)
            conversation = await conv_mgr.get_conversation(chat_id, curr_cid)
            
            if conversation and hasattr(conversation, "history") and conversation.history:
                raw_history = list(conversation.history)
                cutoff_idx = -1
                found_anchor = False
                
                # 预清洗锚点文本
                clean_anchor = re.sub(r'\s+', '', anchor_text) if anchor_text else ""

                # 逆序检索：提取 dict -> content (list) -> type=="text" 结构
                for i in range(len(raw_history) - 1, -1, -1):
                    msg_data = raw_history[i]
                    content = ""
                    
                    # 兼容可能存在的字符串序列化
                    if isinstance(msg_data, str):
                        import json
                        try:
                            msg_data = json.loads(msg_data)
                        except Exception:
                            pass

                    # 💥 精准打击你提取出的 JSON 结构
                    if isinstance(msg_data, dict):
                        c = msg_data.get('content', '')
                        if isinstance(c, str):
                            content = c
                        elif isinstance(c, list):
                            content = "".join([part.get("text", "") for part in c if isinstance(part, dict) and "text" in part])
                    elif hasattr(msg_data, 'content'):
                        c = getattr(msg_data, 'content', '')
                        if isinstance(c, str):
                            content = c
                        elif isinstance(c, list):
                            content = "".join([getattr(part, "text", "") for part in c if hasattr(part, "text")])
                    
                    # 子串强匹配 (无视空格和换行)
                    if content and clean_anchor and clean_anchor in re.sub(r'\s+', '', content):
                        cutoff_idx = i
                        found_anchor = True
                        break
                            
                if found_anchor:
                    start_idx = max(0, cutoff_idx - fetch_count)
                    history_list = raw_history[start_idx:cutoff_idx + 1]
                    logger.debug(f"[ReplyEngine] 锚点匹配成功！精确截取最后 {len(history_list)} 条记忆。")
                else:
                    logger.debug(f"[ReplyEngine] 文本匹配未命中，启动“即时切片”模式补偿提取。")
                    history_list = raw_history[-fetch_count:]
                
        except Exception as e:
            logger.warning(f"[ReplyEngine] 历史记录拉取异常: {e}")
            
        return history_list

    # [修改] 函数位置：astrmai/Brain/reply_engine.py -> ReplyEngine 类下
    async def handle_reply(
        self, 
        event: AstrMessageEvent, 
        raw_text: str, 
        chat_id: str,
        bypassed_tag: str = None,               
        window_events: list = None,
        anchor_event: AstrMessageEvent = None,
        pending_actions: list = None
    ):
        """
        执行回复全流程
        [修改] 引入参数显式化映射，断绝隐式事件对象属性污染导致的系统级状态丢失
        """
        if not raw_text: return

        # 1. 清洗
        clean_text = self._clean_reply_content(raw_text)
        if not clean_text: return

        # =====================================================================
        # 🟢 [核心修复] 强行同步至 AstrBot 原生历史记录（打破永久失忆魔咒）
        # =====================================================================
        try:
            context = getattr(self.state_engine.gateway, 'context', None)
            if context:
                conv_mgr = context.conversation_manager
                curr_cid = await conv_mgr.get_curr_conversation_id(chat_id)
                if curr_cid:
                    from astrbot.core.agent.message import UserMessageSegment, AssistantMessageSegment, TextPart
                    
                    # 💡 获取发送者姓名
                    sender_name = event.get_sender_name() or "群友"
                    
                    # ✨ 【修改此行】：使用富文本保存长期记忆，保留引用和 At 的上下文
                    rich_text = event.get_extra("astrmai_rich_text", event.message_str)
                    formatted_user_text = f"{sender_name}: {rich_text}"
                    
                    user_msg = UserMessageSegment(content=[TextPart(text=formatted_user_text)])
                    ast_msg = AssistantMessageSegment(content=[TextPart(text=clean_text)])
                    
                    await conv_mgr.add_message_pair(
                        cid=curr_cid,
                        user_message=user_msg,
                        assistant_message=ast_msg
                    )
                    logger.debug(f"[{chat_id}] 📝 成功同步对话对（已刻入姓名: {sender_name}）。")
        except Exception as e:
            logger.error(f"[ReplyEngine] 强制同步历史记录失败: {e}")
        # ===

        tag = "neutral"
        force_meme_flag = False
        
        # 🟢 [彻底修复 Bug 1] 使用 get_extra 读取底层工具注入的 bypass 标签，而非 getattr
        _bypassed_tag = bypassed_tag or event.get_extra("astrmai_bypass_mood_analysis", None)
        _window_events = window_events if window_events is not None else event.get_extra("astrmai_window_events", [])
        _anchor_event = anchor_event or event.get_extra("astrmai_anchor_event", None)
        _pending_actions = pending_actions if pending_actions is not None else event.get_extra("astrmai_pending_actions", [])

        # 2. 情绪后处理 (Post-Processing Mood)
        try:
            state = await self.state_engine.get_state(chat_id)
            user_id = event.get_sender_id()
            
            if _bypassed_tag:
                tag = _bypassed_tag
                delta = 0.0
                if tag == "happy":
                    delta = 0.1
                elif tag in ["sad", "angry"]:
                    delta = -0.1
                
                new_mood = await self.state_engine.atomic_update_mood(chat_id, delta=delta)
                logger.info(f"🚀 [ReplyEngine] 短路生效：命中主动表情包工具。Tag: {tag}, 心情更新至: {new_mood:.2f}")
                force_meme_flag = True
                
            else:
                if hasattr(self.state_engine, 'get_user_profile'):
                    profile = await self.state_engine.get_user_profile(user_id)
                    user_affection = getattr(profile, 'social_score', 0.0) if profile else 0.0
                else:
                    user_affection = 0.0
                
                if hasattr(self.mood_manager, 'analyze_mood'):
                    (tag, new_mood) = await self.mood_manager.analyze_mood(
                        text=clean_text, 
                        current_mood=state.mood,
                        user_affection=user_affection
                    )
                elif hasattr(self.mood_manager, 'analyze_text_mood'):
                    (tag, new_mood) = await self.mood_manager.analyze_text_mood(clean_text, state.mood)
                else:
                    new_mood = state.mood
                    
                await self.state_engine.atomic_update_mood(chat_id, absolute_val=new_mood)
            
            logger.debug(f"[Reply] 😃 情绪更新: {tag} ({new_mood:.2f})")
            
            anchor_text = _anchor_event.message_str.strip() if _anchor_event else ""
            # [核心修复 Bug 4] 传递 anchor_event 进行精确 ID 匹配
            history_events = await self._fetch_history(chat_id, anchor_text, anchor_event=_anchor_event)
            
            # 🟢 [核心修复] 调用 route 时，显式将当前对话的触发者 ID 作为 fallback_uid 传入
            target_user_id = AffectionRouter.route(
                history_events=history_events,
                window_events=_window_events,
                trigger_event=event,
                mood_tag=tag,
                config=self.config,
                fallback_uid=user_id
            )

            if target_user_id:
                # [修复 Bug 2]: 强制将其转换为字符串，防止引发 StateEngine 字典缓存击穿与双重锁
                safe_target_uid = str(target_user_id)
                logger.info(f"[ReplyEngine] 🤝 情绪路由器裁决完毕，准备为核心引导用户 {safe_target_uid} 结算好感度。")
                
                if hasattr(self.state_engine, 'calculate_and_update_affection'):
                    await self.state_engine.calculate_and_update_affection(
                        user_id=safe_target_uid,
                        group_id=chat_id,
                        mood_tag=tag,
                        intensity=1.0
                    )
            else:
                logger.debug(f"[ReplyEngine] 🤷‍♂️ 情绪路由器判为流局，仅更新系统心情，跳过所有用户的好感度结算。")
        except AttributeError as e:
            logger.warning(f"[Reply] 情绪模块 API 漂移/失效: {e}")
            tag = "neutral"
        except Exception as e:
            logger.warning(f"[Reply] 情绪分析失败: {e}")
            tag = "neutral"

        # 3. 分段发送
        segments = self._segment_reply_content(clean_text)
        at_targets = [action.get("target_id") for action in _pending_actions if action.get("action") == "at"]
        
        from astrbot.api.event import MessageChain
        for i, seg in enumerate(segments):
            chain = MessageChain()
            
            # 仅在【第一段】回复的最前端，拼接 @ 组件
            if i == 0 and at_targets:
                for target_id in at_targets:
                    uid = target_id
                    if str(target_id).isdigit():
                        uid = int(target_id)
                        
                    chain.chain.append(Comp.At(qq=uid))
                chain.chain.append(Comp.Plain(" "))
                
            chain.chain.append(Comp.Plain(seg))
            
            # 🟢 [彻底修复 Bug 5] 强制统一使用全局 Context 跨越异步生命周期发送消息
            context = getattr(self.state_engine.gateway, 'context', None)
            if context:
                await context.send_message(event.unified_msg_origin, chain)
            else:
                logger.error("[ReplyEngine] 🚨 致命错误：Gateway Context 丢失，无法跨越生命周期发送消息！")
            
            # 拟人化打字延迟
            if i < len(segments) - 1:
                base_factor = getattr(self.config.reply, 'typing_speed_factor', 0.1)
                delay = min(2.0, max(0.5, len(seg) * base_factor))
                await asyncio.sleep(delay)

        # 4. 发送表情包
        if tag and tag != "neutral":
            final_prob = 100 if force_meme_flag else self.meme_probability
            
            # 🟢 [核心修复] 透传跨生命周期的 context 给 send_meme
            global_context = getattr(self.state_engine.gateway, 'context', None)
            
            await send_meme(
                event=event, 
                emotion_tag=tag, 
                probability=final_prob, 
                memes_dir=MEMES_DIR,
                context=global_context  # 传入全局发信器
            )