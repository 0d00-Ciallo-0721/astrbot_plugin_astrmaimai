import asyncio
import hashlib
import re
import time
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass, field
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
from ..infra.trace_runtime import new_trace_id, preview_text
from ..infra.legacy_compat import emit_legacy_focus_thread_extras

from .state_engine import StateEngine
from .judge import Judge
from .sensors import PreFilters
from ..infra.runtime_contracts import (
    FocusThreadContext,
    FreshnessState,
    ReplyFreshnessBudget,
    ReplyMode,
    VisionBundle,
)
from astrbot.api.message_components import Image, Plain, At, Face # 导入 AstrBot 的底层消息组件
from ..infra.runtime_contracts import FocusThreadContext, VisionBundle

@dataclass
class SessionContext:
    """纯内存态并发上下文，全局共享序列池"""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    accumulation_pool: List[Any] = field(default_factory=list)
    is_evaluating: bool = False
    last_active_time: float = field(default_factory=time.time) # [新增] 用于惰性 GC 追踪生命周期


@dataclass
class NormalizedEvent:
    event: AstrMessageEvent
    sender_id: str
    sender_name: str
    text: str
    rich_text: str
    timestamp: float
    is_self: bool
    is_reply_to_bot: bool
    is_at_bot: bool
    is_direct_wakeup: bool
    is_near_context_query: bool
    reply_target_sender_id: str = ""
    reply_target_sender_name: str = ""
    image_urls: List[str] = field(default_factory=list)
    has_direct_vision: bool = False
    is_image_only: bool = False
    token_set: Set[str] = field(default_factory=set)
    index: int = 0


class AttentionGate:
    def __init__(self, state_engine: StateEngine, judge: Judge, sensors: PreFilters,
                 system2_callback, config=None, visual_cortex=None,
                 persona_summarizer=None, frequency_controller=None,
                 private_chat_manager=None, runtime_coordinator=None):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback 
        self.config = config if config else state_engine.config
        self.visual_cortex = visual_cortex
        self.persona_summarizer = persona_summarizer
        # Phase 6.3: 发言频率控制器
        self.frequency_controller = frequency_controller
        self.private_chat_manager = private_chat_manager
        self.runtime_coordinator = runtime_coordinator
        
        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()
        
        # [彻底修复 Bug 3] 新增受控的后台任务追踪池
        self._background_tasks = set()

    # [新增] 从 Image 组件提取 Base64 数据的辅助方法
    async def _extract_image_base64(self, image_component: Any) -> str:
        import base64
        # 1. 尝试直接获取 Base64
        if hasattr(image_component, 'file_to_base64'):
            try:
                res = await image_component.file_to_base64()
                if res: return res
            except Exception:
                pass
        
        # 2. 如果是 URL，发起请求下载
        url = getattr(image_component, 'url', None)
        if url:
            return await self._extract_image_base64_from_url(url)
        
        # 3. 如果是本地路径
        file_path = getattr(image_component, 'file', None) or getattr(image_component, 'path', None)
        if file_path:
            try:
                with open(file_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode('utf-8')
            except Exception:
                pass
        return ""

    # [新增] 从 URL 提取 Base64 数据的辅助方法
    async def _extract_image_base64_from_url(self, url: str) -> str:
        import aiohttp
        import base64
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        return base64.b64encode(data).decode('utf-8')
        except Exception as e:
            logger.debug(f"[{self.__class__.__name__}] 获取图片 URL 失败: {e}")
        return ""

    # [修改] 位置: astrmai/Heart/attention.py -> AttentionGate 类下
    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        async with self._pool_lock:
            if chat_id not in self.focus_pools:
                self.focus_pools[chat_id] = SessionContext()
            # [新增] 每次获取时刷新活跃时间戳
            self.focus_pools[chat_id].last_active_time = time.time()
            return self.focus_pools[chat_id]

    def _is_direct_wakeup_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        if not event:
            return False
        if event.get_extra("astrmai_group_direct_wakeup", False):
            return True
        if event.get_extra("astrmai_bonus_score", 0.0) >= 1.0:
            return True
        try:
            return bool(self.sensors.is_wakeup_signal(event, self_id))
        except Exception:
            return False

    def _is_at_bot_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        if not event or not getattr(event, "message_obj", None) or not getattr(event.message_obj, "message", None):
            return False
        for component in event.message_obj.message:
            component_type = getattr(component, "type", component.__class__.__name__).lower()
            if component_type != "at":
                continue
            target = str(getattr(component, "qq", "") or getattr(component, "target", "") or "")
            if target and target == str(self_id):
                return True
        return False

    def _is_reply_to_bot_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        if not event or not getattr(event, "message_obj", None) or not getattr(event.message_obj, "message", None):
            return False

        bot_names = []
        if hasattr(self.config, "system1") and getattr(self.config.system1, "nicknames", None):
            bot_names = [str(name).strip() for name in self.config.system1.nicknames if str(name).strip()]

        for component in event.message_obj.message:
            component_type = getattr(component, "type", component.__class__.__name__).lower()
            if component_type != "reply":
                continue
            reply_sender_id = str(getattr(component, "sender_id", "") or "")
            reply_sender_name = str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).strip()
            if reply_sender_id and reply_sender_id == str(self_id):
                return True
            if reply_sender_name and reply_sender_name in bot_names:
                return True
        return False

    @staticmethod
    def _is_near_context_query_text(message_text: str) -> bool:
        if not isinstance(message_text, str):
            return False
        normalized = message_text.strip()
        if not normalized:
            return False
        trigger_phrases = [
            "为什么", "哪里", "什么意思", "你刚刚", "刚刚说", "这个", "那个",
            "上一个", "上一句", "不是这个", "为啥", "咋", "啥意思", "不可以",
        ]
        return any(phrase in normalized for phrase in trigger_phrases)

    @staticmethod
    def _tokenize_text(text: str) -> Set[str]:
        if not isinstance(text, str):
            return set()
        normalized = re.sub(r"\[[^\]]+\]", " ", text.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            return set()

        tokens: Set[str] = set(re.findall(r"[a-z0-9_]{2,}", normalized))
        for chunk in re.findall(r"[\u4e00-\u9fff]+", normalized):
            if len(chunk) <= 4:
                tokens.add(chunk)
            else:
                tokens.add(chunk)
                tokens.update(chunk[idx:idx + 2] for idx in range(len(chunk) - 1))
        return {token for token in tokens if token}

    def _extract_reply_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        if not event or not getattr(event, "message_obj", None) or not getattr(event.message_obj, "message", None):
            return "", ""

        for component in event.message_obj.message:
            component_type = getattr(component, "type", component.__class__.__name__).lower()
            if component_type != "reply":
                continue
            target_id = str(getattr(component, "sender_id", "") or "")
            target_name = str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).strip()
            return target_id, target_name
        return "", ""

    def _build_normalized_events(self, events: List[AstrMessageEvent], self_id: str) -> List[NormalizedEvent]:
        normalized_events: List[NormalizedEvent] = []
        for index, event in enumerate(events):
            sender_id = str(event.get_sender_id())
            sender_name = event.get_sender_name() or "群友/用户"
            rich_text = str(event.get_extra("astrmai_rich_text", event.message_str) or "")
            text = str(event.message_str or rich_text or "")
            image_urls = list(
                dict.fromkeys(
                    list(event.get_extra("direct_vision_urls", []) or [])
                    + list(event.get_extra("extracted_image_urls", []) or [])
                )
            )
            token_set = self._tokenize_text(rich_text or text)
            reply_target_sender_id, reply_target_sender_name = self._extract_reply_target(event)
            is_at_bot = self._is_at_bot_event(event, self_id)

            normalized_events.append(
                NormalizedEvent(
                    event=event,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                    rich_text=rich_text,
                    timestamp=float(event.get_extra("astrmai_timestamp", getattr(event, "timestamp", 0.0)) or 0.0),
                    is_self=sender_id == str(self_id),
                    is_reply_to_bot=self._is_reply_to_bot_event(event, self_id),
                    is_at_bot=is_at_bot,
                    is_direct_wakeup=self._is_direct_wakeup_event(event, self_id),
                    is_near_context_query=self._is_near_context_query_text(text or rich_text),
                    reply_target_sender_id=reply_target_sender_id,
                    reply_target_sender_name=reply_target_sender_name,
                    image_urls=image_urls,
                    has_direct_vision=bool(event.get_extra("direct_vision_urls", []) or []),
                    is_image_only=bool(image_urls and not token_set),
                    token_set=token_set,
                    index=index,
                )
            )
        return normalized_events

    def _score_focus_candidate(
        self,
        candidate: NormalizedEvent,
        normalized_events: List[NormalizedEvent],
    ) -> tuple[int, str]:
        if candidate.is_self:
            return -10_000, "self_message"

        score = 20
        reason = "latest_user_message"
        attention_config = getattr(self.config, "attention", None)
        same_speaker_window = int(getattr(getattr(self.config, "attention", None), "thread_same_speaker_followup_sec", 8) or 8)
        reply_priority_enabled = bool(getattr(attention_config, "thread_reply_priority_enabled", True))

        if candidate.is_reply_to_bot and reply_priority_enabled:
            score += 1000
            reason = "reply_to_bot"
        elif candidate.is_at_bot and reply_priority_enabled:
            score += 800
            reason = "at_bot"
        elif candidate.is_direct_wakeup and reply_priority_enabled:
            score += 700
            reason = "direct_wakeup"
        elif candidate.has_direct_vision:
            score += 500
            reason = "direct_vision_request"
        elif candidate.is_near_context_query:
            score += 350
            reason = "near_context_followup"

        for previous in reversed(normalized_events[:candidate.index]):
            if previous.is_self:
                continue
            if previous.sender_id != candidate.sender_id:
                break
            if candidate.timestamp and previous.timestamp and (candidate.timestamp - previous.timestamp) > same_speaker_window:
                break
            score += 120
            if reason == "latest_user_message":
                reason = "same_sender_followup"
            break

        recency_bonus = max(0, 90 - max(0, len(normalized_events) - candidate.index - 1) * 30)
        score += recency_bonus
        return score, reason

    def _select_focus_event(self, events: List[AstrMessageEvent], self_id: str):
        if not events:
            return None, [], "empty"

        attention_config = getattr(self.config, "attention", None)
        if not bool(getattr(attention_config, "focus_thread_enabled", True)):
            candidates = [event for event in events if str(event.get_sender_id()) != str(self_id)]
            if not candidates:
                focus_event = events[-1]
                return focus_event, [event for event in events if event is not focus_event], "fallback_last_event"
            for event in reversed(candidates):
                if self._is_reply_to_bot_event(event, self_id):
                    return event, [item for item in events if item is not event], "reply_to_bot"
            for event in reversed(candidates):
                if self._is_direct_wakeup_event(event, self_id):
                    return event, [item for item in events if item is not event], "direct_wakeup"
            focus_event = candidates[-1]
            return focus_event, [item for item in events if item is not focus_event], "fallback_last_event"

        normalized_events = self._build_normalized_events(events, self_id)
        candidates = [candidate for candidate in normalized_events if not candidate.is_self]
        if not candidates:
            focus_event = events[-1]
            return focus_event, [event for event in events if event is not focus_event], "fallback_last_event"

        best_candidate = max(
            candidates,
            key=lambda candidate: (
                self._score_focus_candidate(candidate, normalized_events)[0],
                candidate.index,
            ),
        )
        focus_reason = self._score_focus_candidate(best_candidate, normalized_events)[1]
        focus_event = best_candidate.event
        return focus_event, [item for item in events if item is not focus_event], focus_reason

    def _resolve_thread_root(
        self,
        focus_candidate: NormalizedEvent,
        normalized_events: List[NormalizedEvent],
    ) -> tuple[Optional[NormalizedEvent], str]:
        if focus_candidate.reply_target_sender_id or focus_candidate.reply_target_sender_name:
            for previous in reversed(normalized_events[:focus_candidate.index]):
                if focus_candidate.reply_target_sender_id and previous.sender_id == focus_candidate.reply_target_sender_id:
                    return previous, "explicit_reply_target"
                if focus_candidate.reply_target_sender_name and previous.sender_name == focus_candidate.reply_target_sender_name:
                    return previous, "explicit_reply_target"
            return None, "explicit_reply_target"

        same_speaker_window = int(getattr(getattr(self.config, "attention", None), "thread_same_speaker_followup_sec", 8) or 8)
        if focus_candidate.is_near_context_query:
            return None, "recent_assistant_turn"

        for previous in reversed(normalized_events[:focus_candidate.index]):
            if previous.is_self:
                continue
            if previous.sender_id != focus_candidate.sender_id:
                break
            if focus_candidate.timestamp and previous.timestamp and (focus_candidate.timestamp - previous.timestamp) > same_speaker_window:
                break
            return previous, "same_sender_chain"
        return focus_candidate, "self_root"

    def _score_thread_relation(
        self,
        candidate: NormalizedEvent,
        focus_candidate: NormalizedEvent,
        root_candidate: Optional[NormalizedEvent],
    ) -> int:
        if candidate.event is focus_candidate.event:
            return 10_000
        if root_candidate and candidate.event is root_candidate.event:
            return 9_000

        score = 0
        shared_tokens = set()
        if candidate.token_set:
            shared_tokens |= candidate.token_set & focus_candidate.token_set
            if root_candidate:
                shared_tokens |= candidate.token_set & root_candidate.token_set

        if root_candidate and candidate.reply_target_sender_id:
            if candidate.reply_target_sender_id == root_candidate.sender_id:
                score += 100
            elif focus_candidate.reply_target_sender_id and candidate.reply_target_sender_id == focus_candidate.reply_target_sender_id:
                score += 85
        if candidate.sender_id == focus_candidate.sender_id and candidate.index < focus_candidate.index:
            same_speaker_window = int(getattr(getattr(self.config, "attention", None), "thread_same_speaker_followup_sec", 8) or 8)
            if not candidate.timestamp or not focus_candidate.timestamp or (focus_candidate.timestamp - candidate.timestamp) <= same_speaker_window:
                score += 40
        if candidate.image_urls and focus_candidate.image_urls:
            if candidate.sender_id == focus_candidate.sender_id:
                score += 35
            if candidate.has_direct_vision and focus_candidate.has_direct_vision:
                score += 25
        if shared_tokens:
            score += 25
        if candidate.is_near_context_query and abs(candidate.index - focus_candidate.index) <= 1:
            score += 25
        if abs(candidate.index - focus_candidate.index) <= 1:
            score += 15
        return score

    @staticmethod
    def _question_like(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        question_keywords = ("?", "？", "为什么", "怎么", "啥", "什么", "吗", "是不是", "能不能", "可不可以")
        return any(keyword in normalized for keyword in question_keywords)

    @staticmethod
    def _emotion_like(text: str) -> bool:
        normalized = str(text or "").strip()
        if not normalized:
            return False
        emotional_keywords = (
            "难受", "焦虑", "害怕", "不舒服", "委屈", "难过", "崩溃", "emo", "痛", "累", "好困", "想哭",
            "呜", "呜呜", "抱抱", "安慰", "救救", "不想活", "烦", "烦死", "想死",
        )
        return any(keyword in normalized for keyword in emotional_keywords)

    def _infer_reply_mode(
        self,
        focus_candidate: NormalizedEvent,
        root_candidate: Optional[NormalizedEvent],
        normalized_events: List[NormalizedEvent],
    ) -> ReplyMode:
        del normalized_events
        interaction_kind = str(focus_candidate.event.get_extra("astrmai_interaction_kind", "") or "").strip().lower()
        text = focus_candidate.rich_text or focus_candidate.text
        if interaction_kind:
            return ReplyMode.PLAYFUL_INTERACTION
        if focus_candidate.image_urls:
            if focus_candidate.has_direct_vision or focus_candidate.is_at_bot or focus_candidate.is_reply_to_bot:
                return ReplyMode.IMAGE_REACTION
            return ReplyMode.AMBIENT_IGNORE
        if self._emotion_like(text):
            return ReplyMode.EMOTIONAL_SUPPORT
        if self._question_like(text):
            return ReplyMode.DIRECT_QUESTION
        if focus_candidate.is_direct_wakeup or focus_candidate.is_at_bot or focus_candidate.is_reply_to_bot:
            return ReplyMode.CASUAL_FOLLOWUP
        if root_candidate and root_candidate.event is not focus_candidate.event:
            return ReplyMode.CASUAL_FOLLOWUP
        return ReplyMode.CASUAL_FOLLOWUP

    @staticmethod
    def _derive_social_state(reply_mode: ReplyMode) -> str:
        mapping = {
            ReplyMode.PLAYFUL_INTERACTION: "playful_present",
            ReplyMode.EMOTIONAL_SUPPORT: "gentle_support",
            ReplyMode.DIRECT_QUESTION: "direct_answering",
            ReplyMode.CASUAL_FOLLOWUP: "casual_presence",
            ReplyMode.IMAGE_REACTION: "light_visual_reaction",
            ReplyMode.LATE_RECONNECT: "late_reconnect",
            ReplyMode.AMBIENT_IGNORE: "ambient_background",
        }
        return mapping.get(reply_mode, "casual_presence")

    def _build_thread_signature(
        self,
        focus_candidate: NormalizedEvent,
        root_candidate: Optional[NormalizedEvent],
        reply_mode: ReplyMode,
    ) -> str:
        root = root_candidate or focus_candidate
        basis = "|".join(
            [
                str(reply_mode.value),
                str(root.sender_id or ""),
                str(root.reply_target_sender_id or ""),
                str(focus_candidate.sender_id or ""),
                hashlib.md5(str(root.rich_text or root.text or "").encode("utf-8")).hexdigest()[:10],
            ]
        )
        return basis

    def _build_focus_thread(
        self,
        focus_candidate: NormalizedEvent,
        root_candidate: Optional[NormalizedEvent],
        normalized_events: List[NormalizedEvent],
    ) -> FocusThreadContext:
        core_events: List[AstrMessageEvent] = []
        related_events: List[AstrMessageEvent] = []
        ambient_events: List[AstrMessageEvent] = []
        attention_config = getattr(self.config, "attention", None)
        thread_enabled = bool(getattr(attention_config, "focus_thread_enabled", True))
        core_limit = int(getattr(attention_config, "focus_thread_core_max_messages", 4) or 4)
        related_limit = int(getattr(attention_config, "focus_thread_related_max_messages", 3) or 3)
        ambient_limit = int(getattr(attention_config, "ambient_background_max_messages", 2) or 2)

        def _append_unique(container: List[AstrMessageEvent], event: AstrMessageEvent, limit: Optional[int] = None):
            if event in container:
                return
            if limit is not None and len(container) >= limit:
                return
            container.append(event)

        _append_unique(core_events, focus_candidate.event, core_limit)
        if root_candidate and root_candidate.event is not focus_candidate.event:
            _append_unique(core_events, root_candidate.event, core_limit)
        reply_mode = self._infer_reply_mode(focus_candidate, root_candidate, normalized_events)
        social_state = self._derive_social_state(reply_mode)
        thread_signature = self._build_thread_signature(focus_candidate, root_candidate, reply_mode)
        freshness_budget = ReplyFreshnessBudget(
            state=FreshnessState.FRESH,
            created_at=float(focus_candidate.timestamp or 0.0),
        )

        if not thread_enabled:
            for candidate in normalized_events:
                if candidate.event is focus_candidate.event:
                    continue
                _append_unique(ambient_events, candidate.event, ambient_limit)
            return FocusThreadContext(
                focus_event=focus_candidate.event,
                root_event=root_candidate.event if root_candidate else None,
                core_events=core_events,
                related_events=related_events,
                ambient_events=ambient_events,
                focus_reason="",
                root_reason="",
                focus_message_text="",
                focus_sender_id=focus_candidate.sender_id,
                focus_sender_name=focus_candidate.sender_name,
                reply_mode=reply_mode,
                social_state=social_state,
                thread_signature=thread_signature,
                freshness_budget=freshness_budget,
                vision_bundle=VisionBundle(
                    image_urls=focus_candidate.image_urls[:],
                    direct_image_urls=focus_candidate.image_urls[:] if focus_candidate.has_direct_vision else [],
                    is_direct_request=focus_candidate.has_direct_vision,
                    is_image_only=focus_candidate.is_image_only,
                    source="focus_thread",
                ),
            )

        scored_candidates = []
        for candidate in normalized_events:
            if candidate.event in core_events:
                continue
            relation_score = self._score_thread_relation(candidate, focus_candidate, root_candidate)
            scored_candidates.append((relation_score, candidate))

        for relation_score, candidate in sorted(scored_candidates, key=lambda item: (item[0], item[1].index), reverse=True):
            if relation_score >= 70:
                _append_unique(core_events, candidate.event, core_limit)
            elif relation_score >= 35:
                _append_unique(related_events, candidate.event, related_limit)
            elif relation_score >= 0:
                _append_unique(ambient_events, candidate.event, ambient_limit)

        for candidate in normalized_events:
            if candidate.event in core_events or candidate.event in related_events or candidate.event in ambient_events:
                continue
            _append_unique(ambient_events, candidate.event, ambient_limit)

        core_events.sort(key=lambda event: next(item.index for item in normalized_events if item.event is event))
        related_events.sort(key=lambda event: next(item.index for item in normalized_events if item.event is event))
        ambient_events.sort(key=lambda event: next(item.index for item in normalized_events if item.event is event))

        return FocusThreadContext(
            focus_event=focus_candidate.event,
            root_event=root_candidate.event if root_candidate else None,
            core_events=core_events,
            related_events=related_events,
            ambient_events=ambient_events,
            focus_reason="",
            root_reason="",
            focus_message_text="",
            focus_sender_id=focus_candidate.sender_id,
            focus_sender_name=focus_candidate.sender_name,
            reply_mode=reply_mode,
            social_state=social_state,
            thread_signature=thread_signature,
            freshness_budget=freshness_budget,
            vision_bundle=VisionBundle(
                image_urls=focus_candidate.image_urls[:],
                direct_image_urls=focus_candidate.image_urls[:] if focus_candidate.has_direct_vision else [],
                is_direct_request=focus_candidate.has_direct_vision,
                is_image_only=focus_candidate.is_image_only,
                source="focus_thread",
            ),
        )

    def _is_image_only(self, event: AstrMessageEvent) -> bool:
        """判断是否为纯图片消息"""
        has_img = bool(event.get_extra("extracted_image_urls"))
        has_text = bool(event.message_str and event.message_str.strip())
        return has_img and not has_text

    def _check_continuous_images(self, pool: List[AstrMessageEvent]) -> int:
        """计算末尾连续图片消息的数量"""
        count = 0
        for e in reversed(pool):
            if self._is_image_only(e):
                count += 1
            else:
                break
        return count

    def _fire_background_task(self, coro):
        """[新增] 安全触发后台任务，接管游离 Task 防止静默崩溃"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)


    def _handle_task_result(self, task: asyncio.Task):
        """[新增] 清理已完成的任务并暴漏异常"""
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[Attention Task Error] 注意力系统后台任务发生异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass       

    async def _record_event_activity(self, chat_id: str, event: AstrMessageEvent, sender_id: str) -> float:
        event_timestamp = float(event.get_extra("astrmai_timestamp", 0.0) or 0.0)
        if event_timestamp <= 0:
            event_timestamp = time.time()
            event.set_extra("astrmai_timestamp", event_timestamp)

        if self.runtime_coordinator:
            await self.runtime_coordinator.mark_activity(
                chat_id,
                event_timestamp,
                sender_id=str(sender_id),
                sender_name=str(event.get_sender_name() or sender_id),
                preview=preview_text(str(event.message_str or ""), 80),
            )
        return event_timestamp

    async def process_event(self, event: AstrMessageEvent) -> str:
        """
        [修改] 注意力判断入口，返回枚举态字符串 (ENGAGED, BUFFERED, IGNORE) 
        精准指导 AstrBot 原生底层的 Stop_Event。
        """
        msg_id = getattr(event.message_obj, 'message_id', None) if getattr(event, 'message_obj', None) else None
        if not msg_id:
            msg_timestamp = getattr(event, 'timestamp', '')
            msg_id = hash(f"{event.message_str}_{event.get_sender_id()}_{msg_timestamp}")

        if not hasattr(AttentionGate, '_global_msg_cache'):
            import collections
            AttentionGate._global_msg_cache = collections.deque(maxlen=200)

        if msg_id in AttentionGate._global_msg_cache:
            return "IGNORE" 
            
        AttentionGate._global_msg_cache.append(msg_id)

        msg_str = event.message_str
        chat_id = str(event.unified_msg_origin)
        
        parts = chat_id.split(":")
        platform_type = parts[1] if len(parts) >= 3 else ("GroupMessage" if event.get_group_id() else "FriendMessage")
        is_private = (platform_type == "FriendMessage")
        event.set_extra("is_private_chat", is_private)
        
        sender_id = str(event.get_sender_id())
        self_id = str(event.get_self_id())
        if not event.get_extra("astrmai_trace_id", ""):
            event.set_extra("astrmai_trace_id", new_trace_id())

        # Phase 8.3: 通知私聊管理器，打断可能的等待状态
        if is_private and self.private_chat_manager:
             self._fire_background_task(
                 self.private_chat_manager.signal_new_message(user_id=sender_id, message_str=msg_str)
             )

        if event.get_extra("astrmai_force_engage", False):
            logger.info(f"[{chat_id}] 🔁 [Wait Resume] target user replied, force-engaging System2.")
            event.set_extra("retrieve_keys", ["CORE_ONLY"])
            event.set_extra("is_fast_mode", True)
            event.set_extra(
                "sys1_thought",
                event.get_extra("astrmai_wait_resume_thought", "对方接上了你刚才的话题，立即继续回应。")
            )
            components = event.message_obj.message if (hasattr(event, "message_obj") and event.message_obj) else event.message_str
            await self._normalize_content_to_str(components, event=event)
            await self._record_event_activity(chat_id, event, sender_id)
            if self.sys2_process:
                self._fire_background_task(self.sys2_process(event, [event]))
            return "ENGAGED"
        
        max_len = getattr(self.config.attention, 'max_message_length', 100)
        if msg_str and len(msg_str.strip()) > max_len:
            return "IGNORE" 
            
        wakeup_words = self.config.system1.wakeup_words if self.config and hasattr(self.config.system1, "wakeup_words") else []
        msg_lower = msg_str.strip().lower() if msg_str else ""
        is_keyword_wakeup = any(msg_lower.startswith(kw.lower()) for kw in wakeup_words) if wakeup_words else False
        is_at_wakeup = self.sensors.is_wakeup_signal(event, self_id)
        is_nickname_wakeup = event.get_extra("astrmai_bonus_score", 0.0) >= 1.0
        
        is_strong_wakeup = is_at_wakeup or is_keyword_wakeup or is_nickname_wakeup
        if not is_private:
            event.set_extra("astrmai_group_direct_wakeup", is_strong_wakeup)

        complex_keywords = ["为什么", "怎么", "帮我", "代码", "解释", "写", "什么", "翻译", "分析"]
        is_simple_payload = len(msg_lower) <= 15 and not any(cw in msg_lower for cw in complex_keywords)

        if is_strong_wakeup and is_simple_payload:
            logger.info(f"[{chat_id}] ⚡ [快速模式] 开启窗口，绕过滑动防抖直达 Sys2！")
            event.set_extra("retrieve_keys", ["CORE_ONLY"])
            event.set_extra("is_fast_mode", True)
            event.set_extra("sys1_thought", "听到召唤，立即响应。")
            
            # 🚀 [新增] 快速提取视觉特征，确保穿透模式下主脑也能看见图
            components = event.message_obj.message if (hasattr(event, "message_obj") and event.message_obj) else event.message_str
            await self._normalize_content_to_str(components, event=event)
            await self._record_event_activity(chat_id, event, sender_id)

            if self.sys2_process:
                self._fire_background_task(self.sys2_process(event, [event]))
            return "ENGAGED"

        is_cmd = await self.sensors.is_command(msg_str)
        if is_cmd:
            setattr(event, "is_command_trigger", True)
            return "IGNORE"

        should_process = await self.sensors.should_process_message(event)
        if not should_process or event.get_extra("astrmai_is_command"):
            return "IGNORE" 

        chat_state = await self.state_engine.get_state(chat_id)
        
        extracted_images = event.get_extra("extracted_image_urls") or []
        direct_vision_urls = event.get_extra("direct_vision_urls") or []
        if extracted_images and not direct_vision_urls and not is_private and not is_strong_wakeup:
            logger.debug(f"[{chat_id}] passive group image share ignored by attention gate.")
            return "IGNORE"
        if extracted_images:
            await self.state_engine.persistence.add_last_message_meta(
                chat_id, sender_id, True, extracted_images
            )

        session = await self._get_or_create_session(chat_id)

        async with session.lock:
            if not session.is_evaluating:
                if not is_strong_wakeup and not is_private: 
                    min_entropy = getattr(self.config.attention, 'throttle_min_entropy', 2)
                    import re
                    pure_text = re.sub(r'[^\w\u4e00-\u9fa5]', '', msg_str) if msg_str else ""
                    if len(pure_text) < min_entropy and not extracted_images:
                        return "IGNORE" 
                    
                    probability = getattr(self.config.attention, 'throttle_probability', 0.1)
                    # Phase 6.3: 使用 FrequencyController 替代简单随机阈值
                    if self.frequency_controller:
                        energy = getattr(chat_state, 'energy', 1.0) if chat_state else 1.0
                        mood = getattr(chat_state, 'mood', 0.0) if chat_state else 0.0
                        should_reply = self.frequency_controller.should_reply(
                            chat_id=chat_id,
                            is_mentioned=is_strong_wakeup,
                            energy=energy,
                            mood=mood,
                            message_text=msg_str or "",
                        )
                        if not should_reply:
                            return "IGNORE"
                    else:
                        import random
                        if random.random() > probability:
                            return "IGNORE"

            if not is_private and not event.get_extra("is_virtual_poke"): 
                msg_hash = hash(msg_str) if msg_str else hash(str(extracted_images))
                if not hasattr(session, 'last_hash'):
                    session.last_hash = None
                    session.repeat_count = 0
                
                if session.last_hash == msg_hash:
                    session.repeat_count += 1
                    threshold = getattr(self.config.attention, 'repeater_threshold', 3)
                    if session.repeat_count == threshold - 1:
                        self._fire_background_task(event.send(event.plain_result(msg_str)))
                    
                    if session.repeat_count >= 1:
                        return "ENGAGED"
                else:
                    session.last_hash = msg_hash
                    session.repeat_count = 0

            session.accumulation_pool.append(event)
            await self._record_event_activity(chat_id, event, sender_id)

            if session.is_evaluating:
                logger.info(f"[{chat_id}] ⏳ [窗口持续] 写入消息 -> 累积池 (当前积压: {len(session.accumulation_pool)}条)")
                return "BUFFERED" 
            
            session.is_evaluating = True

        logger.info(f"[{chat_id}] 👁️ [普通模式] 开启窗口...")
        self._fire_background_task(self._debounce_and_judge(chat_id, session, self_id))
        return "BUFFERED"
    
    async def _normalize_content_to_str(self, components: Any, depth: int = 0, event: AstrMessageEvent = None) -> str:
        """
        [修改] 视觉盲区模式 (Vision-Blind System 1)：
        向 System 1 汇报的文本流中遇到图片强行替换为 [图片]。并将真实 URL 存入 event，留给 System 2。
        同时后台保留多模态记忆解析以完善持久化记忆。
        """
        if depth > 3:
            return "[引用层级过深，已截断]"
            
        if not components:
            return ""
        if isinstance(components, str):
            return components
            
        outline = ""
        if isinstance(components, list):
            for i in components:
                try:
                    component_type = getattr(i, 'type', None)
                    if not component_type:
                        component_type = i.__class__.__name__.lower()
                    
                    if isinstance(i, dict):
                        component_type = i.get("type", "unknown").lower()
                        if component_type in ["plain", "text"]:
                            outline += i.get("text", "")
                        elif component_type == "image":
                            # 🚀 [主脑直通车] 收集真实 URL 留给 System 2
                            url = i.get("url", "") or i.get("file", "") or i.get("path", "")
                            if url and event:
                                vision_urls = event.get_extra("direct_vision_urls", [])
                                if url not in vision_urls:
                                    vision_urls.append(url)
                                event.set_extra("direct_vision_urls", vision_urls)
                            
                            # 🚀 [后台记忆] 触发异步视觉皮层入库（不影响文本流）
                            import random
                            import hashlib
                            prob = getattr(self.config.vision, 'image_recognition_probability', 0.5) if hasattr(self.config, 'vision') else 0.0
                            if random.random() < prob:
                                base64_data = await self._extract_image_base64_from_url(url) if url else ""
                                if base64_data:
                                    pic_md5 = hashlib.md5(base64_data.encode('utf-8')).hexdigest()
                                    if getattr(self, 'visual_cortex', None):
                                        self._fire_background_task(
                                            self.visual_cortex.process_image_async(
                                                pic_md5,
                                                base64_data,
                                                event.unified_msg_origin,
                                            )
                                        )
                                        
                            # 🚀 [视觉盲区] 对 System 1 只暴露简单的占位符
                            outline += "[图片]"
                            
                        elif component_type == "at":
                            name = i.get("name", "")
                            qq = i.get("qq", "User")
                            outline += f"[@{name}({qq})]" if name else f"[@{qq}]"
                        else:
                            val = i.get("text", "")
                            if val: outline += val
                        continue

                    if component_type == "reply" or i.__class__.__name__ == "Reply":
                        sender_id = getattr(i, 'sender_id', '')
                        sender_nickname = getattr(i, 'sender_nickname', '')
                        
                        sender_info = ""
                        if sender_nickname:
                            sender_info = f"{sender_nickname}({sender_id})"
                        elif sender_id:
                            sender_info = f"{sender_id}"
                        else:
                            sender_info = "未知用户"
                        
                        reply_content = ""
                        if hasattr(i, 'chain') and i.chain:
                            reply_content = await self._normalize_content_to_str(i.chain, depth + 1, event)
                        elif hasattr(i, 'message_str') and i.message_str:
                            reply_content = i.message_str
                        elif hasattr(i, 'text') and i.text:
                            reply_content = i.text
                        else:
                            reply_content = "[内容不可用]"
                            
                        if len(reply_content) > 150:
                            reply_content = reply_content[:150] + "..."
                        
                        outline += f"「↪ 引用 {sender_info} 的消息：{reply_content}」"
                        continue
                        
                    if component_type == "plain" or i.__class__.__name__ == "Plain":
                        outline += getattr(i, 'text', '')
                    elif component_type == "image" or i.__class__.__name__ == "Image":
                        # 🚀 [主脑直通车] 收集真实 URL 留给 System 2
                        url = getattr(i, 'url', '') or getattr(i, 'file', '') or getattr(i, 'path', '')
                        if url and event:
                            vision_urls = event.get_extra("direct_vision_urls", [])
                            if url not in vision_urls:
                                vision_urls.append(url)
                            event.set_extra("direct_vision_urls", vision_urls)
                        
                        # 🚀 [后台记忆] 触发异步视觉皮层入库（不影响文本流）
                        import random
                        import hashlib
                        prob = getattr(self.config.vision, 'image_recognition_probability', 0.5) if hasattr(self.config, 'vision') else 0.0
                        if random.random() < prob:
                            base64_data = await self._extract_image_base64(i)
                            if base64_data:
                                pic_md5 = hashlib.md5(base64_data.encode('utf-8')).hexdigest()
                                if getattr(self, 'visual_cortex', None):
                                    self._fire_background_task(
                                        self.visual_cortex.process_image_async(
                                            pic_md5,
                                            base64_data,
                                            event.unified_msg_origin,
                                        )
                                    )
                                    
                        # 🚀 [视觉盲区] 对 System 1 只暴露简单的占位符
                        outline += "[图片]"

                    elif component_type == "face" or i.__class__.__name__ == "Face":
                        outline += f"[表情:{getattr(i, 'id', getattr(i, 'name', ''))}]"
                    elif component_type == "at" or i.__class__.__name__ == "At":
                        qq = getattr(i, 'qq', '')
                        name = getattr(i, 'name', '')
                        if str(qq).lower() == "all":
                            outline += "[@全体成员]"
                        elif name:
                            outline += f"[@{name}({qq})]"
                        else:
                            outline += f"[@{qq}]"
                    elif component_type == "record" or i.__class__.__name__ == "Record":
                        outline += "[语音]"
                    elif component_type == "video" or i.__class__.__name__ == "Video":
                        outline += "[视频]"
                    elif component_type == "share" or i.__class__.__name__ == "Share":
                        title = getattr(i, 'title', '')
                        content = getattr(i, 'content', '')
                        outline += f"[分享:《{title}》{content}]"
                    elif component_type == "contact" or i.__class__.__name__ == "Contact":
                        outline += f"[联系人:{getattr(i, 'id', '')}]"
                    elif component_type == "location" or i.__class__.__name__ == "Location":
                        title = getattr(i, 'title', '')
                        content = getattr(i, 'content', '')
                        outline += f"[位置:{title}({content})]"
                    elif component_type == "music" or i.__class__.__name__ == "Music":
                        title = getattr(i, 'title', '')
                        content = getattr(i, 'content', '')
                        outline += f"[音乐:{title}({content})]"
                    elif component_type == "poke" or i.__class__.__name__ == "Poke":
                        outline += f"[戳一戳 对:{getattr(i, 'qq', '')}]"
                    elif component_type in ["forward", "node", "nodes"] or i.__class__.__name__ in ["Forward", "Node", "Nodes"]:
                        outline += "[合并转发消息]"
                    elif component_type == "json" or i.__class__.__name__ == "Json":
                        data = getattr(i, 'data', None)
                        if isinstance(data, str):
                            import json
                            try:
                                json_data = json.loads(data)
                                if "prompt" in json_data:
                                    outline += f"[JSON卡片:{json_data.get('prompt', '')}]"
                                elif "app" in json_data:
                                    outline += f"[小程序:{json_data.get('app', '')}]"
                                else:
                                    outline += "[JSON消息]"
                            except (json.JSONDecodeError, ValueError, TypeError):
                                outline += "[JSON消息]"
                        else:
                            outline += "[JSON消息]"
                    elif component_type in ["rps", "dice", "shake"] or i.__class__.__name__ in ["RPS", "Dice", "Shake"]:
                        outline += f"[{component_type}]"
                    elif component_type == "file" or i.__class__.__name__ == "File":
                        outline += f"[文件:{getattr(i, 'name', '')}]"
                    elif component_type == "wechatemoji" or i.__class__.__name__ == "WechatEmoji":
                        outline += "[微信表情]"
                    else:
                        if component_type == "anonymous":
                            outline += "[匿名]"
                        elif component_type == "redbag":
                            outline += "[红包]"
                        elif component_type == "xml":
                            outline += "[XML消息]"
                        elif component_type == "cardimage":
                            outline += "[卡片图片]"
                        elif component_type == "tts":
                            outline += "[TTS]"
                        else:
                            val = getattr(i, "text", "")
                            if val:
                                outline += val
                            else:
                                outline += f"[{component_type}]"
                except Exception as e:
                    import traceback
                    from astrbot.api import logger
                    logger.error(f"处理消息组件时出错: {e}")
                    logger.error(f"错误详情: {traceback.format_exc()}")
                    outline += f"[处理失败的消息组件]"
                    continue
                    
        return outline
    
    def _format_interaction_participant(self, name: str, user_id: str, bot_name: str, self_id: str = "") -> str:
        safe_id = str(user_id or "").strip()
        safe_name = str(name or "").strip()
        if safe_id and self_id and safe_id == str(self_id):
            safe_name = safe_name or bot_name or "你"
        if not safe_name:
            safe_name = f"群友{safe_id[-4:]}" if safe_id else "群友"
        if safe_id and safe_id not in safe_name:
            return f"{safe_name}({safe_id})"
        return safe_name

    def _render_structured_interaction(self, event: AstrMessageEvent, bot_name: str) -> str:
        kind = str(event.get_extra("astrmai_interaction_kind", "") or "").lower()
        if kind != "poke":
            return ""

        self_id = str(event.get_self_id() or "")
        actor_label = self._format_interaction_participant(
            event.get_extra("astrmai_interaction_actor_display_name", event.get_extra("astrmai_interaction_actor_name", "")),
            event.get_extra("astrmai_interaction_actor_id", ""),
            bot_name,
            self_id=self_id,
        )
        relative_age_label = str(event.get_extra("astrmai_interaction_relative_age_label", "") or "").strip()
        target_is_bot = bool(event.get_extra("astrmai_interaction_target_is_bot", False))
        if target_is_bot:
            time_prefix = f"{relative_age_label}，" if relative_age_label else ""
            return f"[互动事件：{time_prefix}{actor_label} 对你发起了一次“戳一戳”，伸出手指轻轻碰了碰你的脸颊]"

        target_label = self._format_interaction_participant(
            event.get_extra("astrmai_interaction_target_display_name", event.get_extra("astrmai_interaction_target_name", "")),
            event.get_extra("astrmai_interaction_target_id", ""),
            bot_name,
            self_id=self_id,
        )
        time_prefix = f"{relative_age_label}，" if relative_age_label else ""
        return f"[互动事件：{time_prefix}{actor_label} 对 {target_label} 发起了一次“戳一戳”，伸出手指轻轻碰了碰对方]"

    def _convert_interaction_to_narrative(self, content: str, bot_name: str, event: AstrMessageEvent = None) -> str:
        """
        [优化版] 将上方产生的机器结构化技术标记，转换为大模型视角的自然叙述与动作描写
        """
        import re
        if not content: return ""

        structured_interaction = self._render_structured_interaction(event, bot_name) if event else ""
        if structured_interaction:
            content = structured_interaction

        # 1. 戳一戳虚拟事件翻译 (Interaction: A -> B)
        def poke_repl(match):
            s_name, t_name = match.groups()
            if bot_name and (t_name == bot_name or t_name == '我'):
                return f"[{s_name} 伸出手指戳了戳你的脸蛋]"
            return f"[{s_name} 伸出手指戳了戳 {t_name}]"
        
        content = re.sub(r"\(Interaction:\s*(.*?)\s*->\s*(.*?)\)", poke_repl, content)
            
        # 2. 图片内容翻译
        content = re.sub(r"\[图片描述:\s*(.*?)\s*\(Ref:.*?\)\]", r"[分享了一张图片，画面是：\1]", content)
        content = re.sub(r"\[图片\]", r"[发了一张图片]", content)

        # 3. 引用回复翻译 (联动上方更新的格式)
        # 将结构化的: 「↪ 引用 张三(12345) 的消息：你好呀」 翻译成 AI 更能理解的画面感动作: [指着 张三(12345) 的话回应：你好呀]
        content = re.sub(r"「↪ 引用 (.*?) 的消息：(.*?)」", r"[回复 \1 的话\2]", content)
        
        # 兼容旧版的简易匹配格式
        content = re.sub(r"\(回复\s*(.*?):.*?\)", r"[指着 \1 的话回应]", content)
        content = content.replace("(回复消息)", "[回复了对方]")
        content = content.replace("(回复", "[指着话题回应]")

        # 4. @提及翻译
        if bot_name:
            # 如果艾特了机器人本身 (带或不带QQ号)
            content = re.sub(rf"\[@{bot_name}(?:\([^)]+\))?\]", "[@你]", content)
        
        # 替换其他 @ 用户 (抹平艾特带来的割裂感，将其转换为动作描写)
        content = re.sub(r"\[@(.*?)\]", r"[@ \1]", content)
        
        # 5. 网址链接防噪过滤 (防止极长 URL 破坏 LLM 上下文注意力)
        content = re.sub(r"(https?://[^\s]+)", r"[分享了一个网页链接]", content)

        # 6. 清理多余的技术标识 (如图像缓存提取的 Ref ID) 与冗余空白字符
        content = re.sub(r"\(Ref:.*?\)", "", content)
        content = re.sub(r"\s+", " ", content).strip()
        
        return content

    async def inject_external_event(self, chat_id: str, event_data: dict):
        """
        将外部非原生事件安全地压入倒计时的 2 秒滑动窗口池中。
        """
        import time
        session = await self._get_or_create_session(chat_id)
        
        async with session.lock:
            # 🟢 [修复] 增加 message_str 等默认属性，实现 100% 鸭子类型伪装，防止底层逻辑崩溃
            class ExternalEventAdapter(dict):
                def __init__(self, data):
                    super().__init__(data)
                    # 伪装基础属性，即使漏网也不会引发 AttributeError
                    self.message_str = data.get("content", "") 
                    
                def get_extra(self, key, default=None):
                    return self.get(key, default)
            
            adapted_event = ExternalEventAdapter(event_data)
            
            if "astrmai_timestamp" not in adapted_event:
                adapted_event["astrmai_timestamp"] = time.time()
                
            session.accumulation_pool.append(adapted_event)
            session.last_active_time = time.time()

    async def _format_and_filter_messages(self, events: List[AstrMessageEvent]):
        """
        [修改] 斗图过滤与同源消息折叠，接入转义层。
        [修改] 将 event(e) 参数传递给 _normalize_content_to_str，以激活旁路拦截机制。
        """
        if not events: return "", []
        
        filtered_events = []
        continuous_img_count = 0
        
        # 1. 斗图过滤阶段
        for e in events:
            if self._is_image_only(e):
                continuous_img_count += 1
                if continuous_img_count >= 3:
                    continue # 直接过滤丢弃
            else:
                continuous_img_count = 0
            filtered_events.append(e)

        grouped_texts = []
        curr_sender = None
        curr_msgs = []
        
        # 尝试获取机器人主称呼
        bot_name = "我"
        if hasattr(self, 'config') and self.config and hasattr(self.config, 'system1'):
            if self.config.system1.nicknames:
                bot_name = self.config.system1.nicknames[0]
                
        # 2. 同源聚合与画面感转义阶段
        for e in filtered_events:
            sender = e.get_sender_name()
            if e.get_extra("astrmai_interaction_kind") == "poke":
                sender = self._format_interaction_participant(
                    e.get_extra("astrmai_interaction_actor_name", sender),
                    e.get_extra("astrmai_interaction_actor_id", e.get_sender_id()),
                    bot_name,
                    self_id=str(e.get_self_id() or ""),
                )
            
            # [核心修改] 区分虚拟事件与普通消息组件提取
            if e.get_extra("is_virtual_poke"):
                raw_content = e.message_str
            else:
                components = e.message_obj.message if (hasattr(e, "message_obj") and e.message_obj) else e.message_str
                # [核心修改] 使用 await 等待异步方法返回，并传入 event 以激活旁路拦截
                raw_content = await self._normalize_content_to_str(components, event=e)
                
            # [核心修改] 执行叙事转义
            content = self._convert_interaction_to_narrative(raw_content, bot_name, event=e)
            
            e.set_extra("astrmai_rich_text", content)
            
            # 兜底空消息
            if not content.strip():
                content = "[图片]"
            
            if sender != curr_sender:
                if curr_sender is not None:
                    grouped_texts.append(f"{curr_sender}：{'，'.join(curr_msgs)}")
                curr_sender = sender
                curr_msgs = [content]
            else:
                curr_msgs.append(content)
                
        if curr_sender is not None:
            grouped_texts.append(f"{curr_sender}：{'，'.join(curr_msgs)}")

        return "\n".join(grouped_texts), filtered_events
    
    # [修改] 位置: astrmai/Heart/attention.py -> AttentionGate 类下
    async def _debounce_and_judge(self, chat_id: str, session: SessionContext, self_id: str):
        try:
            while True:
                import time
                no_msg_start_time = time.time()
                last_pool_len = 0
                debounce_window = float(getattr(self.config.attention, 'debounce_window', 2.0))
                
                while True:
                    current_pool_len = len(session.accumulation_pool)
                    
                    if current_pool_len >= 15:
                        break
                        
                    if self._check_continuous_images(session.accumulation_pool) >= 3:
                        break

                    if current_pool_len > last_pool_len:
                        no_msg_start_time = time.time()
                        last_pool_len = current_pool_len
                        ts = session.accumulation_pool[-1].get_extra("astrmai_timestamp")
                        if ts: no_msg_start_time = ts
                    
                    if time.time() - no_msg_start_time > debounce_window:
                        break
                    import asyncio
                    await asyncio.sleep(0.3)

                async with session.lock:
                    events_to_process = list(session.accumulation_pool)
                    session.accumulation_pool.clear()
                    
                if not events_to_process:
                    break 

                total_msgs = len(events_to_process)
                
                bot_reply_msgs = [e for e in events_to_process if hasattr(e, 'get_extra') and e.get_extra("is_external_bot_reply")]
                bot_reply_count = len(bot_reply_msgs)
                
                if bot_reply_count > 0:
                    if bot_reply_count <= (total_msgs / 3):
                        from astrbot.api import logger
                        logger.info(f"[{chat_id}] ⚠️ 检测到插件插话，但处于活跃对话流中 (Bot:{bot_reply_count} / 总:{total_msgs})，判定为聊天背景音，Sys1 继续接管。")
                    else:
                        from astrbot.api import logger
                        logger.info(f"[{chat_id}] 🛑 插件响应占比过高 (Bot:{bot_reply_count} / 总:{total_msgs})，判定为纯功能交互，Sys1 隐退。")
                        return 
                        
                events_to_process = [e for e in events_to_process if not (hasattr(e, 'get_extra') and e.get_extra("is_external_bot_reply"))]
                
                if not events_to_process:
                    break
                
                from astrbot.api import logger
                logger.info(f"[{chat_id}] 🚪 [窗口关闭] 写入sys1进行评估 (共处理 {len(events_to_process)} 条有效消息)...")
                
                try:
                    logger.info(f"[{chat_id}] 🔍 [Sys1 追踪] 开始格式化与过滤消息...")
                    combined_text, final_events = await self._format_and_filter_messages(events_to_process)
                    logger.info(f"[{chat_id}] 🔍 [Sys1 追踪] 消息格式化完毕。最终文本: '{combined_text[:50]}...' (有效事件数: {len(final_events)})")
                    
                    if final_events:
                        normalized_events = self._build_normalized_events(final_events, self_id)
                        focus_event, background_events, focus_reason = self._select_focus_event(final_events, self_id)
                        main_event = focus_event or final_events[-1]
                        focus_candidate = next(
                            (candidate for candidate in normalized_events if candidate.event is main_event),
                            normalized_events[-1],
                        )
                        root_candidate, root_reason = self._resolve_thread_root(focus_candidate, normalized_events)
                        focus_thread = self._build_focus_thread(focus_candidate, root_candidate, normalized_events)
                        focus_thread.focus_reason = focus_reason
                        focus_thread.root_reason = root_reason
                        thread_core_events = [candidate for candidate in focus_thread.core_events if candidate is not main_event]
                        thread_related_events = [candidate for candidate in focus_thread.related_events if candidate is not main_event]
                        ambient_events = [candidate for candidate in focus_thread.ambient_events if candidate is not main_event]
                        ordered_sys2_events = ambient_events + thread_related_events + thread_core_events + [main_event]
                        deduped_sys2_events = []
                        for ordered_event in ordered_sys2_events:
                            if ordered_event not in deduped_sys2_events:
                                deduped_sys2_events.append(ordered_event)
                        ordered_sys2_events = deduped_sys2_events
                        focus_rich_text = main_event.get_extra("astrmai_rich_text", main_event.message_str)
                        focus_sender_name = main_event.get_sender_name() or "群友/用户"
                        focus_message_text = f"[{focus_sender_name}] 说: {focus_rich_text}"
                        focus_thread.focus_message_text = focus_message_text

                        main_event.set_extra("astrmai_focus_thread_context", focus_thread)
                        emit_legacy_focus_thread_extras(
                            main_event,
                            focus_thread,
                            window_events=final_events,
                        )
                        if getattr(getattr(self.config, "global_settings", None), "debug_mode", False):
                            logger.debug(
                                f"[{chat_id}] trace={main_event.get_extra('astrmai_trace_id', '')} "
                                f"focus_reason={focus_reason!r} "
                                f"root_reason={root_reason!r} "
                                f"focus_message_preview={preview_text(focus_rich_text, 120)!r} "
                                f"thread_core_count={len(focus_thread.core_events)} "
                                f"thread_related_count={len(focus_thread.related_events)} "
                                f"ambient_count={len(focus_thread.ambient_events)}"
                            )
                        
                        # 🚀 [全知视界编排] 汇总窗口内的所有图片真实 URL，统一塞给主事件，交由 System 2 接管
                        all_vision_urls = []
                        thread_vision_events = (
                            list(focus_thread.core_events)
                            + list(focus_thread.related_events)
                        )
                        for e in thread_vision_events:
                            urls = e.get_extra("direct_vision_urls", [])
                            if urls:
                                all_vision_urls.extend(urls)
                        
                        unique_urls = list(dict.fromkeys(all_vision_urls)) # 去重并保持顺序
                        if unique_urls:
                            main_event.set_extra("direct_vision_urls", unique_urls)
                            focus_thread.vision_bundle = VisionBundle(
                                image_urls=unique_urls[:],
                                direct_image_urls=unique_urls[:],
                                is_direct_request=bool(unique_urls),
                                is_image_only=focus_candidate.is_image_only,
                                source="focus_thread",
                            )
                        
                        is_wakeup = any(self.sensors.is_wakeup_signal(e, self_id) for e in final_events)
                        is_first_event_wakeup = self._is_direct_wakeup_event(main_event, self_id)
                        
                        sys1_persona = "保持你原本的性格特征"
                        
                        if getattr(self, 'persona_summarizer', None):
                            target_persona_id = getattr(self.config.persona, 'persona_id', "")
                            cache_key = target_persona_id.strip() if target_persona_id else f"session_{chat_id}"
                            
                            cached_data = self.persona_summarizer.cache.get(cache_key)
                            if cached_data and isinstance(cached_data, dict):
                                sys1_persona = cached_data.get("summary", "")
                            else:
                                sys1_persona = f"角色ID: {target_persona_id}" if target_persona_id else "傲娇系AI智能体"

                        logger.info(f"[{chat_id}] ⚖️ [Sys1 追踪] 移交 Judge 裁决 (强唤醒={is_wakeup}, 携带人设长度={len(sys1_persona)})...")
                        
                        # 🚀 [视觉盲区裁决] Sys1 不再接收任何 picid 或 URL，只接收干净的文本与 [图片] 占位符
                        sys1_eval_text = combined_text
                        
                        plan = await self.judge.evaluate(
                            chat_id=chat_id, 
                            message=sys1_eval_text,  
                            is_force_wakeup=is_wakeup,
                            persona_summary=sys1_persona,
                            window_events_count=len(final_events),
                            is_first_event_wakeup=is_first_event_wakeup
                        )
                        
                        logger.info(f"[{chat_id}] 📋 [Sys1 追踪] Judge 裁决结果 -> Action: {plan.action} | Thought: {plan.thought}")
                        
                        main_event.set_extra("sys1_thought", plan.thought)
                        # [Sys3新增] 透传裁决动作供 Planner 区分路由
                        main_event.set_extra("judge_action", plan.action) 

                        if plan.action in ["REPLY", "WAIT", "TOOL_CALL"]:
                            safe_thought = plan.thought or "无"
                            thought_abbr = safe_thought[:5] + "..." if len(safe_thought) > 5 else safe_thought
                            
                            retrieve_keys = plan.meta.get("retrieve_keys", [])

                            logger.info(
                                f"[{chat_id}] 🚀 [窗口结束] 快速注入sys2 | "
                                f"动作: {plan.action} | "
                                f"记忆Keys: {retrieve_keys} | "
                                f"潜意识: {thought_abbr} | "
                                f"携带消息: {len(final_events)}条"
                            )
                            if self.sys2_process:
                                logger.info(f"[{chat_id}] 🔄 [Sys1 追踪] 开始调用 sys2_process (后台异步抛出)...")
                                self._fire_background_task(self.sys2_process(main_event, ordered_sys2_events))
                                logger.info(f"[{chat_id}] ✅ [Sys1 追踪] sys2_process 已安全抛出至后台。")
                        else:
                            logger.info(f"[{chat_id}] 💤 [窗口结束] Sys1 决定静默不回复 (判定Action: {plan.action})")
                    else:
                        logger.info(f"[{chat_id}] 🈳 [Sys1 追踪] 过滤后无有效事件，放弃评估。")
                        
                except Exception as inner_e:
                    from astrbot.api import logger
                    logger.error(f"[{chat_id}] ⚠️ 批次消息处理失败，安全拦截防崩溃: {inner_e}", exc_info=True)

                async with session.lock:
                    if not session.accumulation_pool:
                        break
                    else:
                        from astrbot.api import logger
                        logger.info(f"[{chat_id}] ⚠️ [发现积压消息] 写入sys1，开启新一轮注意力维持...")

        except Exception as e:
            from astrbot.api import logger
            logger.exception(f"Attention Aggregation Critical Error: {e}")
        finally:
            async with session.lock:
                session.is_evaluating = False
                session.last_hash = None
                session.repeat_count = 0
                from astrbot.api import logger
                logger.debug(f"[{chat_id}] 🔓 注意力生命周期结束，锁已安全释放。")
