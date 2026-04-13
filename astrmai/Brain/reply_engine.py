import re
import asyncio
import random
import time
from typing import List
from astrbot.api import logger
try:
    import astrbot.api.message_components as Comp
except ImportError:  # pragma: no cover - 测试桩环境降级
    class _CompatAt:
        def __init__(self, qq=None):
            self.qq = qq

    class _CompatPlain:
        def __init__(self, text=""):
            self.text = text

    class _CompatComp:
        At = _CompatAt
        Plain = _CompatPlain

    Comp = _CompatComp()
from astrbot.api.event import AstrMessageEvent
from ..Heart.affection_router import AffectionRouter
from ..infra.lane_manager import LaneKey
from ..infra.legacy_compat import emit_legacy_reply_runtime_extras
from ..infra.output_guard import is_sendable_segment, sanitize_visible_reply_text
from ..infra.runtime_contracts import (
    FreshnessState,
    OutboundPolicy,
    ReplyMode,
    VisibleReplyArtifact,
)
from ..infra.trace_runtime import preview_text
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
    def __init__(self, state_engine: StateEngine, mood_manager: MoodManager, config=None, runtime_coordinator=None):
        self.state_engine = state_engine
        self.mood_manager = mood_manager
        self.config = config if config else state_engine.config
        self.runtime_coordinator = runtime_coordinator
        
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

    def _reply_max_age_seconds(self) -> float:
        configured = float(getattr(getattr(self.config, "reply", None), "stale_reply_max_age_sec", 0.0) or 0.0)
        if configured > 0:
            return configured
        api_timeout = float(getattr(getattr(self.config, "infra", None), "api_timeout", 15.0) or 15.0)
        return max(30.0, min(90.0, api_timeout * 2.5))

    def _resolve_reply_mode(self, event: AstrMessageEvent) -> ReplyMode:
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        if prompt_envelope and getattr(prompt_envelope, "reply_mode", None):
            return prompt_envelope.reply_mode
        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        if focus_context and getattr(focus_context, "reply_mode", None):
            return focus_context.reply_mode
        raw_mode = str(event.get_extra("astrmai_reply_mode", ReplyMode.CASUAL_FOLLOWUP.value) or ReplyMode.CASUAL_FOLLOWUP.value)
        try:
            return ReplyMode(raw_mode)
        except ValueError:
            return ReplyMode.CASUAL_FOLLOWUP

    def _is_direct_engagement_event(self, event: AstrMessageEvent) -> bool:
        return bool(
            event.get_extra("astrmai_group_direct_wakeup", False)
            or event.get_extra("astrmai_force_engage", False)
            or event.get_extra("is_private_chat", False)
        )

    async def _allow_direct_reply_timeout(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        event_ts: float,
    ) -> bool:
        if not self._is_direct_engagement_event(event):
            return False
        if not self.runtime_coordinator or not hasattr(self.runtime_coordinator, "get_latest_activity"):
            return False

        latest_ts, _, _, _ = await self.runtime_coordinator.get_latest_activity(chat_id)
        return not latest_ts or latest_ts <= event_ts

    async def _check_reply_freshness(self, event: AstrMessageEvent, chat_id: str) -> tuple[FreshnessState, str]:
        event_ts = float(event.get_extra("astrmai_timestamp", 0.0) or 0.0)
        if event_ts <= 0:
            return FreshnessState.FRESH, ""

        reply_age = time.time() - event_ts
        max_age = self._reply_max_age_seconds()
        if reply_age > max_age:
            if await self._allow_direct_reply_timeout(event, chat_id, event_ts):
                logger.info(
                    f"[ReplyEngine] allowing overdue direct reply for {chat_id}: "
                    f"{reply_age:.1f}s>{max_age:.1f}s without newer activity"
                )
                return FreshnessState.FRESH, ""
            return FreshnessState.EXPIRED, f"reply_age_exceeded:{reply_age:.1f}s>{max_age:.1f}s"

        if not self.runtime_coordinator:
            return FreshnessState.FRESH, ""

        if not hasattr(self.runtime_coordinator, "evaluate_reply_freshness"):
            latest_ts, latest_sender_id, latest_sender_name, latest_preview = await self.runtime_coordinator.get_latest_activity(chat_id)
            if latest_ts and latest_ts > event_ts:
                actor = latest_sender_name or latest_sender_id or "unknown"
                return FreshnessState.EXPIRED, f"superseded_by_newer_activity:{actor}:{preview_text(latest_preview, 60)}"
            return FreshnessState.FRESH, ""

        thread_signature = str(event.get_extra("astrmai_thread_signature", "") or "")
        freshness_state, stale_reason = await self.runtime_coordinator.evaluate_reply_freshness(
            chat_id,
            event_ts,
            max_age_seconds=max_age,
            thread_signature=thread_signature,
        )
        if freshness_state != FreshnessState.FRESH and stale_reason.startswith("superseded_by_newer_activity"):
            latest_ts, latest_sender_id, latest_sender_name, latest_preview = await self.runtime_coordinator.get_latest_activity(chat_id)
            actor = latest_sender_name or latest_sender_id or "unknown"
            stale_reason = f"superseded_by_newer_activity:{actor}:{preview_text(latest_preview, 60)}"
        return freshness_state, stale_reason

    def _build_outbound_policy(
        self,
        reply_mode: ReplyMode,
        freshness_state: FreshnessState,
        stale_reason: str,
    ) -> OutboundPolicy:
        if freshness_state == FreshnessState.EXPIRED:
            return OutboundPolicy(
                should_send=False,
                freshness_state=freshness_state,
                blocked_reason=stale_reason or "expired",
            )
        if freshness_state == FreshnessState.STALE_BUT_SALVAGEABLE:
            return OutboundPolicy(
                should_send=True,
                freshness_state=freshness_state,
                length_class="short",
                segment_strategy="single",
                late_rewrite_allowed=True,
                send_delay_profile="fast",
            )
        strategy = "default"
        if reply_mode in {ReplyMode.PLAYFUL_INTERACTION, ReplyMode.IMAGE_REACTION}:
            strategy = "single"
        elif reply_mode == ReplyMode.EMOTIONAL_SUPPORT:
            strategy = "gentle_two_step"
        return OutboundPolicy(
            should_send=True,
            freshness_state=freshness_state,
            length_class="normal",
            segment_strategy=strategy,
            late_rewrite_allowed=False,
            send_delay_profile="default",
        )

    def _rewrite_late_reply(self, reply_mode: ReplyMode, clean_text: str) -> str:
        fallback_map = {
            ReplyMode.PLAYFUL_INTERACTION: "欸，我刚刚还想接你这句来着。",
            ReplyMode.EMOTIONAL_SUPPORT: "我还在这儿，先抱抱你。",
            ReplyMode.DIRECT_QUESTION: "我刚刚看到你这句了，让我接一下。",
            ReplyMode.CASUAL_FOLLOWUP: "我还记着你刚刚这句呢。",
            ReplyMode.IMAGE_REACTION: "刚刚那张我看到啦。",
            ReplyMode.LATE_RECONNECT: "我这会儿才接上你刚刚那句。",
            ReplyMode.AMBIENT_IGNORE: "",
        }
        if not clean_text:
            return fallback_map.get(reply_mode, "")
        first_line = re.split(r"[\r\n。！？!?]", clean_text.strip(), maxsplit=1)[0].strip()
        if len(first_line) > 24:
            first_line = first_line[:24].rstrip("，,。.!?？") + "……"
        fallback = fallback_map.get(reply_mode, "")
        return first_line or fallback

    def _clean_reply_content(self, text: str) -> str:
        """
        [修改] 清洗 LLM 输出的幻觉前缀，并作为兜底防线拦截底层穿透的报错堆栈
        """
        if not text:
            return ""
        fallback_text = getattr(self.config.reply, "fallback_text", "（陷入了短暂的沉默...）")
        cleaned = sanitize_visible_reply_text(text, fallback_text=fallback_text)
        if cleaned != str(text).strip():
            logger.warning("[ReplyEngine] 已清洗或拦截异常回复文本，避免透传给用户。")
        return cleaned
        
        # [新增] 致命异常文本拦截层 (Fallback Interception 双重防线)
        error_keywords = ["Exception:", "All chat models fail", "Traceback", "请求失败", "APITimeoutError"]
        if any(keyword in text for keyword in error_keywords):
            logger.warning("[ReplyEngine] 🚨 在清洗阶段拦截到底层报错透传文本，已切断输出流并强制替换为兜底回复！")
            return getattr(self.config.reply, 'fallback_text', "（陷入了短暂的沉默...）")

        # 去除 [HH:MM:SS] 时间戳
        text = re.sub(r'^\[.*?\]\s*', '', text)
        # 去除 BotName: 前缀 (简单正则，匹配常见的 名字: 格式)
        text = re.sub(r'(?i)^[a-zA-Z0-9_\u4e00-\u9fa5]+[：:]\s*', '', text)
        
        return text.strip()

    def _segment_reply_content(self, text: str, reply_mode: ReplyMode, policy: OutboundPolicy) -> List[str]:
        """
        [修改] 拟人化分段算法 (安全闭环版，彻底解决颜文字切片错位与正则冲突)
        代理调用独立的 TextSegmenter 核心，解决正则切割太粗暴与换行符逃逸的问题。
        """
        if policy.segment_strategy == "single":
            cleaned = re.sub(r'^\n+|\n+$', '', text.strip())
            return [cleaned] if cleaned else []

        if len(text) > self.no_segment_limit:
            # 即使触发不分段机制，也必须净化首尾换行符，斩杀导致气泡错位的幽灵字符
            cleaned = re.sub(r'^\n+|\n+$', '', text.strip())
            return [cleaned] if cleaned else []

        # 直接调用外置的智能状态机分段器，其内部已经妥善处理了片段粘连、标点吞噬和换行符逃逸
        segments = self.segmenter.segment(text)
        if policy.segment_strategy == "gentle_two_step":
            return segments[:2]
        return segments

    def _build_visible_reply_artifact(
        self,
        text: str,
        *,
        reply_mode: ReplyMode = ReplyMode.CASUAL_FOLLOWUP,
        freshness_state: FreshnessState = FreshnessState.FRESH,
        stale_reason: str = "",
    ) -> VisibleReplyArtifact:
        policy = self._build_outbound_policy(reply_mode, freshness_state, stale_reason)
        if not policy.should_send:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason=policy.blocked_reason or "outbound_blocked",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        clean_text = self._clean_reply_content(text)
        if freshness_state == FreshnessState.STALE_BUT_SALVAGEABLE and policy.late_rewrite_allowed:
            clean_text = self._rewrite_late_reply(reply_mode, clean_text)
        if not clean_text:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason="empty_or_blocked_reply",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        segments = [
            segment
            for segment in self._segment_reply_content(clean_text, reply_mode, policy)
            if is_sendable_segment(segment)
        ]
        if not segments:
            return VisibleReplyArtifact(
                visible_text="",
                segments=[],
                persistable_text="",
                blocked_reason="no_sendable_segments",
                metadata={"reply_mode": reply_mode.value, "freshness_state": freshness_state.value},
            )

        visible_text = "\n".join(segments).strip()
        return VisibleReplyArtifact(
            visible_text=visible_text,
            segments=segments,
            persistable_text=visible_text,
            metadata={
                "segment_count": len(segments),
                "reply_mode": reply_mode.value,
                "freshness_state": freshness_state.value,
                "segment_strategy": policy.segment_strategy,
            },
        )

    async def _fetch_history(self, chat_id: str, anchor_text: str, anchor_event: AstrMessageEvent = None) -> list:
        """??? lane ?????????????????? conversation ???"""
        fetch_count = getattr(self.config.attention, "bg_pool_size", 20) if self.config else 20
        lane_manager = getattr(getattr(self.state_engine, "gateway", None), "lane_manager", None)
        if lane_manager is None:
            return []
        try:
            raw_history = await lane_manager.get_lane_history(
                lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
                base_origin=chat_id,
            )
            clean_anchor = re.sub(r"\s+", "", anchor_text or "")
            if clean_anchor:
                cutoff_idx = -1
                for i in range(len(raw_history) - 1, -1, -1):
                    msg_data = raw_history[i]
                    if not isinstance(msg_data, dict):
                        continue
                    content = str(msg_data.get("content", "") or "").strip()
                    if content and clean_anchor in re.sub(r"\s+", "", content):
                        cutoff_idx = i
                        break
                if cutoff_idx >= 0:
                    start_idx = max(0, cutoff_idx - fetch_count)
                    return raw_history[start_idx:cutoff_idx + 1]
            return raw_history[-fetch_count:]
        except Exception as e:
            logger.warning(f"[ReplyEngine] ?? lane ????: {e}")
            return []

    async def _sync_native_history_mirror(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """??????????????????? conversation ???"""
        return

    # [?修改] 函数位置：astrmai/Brain/reply_engine.py -> ReplyEngine 类下
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
        [修改] 彻底解决大模型 API 卡死导致的阻塞问题：优先下发文本回复，后置结算情绪与好感度。
        """
        if not raw_text: return

        reply_mode = self._resolve_reply_mode(event)
        freshness_state, stale_reason = await self._check_reply_freshness(event, chat_id)
        artifact = self._build_visible_reply_artifact(
            raw_text,
            reply_mode=reply_mode,
            freshness_state=freshness_state,
            stale_reason=stale_reason,
        )
        if artifact.blocked:
            logger.debug(f"[{chat_id}] trace={event.get_extra('astrmai_trace_id', '')} reply blocked: {artifact.blocked_reason}")
            return
        if freshness_state == FreshnessState.EXPIRED:
            logger.info(
                f"[ReplyEngine] skipped stale reply for {chat_id}: {stale_reason} | "
                f"preview={preview_text(artifact.visible_text, 80)!r}"
            )
            return
        clean_text = artifact.persistable_text
        logger.debug(
            f"[{chat_id}] trace={event.get_extra('astrmai_trace_id', '')} "
            f"reply artifact segments={len(artifact.segments)} visible={preview_text(artifact.visible_text, 120)!r}"
        )

        # =====================================================================
        # 🟢 [核心修复] 强行同步至 AstrBot 原生历史记录（打破永久失忆魔咒）
        # =====================================================================
        sender_name = event.get_sender_name() or "群友"
        rich_text = event.get_extra("astrmai_rich_text", event.message_str)
        formatted_user_text = f"{sender_name}: {rich_text}"
        await self._sync_native_history_mirror(
            event=event,
            chat_id=chat_id,
            user_text=formatted_user_text,
            assistant_text=artifact.persistable_text,
        )

        # =====================================================================
        # 🟢 [核心架构重构] 步骤 3 提前：先说话！不要让情绪计算阻塞文本的下发
        # =====================================================================
        segments = artifact.segments
        
        _pending_actions = pending_actions if pending_actions is not None else event.get_extra("astrmai_pending_actions", [])
        at_targets = [action.get("target_id") for action in _pending_actions if action.get("action") == "at"]
        if at_targets:
            at_target_names = [
                str(action.get("target_name"))
                for action in _pending_actions
                if action.get("action") == "at" and action.get("target_name")
            ]
            existing_targets = [str(t) for t in (event.get_extra("astrmai_wait_targets", []) or []) if t]
            merged_targets = existing_targets[:]
            for target_id in at_targets:
                target_str = str(target_id)
                if target_str and target_str not in merged_targets:
                    merged_targets.append(target_str)
            emit_legacy_reply_runtime_extras(
                event,
                wait_targets=merged_targets,
                wait_target_name=at_target_names[0] if at_target_names else "",
            )
        
        from astrbot.api.event import MessageChain
        
        # [新增] 免疫标记：防止自身发出的消息触发 main.py 中的旁路嗅探
        emit_legacy_reply_runtime_extras(event, artifact=artifact, is_self_reply=True)
        
        for i, seg in enumerate(segments):
            freshness_state, stale_reason = await self._check_reply_freshness(event, chat_id)
            if freshness_state == FreshnessState.EXPIRED:
                logger.info(
                    f"[ReplyEngine] stopped stale segmented reply for {chat_id}: {stale_reason} | "
                    f"segment_index={i}"
                )
                break
            chain = MessageChain()
            
            if i == 0 and at_targets:
                for target_id in at_targets:
                    uid = target_id
                    if str(target_id).isdigit():
                        uid = int(target_id)
                        
                    chain.chain.append(Comp.At(qq=uid))
                chain.chain.append(Comp.Plain(" "))
                
            chain.chain.append(Comp.Plain(seg))
            
            context = getattr(self.state_engine.gateway, 'context', None)
            if context:
                await context.send_message(event.unified_msg_origin, chain)
                artifact.sent = True
                if not event.get_extra("astrmai_reply_sent", False):
                    emit_legacy_reply_runtime_extras(event, artifact=artifact, reply_sent=True)
            else:
                logger.error("[ReplyEngine] 🚨 致命错误：Gateway Context 丢失，无法跨越生命周期发送消息！")
            
            if i < len(segments) - 1:
                base_factor = getattr(self.config.reply, 'typing_speed_factor', 0.1)
                delay = min(2.0, max(0.5, len(seg) * base_factor))
                await asyncio.sleep(delay)

        # =====================================================================
        # 🟢 步骤 2 滞后：用户已经看到回复了，现在后台慢慢算情绪和好感度
        # =====================================================================
        if not artifact.sent:
            return

        tag = "neutral"
        force_meme_flag = False
        
        _bypassed_tag = bypassed_tag or event.get_extra("astrmai_bypass_mood_analysis", None)
        _window_events = window_events if window_events is not None else event.get_extra("astrmai_window_events", [])
        _thread_root_event = event.get_extra("astrmai_focus_thread_root_event", None)
        _focus_event = event.get_extra("astrmai_focus_event", None)
        _anchor_event = anchor_event or _thread_root_event or _focus_event or event.get_extra("astrmai_anchor_event", None)

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
                # 🟢 MoodManager 调用已废除，情绪变化由 Judge 在前置阶段完成，这里仅简单衰减情绪
                new_mood = await self.state_engine.atomic_update_mood(chat_id, delta=0.0) # 触发内部的 decay
            
            logger.debug(f"[Reply] 😃 情绪衰减更新: ({new_mood:.2f})")
            
            # ==========================================
            # 🟢 靶向情感结算与私聊挖掘计数
            # ==========================================
            is_private_chat = "FriendMessage" in chat_id or not event.get_group_id()
            target_user_id = None
            
            if is_private_chat:
                target_user_id = str(user_id)
                logger.debug(f"[ReplyEngine] 🎯 检测到私聊环境，绕过群聊归因，100% 靶向用户 {target_user_id} 结算情绪。")
                
                # 仅在私聊环境中进行挖掘计数累加
                try:
                    profile = await self.state_engine.get_user_profile(user_id)
                    async with self.state_engine._get_user_lock(user_id):
                        profile.message_count_for_profiling += 1
                        profile.is_dirty = True
                    if hasattr(self.state_engine.persistence, 'save_user_profile'):
                        await self.state_engine.persistence.save_user_profile(profile)
                except Exception as e:
                    logger.warning(f"[ReplyEngine] 私聊互动计数失败: {e}")
                    
            else:
                anchor_text = _anchor_event.message_str.strip() if _anchor_event else ""
                if getattr(getattr(self.config, 'global_settings', None), 'debug_mode', False):
                    logger.debug(
                        f"[ReplyEngine] focus_anchor={anchor_text[:120]!r} "
                        f"focus_reason={event.get_extra('astrmai_focus_reason', '')!r} "
                        f"root_reason={event.get_extra('astrmai_focus_thread_root_reason', '')!r}"
                    )
                history_events = await self._fetch_history(chat_id, anchor_text, anchor_event=_anchor_event)
                
                target_user_id = AffectionRouter.route(
                    history_events=history_events,
                    window_events=_window_events,
                    trigger_event=event,
                    mood_tag=tag,
                    config=self.config,
                    fallback_uid=user_id
                )

            if target_user_id:
                safe_target_uid = str(target_user_id)
                logger.info(f"[ReplyEngine] 🤝 准备为核心引导用户 {safe_target_uid} 结算好感度。")
                
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

        # 4. 发送表情包 (在文本发完、情绪算完之后，作为延时的补充动作发出)
        if tag and tag != "neutral":
            final_prob = 100 if force_meme_flag else self.meme_probability
            
            global_context = getattr(self.state_engine.gateway, 'context', None)
            
            await send_meme(
                event=event, 
                emotion_tag=tag, 
                probability=final_prob, 
                memes_dir=MEMES_DIR,
                context=global_context 
            )
