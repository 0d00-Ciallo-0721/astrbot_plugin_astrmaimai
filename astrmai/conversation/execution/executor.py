from __future__ import annotations

import asyncio
import copy
import re
import time
from time import monotonic
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api.message_components import Plain
except ImportError:  # pragma: no cover
    class Plain:  # type: ignore[override]
        def __init__(self, text: str):
            self.text = text


try:
    from astrbot.core.agent.tool import ToolSet
except ImportError:  # pragma: no cover
    class ToolSet:  # type: ignore[override]
        def __init__(self, tools):
            self.tools = tools


from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...infrastructure.gateway.output_guard import validate_visible_output_text
from ...infrastructure.gateway.gateway_exceptions import LLMCascadeFailureException
from ...multimodal.vision_prompt import (
    VISION_SYSTEM_PROMPT,
    VISION_USER_PROMPT,
    normalize_vision_result,
    render_vision_record,
)
from ..contracts.focus_context import FocusThreadContext, FreshnessState, VisionBundle
from ..contracts.prompt_envelope import PromptEnvelope
from ..planning.tool_contracts import record_tool_lifecycle
from ..planning.tool_disclosure import normalize_requested_packages, select_tools_by_packages


class ConcurrentExecutor:
    def __init__(
        self,
        context,
        gateway,
        reply_engine,
        evolution_manager,
        config=None,
        runtime_coordinator=None,
    ):
        self.context = context
        self.gateway = gateway
        self.reply_engine = reply_engine
        self.evolution_manager = evolution_manager
        self.config = config if config else gateway.config
        self.runtime_coordinator = runtime_coordinator
        self._chat_locks = {}
        self._chat_pending_count = {}
        self._global_lock = asyncio.Lock()
        self._native_vision_breakers: dict[str, float] = {}

    def refresh_config(self, config) -> None:
        self.config = config

    def _build_vision_bundle(
        self,
        event: AstrMessageEvent,
        direct_vision_urls: Optional[list[str]],
    ) -> VisionBundle:
        barrier_complete = bool(event.get_extra("astrmai_vision_barrier_complete", False))
        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        if isinstance(focus_context, FocusThreadContext):
            bundle = focus_context.vision_bundle
            image_urls = list(dict.fromkeys(list(bundle.image_urls or []) + list(direct_vision_urls or [])))
            direct_urls = list(dict.fromkeys(list(bundle.direct_image_urls or []) + list(direct_vision_urls or [])))
            return VisionBundle(
                image_urls=image_urls,
                direct_image_urls=[] if barrier_complete else direct_urls,
                is_direct_request=bundle.is_direct_request or bool(direct_urls),
                is_image_only=bundle.is_image_only,
                source=bundle.source or "focus_thread",
            )

        urls = list(dict.fromkeys(list(direct_vision_urls or [])))
        return VisionBundle(
            image_urls=urls,
            direct_image_urls=[] if barrier_complete else urls[:],
            is_direct_request=bool(urls),
            is_image_only=bool(urls and not (event.message_str or "").strip()),
            source="event_extra",
        )

    def _build_sanitized_execution_event(
        self,
        event: AstrMessageEvent,
        vision_bundle: VisionBundle,
    ) -> AstrMessageEvent:
        try:
            sanitized_event = copy.copy(event)
            if hasattr(event, "message_obj") and event.message_obj:
                sanitized_message_obj = copy.copy(event.message_obj)
                safe_text = (
                    event.message_str.strip()
                    if event.message_str
                    else ("[image-or-special-message]" if vision_bundle.image_urls else "[special-message]")
                )
                sanitized_message_obj.message = [Plain(safe_text)]
                sanitized_event.message_obj = sanitized_message_obj
            return sanitized_event
        except Exception:
            logger.debug("[Executor] _build_sanitized_execution_event failed", exc_info=True)
            return event

    @staticmethod
    def _sync_execution_event_trace(source_event: AstrMessageEvent, target_event: AstrMessageEvent) -> None:
        if source_event is target_event:
            return
        if not hasattr(source_event, "get_extra") or not hasattr(target_event, "set_extra"):
            return
        for key in (
            "astrmai_request_trace",
            "astrmai_post_hook_system_hash",
            "astrmai_tool_execution_trace",
            "astrmai_tool_lifecycle_trace",
            "astrmai_tool_invocation_plans",
            "astrmai_required_tools",
            "astrmai_prepared_required_tools",
            "astrmai_requested_tool_packages",
            "astrmai_tool_disclosure_expanded_once",
            "astrmai_disclosure_expanded_packages",
            "astrmai_pending_actions",
            "astrmai_at_action_verified",
            "astrmai_at_action_target_id",
            "astrmai_at_action_group_id",
            "astrmai_bypass_mood_analysis",
            "astrmai_force_meme",
            "astrmai_tool_clarification_needed",
            "astrmai_tool_clarification_prompt",
            "astrmai_tool_clarification_missing_slots",
        ):
            value = source_event.get_extra(key, None)
            if value is not None:
                target_event.set_extra(key, value)

    @staticmethod
    def _record_required_tool_outcomes(event: AstrMessageEvent) -> list[str]:
        required = {
            str(name or "").strip()
            for name in event.get_extra("astrmai_required_tools", []) or []
            if str(name or "").strip()
        }
        if not required:
            return []
        executed = {
            str(item.get("tool_name") or "").strip()
            for item in event.get_extra("astrmai_tool_execution_trace", []) or []
            if isinstance(item, dict) and str(item.get("status", "success")) == "success"
        }
        queued = {
            str(item.get("tool") or "").strip()
            for item in event.get_extra("astrmai_tool_lifecycle_trace", []) or []
            if isinstance(item, dict) and str(item.get("phase") or "") == "action_queued"
        }
        prepared = {
            str(name or "").strip()
            for name in event.get_extra("astrmai_prepared_required_tools", []) or []
        }
        satisfied = executed | queued | prepared
        current_group_id = ""
        if hasattr(event, "get_group_id"):
            try:
                current_group_id = str(event.get_group_id() or "").strip()
            except Exception:
                current_group_id = ""
        verified_at_action = any(
            isinstance(item, dict)
            and str(item.get("action") or "") == "at"
            and bool(item.get("verified_current_group"))
            and bool(current_group_id)
            and str(item.get("group_id") or "").strip() == current_group_id
            and bool(str(item.get("target_id") or "").strip())
            for item in event.get_extra("astrmai_pending_actions", []) or []
        )
        missing: list[str] = []
        for tool_name in sorted(required):
            is_satisfied = tool_name in satisfied
            if tool_name == "construct_at_event":
                is_satisfied = bool(verified_at_action)
            if is_satisfied:
                record_tool_lifecycle(
                    event,
                    tool_name,
                    "required_tool_outcome",
                    status="satisfied",
                )
            else:
                missing.append(tool_name)
                record_tool_lifecycle(
                    event,
                    tool_name,
                    "required_tool_outcome",
                    source="model_tool_call",
                    status="missing",
                    reason="model_did_not_call_required_tool",
                )
        return missing

    @staticmethod
    def _required_tool_retry_prompt(api_prompt: str, missing_tools: list[str]) -> str:
        tool_list = "、".join(missing_tools)
        return (
            f"{api_prompt}\n\n"
            "[SYSTEM TOOL ENFORCEMENT]\n"
            f"用户本轮明确要求的工具尚未执行：{tool_list}。"
            "如果工具参数已经足够，你必须先各调用一次当前提供的工具，再依据真实工具结果回答原始请求。"
            "如果无法可靠构造工具参数，不要猜测、不要伪造、不要声称已查询或已执行；"
            "请直接向用户追问缺少的目标、内容或上下文。"
        )

    @staticmethod
    def _required_tool_missing_reply(event: AstrMessageEvent, missing_tools: list[str]) -> str:
        prompt = str(event.get_extra("astrmai_tool_clarification_prompt", "") or "").strip()
        if prompt:
            return prompt
        labels = {
            "space_transition_action": "跨会话发消息",
            "construct_at_event": "@某人",
            "memory_write_correction_tool": "纠正记忆",
            "unverified_report_record_tool": "记录未确认说法",
            "persona_fact_check_tool": "核查设定事实",
            "proactive_meme": "发表情包",
            "qq_custom_face_send_tool": "发自定义表情",
            "message_reaction_action": "互动回应",
        }
        tool_labels = [labels.get(name, name) for name in missing_tools]
        if "space_transition_action" in missing_tools:
            return "我还没能确认要发给谁、具体转达什么，所以没有发送。你把目标和要说的话都告诉我，我再帮你传达。"
        if "construct_at_event" in missing_tools:
            return "我还没能在当前群确认你要叫的人，所以没有发送假 @。请给我对方当前群昵称或 QQ 号。"
        return "我还没能确认这次要执行的具体信息，所以没有操作。你再补充一下：" + "、".join(tool_labels)

    async def _handle_required_tool_missing(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        bot_id: str,
        missing_tools: list[str],
        *,
        model: str,
    ) -> Optional[str]:
        reply_text = self._required_tool_missing_reply(event, missing_tools)
        event.set_extra("astrmai_execution_status", "tool_clarification_sent")
        event.set_extra("astrmai_tool_missing_required", list(missing_tools))
        debug_trace(
            event,
            "execution.executor.required_tool_missing",
            missing_tools=list(missing_tools),
            reply_preview=preview_text(reply_text, 120),
        )
        return await self._finalize_reply(
            event,
            chat_id,
            bot_id,
            reply_text,
            trace_mode="tool_clarification",
            model=model,
        )

    def _mark_vision_direct_state(
        self,
        event: AstrMessageEvent,
        *,
        invoked: bool,
        outcome: str,
        skip_reason: str = "",
        details: str = "",
        attempted_models: Optional[list[str]] = None,
        failure_reason: str = "",
        failure_kind: str = "",
    ) -> None:
        if hasattr(event, "set_extra"):
            event.set_extra("vision_direct_invoked", invoked)
            event.set_extra("vision_direct_outcome", outcome)
            event.set_extra("vision_direct_skip_reason", skip_reason)
            event.set_extra("vision_direct_attempted_models", list(attempted_models or []))
            event.set_extra("vision_direct_failure_reason", failure_reason)
            event.set_extra("vision_direct_failure_kind", failure_kind)
        debug_trace(
            event,
            "execution.executor.vision_direct",
            invoked=invoked,
            outcome=outcome,
            skip_reason=skip_reason,
            details=details,
            attempted_models=list(attempted_models or []),
            failure_reason=failure_reason,
            failure_kind=failure_kind,
        )

    def _mark_vision_main_reply_state(
        self,
        event: AstrMessageEvent,
        *,
        strategy: str,
        selected: bool,
        outcome: str,
        breaker_until: float = 0.0,
        fallback_reason: str = "",
        details: str = "",
    ) -> None:
        if hasattr(event, "set_extra"):
            event.set_extra("vision_main_reply_strategy", strategy)
            event.set_extra("vision_native_direct_selected", selected)
            event.set_extra("vision_native_direct_outcome", outcome)
            event.set_extra("vision_native_direct_breaker_until", breaker_until)
            event.set_extra("vision_native_direct_fallback_reason", fallback_reason)
        debug_trace(
            event,
            "execution.executor.vision_main_reply",
            strategy=strategy,
            selected=selected,
            outcome=outcome,
            breaker_until=breaker_until,
            fallback_reason=fallback_reason,
            details=details,
        )

    def _native_main_reply_vision_enabled(self) -> bool:
        vision_cfg = getattr(self.config, "vision", None)
        return bool(getattr(vision_cfg, "use_native_main_reply_vision", False))

    def _native_main_reply_breaker_until(self, chat_id: str) -> float:
        now = monotonic()
        breaker_until = float(self._native_vision_breakers.get(chat_id, 0.0) or 0.0)
        if breaker_until and breaker_until <= now:
            self._native_vision_breakers.pop(chat_id, None)
            return 0.0
        return breaker_until

    def _open_native_main_reply_breaker(self, chat_id: str) -> float:
        vision_cfg = getattr(self.config, "vision", None)
        cooldown = int(getattr(vision_cfg, "native_main_reply_failure_cooldown_sec", 180) or 180)
        cooldown = max(1, cooldown)
        breaker_until = monotonic() + float(cooldown)
        self._native_vision_breakers[chat_id] = breaker_until
        return breaker_until

    def _should_attempt_native_main_reply_vision(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        vision_bundle: VisionBundle,
    ) -> tuple[bool, str, float]:
        if not self._native_main_reply_vision_enabled():
            return False, "disabled", 0.0
        if not vision_bundle.direct_image_urls:
            return False, str(event.get_extra("vision_direct_skip_reason", "") or "not_direct_path"), 0.0
        breaker_until = self._native_main_reply_breaker_until(chat_id)
        if breaker_until > monotonic():
            return False, "breaker_open", breaker_until
        return True, "", 0.0

    def _normalize_vision_result(
        self,
        result_dict: Any,
    ) -> tuple[Optional[str], list[str], str]:
        payload, invalid_reason = normalize_vision_result(result_dict)
        if payload is None:
            return None, [], invalid_reason
        return str(payload["description"]), list(payload["emotion_tags"]), ""

    def _classify_execution_failure_kind(self, error_message: Any) -> str:
        if hasattr(error_message, "last_failure_kind"):
            return str(getattr(error_message, "last_failure_kind", "") or "unknown")
        classifier = getattr(self.gateway, "_classify_failure_kind", None)
        if callable(classifier):
            try:
                return str(classifier(error_message).value)
            except Exception:
                logger.debug("[Executor] _classify_execution_failure_kind inner failed", exc_info=True)
                pass
        lowered = str(error_message).lower()
        if "empty_response" in lowered:
            return "empty_response"
        if "provider_failure_text" in lowered:
            return "provider_failure_text"
        if "unsafe_or_empty_text" in lowered:
            return "unsafe_or_empty_text"
        if "prompt_scaffold_text" in lowered:
            return "prompt_scaffold_text"
        if "tool_protocol_text" in lowered:
            return "tool_protocol_text"
        if "json" in lowered:
            return "json_decode_error"
        if "timeout" in lowered:
            return "timeout"
        if "payload" in lowered or "validation error" in lowered:
            return "bad_payload"
        return "unknown"

    def _extract_cascade_failure_meta(self, exc: Exception) -> tuple[list[str], str, str]:
        attempted_models = list(getattr(exc, "attempted_models", []) or [])
        failure_reason = str(
            getattr(exc, "failure_reason", "")
            or getattr(exc, "error_message", "")
            or str(exc)
        )
        failure_kind = str(getattr(exc, "last_failure_kind", "") or self._classify_execution_failure_kind(failure_reason))
        return attempted_models, failure_reason, failure_kind

    def _is_executor_failure_fatal(self, error_message: str) -> bool:
        is_fatal = getattr(self.gateway, "_is_fatal_failure", None)
        if callable(is_fatal):
            try:
                return bool(is_fatal(error_message))
            except Exception:
                logger.debug("[AstrMai-exec] _is_executor_failure_fatal inner failed", exc_info=True)
                return False
        return False

    async def _evaluate_execution_freshness(self, event: AstrMessageEvent, chat_id: str) -> tuple[FreshnessState, str]:
        if not self.runtime_coordinator:
            return FreshnessState.FRESH, ""

        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        focus_timestamp = float(event.get_extra("astrmai_timestamp", getattr(event, "timestamp", 0.0)) or 0.0)
        thread_signature = ""

        if isinstance(focus_context, FocusThreadContext):
            focus_timestamp = float(focus_context.freshness_budget.created_at or focus_timestamp)
            thread_signature = str(focus_context.thread_signature or "")
        elif isinstance(prompt_envelope, PromptEnvelope):
            thread_signature = str(prompt_envelope.thread_signature or "")

        max_age_seconds = float(getattr(getattr(self.config, "reply", None), "stale_reply_max_age_sec", 0.0) or 0.0)
        if max_age_seconds <= 0:
            api_timeout = float(getattr(getattr(self.config, "infra", None), "api_timeout", 15.0) or 15.0)
            max_age_seconds = max(30.0, min(90.0, api_timeout * 2.5))

        return await self.runtime_coordinator.evaluate_reply_freshness(
            chat_id,
            focus_timestamp,
            max_age_seconds=max_age_seconds,
            thread_signature=thread_signature,
        )

    async def _acquire_chat_execution_lock(self, chat_id: str):
        using_runtime_coordinator = self.runtime_coordinator is not None
        if using_runtime_coordinator:
            chat_lock = await self.runtime_coordinator.try_acquire_executor(chat_id, max_pending=2)
            if chat_lock is None:
                return None, True
            return chat_lock, True

        async with self._global_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = asyncio.Lock()
                self._chat_pending_count[chat_id] = 0
            if self._chat_pending_count[chat_id] >= 2:
                return None, False
            self._chat_pending_count[chat_id] += 1
            chat_lock = self._chat_locks[chat_id]
        await chat_lock.acquire()
        return chat_lock, False

    async def _release_chat_execution_lock(
        self,
        chat_id: str,
        using_runtime_coordinator: bool,
        chat_lock: Optional[asyncio.Lock],
    ) -> None:
        if chat_lock is not None and chat_lock.locked():
            chat_lock.release()
        if using_runtime_coordinator:
            await self.runtime_coordinator.release_executor(chat_id)
            return
        async with self._global_lock:
            self._chat_pending_count[chat_id] -= 1
            if self._chat_pending_count[chat_id] == 0:
                self._chat_locks.pop(chat_id, None)
                self._chat_pending_count.pop(chat_id, None)

    @staticmethod
    def _is_group_chat_event(event: AstrMessageEvent, chat_id: str) -> bool:
        try:
            if str(event.get_group_id() or "").strip():
                return True
        except Exception:
            pass
        return "GroupMessage" in str(chat_id or "")

    @staticmethod
    def _sanitize_lane_scope(value: str) -> str:
        cleaned = re.sub(r"[^0-9A-Za-z_.:-]+", "_", str(value or "").strip())
        cleaned = cleaned.strip("._:-")
        return cleaned[:96]

    def _resolve_dialog_lane_identity(self, event: AstrMessageEvent, chat_id: str) -> tuple[LaneKey, str]:
        if not self._is_group_chat_event(event, chat_id):
            return LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id), chat_id

        thread_id = str(event.get_extra("astrmai_turn_thread_id", "") or "").strip()
        if not thread_id:
            focus_context = event.get_extra("astrmai_focus_thread_context", None)
            if isinstance(focus_context, FocusThreadContext):
                thread_id = str(focus_context.thread_signature or "").strip()
        if not thread_id:
            try:
                sender_id = str(event.get_sender_id() or "").strip()
            except Exception:
                sender_id = ""
            if sender_id:
                thread_id = f"sender:{sender_id}"

        scope_suffix = self._sanitize_lane_scope(thread_id)
        if not scope_suffix:
            return LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id), chat_id
        scoped_origin = f"{chat_id}@@thread:{scope_suffix}"
        scoped_id = f"{chat_id}#{scope_suffix}"
        return LaneKey(subsystem="sys2", task_family="dialog", scope_id=scoped_id), scoped_origin

    def _execution_runtime_values(self, event: AstrMessageEvent, chat_id: str) -> dict[str, Any]:
        bot_id = str(event.get_self_id()) if hasattr(event, "get_self_id") else "SELF_BOT"
        is_fast_mode = event.get_extra("is_fast_mode", False)
        config_max_steps = getattr(self.config.agent, "max_steps", 5)
        tool_tier = str(event.get_extra("astrmai_tool_tier", "full") or "full")
        max_steps = max(5, config_max_steps)
        timeout = 15 if is_fast_mode else self.config.agent.timeout
        prefix_hash = event.get_extra("astrmai_prefix_hash", "")
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        raw_user_text = (
            prompt_envelope.raw_user_text
            if isinstance(prompt_envelope, PromptEnvelope)
            else event.get_extra("astrmai_raw_user_text", "")
        )
        dialog_lane_key, dialog_base_origin = self._resolve_dialog_lane_identity(event, chat_id)
        return {
            "bot_id": bot_id,
            "is_fast_mode": is_fast_mode,
            "max_steps": max_steps,
            "timeout": timeout,
            "tool_tier": tool_tier,
            "prefix_hash": prefix_hash,
            "raw_user_text": raw_user_text,
            "dialog_lane_key": dialog_lane_key,
            "dialog_base_origin": dialog_base_origin,
        }

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", "") or "").strip()

    def _expand_tools_for_disclosure_request(
        self,
        event: AstrMessageEvent,
        current_tools: list[Any],
    ) -> tuple[list[Any], list[str]]:
        if not hasattr(event, "get_extra") or not hasattr(event, "set_extra"):
            return current_tools, []
        if event.get_extra("astrmai_tool_disclosure_expanded_once", False):
            return current_tools, []
        conversation = getattr(self.config, "conversation", None)
        if not bool(getattr(conversation, "tool_disclosure_allow_second_pass", True)):
            return current_tools, []
        requested = normalize_requested_packages(event.get_extra("astrmai_requested_tool_packages", []))
        if not requested:
            return current_tools, []
        allowed = set(normalize_requested_packages(event.get_extra("astrmai_disclosure_second_pass_packages", [])))
        packages = [package for package in requested if package in allowed]
        if not packages:
            return current_tools, []
        hidden_tools = list(getattr(event, "_astrmai_disclosure_hidden_tools", []) or [])
        if not hidden_tools:
            return current_tools, []
        max_extra = max(1, int(getattr(conversation, "tool_disclosure_max_tools_task", 16) or 16))
        additions = select_tools_by_packages(
            hidden_tools,
            packages,
            name_resolver=self._tool_name,
            max_tools=max_extra,
        )
        if not additions:
            return current_tools, []
        existing = {self._tool_name(tool) for tool in current_tools or []}
        merged = list(current_tools or [])
        added_names: list[str] = []
        for tool in additions:
            name = self._tool_name(tool)
            if not name or name in existing:
                continue
            existing.add(name)
            merged.append(tool)
            added_names.append(name)
        if not added_names:
            return current_tools, []
        event.set_extra("astrmai_tool_disclosure_expanded_once", True)
        event.set_extra("astrmai_disclosure_expanded_packages", packages)
        turn_context = event.get_extra("astrmai_turn_context", None)
        tools_state = getattr(turn_context, "tools", None)
        if tools_state is not None:
            tools_state.disclosure_expanded_packages = list(packages)
            tools_state.filtered_tools = [self._tool_name(tool) for tool in merged if self._tool_name(tool)]
            tools_state.record_step(
                "executor.tool_disclosure_second_pass",
                [self._tool_name(tool) for tool in current_tools or []],
                tools_state.filtered_tools,
                "requested_packages(" + ",".join(packages) + ")",
            )
        for tool_name in added_names:
            record_tool_lifecycle(
                event,
                tool_name,
                "disclosed_second_pass",
                source="bot_capability_lookup",
                status="available",
                reason="requested_packages(" + ",".join(packages) + ")",
            )
        return merged, packages

    async def _inject_direct_vision_context(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        model_prompt: str,
        system_prompt: str,
        vision_bundle: VisionBundle,
    ) -> tuple[str, str]:
        if not vision_bundle.direct_image_urls:
            self._mark_vision_direct_state(
                event,
                invoked=False,
                outcome="skipped",
                skip_reason=str(event.get_extra("vision_direct_skip_reason", "") or "not_direct_path"),
            )
            return model_prompt, system_prompt

        import base64
        import io
        import os
        import tempfile
        from PIL import Image

        logger.info(f"[{chat_id}] vision direct path triggered in executor")
        self._mark_vision_direct_state(event, invoked=True, outcome="skipped")
        vision_descriptions: list[str] = []
        saw_invalid_output = False
        saw_exception = False
        for url_or_path in vision_bundle.direct_image_urls:
            temp_file_path = None
            created_temp_file_path = None
            try:
                image_bytes = None
                if str(url_or_path).startswith("data:image"):
                    _, encoded = str(url_or_path).split(",", 1)
                    image_bytes = base64.b64decode(encoded)
                # ponytail: sync file I/O in async context — acceptable for local temp files.
                # If this becomes a bottleneck, wrap in asyncio.to_thread().
                elif os.path.exists(url_or_path):
                    temp_file_path = url_or_path
                elif str(url_or_path).startswith("http"):
                    logger.debug(f"[{chat_id}] remote image URL skipped because remote fetching is disabled: {url_or_path}")

                if image_bytes:
                    try:
                        img_format = Image.open(io.BytesIO(image_bytes)).format.lower()
                    except Exception:
                        img_format = "jpeg"
                    # ponytail: sync tempfile.mkstemp/os.fdopen in async context.
                    # OK for small writes; migrate to aiofiles if I/O stalls observed.
                    fd, temp_file_path = tempfile.mkstemp(suffix=f".{img_format}")
                    with os.fdopen(fd, "wb") as file_obj:
                        file_obj.write(image_bytes)
                    created_temp_file_path = temp_file_path

                if temp_file_path and os.path.exists(temp_file_path):
                    result_dict = await self.gateway.call_vision_task(
                        image_data=temp_file_path,
                        prompt=VISION_USER_PROMPT,
                        system_prompt=VISION_SYSTEM_PROMPT,
                        lane_key=LaneKey(subsystem="sys1", task_family="vision", scope_id=chat_id),
                        base_origin=chat_id,
                    )
                    payload, invalid_reason = normalize_vision_result(result_dict)
                    if payload is None:
                        saw_invalid_output = True
                        logger.warning(f"[{chat_id}] vision side-path invalid output: {invalid_reason}")
                        continue
                    vision_line = render_vision_record(payload)
                    if payload.get("type") == "image":
                        description = str(payload.get("description") or "").strip()
                        suffix = "" if description.endswith(("。", "！", "？", ".", "!", "?")) else "。"
                        raw_tags = result_dict.get("emotion_tags") if isinstance(result_dict, dict) else []
                        tag_items = raw_tags if isinstance(raw_tags, list) else [raw_tags] if isinstance(raw_tags, str) else []
                        tags = [str(tag).strip() for tag in tag_items if str(tag).strip()]
                        feeling_line = f"\n它给我的感觉是：{', '.join(tags)}。" if tags else ""
                        vision_line = f"我刚看到一张图片，画面是：{description}{suffix}{feeling_line}\n{vision_line}"
                    vision_descriptions.append(vision_line)
            except Exception as exc:
                saw_exception = True
                attempted_models, failure_reason, failure_kind = self._extract_cascade_failure_meta(exc)
                logger.error(f"[{chat_id}] vision side-path failed: {exc}")
                self._mark_vision_direct_state(
                    event,
                    invoked=True,
                    outcome="exception",
                    attempted_models=attempted_models,
                    failure_reason=failure_reason,
                    failure_kind=failure_kind,
                )
            finally:
                if created_temp_file_path and os.path.exists(created_temp_file_path):
                    try:
                        os.remove(created_temp_file_path)
                    except Exception:
                        logger.debug("[Executor] temp file cleanup failed", exc_info=True)
                        pass

        if vision_descriptions:
            vision_inject = (
                "\n\n"
                + "\n".join(vision_descriptions)
            )
            model_prompt += vision_inject
            self._mark_vision_direct_state(
                event,
                invoked=True,
                outcome="success",
                details=f"descriptions={len(vision_descriptions)}",
            )
        elif saw_invalid_output:
            self._mark_vision_direct_state(
                event,
                invoked=True,
                outcome="invalid_output",
                details="all_descriptions_rejected",
            )
        elif saw_exception:
            if not event.get_extra("vision_direct_failure_reason"):
                self._mark_vision_direct_state(
                    event,
                    invoked=True,
                    outcome="exception",
                )
        else:
            self._mark_vision_direct_state(
                event,
                invoked=True,
                outcome="exception",
                details="no_usable_image_input",
            )
        return model_prompt, system_prompt

    async def _check_pre_model_freshness(self, event: AstrMessageEvent, chat_id: str, label: str) -> bool:
        freshness_state, freshness_reason = await self._evaluate_execution_freshness(event, chat_id)
        if freshness_state == FreshnessState.EXPIRED:
            logger.info(f"[{chat_id}] stop expired {label}: {freshness_reason}")
            return False
        return True

    async def _finalize_reply(self, event: AstrMessageEvent, chat_id: str, bot_id: str, reply_text: str, *, trace_mode: str, model: str) -> Optional[str]:
        artifact = await self.reply_engine.handle_reply(event, reply_text, chat_id)
        sent = bool(getattr(artifact, "sent", False)) if artifact is not None else True
        if not sent:
            metadata = getattr(artifact, "metadata", {}) or {}
            send_status = str(metadata.get("send_status", "") or "")
            blocked_reason = str(getattr(artifact, "blocked_reason", "") or "")
            if "stale" in blocked_reason:
                event.set_extra("astrmai_execution_status", "stale_drop")
            elif send_status == "duplicate_blocked":
                event.set_extra("astrmai_execution_status", "duplicate_blocked")
            else:
                event.set_extra("astrmai_execution_status", "send_failed")
                raise RuntimeError(blocked_reason or send_status or "visible reply was not sent")
            return None
        if hasattr(self.evolution_manager, "process_bot_reply"):
            await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
        event.set_extra(
            "astrmai_execution_status",
            "partial_sent"
            if str((getattr(artifact, "metadata", {}) or {}).get("send_status", "")) == "partial_sent"
            else "sent",
        )
        debug_trace(
            event,
            "execution.executor.exit",
            mode=trace_mode,
            model=model,
            reply_preview=preview_text(reply_text, 120),
        )
        return reply_text

    async def _run_text_mode(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        api_prompt: str,
        system_prompt: str,
        runtime: dict[str, Any],
        *,
        image_urls: Optional[list[str]] = None,
        raise_on_exhaustion: bool = False,
    ) -> Optional[str]:
        last_error = ""
        last_failure_kind = "unknown"
        attempted_models: list[str] = []
        agent_models = self.gateway.get_agent_models()
        selection_meta = dict(getattr(self.gateway, "_last_agent_model_selection", {}) or {})
        for index, provider_id in enumerate(agent_models):
            if not await self._check_pre_model_freshness(event, chat_id, "text execution"):
                if hasattr(event, "set_extra"):
                    event.set_extra("astrmai_execution_status", "stale_drop")
                debug_trace(
                    event,
                    "execution.executor.stale_drop",
                    mode="chat",
                    reason="freshness_expired",
                    model=provider_id,
                    pool_index=index,
                    pool_size=len(agent_models),
                    label="text execution",
                )
                return None
            attempted_models.append(provider_id)
            try:
                result = await self.gateway.chat_in_lane_result(
                    event=event,
                    lane_key=runtime["dialog_lane_key"],
                    base_origin=runtime["dialog_base_origin"],
                    prompt=api_prompt,
                    system_prompt=system_prompt,
                    models=[provider_id],
                    prefix_hash=runtime["prefix_hash"],
                    image_urls=image_urls,
                    use_fallback=False,
                    raw_user_text=runtime["raw_user_text"],
                )
                reply_text = result.text
                safe_reply_text, failure_kind = validate_visible_output_text(reply_text)
                if failure_kind:
                    raise ValueError(failure_kind)
                return await self._finalize_reply(
                    event,
                    chat_id,
                    runtime["bot_id"],
                    safe_reply_text,
                    trace_mode="chat",
                    model=provider_id,
                )
            except Exception as exc:
                last_error = str(exc)
                last_failure_kind = self._classify_execution_failure_kind(last_error)
                debug_trace(
                    event,
                    "execution.executor.model_failure",
                    mode="chat",
                    pool_name="agent",
                    model=provider_id,
                    failure_kind=last_failure_kind,
                    fatal=self._is_executor_failure_fatal(last_error),
                    will_retry_or_switch=index < len(agent_models) - 1,
                    error_preview=preview_text(last_error, 120),
                    skipped_cooldown_models=list(selection_meta.get("skipped_cooldown_models", []) or []),
                    cooldown_overridden=bool(selection_meta.get("cooldown_overridden", False)),
                )
                logger.warning(f"[{chat_id}] chat model {provider_id} failed, trying next: {exc}")
                continue

        debug_trace(
            event,
            "execution.executor.model_pool_exhausted",
            mode="chat",
            pool_name="agent",
            attempted_models=attempted_models,
            last_failure_kind=last_failure_kind,
            fallback_triggered=True,
            skipped_cooldown_models=list(selection_meta.get("skipped_cooldown_models", []) or []),
            cooldown_overridden=bool(selection_meta.get("cooldown_overridden", False)),
        )
        logger.error(f"[{chat_id}] all chat models exhausted: {last_error}")
        if raise_on_exhaustion:
            raise RuntimeError(last_error or "chat model pool exhausted")
        return await self._handle_fatal_fallback(event, chat_id, f"all chat models exhausted:\n{last_error}")

    async def _run_tool_mode(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        execution_event: AstrMessageEvent,
        api_prompt: str,
        system_prompt: str,
        tools: list[Any],
        runtime: dict[str, Any],
        *,
        image_urls: Optional[list[str]] = None,
        raise_on_exhaustion: bool = False,
    ) -> Optional[str]:
        tool_set = ToolSet(tools)
        last_error = ""
        last_failure_kind = "unknown"
        attempted_models: list[str] = []
        agent_models = self.gateway.get_agent_models()
        selection_meta = dict(getattr(self.gateway, "_last_agent_model_selection", {}) or {})
        for index, provider_id in enumerate(agent_models):
            if not await self._check_pre_model_freshness(event, chat_id, "tool execution"):
                if hasattr(event, "set_extra"):
                    event.set_extra("astrmai_execution_status", "stale_drop")
                debug_trace(
                    event,
                    "execution.executor.stale_drop",
                    mode="tool",
                    reason="freshness_expired",
                    model=provider_id,
                    pool_index=index,
                    pool_size=len(agent_models),
                    label="tool execution",
                )
                return None
            attempted_models.append(provider_id)
            try:
                result = await self.gateway.tool_chat_in_lane_result(
                    lane_key=runtime["dialog_lane_key"],
                    base_origin=runtime["dialog_base_origin"],
                    event=execution_event,
                    prompt=api_prompt,
                    system_prompt=system_prompt,
                    tools=tool_set,
                    models=[provider_id],
                    max_steps=runtime["max_steps"],
                    timeout=runtime["timeout"],
                    image_urls=image_urls,
                    prefix_hash=runtime["prefix_hash"],
                    raw_user_text=runtime["raw_user_text"],
                )
                self._sync_execution_event_trace(execution_event, event)
                expanded_tools, expanded_packages = self._expand_tools_for_disclosure_request(event, tools)
                if expanded_packages:
                    tools = expanded_tools
                    tool_set = ToolSet(tools)
                    result = await self.gateway.tool_chat_in_lane_result(
                        lane_key=runtime["dialog_lane_key"],
                        base_origin=runtime["dialog_base_origin"],
                        event=execution_event,
                        prompt=(
                            f"{api_prompt}\n\n"
                            "[SYSTEM TOOL DISCLOSURE]\n"
                            "系统已按上一轮工具能力自检追加工具包："
                            + "、".join(expanded_packages)
                            + "。请先使用新增工具获取事实，再给出最终回复。"
                        ),
                        system_prompt=system_prompt,
                        tools=tool_set,
                        models=[provider_id],
                        max_steps=runtime["max_steps"],
                        timeout=runtime["timeout"],
                        image_urls=image_urls,
                        prefix_hash=runtime["prefix_hash"],
                        raw_user_text=runtime["raw_user_text"],
                    )
                    self._sync_execution_event_trace(execution_event, event)
                missing_required = self._record_required_tool_outcomes(event)
                if missing_required:
                    retry_tools = [
                        tool
                        for tool in tools
                        if str(getattr(tool, "name", "") or "").strip() in set(missing_required)
                    ]
                    if retry_tools:
                        for tool_name in missing_required:
                            record_tool_lifecycle(
                                event,
                                tool_name,
                                "required_tool_retry",
                                source="executor_enforcement",
                                status="started",
                            )
                        result = await self.gateway.tool_chat_in_lane_result(
                            lane_key=runtime["dialog_lane_key"],
                            base_origin=runtime["dialog_base_origin"],
                            event=execution_event,
                            prompt=self._required_tool_retry_prompt(api_prompt, missing_required),
                            system_prompt=system_prompt,
                            tools=ToolSet(retry_tools),
                            models=[provider_id],
                            max_steps=max(2, runtime["max_steps"]),
                            timeout=runtime["timeout"],
                            image_urls=image_urls,
                            prefix_hash=runtime["prefix_hash"],
                            raw_user_text=runtime["raw_user_text"],
                        )
                        self._sync_execution_event_trace(execution_event, event)
                        missing_required = self._record_required_tool_outcomes(event)
                    if missing_required:
                        return await self._handle_required_tool_missing(
                            event,
                            chat_id,
                            runtime["bot_id"],
                            sorted(missing_required),
                            model=provider_id,
                        )
                reply_text = result.text
                if not reply_text:
                    raise ValueError("empty tool reply")
                if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                    if hasattr(event, "set_extra"):
                        event.set_extra("astrmai_execution_signal", "wait")
                        event.set_extra("astrmai_execution_status", "skipped_wait")
                    debug_trace(event, "execution.executor.wait_signal", model=provider_id)
                    return None
                if "[TERMINAL_YIELD]:" in reply_text:
                    idx = reply_text.find("[TERMINAL_YIELD]:")
                    terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:"):].strip()
                    safe_content, failure_kind = validate_visible_output_text(terminal_content)
                    if failure_kind:
                        raise ValueError(failure_kind)
                    return await self._finalize_reply(
                        event,
                        chat_id,
                        runtime["bot_id"],
                        safe_content,
                        trace_mode="tool_terminal_yield",
                        model=provider_id,
                    )
                safe_reply_text, failure_kind = validate_visible_output_text(reply_text)
                if failure_kind:
                    raise ValueError(failure_kind)
                return await self._finalize_reply(
                    event,
                    chat_id,
                    runtime["bot_id"],
                    safe_reply_text,
                    trace_mode="tool",
                    model=provider_id,
                )
            except Exception as exc:
                last_error = str(exc)
                last_failure_kind = self._classify_execution_failure_kind(last_error)
                debug_trace(
                    event,
                    "execution.executor.model_failure",
                    mode="tool",
                    pool_name="agent",
                    model=provider_id,
                    failure_kind=last_failure_kind,
                    fatal=self._is_executor_failure_fatal(last_error),
                    will_retry_or_switch=index < len(agent_models) - 1,
                    error_preview=preview_text(last_error, 120),
                    skipped_cooldown_models=list(selection_meta.get("skipped_cooldown_models", []) or []),
                    cooldown_overridden=bool(selection_meta.get("cooldown_overridden", False)),
                )
                logger.warning(f"[{chat_id}] tool model {provider_id} failed, trying next: {exc}")
                continue

        debug_trace(
            event,
            "execution.executor.model_pool_exhausted",
            mode="tool",
            pool_name="agent",
            attempted_models=attempted_models,
            last_failure_kind=last_failure_kind,
            fallback_triggered=True,
            skipped_cooldown_models=list(selection_meta.get("skipped_cooldown_models", []) or []),
            cooldown_overridden=bool(selection_meta.get("cooldown_overridden", False)),
        )
        logger.error(f"[{chat_id}] all tool models exhausted: {last_error}")
        if raise_on_exhaustion:
            raise RuntimeError(last_error or "tool model pool exhausted")
        return await self._handle_fatal_fallback(
            event,
            chat_id,
            last_error if last_error else "tool model pool exhausted",
        )

    async def execute(
        self,
        event: AstrMessageEvent,
        prompt: str,
        system_prompt: str,
        tools: list[Any] = None,
        direct_vision_urls: list[str] = None,
    ) -> Optional[str]:
        debug_trace(
            event,
            "execution.executor.enter",
            tool_count=len(tools or []),
            has_vision=bool(direct_vision_urls),
            prompt=preview_text(prompt, 120),
        )

        chat_id = event.unified_msg_origin
        chat_lock, using_runtime_coordinator = await self._acquire_chat_execution_lock(chat_id)
        if chat_lock is None:
            logger.warning(f"[{chat_id}] executor dropped because pending tasks exceeded budget")
            debug_trace(event, "execution.executor.dropped", reason="too_many_pending")
            event.set_extra("astrmai_execution_status", "pending_drop")
            return None

        try:
            models = self.gateway.get_agent_models()
            if not models:
                logger.error(f"[{chat_id}] no configured agent model")
                event.set_extra("astrmai_execution_status", "fatal_no_send")
                return None

            runtime = self._execution_runtime_values(event, chat_id)
            try:
                event._is_final_reply_phase = True
                if not await self._check_pre_model_freshness(event, chat_id, "executor calculation"):
                    event.set_extra("astrmai_execution_status", "stale_drop")
                    debug_trace(event, "execution.executor.stale_drop", reason="freshness_check_failed")
                    return None

                vision_bundle = self._build_vision_bundle(event, direct_vision_urls)
                execution_event = self._build_sanitized_execution_event(event, vision_bundle)
                should_use_native, native_skip_reason, breaker_until = self._should_attempt_native_main_reply_vision(
                    event,
                    chat_id,
                    vision_bundle,
                )
                if should_use_native:
                    self._mark_vision_main_reply_state(
                        event,
                        strategy="native_direct",
                        selected=True,
                        outcome="skipped",
                    )
                    try:
                        if tools is None or len(tools) == 0:
                            result = await self._run_text_mode(
                                event,
                                chat_id,
                                prompt,
                                system_prompt,
                                runtime,
                                image_urls=vision_bundle.direct_image_urls,
                                raise_on_exhaustion=True,
                            )
                        else:
                            result = await self._run_tool_mode(
                                event,
                                chat_id,
                                execution_event,
                                prompt,
                                system_prompt,
                                tools,
                                runtime,
                                image_urls=vision_bundle.direct_image_urls,
                                raise_on_exhaustion=True,
                            )
                        if result is not None:
                            self._mark_vision_main_reply_state(
                                event,
                                strategy="native_direct",
                                selected=True,
                                outcome="success",
                                details=f"images={len(vision_bundle.direct_image_urls)}",
                            )
                        return result
                    except Exception as exc:
                        fallback_reason = self._classify_execution_failure_kind(str(exc))
                        breaker_until = self._open_native_main_reply_breaker(chat_id)
                        logger.warning(
                            f"[{chat_id}] native main-reply vision failed; falling back to relay chain: {exc}"
                        )
                        self._mark_vision_main_reply_state(
                            event,
                            strategy="native_direct",
                            selected=True,
                            outcome="fallback_to_relay",
                            breaker_until=breaker_until,
                            fallback_reason=fallback_reason,
                            details=preview_text(str(exc), 120),
                        )
                else:
                    outcome = "breaker_open" if native_skip_reason == "breaker_open" else "skipped"
                    self._mark_vision_main_reply_state(
                        event,
                        strategy="relay",
                        selected=False,
                        outcome=outcome,
                        breaker_until=breaker_until,
                        fallback_reason=native_skip_reason,
                    )

                api_prompt, relay_system_prompt = await self._inject_direct_vision_context(
                    event, chat_id, prompt, system_prompt, vision_bundle
                )

                if tools is None or len(tools) == 0:
                    return await self._run_text_mode(event, chat_id, api_prompt, relay_system_prompt, runtime)
                return await self._run_tool_mode(
                    event,
                    chat_id,
                    execution_event,
                    api_prompt,
                    relay_system_prompt,
                    tools,
                    runtime,
                )
            except Exception as exc:
                logger.error(f"[{chat_id}] executor core crashed: {exc}")
                return await self._handle_fatal_fallback(event, chat_id, f"executor core exception:\n{exc}")
            finally:
                if hasattr(event, "_is_final_reply_phase"):
                    delattr(event, "_is_final_reply_phase")
        finally:
            await self._release_chat_execution_lock(chat_id, using_runtime_coordinator, chat_lock)
    async def _handle_fatal_fallback(self, event: AstrMessageEvent, chat_id: str, error_detail: str) -> Optional[str]:
        logger.error(f"[{chat_id}] fatal executor fallback triggered")
        if "required_tool_not_called:" in str(error_detail or ""):
            missing_text = str(error_detail).split("required_tool_not_called:", 1)[1]
            missing_tools = [
                item.strip()
                for item in missing_text.replace("\n", ",").split(",")
                if item.strip()
            ]
            return await self._handle_required_tool_missing(
                event,
                chat_id,
                str(event.get_self_id()) if hasattr(event, "get_self_id") else "SELF_BOT",
                missing_tools or ["unknown_tool"],
                model="fallback",
            )
        if str(event.get_extra("astrmai_execution_status", "") or "") == "stale_drop":
            debug_trace(
                event,
                "execution.executor.fatal_fallback_skipped",
                reason="stale_drop",
                error_preview=preview_text(error_detail, 120),
            )
            return None
        fallback_msg = getattr(self.config.reply, "fallback_text", "(temporary silence...)")
        try:
            artifact = await self.reply_engine.handle_reply(event, fallback_msg, chat_id)
            sent = bool(getattr(artifact, "sent", False)) if artifact is not None else True
        except Exception as exc:
            sent = False
            logger.error(f"[{chat_id}] fatal fallback send failed: {exc}")
        event.set_extra("astrmai_execution_status", "fallback_sent" if sent else "fatal_no_send")

        config_global = getattr(self.config, "global_settings", None)
        if not (config_global and getattr(config_global, "enable_error_interception", True)):
            return fallback_msg if sent else None

        admin_ids = getattr(config_global, "admin_ids", [])
        if not admin_ids:
            return fallback_msg if sent else None

        from astrbot.api.event import MessageChain

        platform_id = event.unified_msg_origin.split(":")[0]
        error_report = (
            "[AstrMai executor alert]\n"
            f"Target: {event.unified_msg_origin}\n"
            f"Error detail:\n{error_detail}"
        )
        chain = MessageChain().message(error_report)

        for admin_id in admin_ids:
            try:
                admin_umo = f"{platform_id}:FriendMessage:{admin_id}"
                await self.context.send_message(admin_umo, chain)
                logger.debug(f"[Executor] pushed alert to admin {admin_id}")
            except Exception as exc:
                logger.error(f"[Executor] failed to push alert to admin {admin_id}: {exc}")
        return fallback_msg if sent else None


__all__ = ["ConcurrentExecutor"]
