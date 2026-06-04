from __future__ import annotations

import asyncio
import collections
import re
import time
from typing import Any, Dict, List, Set

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp

from ..contracts.turn_context import ensure_turn_context
from ...infrastructure.compat.legacy_compat import emit_legacy_focus_thread_extras
from ...infrastructure.runtime.trace_runtime import debug_trace, new_trace_id, preview_text
from .decision_router import AttentionDecisionRouter
from .event_normalizer import SessionContext, build_normalized_events
from .focus_selector import score_focus_candidate, select_focus_event
from .perception import PerceptionBuilder
from .thread_builder import build_focus_thread, resolve_thread_root
from .vision_binding import extract_image_base64, extract_image_base64_from_url
from .window_buffer import AttentionWindowBuffer


class _SyntheticExternalEvent:
    def __init__(self, data: dict[str, Any]):
        self._data = dict(data or {})
        self._extra = dict(self._data.get("extra", {}) or {})
        if "is_external_bot_reply" in self._data and "is_external_bot_reply" not in self._extra:
            self._extra["is_external_bot_reply"] = bool(self._data.get("is_external_bot_reply"))
        self.message_str = str(self._data.get("message_str", self._data.get("content", "")) or "")
        self.timestamp = float(self._data.get("timestamp", time.time()) or time.time())
        self.unified_msg_origin = str(self._data.get("unified_msg_origin", "") or "")
        self.message_obj = self._data.get("message_obj")

    def get_sender_id(self):
        return str(self._data.get("sender_id", "") or "")

    def get_sender_name(self):
        return str(self._data.get("sender_name", "") or "")

    def get_group_id(self):
        return str(self._data.get("group_id", "") or "")

    def get_self_id(self):
        return str(self._data.get("self_id", "") or "")

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class AttentionGate:
    ATTENTION_WINDOW_TTL_SECONDS = 30.0
    ATTENTION_WINDOW_MAX_EVENTS = 12

    def __init__(
        self,
        state_engine,
        judge,
        sensors,
        system2_callback,
        config=None,
        visual_cortex=None,
        persona_summarizer=None,
        frequency_controller=None,
        private_chat_manager=None,
        runtime_coordinator=None,
        chat_loop_kernel=None,
    ):
        self.state_engine = state_engine
        self.judge = judge
        self.sensors = sensors
        self.sys2_process = system2_callback
        self.config = config if config else state_engine.config
        self.visual_cortex = visual_cortex
        self.persona_summarizer = persona_summarizer
        self.frequency_controller = frequency_controller
        self.private_chat_manager = private_chat_manager
        self.runtime_coordinator = runtime_coordinator
        self.chat_loop_kernel = chat_loop_kernel
        self.dialogue_store = getattr(state_engine, "dialogue_store", None)
        self.context_compaction = getattr(state_engine, "context_compaction", None)
        if self.context_compaction is None:
            logger.warning(
                "[AttentionGate] state_engine.context_compaction is None — "
                "compaction evaluation will be disabled; segments may accumulate unboundedly"
            )

        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self._global_message_cache = collections.OrderedDict()
        self.perception_builder = PerceptionBuilder(self)
        self.window_buffer = AttentionWindowBuffer(self)
        self.decision_router = AttentionDecisionRouter(self)

    async def _extract_image_base64(self, image_component):
        return await extract_image_base64(self, image_component)

    async def _extract_image_base64_from_url(self, url: str):
        return await extract_image_base64_from_url(self, url)

    def _build_normalized_events(self, events, self_id: str):
        return build_normalized_events(self, events, self_id)

    def _score_focus_candidate(self, candidate, normalized_events):
        return score_focus_candidate(self, candidate, normalized_events)

    def _select_focus_event(self, events, self_id: str, normalized_events=None):
        return select_focus_event(self, events, self_id, normalized_events=normalized_events)

    def _resolve_thread_root(self, focus_candidate, normalized_events):
        return resolve_thread_root(self, focus_candidate, normalized_events)

    def _build_focus_thread(self, focus_candidate, root_candidate, normalized_events):
        return build_focus_thread(self, focus_candidate, root_candidate, normalized_events)

    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        async with self._pool_lock:
            session = self.focus_pools.get(chat_id)
            if session is None:
                session = SessionContext()
                session.last_message_hash = ""
                session.repeat_count = 0
                session.last_active_user_time = 0.0
                session.last_window_open_ts = 0.0
                self.focus_pools[chat_id] = session
            session.last_active_time = time.time()
            return session

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
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in message:
            component_type = getattr(component, "type", component.__class__.__name__).lower()
            if component_type != "at":
                continue
            target = str(getattr(component, "qq", "") or getattr(component, "target", "") or "")
            if target == str(self_id):
                return True
        return False

    def _is_reply_to_bot_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        bot_names = [str(name).strip() for name in getattr(getattr(self.config, "system1", None), "nicknames", []) or [] if str(name).strip()]
        for component in message:
            component_type = getattr(component, "type", component.__class__.__name__).lower()
            if component_type != "reply":
                continue
            reply_sender_id = str(getattr(component, "sender_id", "") or "")
            reply_sender_name = str(
                getattr(component, "sender_nickname", "")
                or getattr(component, "sender_name", "")
                or ""
            ).strip()
            if reply_sender_id == str(self_id):
                return True
            if reply_sender_name and reply_sender_name in bot_names:
                return True
        return False

    def _resolve_wakeup_flags(self, event: AstrMessageEvent, self_id: str, msg_str: str) -> tuple[bool, bool, bool, bool]:
        is_direct = self._is_direct_wakeup_event(event, self_id)
        is_at_bot = self._is_at_bot_event(event, self_id)
        is_reply = self._is_reply_to_bot_event(event, self_id)
        normalized = str(msg_str or "").strip()
        is_name_only = bool(normalized) and normalized in {
            str(name).strip() for name in getattr(getattr(self.config, "system1", None), "nicknames", []) or [] if str(name).strip()
        }
        return is_direct, is_at_bot, is_reply, is_name_only

    @staticmethod
    def _is_near_context_query_text(message_text: str) -> bool:
        if not isinstance(message_text, str):
            return False
        normalized = message_text.strip()
        if not normalized:
            return False
        trigger_phrases = [
            "为什么",
            "哪里",
            "什么意思",
            "你刚刚",
            "刚刚说",
            "上一个",
            "上一句",
            "不是这个",
            "为啥",
            "咋",
            "什么",
            "啥",
            "啥意思",
            "不可以",
        ]
        return any(phrase in normalized for phrase in trigger_phrases)

    @staticmethod
    def _tokenize_text(text: str) -> Set[str]:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return set()
        return {token for token in re.split(r"[^\w\u4e00-\u9fff]+", normalized) if token}

    def _extract_reply_target(self, event: AstrMessageEvent) -> tuple[str, str]:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in message:
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

    def _is_image_only(self, event: AstrMessageEvent) -> bool:
        has_img = bool(event.get_extra("extracted_image_urls") or event.get_extra("direct_vision_urls"))
        has_text = bool(str(getattr(event, "message_str", "") or "").strip())
        return has_img and not has_text

    def _check_continuous_images(self, pool: List[AstrMessageEvent]) -> int:
        count = 0
        for candidate in reversed(pool):
            if self._is_image_only(candidate):
                count += 1
            else:
                break
        return count

    def _fire_background_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)
        return task

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.error(f"[Attention Task Error] {exc}", exc_info=exc)

    async def _record_event_activity(self, chat_id: str, event: AstrMessageEvent, sender_id: str) -> float:
        session = await self._get_or_create_session(chat_id)
        now = time.time()
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_timestamp", now)
            ensure_turn_context(event).perception.timestamp = now
        if sender_id and sender_id != str(getattr(self.state_engine, "bot_id", "") or ""):
            session.last_active_user_time = now
        if self.runtime_coordinator and hasattr(self.runtime_coordinator, "mark_activity"):
            try:
                await self.runtime_coordinator.mark_activity(
                    chat_id,
                    now,
                    sender_id,
                    event.get_sender_name() if hasattr(event, "get_sender_name") else "",
                    str(getattr(event, "message_str", "") or ""),
                    event.get_extra("astrmai_thread_signature", None) if hasattr(event, "get_extra") else None,
                )
            except Exception as exc:
                logger.debug(f"[AttentionGate] runtime activity mark degraded: {exc}")
        return now

    async def _record_dialogue_segment_from_event(self, chat_id: str, event: AstrMessageEvent) -> None:
        store = getattr(self, "dialogue_store", None)
        if not store:
            return
        try:
            await store.append_segment(
                chat_id,
                event_id=self._build_message_id(event),
                speaker_id=str(getattr(event, "get_sender_id", lambda: "")() or ""),
                speaker_name=str(getattr(event, "get_sender_name", lambda: "")() or ""),
                content=str(getattr(event, "message_str", "") or ""),
                role="user",
                message_kind="image" if self._is_image_only(event) else "text",
                is_bot=False,
                reply_target_sender_id=self._extract_reply_target(event)[0],
                reply_target_sender_name=self._extract_reply_target(event)[1],
                is_at_bot=self._is_at_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                is_reply_to_bot=self._is_reply_to_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                has_direct_vision=bool(event.get_extra("direct_vision_urls", []) or event.get_extra("extracted_image_urls", [])),
                is_image_only=self._is_image_only(event),
                timestamp=float(getattr(event, "timestamp", time.time()) or time.time()),
            )
        except Exception as exc:
            logger.debug(f"[AttentionGate] dialogue segment record degraded: {exc}")

    def _ensure_global_msg_cache(self):
        if not isinstance(self._global_message_cache, collections.OrderedDict):
            self._global_message_cache = collections.OrderedDict()
        return self._global_message_cache

    def _build_message_id(self, event: AstrMessageEvent):
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if message_id:
            return message_id
        sender_id = str(event.get_sender_id() or "")
        timestamp = float(getattr(event, "timestamp", 0.0) or 0.0)
        return f"{sender_id}:{timestamp}:{preview_text(str(getattr(event, 'message_str', '') or ''), 40)}"

    async def _append_dialogue_segment(self, event: AstrMessageEvent) -> None:
        store = getattr(self, "dialogue_store", None)
        if store is None:
            return
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not chat_id:
            return
        content = str(getattr(event, "message_str", "") or "").strip()
        if not content and not (event.get_extra("extracted_image_urls") or event.get_extra("direct_vision_urls")):
            return
        try:
            await store.append_segment(
                chat_id,
                event_id=self._build_message_id(event),
                speaker_id=str(event.get_sender_id() or ""),
                speaker_name=str(event.get_sender_name() or ""),
                content=content,
                role="user",
                message_kind="image" if bool(event.get_extra("extracted_image_urls") or event.get_extra("direct_vision_urls")) and not content else ("mixed" if bool(event.get_extra("extracted_image_urls") or event.get_extra("direct_vision_urls")) else "text"),
                is_bot=False,
                reply_target_sender_id=self._extract_reply_target(event)[0],
                reply_target_sender_name=self._extract_reply_target(event)[1],
                is_at_bot=self._is_at_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                is_reply_to_bot=self._is_reply_to_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                has_direct_vision=bool(event.get_extra("direct_vision_urls") or []),
                is_image_only=self._is_image_only(event),
                timestamp=float(getattr(event, "timestamp", 0.0) or time.time()),
            )
        except Exception as exc:
            logger.debug(f"[AttentionGate] dialogue segment append degraded: {exc}")

    def _compute_debounce_delay(self, session: SessionContext, is_private: bool, is_strong_wakeup: bool) -> float:
        return self.window_buffer.compute_debounce_delay(session, is_private, is_strong_wakeup)

    def _prune_attention_window(self, session: SessionContext, now: float | None = None) -> list[AstrMessageEvent]:
        return self.window_buffer.prune(session, now=now)

    def _append_attention_window(self, session: SessionContext, events: list[AstrMessageEvent], timestamp: float | None = None) -> None:
        self.window_buffer.append(session, events, timestamp=timestamp)

    def _merge_attention_window(self, session: SessionContext, batch_events: list[AstrMessageEvent]) -> list[AstrMessageEvent]:
        return self.window_buffer.merge(session, batch_events)

    def _resolve_event_context(self, event: AstrMessageEvent) -> dict[str, Any]:
        group_id = str(getattr(event, "get_group_id", lambda: "")() or "")
        chat_id = group_id or str(getattr(event, "unified_msg_origin", "") or "default")
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        msg_str = str(getattr(event, "message_str", "") or "")
        extracted_images = list(dict.fromkeys(list(event.get_extra("direct_vision_urls", []) or []) + list(event.get_extra("extracted_image_urls", []) or [])))
        is_private = not bool(group_id)
        return {
            "chat_id": chat_id,
            "self_id": self_id,
            "sender_id": sender_id,
            "msg_str": msg_str,
            "extracted_images": extracted_images,
            "is_private": is_private,
        }

    async def _engage_immediately(self, event: AstrMessageEvent, chat_id: str, retrieve_keys: list[str], *, fast_mode: bool) -> str:
        event.set_extra("retrieve_keys", list(retrieve_keys))
        event.set_extra("is_fast_mode", bool(fast_mode))
        event.set_extra("astrmai_trace_id", event.get_extra("astrmai_trace_id", new_trace_id()))
        turn_context = ensure_turn_context(event)
        turn_context.attention.retrieve_keys = list(retrieve_keys)
        turn_context.attention.is_fast_mode = bool(fast_mode)
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        await self._record_event_activity(chat_id, event, sender_id)
        session = await self._get_or_create_session(chat_id)
        async with session.lock:
            session.accumulation_pool = [event]
        if self.sys2_process:
            self._fire_background_task(self.sys2_process(event, [event]))
        return "ENGAGED"

    async def _handle_force_engage(self, event: AstrMessageEvent, chat_id: str) -> str | None:
        if event.get_extra("astrmai_force_engage", False):
            return await self._engage_immediately(event, chat_id, ["ALL"], fast_mode=False)
        return None

    async def _apply_primary_mood_update(self, event: AstrMessageEvent, chat_id: str, msg_str: str) -> None:
        if (
            not msg_str.strip()
            or bool(event.get_extra("astrmai_is_proactive_event", False))
            or bool(event.get_extra("astrmai_primary_mood_applied", False))
            or not hasattr(self.state_engine, "update_mood")
        ):
            return
        try:
            mood_tag, mood_value = await self.state_engine.update_mood(chat_id, msg_str)
            event.set_extra("astrmai_primary_mood_applied", True)
            event.set_extra("astrmai_primary_mood_tag", str(mood_tag or "neutral"))
            event.set_extra("astrmai_primary_mood_value", float(mood_value))
            event.set_extra("astrmai_primary_mood_source", "attention_ingress")
        except Exception as exc:
            logger.debug(f"[AttentionGate] primary mood update degraded: {exc}")

    def _is_simple_wakeup_payload(self, msg_str: str) -> bool:
        normalized = str(msg_str or "").strip()
        if not normalized:
            return False
        return len(normalized) <= 12 and len(self._tokenize_text(normalized)) <= 2

    async def _handle_fast_wakeup(self, event: AstrMessageEvent, chat_id: str, is_strong_wakeup: bool) -> str | None:
        if not is_strong_wakeup:
            return None
        if not self._is_simple_wakeup_payload(getattr(event, "message_str", "")):
            return None
        event.set_extra("astrmai_group_direct_wakeup", True)
        return await self._engage_immediately(event, chat_id, ["CORE_ONLY"], fast_mode=True)

    async def _passes_sensor_filters(self, event: AstrMessageEvent, msg_str: str) -> bool:
        if hasattr(self.sensors, "is_command"):
            try:
                if await self.sensors.is_command(msg_str):
                    return False
            except Exception:
                pass
        if hasattr(self.sensors, "should_process_message"):
            try:
                return bool(await self.sensors.should_process_message(event))
            except Exception:
                return True
        return True

    def _should_ignore_passive_group_image(self, is_private: bool, extracted_images: list[Any], is_strong_wakeup: bool) -> bool:
        return bool(extracted_images) and not is_private and not is_strong_wakeup

    def _should_skip_by_throttle(self, msg_str: str, extracted_images: list[Any], chat_state: Any, chat_id: str, is_private: bool, is_strong_wakeup: bool) -> str | None:
        del chat_id
        if is_private or is_strong_wakeup:
            return None
        if extracted_images and not msg_str.strip():
            return None
        should_drop = getattr(chat_state, "should_drop", False)
        return "THROTTLED" if should_drop else None

    def _handle_repeater_echo(self, event: AstrMessageEvent, session: SessionContext, is_private: bool, extracted_images: list[Any], msg_str: str) -> str | None:
        _ = event  # 参数保留用于接口一致性，方法体内仅使用 session 状态
        if is_private:
            return None
        msg_hash = f"{msg_str}|{bool(extracted_images)}"
        if getattr(session, "last_message_hash", "") == msg_hash and msg_str.strip():
            session.repeat_count = int(getattr(session, "repeat_count", 0) or 0) + 1
            if session.repeat_count >= 2:
                return "repeater_echo"
        else:
            session.last_message_hash = msg_hash
            session.repeat_count = 0
        return None

    async def _normalize_content_to_str(self, components: Any, depth: int = 0, event: AstrMessageEvent = None) -> str:
        _ = event  # 参数保留用于递归接口一致性
        if depth > 3:
            return "[content depth exceeded]"
        if components is None:
            return ""
        if isinstance(components, str):
            return components
        if isinstance(components, (list, tuple)):
            parts = [await self._normalize_content_to_str(item, depth + 1) for item in components]
            return " ".join(part for part in parts if part).strip()
        if isinstance(components, Comp.Plain):
            return str(getattr(components, "text", "") or "")
        if hasattr(components, "text"):
            return str(getattr(components, "text", "") or "")
        if hasattr(components, "sender_nickname"):
            return str(getattr(components, "sender_nickname", "") or "")
        return str(components or "")

    def _format_interaction_participant(self, name: str, user_id: str, bot_name: str, self_id: str = "") -> str:
        if self_id and str(user_id or "") == str(self_id):
            return bot_name or "Bot"
        return name or user_id or "Unknown"

    def _render_structured_interaction(self, event: AstrMessageEvent, bot_name: str) -> str:
        sender = self._format_interaction_participant(event.get_sender_name(), event.get_sender_id(), bot_name, event.get_self_id())
        return f"[{sender}] {str(getattr(event, 'message_str', '') or '').strip()}".strip()

    def _convert_interaction_to_narrative(self, content: str, bot_name: str, event: AstrMessageEvent = None) -> str:
        _ = (bot_name, event)  # 参数保留用于接口一致性
        return str(content or "").strip()

    def bind_chat_loop_kernel(self, chat_loop_kernel) -> None:
        self.chat_loop_kernel = chat_loop_kernel

    async def inject_external_event(self, chat_id: str, event_data: dict):
        event = _SyntheticExternalEvent(dict(event_data or {}, unified_msg_origin=chat_id))
        source = str(event.get_extra("astrmai_loop_source", "") or "").strip()
        if not source:
            if event.get_extra("astrmai_is_proactive_event", False):
                source = "proactive_dispatcher"
            elif event.get_extra("is_external_bot_reply", False):
                source = "external_result_bridge"
        if source:
            event.set_extra("astrmai_loop_source", source)
        if self.chat_loop_kernel is not None and hasattr(self.chat_loop_kernel, "tick"):
            tick = await self.chat_loop_kernel.tick(chat_id=chat_id, trigger="external", event=event)
            return tick.dispatch_result
        return await self.process_event(event)

    async def _format_and_filter_messages(self, events: List[AstrMessageEvent]):
        filtered = []
        for event in events:
            if not event:
                continue
            text = str(getattr(event, "message_str", "") or "").strip()
            if text or event.get_extra("extracted_image_urls") or event.get_extra("direct_vision_urls"):
                filtered.append(event)
        return filtered

    async def process_event(self, event):
        trace_id = event.get_extra("astrmai_trace_id", "") or new_trace_id()
        event.set_extra("astrmai_trace_id", trace_id)
        turn_context = ensure_turn_context(event)

        message_cache = self._ensure_global_msg_cache()
        message_id = self._build_message_id(event)
        if message_id in message_cache:
            return "DUPLICATED"
        message_cache[message_id] = time.time()
        while len(message_cache) > 256:
            message_cache.popitem(last=False)

        perception = self.perception_builder.build(event)
        turn_context.perception = perception
        context = perception.as_event_context()
        chat_id = context["chat_id"]
        self_id = context["self_id"]
        sender_id = context["sender_id"]
        msg_str = context["msg_str"]
        extracted_images = context["extracted_images"]
        is_private = context["is_private"]

        debug_trace(event, "attention_ingress", chat_id=chat_id, sender_id=sender_id, preview=preview_text(msg_str, 80))

        session = await self._get_or_create_session(chat_id)

        await self._apply_primary_mood_update(event, chat_id, msg_str)

        forced = await self._handle_force_engage(event, chat_id)
        if forced:
            return forced

        is_direct = perception.is_direct_wakeup
        is_at_bot = perception.is_at_bot
        is_reply = perception.is_reply_to_bot
        is_strong_wakeup = perception.is_strong_wakeup
        if is_direct:
            event.set_extra("astrmai_group_direct_wakeup", True)
        if is_at_bot:
            event.set_extra("astrmai_at_bot_wakeup", True)
        if is_reply:
            event.set_extra("astrmai_reply_wakeup", True)

        fast_result = await self._handle_fast_wakeup(event, chat_id, is_strong_wakeup)
        if fast_result:
            return fast_result

        if not await self._passes_sensor_filters(event, msg_str):
            return "FILTERED"

        if self._should_ignore_passive_group_image(is_private, extracted_images, is_strong_wakeup):
            return "IGNORED_IMAGE"

        if is_private and self.private_chat_manager and not is_strong_wakeup:
            try:
                await self.private_chat_manager.signal_new_message(sender_id, msg_str, chat_id=chat_id)
            except Exception as exc:
                logger.debug(f"[AttentionGate] private chat wait signal degraded: {exc}")
            return "PRIVATE_WAIT"

        chat_state = None
        if hasattr(self.state_engine, "get_state"):
            try:
                maybe_state = self.state_engine.get_state(chat_id)
                chat_state = await maybe_state if asyncio.iscoroutine(maybe_state) else maybe_state
            except Exception:
                chat_state = None

        throttle_result = self._should_skip_by_throttle(msg_str, extracted_images, chat_state, chat_id, is_private, is_strong_wakeup)
        if throttle_result:
            return throttle_result

        repeater_result = self._handle_repeater_echo(event, session, is_private, extracted_images, msg_str)
        if repeater_result:
            return repeater_result

        await self._record_event_activity(chat_id, event, sender_id)
        await self._append_dialogue_segment(event)

        async with session.lock:
            session.accumulation_pool.append(event)
            session.last_active_time = time.time()
            should_schedule = not session.is_evaluating
            session.is_evaluating = True

        if should_schedule:
            self._fire_background_task(
                self._debounce_and_judge(
                    chat_id,
                    session,
                    self_id,
                    is_private=is_private,
                    is_strong_wakeup=is_strong_wakeup,
                )
            )
        return "BUFFERED"

    @staticmethod
    def _build_judge_window_message(events: list[AstrMessageEvent]) -> str:
        return AttentionDecisionRouter.build_judge_window_message(events)

    async def _evaluate_judge_gate(
        self,
        chat_id: str,
        focus_event: AstrMessageEvent,
        focus_thread,
        events: list[AstrMessageEvent],
        *,
        is_strong_wakeup: bool,
    ) -> str:
        decision = await self.decision_router.evaluate(
            chat_id,
            focus_event,
            focus_thread,
            events,
            is_strong_wakeup=is_strong_wakeup,
        )
        return decision.action

    async def _debounce_and_judge(
        self,
        chat_id: str,
        session: SessionContext,
        self_id: str,
        *,
        is_private: bool = False,
        is_strong_wakeup: bool = False,
    ):
        try:
            await asyncio.sleep(self._compute_debounce_delay(session, is_private, is_strong_wakeup))
            async with session.lock:
                batch_events = list(session.accumulation_pool)
                session.accumulation_pool.clear()
                merged_events = self._merge_attention_window(session, batch_events)

            if not batch_events:
                return

            events = await self._format_and_filter_messages(merged_events)
            if not events:
                return

            normalized = self._build_normalized_events(events, self_id)
            focus_event, _, _ = self._select_focus_event(events, self_id, normalized_events=normalized)
            if focus_event is None:
                focus_event = events[-1]
            focus_candidate = next((candidate for candidate in normalized if candidate.event is focus_event), None)
            if focus_candidate is None:
                focus_candidate = normalized[-1]
            root_candidate, root_reason = self._resolve_thread_root(focus_candidate, normalized)
            focus_thread = self._build_focus_thread(focus_candidate, root_candidate, normalized)
            focus_thread.focus_reason = focus_thread.focus_reason or "selected_focus_event"
            focus_thread.root_reason = focus_thread.root_reason or root_reason

            emit_legacy_focus_thread_extras(focus_event, focus_thread, window_events=events)
            retrieve_keys = ["CORE_ONLY"] if focus_candidate.is_near_context_query else ["ALL"]
            focus_event.set_extra("retrieve_keys", retrieve_keys)
            focus_event.set_extra("is_fast_mode", False)
            turn_context = ensure_turn_context(focus_event)
            turn_context.attention.window_events = list(events)
            turn_context.attention.focus_thread = focus_thread
            turn_context.attention.retrieve_keys = list(retrieve_keys)
            turn_context.attention.is_fast_mode = False
            turn_context.attention.focus_reason = focus_thread.focus_reason
            turn_context.attention.root_reason = focus_thread.root_reason
            if self.context_compaction is not None:
                self._fire_background_task(
                    self.context_compaction.schedule_compaction_evaluation(
                        chat_id,
                        focus_context=focus_thread,
                        message_source="user",
                    )
                )
            should_skip_judge = bool(
                focus_candidate.is_direct_wakeup
                or focus_candidate.is_at_bot
                or focus_candidate.is_reply_to_bot
                or focus_candidate.has_direct_vision
                or is_strong_wakeup
            )
            judge_action = await self._evaluate_judge_gate(
                chat_id,
                focus_event,
                focus_thread,
                events,
                is_strong_wakeup=should_skip_judge,
            )
            focus_event.set_extra("judge_action", judge_action)
            turn_context.attention.judge_action = judge_action
            debug_trace(
                focus_event,
                "attention_focus_ready",
                chat_id=chat_id,
                focus_reason=focus_thread.focus_reason,
                root_reason=focus_thread.root_reason,
                focus_preview=preview_text(str(getattr(focus_event, "message_str", "") or ""), 80),
                judge_action=judge_action,
            )
            if judge_action == "WAIT":
                async with session.lock:
                    self._append_attention_window(session, batch_events)
                return
            if judge_action == "IGNORE":
                async with session.lock:
                    self._append_attention_window(session, [focus_event])
                return
            async with session.lock:
                self._append_attention_window(session, batch_events)
            if self.sys2_process:
                await self.sys2_process(focus_event, focus_thread.all_thread_events())
        finally:
            reschedule_pending = False
            async with session.lock:
                if session.accumulation_pool:
                    reschedule_pending = True
                else:
                    session.is_evaluating = False
            if reschedule_pending:
                self._fire_background_task(
                    self._debounce_and_judge(
                        chat_id,
                        session,
                        self_id,
                        is_private=is_private,
                        is_strong_wakeup=False,
                    )
                )


__all__ = ["AttentionGate"]
