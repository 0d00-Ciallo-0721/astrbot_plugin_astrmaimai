from __future__ import annotations

import asyncio
import collections
import hashlib
import inspect
import re
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Set

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp

from ..contracts.turn_context import ensure_turn_context
from ..contracts.turn_identity import TurnIdentity, build_p0_thread_id
from ..threading.group_thread_resolver import resolve_group_thread
from ...infrastructure.compat.legacy_compat import emit_legacy_focus_thread_extras
from ...infrastructure.runtime.trace_runtime import debug_trace, new_trace_id, preview_text
from .decision_router import AttentionDecisionRouter
from .event_normalizer import SessionContext, build_normalized_events
from .focus_selector import score_focus_candidate, select_focus_event
from .perception import PerceptionBuilder
from .thread_builder import build_focus_thread, resolve_thread_root
from .vision_binding import extract_image_base64, extract_image_base64_from_url
from .window_buffer import AttentionWindowBuffer


class _SyntheticExternalEvent(AstrMessageEvent):
    def __init__(self, data: dict[str, Any]):
        self._data = dict(data or {})
        self._extra = dict(self._data.get("extra", {}) or {})
        if "is_external_bot_reply" in self._data and "is_external_bot_reply" not in self._extra:
            self._extra["is_external_bot_reply"] = bool(self._data.get("is_external_bot_reply"))
        self.message_str = str(self._data.get("message_str", self._data.get("content", "")) or "")
        self.timestamp = float(self._data.get("timestamp", time.time()) or time.time())
        self.unified_msg_origin = str(self._data.get("unified_msg_origin", "") or "")
        self.message_obj = self._data.get("message_obj")  # reserved for future use

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
    ATTENTION_WINDOW_TTL_SECONDS = 180.0
    ATTENTION_WINDOW_MAX_EVENTS = 12
    BACKGROUND_TASK_MAX_CONCURRENCY = 8
    MESSAGE_DEDUP_FALLBACK_TTL_SECONDS = 2.0

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
        private_turn_coordinator=None,
        runtime_coordinator=None,
        chat_loop_kernel=None,
        conversation_continuity=None,
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
        self.private_turn_coordinator = private_turn_coordinator
        self.runtime_coordinator = runtime_coordinator
        self.chat_loop_kernel = chat_loop_kernel
        self.conversation_continuity = conversation_continuity
        self._proactive_injection_lock: dict[str, asyncio.Lock] = {}
        self._proactive_dispatching: dict[str, bool] = {}
        self._deferred_messages: dict[str, list] = {}  # ponytail: R12 — queue blocked messages
        self.dialogue_store = getattr(state_engine, "dialogue_store", None)
        self.context_compaction = getattr(state_engine, "context_compaction", None)
        if self.context_compaction is None:
            logger.info(
                "[AttentionGate] state_engine.context_compaction is None — "
                "compaction evaluation will be disabled; segments may accumulate unboundedly"
            )

        self.focus_pools: Dict[str, SessionContext] = {}
        self._pool_lock = asyncio.Lock()
        # ponytail: guard against unbounded focus_pools growth
        self._last_focus_pool_prune: float = 0.0
        self._background_tasks: set[asyncio.Task] = set()
        self._session_tasks: set[asyncio.Task] = set()
        self._background_task_semaphore = asyncio.Semaphore(self.BACKGROUND_TASK_MAX_CONCURRENCY)
        # Platform IDs persist until FIFO eviction; content fallbacks use a short TTL.
        self._global_message_cache = collections.OrderedDict()
        self.perception_builder = PerceptionBuilder(self)
        self.window_buffer = AttentionWindowBuffer(self)
        self.decision_router = AttentionDecisionRouter(self)

    def refresh_config(self, config):
        self.config = config
        if self.private_turn_coordinator is not None:
            self.private_turn_coordinator.refresh_config(config)
        if self.conversation_continuity is not None:
            refresh_continuity = getattr(self.conversation_continuity, "refresh_config", None)
            if callable(refresh_continuity):
                refresh_continuity(config)

    def get_proactive_lock(self, chat_id: str) -> asyncio.Lock:
        """Return a per-chat lock for proactive injection serialization."""
        if chat_id not in self._proactive_injection_lock:
            self._proactive_injection_lock[chat_id] = asyncio.Lock()
        return self._proactive_injection_lock[chat_id]

    async def drain_deferred_messages(self, chat_id: str, limit: int = 5):
        """Replay messages queued during proactive dispatching."""
        queue = self._deferred_messages.pop(chat_id, None)
        if not queue:
            return
        drained = 0
        for event in queue[:limit]:
            try:
                await self.process_event(event)
                drained += 1
            except Exception:
                logger.warning(
                    f"[AttentionGate] drain deferred message failed for {chat_id}",
                    exc_info=True,
                )
        if drained:
            logger.info(f"[AttentionGate] drained {drained} deferred messages for {chat_id}")

    async def clear_chat_state(self, chat_id: str) -> bool:
        removed = False
        removed_session = None
        async with self._pool_lock:
            removed_session = self.focus_pools.pop(chat_id, None)
            removed = removed_session is not None or removed
            removed = self._proactive_dispatching.pop(chat_id, None) is not None or removed
            removed = self._deferred_messages.pop(chat_id, None) is not None or removed
            removed = self._proactive_injection_lock.pop(chat_id, None) is not None or removed
        removed = bool(
            await self._cancel_session_workers(chat_id, session=removed_session)
        ) or removed
        clear_pending = getattr(self.private_turn_coordinator, "clear_pending_batch", None)
        if callable(clear_pending):
            removed = bool(clear_pending(chat_id)) or removed
        clear_continuity = getattr(self.conversation_continuity, "clear", None)
        if callable(clear_continuity):
            had_continuity = bool(
                chat_id in set(self.conversation_continuity.chats())
                if hasattr(self.conversation_continuity, "chats")
                else False
            )
            clear_continuity(chat_id)
            removed = had_continuity or removed
        compaction = getattr(self, "context_compaction", None)
        if compaction is not None and hasattr(compaction, "clear_chat_state"):
            try:
                removed = bool(compaction.clear_chat_state(chat_id)) or removed
            except Exception as exc:
                logger.debug(f"[AttentionGate] context compaction clear degraded for {chat_id}: {exc}")
        return removed

    async def _cancel_session_workers(
        self,
        chat_id: str | None = None,
        *,
        session: SessionContext | None = None,
    ) -> int:
        tasks = []
        sessions = []
        for task in list(self._session_tasks):
            worker_context = getattr(task, "_worker_context", None)
            worker_chat_id = str(getattr(worker_context, "chat_id", "") or "")
            if chat_id is not None and worker_chat_id != str(chat_id):
                continue
            worker_session = getattr(worker_context, "session", None)
            if session is not None and worker_session is not session:
                continue
            tasks.append(task)
            if worker_session is not None and all(existing is not worker_session for existing in sessions):
                sessions.append(worker_session)

        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in sessions:
            async with session.lock:
                session.is_evaluating = False
                if chat_id is not None:
                    session.accumulation_pool.clear()
        return len(tasks)

    async def _extract_image_base64(self, image_component):
        return await extract_image_base64(self, image_component)

    async def _extract_image_base64_from_url(self, url: str):
        return await extract_image_base64_from_url(self, url)

    def _build_normalized_events(self, events, self_id: str):
        return build_normalized_events(self, events, self_id)

    def _score_focus_candidate(self, candidate, normalized_events):
        return score_focus_candidate(self, candidate, normalized_events)

    def _select_focus_event(self, events, self_id: str, normalized_events=None, *, is_private: bool = False):
        return select_focus_event(
            self,
            events,
            self_id,
            normalized_events=normalized_events,
            is_private=is_private,
        )

    def _resolve_thread_root(self, focus_candidate, normalized_events):
        return resolve_thread_root(self, focus_candidate, normalized_events)

    def _build_focus_thread(self, focus_candidate, root_candidate, normalized_events):
        return build_focus_thread(self, focus_candidate, root_candidate, normalized_events)

    async def _get_or_create_session(self, chat_id: str) -> SessionContext:
        # ponytail: prune stale focus pools every 300s
        now = time.time()
        if now - self._last_focus_pool_prune > 300:
            self._prune_stale_focus_pools()
            self._last_focus_pool_prune = now

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

    # ponytail: remove focus_pools entries idle > 24h to prevent unbounded growth
    def _prune_stale_focus_pools(self, max_age: float = 86400.0):
        now = time.time()
        stale = [cid for cid, s in self.focus_pools.items() if now - float(s.last_active_time) > max_age]
        for cid in stale:
            self.focus_pools.pop(cid, None)
            self._proactive_injection_lock.pop(cid, None)

    def _is_direct_wakeup_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        if not event:
            return False
        if event.get_extra("astrmai_group_direct_wakeup", False):
            return True
        if event.get_extra("astrmai_bonus_score", 0.0) >= 1.0:
            return True
        if self.sensors is None:
            return False
        try:
            return bool(self.sensors.is_wakeup_signal(event, self_id))
        except Exception:
            logger.warning("[AttentionGate] is_direct_wakeup_event failed", exc_info=True)
            return False

    def _is_at_bot_event(self, event: AstrMessageEvent, self_id: str) -> bool:
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in message:
            component_type = str(getattr(component, "type", component.__class__.__name__)).lstrip("_").lower()
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
            component_type = str(getattr(component, "type", component.__class__.__name__)).lstrip("_").lower()
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
        has_img = bool(
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls"))
            or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls"))
        )
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

    async def _run_managed_system2_task(self, coro, event):
        task = asyncio.current_task()
        turn = event.get_extra("astrmai_turn_identity", None) if hasattr(event, "get_extra") else None
        coordinator = self.runtime_coordinator
        registered = True
        if coordinator is not None and hasattr(coordinator, "register_turn_task"):
            registered = await coordinator.register_turn_task(turn, task)
        if not registered:
            if hasattr(coro, "close"):
                coro.close()
            return None
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._handle_system2_failure(event, exc)
            return None
        finally:
            if coordinator is not None and hasattr(coordinator, "unregister_turn_task"):
                await coordinator.unregister_turn_task(turn, task)

    async def _run_background_task(self, coro, event=None):
        async with self._background_task_semaphore:
            if event is not None:
                return await self._run_managed_system2_task(coro, event)
            return await coro

    def _fire_background_task(self, coro, event=None):
        task = asyncio.create_task(self._run_background_task(coro, event))
        task._astrmai_inner_coro = coro
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)
        return task

    def _fire_priority_task(self, coro, event=None):
        managed_coro = self._run_managed_system2_task(coro, event) if event is not None else coro
        task = asyncio.create_task(managed_coro)
        task._astrmai_inner_coro = coro
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)
        return task

    def _handle_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        if task.cancelled():
            inner_coro = getattr(task, "_astrmai_inner_coro", None)
            if hasattr(inner_coro, "close"):
                inner_coro.close()
            return
        try:
            task.result()
        except Exception as exc:
            logger.error(f"[Attention Task Error] {exc}", exc_info=exc)

    def _spawn_session_worker(
        self,
        chat_id: str,
        session: SessionContext,
        self_id: str,
        *,
        is_private: bool = False,
        is_strong_wakeup: bool = False,
    ):
        task = asyncio.create_task(
            self._debounce_and_judge(
                chat_id,
                session,
                self_id,
                is_private=is_private,
                is_strong_wakeup=is_strong_wakeup,
            )
        )
        task._worker_context = SimpleNamespace(chat_id=chat_id, session=session, self_id=self_id)
        self._session_tasks.add(task)
        task.add_done_callback(self._handle_session_worker_result)
        return task

    def _handle_session_worker_result(self, task: asyncio.Task):
        self._session_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.error(f"[Attention Worker Error] {exc}", exc_info=exc)
            worker_context = getattr(task, "_worker_context", None)
            if worker_context is not None:
                recovery = asyncio.create_task(self._recover_failed_session_worker(worker_context))
                self._background_tasks.add(recovery)
                recovery.add_done_callback(self._handle_background_task_result)

    async def _recover_failed_session_worker(self, worker_context):
        chat_id = str(getattr(worker_context, "chat_id", "") or "")
        session = getattr(worker_context, "session", None)
        self_id = str(getattr(worker_context, "self_id", "") or "")
        if not chat_id or session is None:
            return
        async with session.lock:
            has_pending = bool(session.accumulation_pool)
            session.is_evaluating = False
        if has_pending:
            self._spawn_session_worker(chat_id, session, self_id)

    def _handle_background_task_result(self, task: asyncio.Task):
        self._background_tasks.discard(task)
        if task.cancelled():
            inner_coro = getattr(task, "_astrmai_inner_coro", None)
            if hasattr(inner_coro, "close"):
                inner_coro.close()
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"[AttentionGate] background task failed: {exc}", exc_info=(type(exc), exc, exc.__traceback__))

    def _schedule_compaction_task(self, chat_id: str, focus_context) -> asyncio.Task | None:
        if self.context_compaction is None:
            return None
        return self._fire_background_task(
            self.context_compaction.schedule_compaction_evaluation(
                chat_id,
                focus_context=focus_context,
                message_source="user",
            )
        )

    async def _record_event_activity(self, chat_id: str, event: AstrMessageEvent, sender_id: str) -> float:
        session = await self._get_or_create_session(chat_id)
        now = time.time()
        is_anonymous_sender = str(sender_id or "").startswith("80000000")
        activity_sender_id = "" if is_anonymous_sender else sender_id
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_timestamp", now)
            ensure_turn_context(event).perception.timestamp = now
        if activity_sender_id and activity_sender_id != str(getattr(self.state_engine, "bot_id", "") or ""):
            session.last_active_user_time = now
        if self.runtime_coordinator and hasattr(self.runtime_coordinator, "mark_activity"):
            try:
                await self.runtime_coordinator.mark_activity(
                    chat_id,
                    now,
                    activity_sender_id,
                    event.get_sender_name() if hasattr(event, "get_sender_name") else "",
                    str(getattr(event, "message_str", "") or ""),
                    event.get_extra("astrmai_thread_signature", None) if hasattr(event, "get_extra") else None,
                )
            except Exception as exc:
                logger.debug(f"[AttentionGate] runtime activity mark degraded: {exc}")
        return now

    async def _ensure_turn_identity(self, event: AstrMessageEvent, chat_id: str, is_private: bool) -> None:
        if event.get_extra("astrmai_turn_identity", None) is not None:
            return
        conversation_config = getattr(self.config, "conversation", None)
        if not bool(getattr(conversation_config, "conversation_generation_enabled", True)):
            return
        coordinator = self.runtime_coordinator
        if coordinator is None or not hasattr(coordinator, "advance_generation"):
            return
        mode = "private" if is_private else "group"
        if not is_private and bool(getattr(conversation_config, "group_thread_wait_enabled", True)):
            resolution = resolve_group_thread(event, chat_id)
            thread_id = resolution.thread_id
            event.set_extra("astrmai_group_thread_source", resolution.source)
            event.set_extra("astrmai_group_thread_confidence", resolution.confidence)
        else:
            thread_id = build_p0_thread_id(mode, chat_id)
        generation = await coordinator.advance_generation(chat_id, thread_id)
        created_at = time.time()
        turn = TurnIdentity(
            mode=mode,
            chat_id=chat_id,
            thread_id=thread_id,
            generation=generation,
            sender_id=str(event.get_sender_id() or ""),
            input_message_ids=tuple(item for item in (self._build_message_id(event),) if item),
            created_at=created_at,
        )
        event.set_extra("astrmai_turn_identity", turn)
        event.set_extra("astrmai_turn_mode", mode)
        event.set_extra("astrmai_turn_thread_id", thread_id)
        event.set_extra("astrmai_turn_generation", generation)
        event.set_extra("astrmai_turn_created_at", created_at)

    def _bind_private_batch_turn(self, focus_event, batch_events: list[AstrMessageEvent], chat_id: str) -> None:
        if not batch_events:
            return

        focus_event.set_extra("is_private_chat", True)
        focus_event.set_extra("astrmai_turn_mode", "private")
        latest_timestamp = max(
            float(event.get_extra("astrmai_timestamp", getattr(event, "timestamp", 0.0)) or 0.0)
            for event in batch_events
        )
        previous_timestamp = float(
            focus_event.get_extra("astrmai_timestamp", getattr(focus_event, "timestamp", 0.0)) or 0.0
        )
        if previous_timestamp != latest_timestamp:
            focus_event.set_extra("astrmai_focus_original_timestamp", previous_timestamp)
        focus_event.set_extra("astrmai_timestamp", latest_timestamp)
        focus_event.set_extra("astrmai_private_batch_latest_ts", latest_timestamp)

        turns = [
            event.get_extra("astrmai_turn_identity", None)
            for event in batch_events
            if event.get_extra("astrmai_turn_identity", None) is not None
        ]
        if not turns:
            return
        latest_turn = max(
            turns,
            key=lambda turn: (int(getattr(turn, "generation", 0) or 0), float(getattr(turn, "created_at", 0.0) or 0.0)),
        )
        message_ids: list[str] = []
        for event in batch_events:
            turn = event.get_extra("astrmai_turn_identity", None)
            turn_ids = tuple(getattr(turn, "input_message_ids", ()) or ())
            candidates = turn_ids or (self._build_message_id(event),)
            for message_id in candidates:
                normalized_id = str(message_id or "").strip()
                if normalized_id and normalized_id not in message_ids:
                    message_ids.append(normalized_id)

        merged_turn = TurnIdentity(
            mode="private",
            chat_id=str(getattr(latest_turn, "chat_id", "") or chat_id),
            thread_id=str(
                getattr(latest_turn, "thread_id", "")
                or build_p0_thread_id("private", chat_id)
            ),
            generation=int(getattr(latest_turn, "generation", 0) or 0),
            sender_id=str(focus_event.get_sender_id() or getattr(latest_turn, "sender_id", "") or ""),
            input_message_ids=tuple(message_ids),
            created_at=float(getattr(latest_turn, "created_at", 0.0) or latest_timestamp),
        )
        focus_event.set_extra("astrmai_turn_identity", merged_turn)
        focus_event.set_extra("astrmai_turn_thread_id", merged_turn.thread_id)
        focus_event.set_extra("astrmai_turn_generation", merged_turn.generation)
        focus_event.set_extra("astrmai_turn_created_at", merged_turn.created_at)
        burst_basis = f"{chat_id}|{merged_turn.generation}|{'|'.join(message_ids)}"
        focus_event.set_extra(
            "astrmai_private_burst_id",
            hashlib.sha256(burst_basis.encode("utf-8", errors="ignore")).hexdigest()[:16],
        )

    async def _handle_system2_failure(self, event: AstrMessageEvent, exc: Exception) -> None:
        logger.error(f"[Attention Task Error] {exc}", exc_info=exc)
        coordinator = self.runtime_coordinator
        turn = event.get_extra("astrmai_turn_identity", None)
        if coordinator is not None and hasattr(coordinator, "is_current_turn"):
            if not await coordinator.is_current_turn(turn):
                return
        if event.get_extra("astrmai_system2_failure_handled", False):
            return
        event.set_extra("astrmai_system2_failure_handled", True)
        reply_sent = bool(event.get_extra("astrmai_reply_sent", False))
        if not reply_sent and hasattr(event, "send") and hasattr(event, "plain_result"):
            fallback_text = str(
                getattr(getattr(self.config, "reply", None), "fallback_text", "")
                or "（陷入了短暂的沉默...）"
            )
            try:
                await event.send(event.plain_result(fallback_text))
                event.set_extra("astrmai_reply_sent", True)
                reply_sent = True
            except Exception as send_exc:
                logger.error(f"[AttentionGate] failed to send System2 fallback: {send_exc}")
        callback = event.get_extra("astrmai_proactive_completion_callback", None)
        if callback is not None and not event.get_extra("astrmai_proactive_completed", False):
            event.set_extra("astrmai_proactive_completed", True)
            try:
                result = callback(reply_sent, "")
                if inspect.isawaitable(result):
                    await result
            except Exception as callback_exc:
                logger.error(f"[AttentionGate] proactive completion callback failed: {callback_exc}")

    async def _record_dialogue_segment_from_event(self, chat_id: str, event: AstrMessageEvent) -> None:
        store = getattr(self, "dialogue_store", None)
        if not store:
            return
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if sender_id.startswith("80000000"):
            return
        try:
            await store.append_segment(
                chat_id,
                event_id=self._build_message_id(event),
                speaker_id=sender_id,
                speaker_name=str(getattr(event, "get_sender_name", lambda: "")() or ""),
                content=str(getattr(event, "message_str", "") or ""),
                role="user",
                message_kind="image" if self._is_image_only(event) else "text",
                is_bot=False,
                reply_target_sender_id=self._extract_reply_target(event)[0],
                reply_target_sender_name=self._extract_reply_target(event)[1],
                is_at_bot=self._is_at_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                is_reply_to_bot=self._is_reply_to_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                has_direct_vision=bool(
                    event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))
                    or event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", []))
                ),
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

    def _build_message_dedup_key(self, event: AstrMessageEvent) -> tuple[str, bool]:
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        message_id = str(getattr(getattr(event, "message_obj", None), "message_id", "") or "")
        if message_id:
            return f"message:{chat_id}:{message_id}", False
        sender_id = str(event.get_sender_id() or "")
        content = str(getattr(event, "message_str", "") or "")
        image_refs = (
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", []))
            or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))
            or []
        )
        content = f"{content}\n{repr(image_refs)}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
        return f"fallback:{chat_id}:{sender_id}:{digest}", True

    def _claim_message(self, event: AstrMessageEvent, *, now: float | None = None) -> bool:
        message_cache = self._ensure_global_msg_cache()
        message_key, is_fallback = self._build_message_dedup_key(event)
        current_time = time.time() if now is None else float(now)
        previous_time = message_cache.get(message_key)
        if previous_time is not None:
            if not is_fallback or current_time - float(previous_time) <= self.MESSAGE_DEDUP_FALLBACK_TTL_SECONDS:
                return False
            message_cache.pop(message_key, None)
        message_cache[message_key] = current_time
        while len(message_cache) > 256:
            message_cache.popitem(last=False)
        return True

    async def _append_dialogue_segment(self, event: AstrMessageEvent) -> None:
        store = getattr(self, "dialogue_store", None)
        if store is None:
            return
        chat_id = str(getattr(event, "unified_msg_origin", "") or "")
        if not chat_id:
            return
        sender_id = str(event.get_sender_id() or "")
        if sender_id.startswith("80000000"):
            return
        content = str(getattr(event, "message_str", "") or "").strip()
        if not content and not (
            event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls"))
            or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls"))
        ):
            return
        try:
            await store.append_segment(
                chat_id,
                event_id=self._build_message_id(event),
                speaker_id=sender_id,
                speaker_name=str(event.get_sender_name() or ""),
                content=content,
                role="user",
                message_kind="image" if bool(event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls")) or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls"))) and not content else ("mixed" if bool(event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls")) or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls"))) else "text"),
                is_bot=False,
                reply_target_sender_id=self._extract_reply_target(event)[0],
                reply_target_sender_name=self._extract_reply_target(event)[1],
                is_at_bot=self._is_at_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                is_reply_to_bot=self._is_reply_to_bot_event(event, str(getattr(self.state_engine, "bot_id", "") or "")),
                has_direct_vision=bool(event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls")) or []),
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
        chat_id = str(getattr(event, "unified_msg_origin", "") or group_id or "default")
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        msg_str = str(getattr(event, "message_str", "") or "")
        extracted_images = list(
            dict.fromkeys(
                list(event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", [])) or [])
                + list(event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", [])) or [])
            )
        )
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
        if self.sys2_process:
            self._fire_priority_task(self.sys2_process(event, [event]), event)
        return "ENGAGED"

    async def _handle_force_engage(self, event: AstrMessageEvent, chat_id: str) -> str | None:
        if event.get_extra("astrmai_force_engage", False):
            return await self._engage_immediately(event, chat_id, ["ALL"], fast_mode=False)
        return None

    async def _complete_proactive_candidate(self, event: AstrMessageEvent, *, reason: str) -> None:
        if not bool(event.get_extra("astrmai_is_proactive_event", False)):
            return
        if bool(event.get_extra("astrmai_proactive_completed", False)):
            return
        event.set_extra("astrmai_proactive_completed", True)
        decision = event.get_extra("astrmai_proactive_dispatch_decision", None)
        if decision is not None:
            if isinstance(decision, dict):
                decision["reply_sent"] = False
                decision["reply_preview"] = ""
                decision["status"] = "skipped"
                decision["blocked_reason"] = str(reason or "proactive_candidate_skipped")
            else:
                setattr(decision, "reply_sent", False)
                setattr(decision, "reply_preview", "")
                setattr(decision, "status", "skipped")
                setattr(decision, "blocked_reason", str(reason or "proactive_candidate_skipped"))
        callback = event.get_extra("astrmai_proactive_completion_callback", None)
        if callable(callback):
            try:
                result = callback(False, "")
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.debug(f"[AttentionGate] proactive completion callback degraded: {exc}")

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
                try:
                    logger.exception(f"[AttentionGate] sensor is_command check failed on msg={msg_str[:100]!r}")
                except AttributeError:
                    logger.error(f"[AttentionGate] sensor is_command check failed on msg={msg_str[:100]!r}")
        if hasattr(self.sensors, "should_process_message"):
            try:
                return bool(await self.sensors.should_process_message(event))
            except Exception:
                try:
                    logger.exception("[AttentionGate] sensor should_process_message check failed, defaulting to pass")
                except AttributeError:
                    logger.error("[AttentionGate] sensor should_process_message check failed, defaulting to pass")
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
        should_drop = bool(getattr(chat_state, "should_drop", False))
        return "THROTTLED" if should_drop else None

    def _handle_repeater_echo(self, session: SessionContext, is_private: bool, extracted_images: list[Any], msg_str: str) -> str | None:
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
            if text or event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls")) or event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls")):
                filtered.append(event)
        return filtered

    async def process_event(self, event):
        trace_id = event.get_extra("astrmai_trace_id", "") or new_trace_id()
        event.set_extra("astrmai_trace_id", trace_id)
        turn_context = ensure_turn_context(event)

        _chat_id = getattr(event, "unified_msg_origin", "") or ""
        if self._proactive_dispatching.get(_chat_id, False) and not event.get_extra("astrmai_is_proactive_event", False):
            # ponytail: R12 — queue deferred message instead of silently dropping
            queue = self._deferred_messages.setdefault(_chat_id, [])
            if len(queue) < 5:
                queue.append(event)
            logger.warning(
                f"[AttentionGate] proactive dispatching in progress; queued message for {_chat_id} (queue={len(queue)})"
            )
            return "PROACTIVE_BLOCKED"

        if not self._claim_message(event):
            return "DUPLICATED"

        private_ingress = not bool(event.get_group_id())
        sensor_checked = False
        if private_ingress:
            if not await self._passes_sensor_filters(event, str(getattr(event, "message_str", "") or "")):
                return "FILTERED"
            sensor_checked = True

        perception = self.perception_builder.build(event)
        context = perception.as_event_context()
        chat_id = context["chat_id"]
        self_id = context["self_id"]
        sender_id = context["sender_id"]
        msg_str = context["msg_str"]
        extracted_images = context["extracted_images"]
        is_private = context["is_private"]

        event.set_extra("is_private_chat", bool(is_private))
        event.set_extra("astrmai_turn_mode", "private" if is_private else "group")

        if is_private and self.private_turn_coordinator is not None:
            note_new_message = getattr(self.private_turn_coordinator, "note_new_message", None)
            if callable(note_new_message):
                note_new_message(chat_id)
        await self._ensure_turn_identity(event, chat_id, is_private)

        debug_trace(event, "attention_ingress", chat_id=chat_id, sender_id=sender_id, preview=preview_text(msg_str, 80))

        session = await self._get_or_create_session(chat_id)

        defer_private_context = bool(is_private and self.private_turn_coordinator is not None)
        if not defer_private_context:
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

        if not is_private and is_strong_wakeup:
            prepare_direct_event = getattr(self.private_turn_coordinator, "prepare_direct_event", None)
            if callable(prepare_direct_event):
                try:
                    await prepare_direct_event(event, chat_id)
                except Exception as exc:
                    logger.warning(
                        f"[AttentionGate] direct group vision barrier degraded for {chat_id}: {exc}",
                        exc_info=True,
                    )
            perception = self.perception_builder.build(event)
            context = perception.as_event_context()
            msg_str = context["msg_str"]
            extracted_images = context["extracted_images"]
            is_direct = perception.is_direct_wakeup
            is_at_bot = perception.is_at_bot
            is_reply = perception.is_reply_to_bot
            is_strong_wakeup = perception.is_strong_wakeup

        fast_result = None if is_private else await self._handle_fast_wakeup(event, chat_id, is_strong_wakeup)
        if fast_result:
            return fast_result

        if not sensor_checked and not await self._passes_sensor_filters(event, msg_str):
            await self._complete_proactive_candidate(event, reason="sensor_filtered")
            return "FILTERED"

        if self._should_ignore_passive_group_image(is_private, extracted_images, is_strong_wakeup):
            await self._complete_proactive_candidate(event, reason="passive_group_image")
            return "IGNORED_IMAGE"

        if is_private and self.private_chat_manager and not is_strong_wakeup:
            try:
                wait_signaled = await self.private_chat_manager.signal_new_message(
                    sender_id,
                    msg_str,
                    chat_id=chat_id,
                )
                if wait_signaled:
                    logger.debug(f"[AttentionGate] private wait resumed without consuming message for {chat_id}")
            except Exception as exc:
                logger.warning(
                    f"[AttentionGate] private chat wait signal failed for {chat_id}, "
                    f"falling back to normal attention: {exc}"
                )

        chat_state = None
        if hasattr(self.state_engine, "get_state"):
            try:
                maybe_state = self.state_engine.get_state(chat_id)
                chat_state = await maybe_state if asyncio.iscoroutine(maybe_state) else maybe_state
            except Exception:
                logger.warning("[AttentionGate] get_state failed", exc_info=True)
                chat_state = None

        throttle_result = self._should_skip_by_throttle(msg_str, extracted_images, chat_state, chat_id, is_private, is_strong_wakeup)
        if throttle_result:
            await self._complete_proactive_candidate(event, reason=str(throttle_result or "throttled").lower())
            return throttle_result

        repeater_result = self._handle_repeater_echo(session, is_private, extracted_images, msg_str)
        if repeater_result:
            await self._complete_proactive_candidate(event, reason=repeater_result)
            return repeater_result

        is_proactive_event = bool(event.get_extra("astrmai_is_proactive_event", False))
        if is_proactive_event:
            now = time.time()
            event.set_extra("astrmai_timestamp", now)
            ensure_turn_context(event).perception.timestamp = now
        else:
            await self._record_event_activity(chat_id, event, sender_id)
        if not defer_private_context and not is_proactive_event:
            await self._append_dialogue_segment(event)

        async with session.lock:
            session.accumulation_pool.append(event)
            session.last_active_time = time.time()
            should_schedule = not session.is_evaluating
            session.is_evaluating = True

        if should_schedule:
            self._spawn_session_worker(
                chat_id,
                session,
                self_id,
                is_private=is_private,
                is_strong_wakeup=is_strong_wakeup,
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

    async def _send_private_topic_confirmation(
        self,
        event: AstrMessageEvent,
        confirmation_text: str,
    ) -> bool:
        if not confirmation_text or not hasattr(event, "send") or not hasattr(event, "plain_result"):
            return False
        try:
            await event.send(event.plain_result(confirmation_text))
            event.set_extra("astrmai_reply_sent", True)
            event.set_extra("astrmai_topic_confirmation_sent", True)
            return True
        except Exception as exc:
            logger.warning(f"[AttentionGate] private topic confirmation send failed: {exc}")
            return False

    async def _debounce_and_judge(
        self,
        chat_id: str,
        session: SessionContext,
        self_id: str,
        *,
        is_private: bool = False,
        is_strong_wakeup: bool = False,
    ):
        current_is_strong_wakeup = is_strong_wakeup
        while True:
            if is_private and self.private_turn_coordinator is not None:
                await self.private_turn_coordinator.wait_for_input_stability(session)
            else:
                await asyncio.sleep(self._compute_debounce_delay(session, is_private, current_is_strong_wakeup))
            async with session.lock:
                batch_events = list(session.accumulation_pool)
                session.accumulation_pool.clear()

            if batch_events:
                if is_private and self.private_turn_coordinator is not None:
                    merge_pending_batch = getattr(
                        self.private_turn_coordinator,
                        "merge_pending_batch",
                        None,
                    )
                    if callable(merge_pending_batch):
                        batch_events = merge_pending_batch(chat_id, batch_events)
                    await self.private_turn_coordinator.prepare_batch(batch_events, chat_id)
                    async with session.lock:
                        if session.accumulation_pool:
                            session.accumulation_pool = batch_events + list(session.accumulation_pool)
                            current_is_strong_wakeup = False
                            continue
                    mood_event = None
                    mood_texts: list[str] = []
                    for prepared_event in batch_events:
                        self.perception_builder.build(prepared_event)
                        if not bool(prepared_event.get_extra("astrmai_private_context_recorded", False)):
                            rich_text = str(
                                prepared_event.get_extra(
                                    "astrmai_rich_text",
                                    getattr(prepared_event, "message_str", ""),
                                )
                                or ""
                            )
                            if rich_text.strip():
                                mood_event = prepared_event
                                mood_texts.append(rich_text)
                            await self._append_dialogue_segment(prepared_event)
                            prepared_event.set_extra("astrmai_private_context_recorded", True)
                    if mood_event is not None and mood_texts:
                        await self._apply_primary_mood_update(
                            mood_event,
                            chat_id,
                            "\n".join(mood_texts),
                        )
                merged_events = self._merge_attention_window(session, batch_events)
                events = await self._format_and_filter_messages(merged_events)
                if events:
                    normalized = self._build_normalized_events(events, self_id)
                    focus_event, _, focus_reason = self._select_focus_event(
                        events,
                        self_id,
                        normalized_events=normalized,
                        is_private=is_private,
                    )
                    if focus_event is None:
                        focus_event = events[-1]
                    if is_private:
                        bind_batch_context = getattr(
                            self.private_turn_coordinator,
                            "bind_batch_context",
                            None,
                        )
                        if callable(bind_batch_context):
                            bind_batch_context(batch_events, focus_event)
                        self._bind_private_batch_turn(focus_event, batch_events, chat_id)
                        normalized = self._build_normalized_events(events, self_id)
                    focus_candidate = next((candidate for candidate in normalized if candidate.event is focus_event), None)
                    if focus_candidate is None:
                        focus_candidate = normalized[-1]
                    root_candidate, root_reason = self._resolve_thread_root(focus_candidate, normalized)
                    focus_thread = self._build_focus_thread(focus_candidate, root_candidate, normalized)
                    focus_thread.focus_reason = focus_thread.focus_reason or focus_reason or "selected_focus_event"
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
                    topic_confirmation_required = False
                    if is_private and self.conversation_continuity is not None:
                        evaluate_topic = getattr(
                            self.conversation_continuity,
                            "evaluate_private_message",
                            None,
                        )
                        if callable(evaluate_topic):
                            topic_decision = evaluate_topic(
                                chat_id,
                                str(
                                    focus_event.get_extra(
                                        "astrmai_rich_text",
                                        getattr(focus_event, "message_str", ""),
                                    )
                                    or ""
                                ),
                            )
                            focus_event.set_extra("astrmai_private_topic_status", topic_decision.get("status", ""))
                            focus_event.set_extra("astrmai_private_topic_inherited", bool(topic_decision.get("inherited", False)))
                            focus_event.set_extra("astrmai_private_topic_age_sec", float(topic_decision.get("age_seconds", 0.0) or 0.0))
                            focus_event.set_extra("astrmai_private_topic_label", str(topic_decision.get("topic", "") or ""))
                            prompt_summary = str(topic_decision.get("prompt_summary", "") or "")
                            if prompt_summary:
                                focus_event.set_extra("astrmai_private_topic_context", prompt_summary)
                            topic_confirmation_required = bool(topic_decision.get("requires_confirmation", False))
                            if topic_confirmation_required:
                                focus_event.set_extra(
                                    "astrmai_private_topic_confirmation_text",
                                    str(topic_decision.get("confirmation_text", "") or ""),
                                )
                    self._schedule_compaction_task(chat_id, focus_thread)
                    should_skip_judge = bool(
                        focus_candidate.is_direct_wakeup
                        or focus_candidate.is_at_bot
                        or focus_candidate.is_reply_to_bot
                        or focus_candidate.has_direct_vision
                        or current_is_strong_wakeup
                    )
                    if topic_confirmation_required:
                        judge_action = "TOPIC_CONFIRM"
                    else:
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
                        private_burst_size=len(batch_events) if is_private else 0,
                        private_generation=(
                            getattr(focus_event.get_extra("astrmai_turn_identity", None), "generation", 0)
                            if is_private
                            else 0
                        ),
                        private_vision_count=(
                            len(focus_event.get_extra("astrmai_vision_records", []) or [])
                            if is_private
                            else 0
                        ),
                        private_vision_failed=(
                            bool(focus_event.get_extra("astrmai_vision_barrier_failed", False))
                            if is_private
                            else False
                        ),
                    )
                    if judge_action == "TOPIC_CONFIRM":
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                        await self._send_private_topic_confirmation(
                            focus_event,
                            str(
                                focus_event.get_extra("astrmai_private_topic_confirmation_text", "")
                                or "这个话题已经隔了一会儿了，还要继续聊吗？回复“继续”就好～"
                            ),
                        )
                    elif judge_action == "WAIT":
                        if bool(focus_event.get_extra("astrmai_is_proactive_event", False)):
                            await self._complete_proactive_candidate(focus_event, reason="proactive_judge_wait")
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                    elif judge_action == "IGNORE":
                        if bool(focus_event.get_extra("astrmai_is_proactive_event", False)):
                            await self._complete_proactive_candidate(focus_event, reason="proactive_judge_ignore")
                        async with session.lock:
                            self._append_attention_window(session, [focus_event])
                    else:
                        async with session.lock:
                            self._append_attention_window(session, batch_events)
                        if self.sys2_process:
                            if is_private:
                                begin_pending_batch = getattr(
                                    self.private_turn_coordinator,
                                    "begin_pending_batch",
                                    None,
                                )
                                pending_revision = (
                                    begin_pending_batch(chat_id, batch_events)
                                    if callable(begin_pending_batch)
                                    else 0
                                )
                                try:
                                    reply_result = await self.sys2_process(
                                        focus_event,
                                        focus_thread.all_thread_events(),
                                    )
                                    finish_pending_batch = getattr(
                                        self.private_turn_coordinator,
                                        "finish_pending_batch",
                                        None,
                                    )
                                    if callable(finish_pending_batch):
                                        finish_pending_batch(
                                            chat_id,
                                            pending_revision,
                                            bool(reply_result),
                                        )
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    logger.error(f"[AttentionGate] private System2 dispatch failed for {chat_id}: {exc}", exc_info=True)
                            else:
                                self._fire_background_task(
                                    self.sys2_process(focus_event, focus_thread.all_thread_events()),
                                    focus_event,
                                )

            async with session.lock:
                if session.accumulation_pool:
                    current_is_strong_wakeup = False
                    continue
                session.is_evaluating = False
                return


__all__ = ["AttentionGate"]
