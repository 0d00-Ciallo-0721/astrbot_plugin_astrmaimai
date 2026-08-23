from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
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
from ...infrastructure.runtime.dialog_lane_identity import resolve_dialog_lane_identity
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...infrastructure.gateway.output_guard import validate_visible_output_text
from ...infrastructure.gateway.gateway_exceptions import LLMCascadeFailureException
from ...multimodal.vision_prompt import (
    VISION_SYSTEM_PROMPT,
    VISION_USER_PROMPT,
    normalize_vision_result,
    render_vision_record,
)
from ..contracts.dialog_history_policy import DialogHistoryPolicy
from ..contracts.focus_context import FocusThreadContext, FreshnessState, VisionBundle
from ..contracts.prompt_envelope import PromptEnvelope
from ..contracts.reread import RereadActionRequest
from ..vision_state import (
    classify_vision_failure_text,
    guard_unresolved_image_reply,
    has_valid_image_context,
    reply_mentions_image,
    vision_analysis_observation_facts,
    vision_observation_facts,
)
from ..planning.tool_contracts import (
    TOOL_CAPABILITIES,
    is_model_disclosure_requestable,
    record_tool_lifecycle,
)
from ..planning.tool_disclosure import (
    FAMILY_TO_PACKAGES,
    normalize_requested_packages,
    select_tools_by_names,
    select_tools_by_packages,
)
from .group_actor_consistency import GroupActorConsistencyGuard
from .reply_freshness import ReplyFreshnessMixin, is_stale_reply_reason, resolve_reply_max_age_seconds
from ...infrastructure.runtime.turn_call_ledger import (
    begin_stage,
    clamp_timeout_to_turn_budget,
    finish_stage,
    record_vision_observation,
)


@dataclass(slots=True)
class ToolDisclosureExpansion:
    tools: list[Any]
    requested_packages: list[str] = field(default_factory=list)
    requested_tools: list[str] = field(default_factory=list)
    added_tools: list[str] = field(default_factory=list)
    rejected_requests: list[dict[str, str]] = field(default_factory=list)
    source: str = ""


class ConcurrentExecutor:
    def __init__(
        self,
        context,
        gateway,
        reply_engine,
        evolution_manager,
        config=None,
        runtime_coordinator=None,
        visual_cortex=None,
        image_resolver=None,
    ):
        self.context = context
        self.gateway = gateway
        self.reply_engine = reply_engine
        self.evolution_manager = evolution_manager
        self.config = config if config else gateway.config
        self.runtime_coordinator = runtime_coordinator
        self.visual_cortex = visual_cortex
        self.image_resolver = image_resolver
        self.reread_action_dispatcher = None
        self._chat_locks = {}
        self._chat_thread_locks = {}
        self._chat_pending_count = {}
        self._global_lock = asyncio.Lock()
        self._native_vision_breakers: dict[str, float] = {}

    def refresh_config(self, config) -> None:
        self.config = config

    def bind_reread_action_dispatcher(self, dispatcher) -> None:
        self.reread_action_dispatcher = dispatcher

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
                # OPT-12/TL-03: 原始组件以 extra 保留供只读工具（vision/artifact）
                # 解析——sanitize 后"当前消息"路径必然假阴性（首调"没有发现图片"，
                # 实测一轮浪费 4 次工具调用 21.5s）；该 extra 不回流 prompt
                try:
                    sanitized_event.set_extra(
                        "astrmai_original_message_segments",
                        list(getattr(event.message_obj, "message", None) or []),
                    )
                except Exception:
                    logger.debug("[Executor] original segments preserve degraded", exc_info=True)
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
            "astrmai_tool_intent_contracts",
            "astrmai_tool_invocation_plans",
            "astrmai_tool_contract_outcomes",
            "astrmai_tool_contract_unsatisfied",
            "astrmai_tool_correction_pass_used",
            "astrmai_tool_correction_packages",
            "astrmai_tool_correction_reason",
            "astrmai_tool_second_pass_resolution",
            "astrmai_tool_second_pass_selected_tools",
            "astrmai_tool_second_pass_reason",
            "astrmai_required_tools",
            "astrmai_prepared_required_tools",
            "astrmai_requested_tool_packages",
            "astrmai_requested_tool_names",
            "astrmai_tool_disclosure_requests",
            "astrmai_tool_disclosure_request_source",
            "astrmai_tool_disclosure_rejected_requests",
            "astrmai_hidden_requestable_tools",
            "astrmai_tool_disclosure_expanded_once",
            "astrmai_disclosure_expanded_packages",
            "astrmai_disclosure_expanded_tools",
            "astrmai_second_pass_added_tools",
            "astrmai_pending_actions",
            "astrmai_at_action_verified",
            "astrmai_at_action_target_id",
            "astrmai_at_action_group_id",
            "astrmai_bypass_mood_analysis",
            "astrmai_force_meme",
            "astrmai_tool_clarification_needed",
            "astrmai_tool_clarification_prompt",
            "astrmai_tool_clarification_missing_slots",
            "astrmai_vision_observation",
            "astrmai_vision_observability",
            "astrmai_vision_state",
            "astrmai_vision_tool_disclosed",
            "astrmai_vision_tool_required",
            "astrmai_vision_tool_selected",
            "astrmai_vision_tool_result_status",
            "astrmai_autonomous_vision_need",
            "astrmai_autonomous_vision_reason",
            "astrmai_autonomous_vision_candidate_id",
            "astrmai_reread_request",
            "astrmai_reread_action_dispatcher",
        ):
            value = source_event.get_extra(key, None)
            if value is not None:
                target_event.set_extra(key, value)

    async def _dispatch_reread_request(self, event: AstrMessageEvent) -> bool:
        raw = event.get_extra("astrmai_reread_request", None)
        if not isinstance(raw, dict):
            return False
        dispatcher = event.get_extra("astrmai_reread_action_dispatcher", None) or self.reread_action_dispatcher
        if dispatcher is None or not hasattr(dispatcher, "dispatch"):
            raise RuntimeError("reread_dispatcher_unavailable")
        text = str(raw.get("text", "") or "").strip()
        chat_id = str(raw.get("chat_id", "") or getattr(event, "unified_msg_origin", "") or "").strip()
        if not text or not chat_id:
            raise ValueError("invalid_reread_request")
        request = RereadActionRequest(
            chat_id=chat_id,
            text=text,
            fingerprint=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            trigger_kind=str(raw.get("trigger_kind", "group_reread_active") or "group_reread_active"),
            source_event_ids=tuple(str(item) for item in raw.get("source_event_ids", []) or [] if str(item)),
            explanation=str(raw.get("explanation", "") or ""),
        )
        result = await dispatcher.dispatch(event, request)
        event.set_extra("astrmai_reread_dispatch_status", result.status)
        if result.sent:
            event.set_extra("astrmai_execution_status", "reread_dispatched")
            return True
        if result.status in {"cooldown", "duplicate", "stale", "shutdown"}:
            event.set_extra("astrmai_execution_status", f"skipped_reread_{result.status}")
            return True
        raise RuntimeError(f"reread_dispatch_{result.status}:{result.detail}")

    @staticmethod
    def _record_required_tool_outcomes(event: AstrMessageEvent) -> list[str]:
        raw_plans = event.get_extra("astrmai_tool_invocation_plans", []) or []
        plans = [dict(item) for item in raw_plans if isinstance(item, dict) and item.get("required", True)]
        if not plans:
            plans = [
                {
                    "tool_name": str(name or "").strip(),
                    "family": "",
                    "required": True,
                    "acceptable_statuses": ["success"],
                    "acceptable_source_domains": [],
                    "operation": "",
                }
                for name in event.get_extra("astrmai_required_tools", []) or []
                if str(name or "").strip()
            ]
        if not plans:
            return []
        execution_trace = [
            dict(item)
            for item in event.get_extra("astrmai_tool_execution_trace", []) or []
            if isinstance(item, dict)
        ]
        queued = {
            str(item.get("tool") or "").strip()
            for item in event.get_extra("astrmai_tool_lifecycle_trace", []) or []
            if isinstance(item, dict) and str(item.get("phase") or "") == "action_queued"
        }
        prepared = {
            str(name or "").strip()
            for name in event.get_extra("astrmai_prepared_required_tools", []) or []
        }
        satisfied = queued | prepared
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
        outcomes: list[dict[str, Any]] = []
        for plan in plans:
            tool_name = str(plan.get("tool_name") or "").strip()
            if not tool_name:
                continue
            family = str(plan.get("family") or "").strip()
            acceptable_statuses = {
                str(status or "").strip()
                for status in plan.get("acceptable_statuses", ["success"]) or ["success"]
                if str(status or "").strip()
            }
            acceptable_domains = {
                str(domain or "").strip()
                for domain in plan.get("acceptable_source_domains", []) or []
                if str(domain or "").strip()
            }
            expected_operation = str(plan.get("operation") or "").strip()
            matching = [
                item
                for item in execution_trace
                if str(item.get("tool_name") or "").strip() == tool_name
            ]
            accepted_execution = None
            mismatch_reason = "model_did_not_call_required_tool"
            for item in reversed(matching):
                status = str(item.get("status", "success") or "success").strip()
                source_domain = str(item.get("source_domain") or "").strip()
                operation = str(item.get("operation") or "").strip()
                if status not in acceptable_statuses:
                    mismatch_reason = f"tool_status_{status or 'unknown'}"
                    continue
                if acceptable_domains and source_domain not in acceptable_domains:
                    mismatch_reason = "tool_source_domain_mismatch"
                    continue
                if expected_operation and operation != expected_operation:
                    mismatch_reason = "tool_operation_mismatch"
                    continue
                accepted_execution = item
                break

            is_satisfied = accepted_execution is not None or tool_name in satisfied
            if tool_name == "construct_at_event":
                is_satisfied = bool(verified_at_action)
                if not is_satisfied:
                    mismatch_reason = "at_action_not_verified_in_current_group"
            if is_satisfied:
                record_tool_lifecycle(
                    event,
                    tool_name,
                    "required_tool_outcome",
                    status="satisfied",
                )
                outcome = "satisfied"
                reason = "accepted_tool_result" if accepted_execution is not None else "deterministic_fallback_ready"
            else:
                if tool_name not in missing:
                    missing.append(tool_name)
                record_tool_lifecycle(
                    event,
                    tool_name,
                    "required_tool_outcome",
                    source="model_tool_call",
                    status="missing",
                    reason=mismatch_reason,
                )
                outcome = "unsatisfied"
                reason = mismatch_reason
            observed = accepted_execution or (matching[-1] if matching else {})
            outcomes.append(
                {
                    "tool_name": tool_name,
                    "family": family,
                    "outcome": outcome,
                    "reason": reason,
                    "expected_source_domains": sorted(acceptable_domains),
                    "expected_operation": expected_operation,
                    "acceptable_statuses": sorted(acceptable_statuses),
                    "observed_status": str(observed.get("status") or ""),
                    "observed_source_domain": str(observed.get("source_domain") or ""),
                    "observed_operation": str(observed.get("operation") or ""),
                }
            )
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_required_tool_missing", list(missing))
            event.set_extra("astrmai_tool_contract_outcomes", outcomes)
            event.set_extra("astrmai_tool_contract_unsatisfied", list(missing))
        turn_context = event.get_extra("astrmai_turn_context", None)
        tools_state = getattr(turn_context, "tools", None)
        if tools_state is not None:
            tools_state.contract_outcomes = list(outcomes)
            tools_state.contract_unsatisfied = list(missing)
        return missing

    @staticmethod
    def _record_tool_second_pass_resolution(
        event: AstrMessageEvent,
        resolution: str,
        *,
        reason: str = "",
        selected_tools: list[str] | None = None,
    ) -> None:
        selected = list(
            dict.fromkeys(
                str(name or "").strip()
                for name in (selected_tools or [])
                if str(name or "").strip()
            )
        )
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_tool_second_pass_resolution", str(resolution or ""))
            event.set_extra("astrmai_tool_second_pass_selected_tools", selected)
            event.set_extra("astrmai_tool_second_pass_reason", str(reason or "")[:160])
        turn_context = event.get_extra("astrmai_turn_context", None)
        tools_state = getattr(turn_context, "tools", None)
        if tools_state is not None:
            tools_state.second_pass_resolution = str(resolution or "")
            tools_state.second_pass_selected_tools = selected
            tools_state.second_pass_reason = str(reason or "")[:160]
            added = set(getattr(tools_state, "second_pass_added_tools", []) or [])
            tools_state.second_pass_tool_executed = bool(added & set(selected))

    @staticmethod
    def _executed_tool_names(event: AstrMessageEvent) -> list[str]:
        return list(
            dict.fromkeys(
                str(item.get("tool_name") or "").strip()
                for item in event.get_extra("astrmai_tool_execution_trace", []) or []
                if isinstance(item, dict) and str(item.get("tool_name") or "").strip()
            )
        )

    @staticmethod
    def _missing_tool_resolution(event: AstrMessageEvent, missing_tools: list[str]) -> str:
        missing = {str(name or "").strip() for name in missing_tools if str(name or "").strip()}
        terminal_failures = {"failed", "not_found", "insufficient", "unavailable", "error", "timeout"}
        for item in event.get_extra("astrmai_tool_execution_trace", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("tool_name") or "").strip() not in missing:
                continue
            if str(item.get("status") or "").strip().lower() in terminal_failures:
                return "degraded"
        return "unresolved"

    @staticmethod
    def _request_contract_correction_packages(event: AstrMessageEvent, missing_tools: list[str]) -> list[str]:
        missing = {str(name or "").strip() for name in missing_tools if str(name or "").strip()}
        if not missing:
            return []
        plans = [
            dict(item)
            for item in event.get_extra("astrmai_tool_invocation_plans", []) or []
            if isinstance(item, dict) and str(item.get("tool_name") or "").strip() in missing
        ]
        packages: list[str] = []
        for plan in plans:
            for package in FAMILY_TO_PACKAGES.get(str(plan.get("family") or "").strip(), ()):
                if package not in packages:
                    packages.append(package)
        if not packages:
            return []
        existing = list(event.get_extra("astrmai_requested_tool_packages", []) or [])
        merged = list(normalize_requested_packages([*existing, *packages]))
        event.set_extra("astrmai_requested_tool_packages", merged)
        return packages

    @staticmethod
    def _required_tool_retry_prompt(
        api_prompt: str,
        missing_tools: list[str],
        expanded_packages: Optional[list[str]] = None,
        added_tools: Optional[list[str]] = None,
        invocation_plans: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        tool_list = "、".join(missing_tools)
        disclosure_line = ""
        if expanded_packages:
            disclosure_line = "系统已追加工具包：" + "、".join(expanded_packages) + "。"
        if added_tools:
            disclosure_line += "本次新增工具：" + "、".join(added_tools) + "；回答前必须先调用与用户需求匹配的新增工具。"
        missing_set = {str(name or "").strip() for name in missing_tools if str(name or "").strip()}
        contract_lines: list[str] = []
        for plan in invocation_plans or []:
            if not isinstance(plan, dict):
                continue
            tool_name = str(plan.get("tool_name") or "").strip()
            if not tool_name or tool_name not in missing_set:
                continue
            contract = {
                "tool": tool_name,
                "entity_domain": str(plan.get("entity_domain") or "").strip(),
                "operation": str(plan.get("operation") or "").strip(),
                "target": str(plan.get("target") or "").strip(),
                "arguments": dict(plan.get("prepared_arguments") or {}),
            }
            contract_lines.append(json.dumps(contract, ensure_ascii=False, separators=(",", ":")))
        contract_block = ""
        if contract_lines:
            contract_block = "本轮未满足的结构化调用契约：\n" + "\n".join(contract_lines) + "\n"
        return (
            f"{api_prompt}\n\n"
            "[SYSTEM TOOL ENFORCEMENT]\n"
            "[SYSTEM TOOL CORRECTION]\n"
            f"{disclosure_line}用户本轮意图尚未被对应工具族满足：{tool_list or '上一轮申请的能力'}。"
            f"{contract_block}"
            "工具调用成功不等于任务完成；必须使用与实体域和操作匹配的工具，并依据真实结果回答。"
            "如果工具参数已经足够，请先调用匹配工具；工具返回 not_found 时要如实传达未找到。"
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
            "message_reaction_action": "互动回应",
            "qq_friend_lookup": "读取机器人 QQ 好友事实",
            "self_lore_query": "查询角色设定人物",
        }
        tool_labels = [labels.get(name, name) for name in missing_tools]
        if "space_transition_action" in missing_tools:
            return "我还没能确认要发给谁、具体转达什么，所以没有发送。你把目标和要说的话都告诉我，我再帮你传达。"
        if "construct_at_event" in missing_tools:
            return "我还没能在当前群确认你要叫的人，所以没有发送假 @。请给我对方当前群昵称或 QQ 号。"
        if "qq_friend_lookup" in missing_tools:
            return "我还没能读取机器人自己的 QQ 好友列表，所以不能猜谁是好友。你可以稍后再试，或给我准确 QQ 号让我重新核对。"
        if "self_lore_query" in missing_tools:
            return "我还没能从当前角色设定里查到这个人物，所以不会拿现实好友或聊天记忆来冒充答案。请补充人物名或设定出处。"
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

    def _vision_reply_policy(self) -> str:
        vision_cfg = getattr(self.config, "vision", None)
        raw = str(
            getattr(vision_cfg, "vision_reply_policy", "超时后忽略图片并继续回复")
            or ""
        ).strip()
        if raw in {"必须识别成功后再回复", "require_analysis", "strict"}:
            return "require_analysis"
        return "timeout_fallback"

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
        if event.get_extra("astrmai_final_vision_target", None):
            return False, "final_reply_translation_required", 0.0
        if not self._native_main_reply_vision_enabled():
            return False, "disabled", 0.0
        if self._vision_reply_policy() == "require_analysis":
            return False, "strict_requires_relay_analysis", 0.0
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

    @staticmethod
    def _side_effect_footprint(event: AstrMessageEvent) -> int:
        # OPT-09/TL-04: 只统计真实对外副作用（待提交 QQ 动作 + 跨会话已发送），
        # 不含纯查询工具——gateway 侧旧计数把查询也算副作用，一次查询即禁重试
        if event is None or not hasattr(event, "get_extra"):
            return 0
        pending = event.get_extra("astrmai_pending_actions", []) or []
        sends = event.get_extra("astrmai_cross_session_sends", []) or []
        pending_count = len(pending) if isinstance(pending, list) else 0
        sends_count = len(sends) if isinstance(sends, list) else 0
        return pending_count + sends_count

    def _vision_side_path_timeout_override(self) -> float:
        # OPT-07/RT-05: 单图超时 = min(配置的图片分析超时, turn 剩余预算[留主回复保留额])
        timing = getattr(self.config, "timing", None)
        try:
            configured = float(getattr(timing, "image_analysis_timeout_sec", 90.0) or 90.0)
        except (TypeError, ValueError):
            configured = 90.0
        return clamp_timeout_to_turn_budget(None, max(1.0, configured), reserve_for_reply=True)

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

        # 与 mark_activity 侧保持同一标识空间：优先 turn thread id
        turn = event.get_extra("astrmai_turn_identity", None)
        turn_thread_id = str(
            getattr(turn, "thread_id", "")
            or event.get_extra("astrmai_turn_thread_id", "")
            or ""
        ).strip()
        if turn_thread_id:
            thread_signature = turn_thread_id

        max_age_seconds = resolve_reply_max_age_seconds(self.config)

        return await self.runtime_coordinator.evaluate_reply_freshness(
            chat_id,
            focus_timestamp,
            max_age_seconds=max_age_seconds,
            thread_signature=thread_signature,
            allow_parallel_threads=not bool(event.get_extra("is_private_chat", False)),
        )

    def _executor_lock_wait_timeout(self, event: AstrMessageEvent) -> float:
        timing = getattr(self.config, "timing", None)
        try:
            configured = float(getattr(timing, "executor_lock_wait_timeout_sec", 15.0) or 15.0)
        except (TypeError, ValueError):
            configured = 15.0
        return clamp_timeout_to_turn_budget(event, max(0.1, configured), reserve_for_reply=True)

    @staticmethod
    def _turn_thread_id(event: AstrMessageEvent) -> str:
        turn = event.get_extra("astrmai_turn_identity", None)
        return str(
            getattr(turn, "thread_id", "")
            or event.get_extra("astrmai_turn_thread_id", "")
            or ""
        ).strip()

    async def _acquire_chat_execution_lock(self, chat_id: str, event: AstrMessageEvent | None = None):
        timeout_sec = self._executor_lock_wait_timeout(event) if event is not None else 15.0
        thread_id = self._turn_thread_id(event) if event is not None else ""
        using_runtime_coordinator = self.runtime_coordinator is not None
        if using_runtime_coordinator:
            if timeout_sec <= 0.0:
                return None, True, "queue_timeout"
            try:
                try:
                    acquire = self.runtime_coordinator.try_acquire_executor(
                        chat_id,
                        max_pending=2,
                        thread_id=thread_id,
                    )
                except TypeError:
                    acquire = self.runtime_coordinator.try_acquire_executor(chat_id, max_pending=2)
                chat_lock = await asyncio.wait_for(acquire, timeout=max(0.1, timeout_sec))
            except asyncio.TimeoutError:
                return None, True, "queue_timeout"
            if chat_lock is None:
                return None, True, "too_many_pending"
            return chat_lock, True, ""

        async with self._global_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = asyncio.Lock()
                self._chat_pending_count[chat_id] = 0
            if self._chat_pending_count[chat_id] >= 2:
                return None, False, "too_many_pending"
            self._chat_pending_count[chat_id] += 1
            if not hasattr(self, "_chat_thread_locks"):
                self._chat_thread_locks = {}
            if thread_id:
                thread_locks = self._chat_thread_locks.setdefault(chat_id, {})
                chat_lock = thread_locks.setdefault(thread_id, asyncio.Lock())
            else:
                chat_lock = self._chat_locks[chat_id]
        if timeout_sec <= 0.0:
            acquired = False
        else:
            try:
                await asyncio.wait_for(chat_lock.acquire(), timeout=max(0.1, timeout_sec))
                acquired = True
            except asyncio.TimeoutError:
                acquired = False
        if not acquired:
            async with self._global_lock:
                self._chat_pending_count[chat_id] = max(0, self._chat_pending_count.get(chat_id, 1) - 1)
                if self._chat_pending_count.get(chat_id, 0) == 0:
                    self._chat_locks.pop(chat_id, None)
                    self._chat_pending_count.pop(chat_id, None)
            return None, False, "queue_timeout"
        return chat_lock, False, ""

    async def _release_chat_execution_lock(
        self,
        chat_id: str,
        using_runtime_coordinator: bool,
        chat_lock: Optional[asyncio.Lock],
        event: AstrMessageEvent | None = None,
    ) -> None:
        if chat_lock is not None and chat_lock.locked():
            chat_lock.release()
        if using_runtime_coordinator:
            thread_id = self._turn_thread_id(event) if event is not None else ""
            try:
                await self.runtime_coordinator.release_executor(chat_id, thread_id=thread_id)
            except TypeError:
                await self.runtime_coordinator.release_executor(chat_id)
            return
        async with self._global_lock:
            self._chat_pending_count[chat_id] -= 1
            if self._chat_pending_count[chat_id] == 0:
                self._chat_locks.pop(chat_id, None)
                getattr(self, "_chat_thread_locks", {}).pop(chat_id, None)
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
        return resolve_dialog_lane_identity(event, chat_id)

    def _execution_runtime_values(self, event: AstrMessageEvent, chat_id: str) -> dict[str, Any]:
        bot_id = str(event.get_self_id()) if hasattr(event, "get_self_id") else "SELF_BOT"
        is_fast_mode = event.get_extra("is_fast_mode", False)
        config_max_steps = getattr(self.config.agent, "max_steps", 5)
        tool_tier = str(event.get_extra("astrmai_tool_tier", "full") or "full")
        max_steps = max(5, config_max_steps)
        timing = getattr(self.config, "timing", None)
        fast_timeout = getattr(timing, "fast_mode_execution_timeout_sec", 15)
        timeout = max(1, int(fast_timeout or 15)) if is_fast_mode else self.config.agent.timeout
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
    ) -> ToolDisclosureExpansion:
        expansion = ToolDisclosureExpansion(tools=list(current_tools or []))
        if not hasattr(event, "get_extra") or not hasattr(event, "set_extra"):
            return expansion
        if event.get_extra("astrmai_tool_disclosure_expanded_once", False):
            return expansion
        conversation = getattr(self.config, "conversation", None)
        if not bool(getattr(conversation, "tool_disclosure_allow_second_pass", True)):
            return expansion
        requested = normalize_requested_packages(event.get_extra("astrmai_requested_tool_packages", []))
        raw_requested_names = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in event.get_extra("astrmai_requested_tool_names", []) or []
                if str(item or "").strip()
            )
        )
        rejected = [
            dict(item)
            for item in event.get_extra("astrmai_tool_disclosure_rejected_requests", []) or []
            if isinstance(item, dict)
        ]
        requested_names: list[str] = []
        for name in raw_requested_names:
            if is_model_disclosure_requestable(name):
                requested_names.append(name)
            else:
                rejected.append(
                    {
                        "field": "needed_tool",
                        "value": name,
                        "reason": "model_disclosure_requires_readonly_tool",
                    }
                )
        expansion.requested_tools = list(requested_names)
        expansion.rejected_requests = list(rejected)
        expansion.source = str(event.get_extra("astrmai_tool_disclosure_request_source", "") or "")
        if not requested and not requested_names:
            event.set_extra("astrmai_tool_disclosure_rejected_requests", rejected)
            turn_context = event.get_extra("astrmai_turn_context", None)
            tools_state = getattr(turn_context, "tools", None)
            if tools_state is not None:
                tools_state.disclosure_request_source = expansion.source
                tools_state.disclosure_requested_tools = []
                tools_state.disclosure_rejected_requests = list(rejected)
            return expansion
        allowed = set(normalize_requested_packages(event.get_extra("astrmai_disclosure_second_pass_packages", [])))
        packages = [package for package in requested if package in allowed]
        for package in requested:
            if package not in allowed:
                rejected.append(
                    {
                        "field": "needed_package",
                        "value": package,
                        "reason": "package_not_allowed_this_turn",
                    }
                )
        expansion.requested_packages = list(packages)
        expansion.rejected_requests = list(rejected)
        event.set_extra("astrmai_tool_disclosure_rejected_requests", rejected)
        turn_context = event.get_extra("astrmai_turn_context", None)
        tools_state = getattr(turn_context, "tools", None)
        if tools_state is not None:
            tools_state.disclosure_request_source = expansion.source
            tools_state.disclosure_requested_tools = list(requested_names)
            tools_state.disclosure_rejected_requests = list(rejected)
        if not packages and not requested_names:
            return expansion
        hidden_tools = list(getattr(event, "_astrmai_disclosure_hidden_tools", []) or [])
        if not hidden_tools:
            return expansion
        max_extra = max(1, int(getattr(conversation, "tool_disclosure_max_tools_task", 16) or 16))
        additions = select_tools_by_packages(
            hidden_tools,
            packages,
            name_resolver=self._tool_name,
            max_tools=max_extra,
        )
        exact_additions = select_tools_by_names(
            hidden_tools,
            requested_names,
            name_resolver=self._tool_name,
        )
        merged_additions: list[Any] = []
        seen_addition_names: set[str] = set()
        for tool in [*additions, *exact_additions]:
            name = self._tool_name(tool)
            if not name or name in seen_addition_names or not is_model_disclosure_requestable(name):
                continue
            seen_addition_names.add(name)
            merged_additions.append(tool)
        additions = merged_additions[:max_extra]
        if not additions:
            return expansion
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
            return expansion
        expansion.tools = merged
        expansion.added_tools = list(added_names)
        event.set_extra("astrmai_tool_disclosure_expanded_once", True)
        event.set_extra("astrmai_disclosure_expanded_packages", packages)
        event.set_extra("astrmai_disclosure_expanded_tools", added_names)
        event.set_extra("astrmai_second_pass_added_tools", added_names)
        event.set_extra("astrmai_tool_disclosure_rejected_requests", rejected)
        if tools_state is not None:
            tools_state.disclosure_expanded_packages = list(packages)
            tools_state.disclosure_request_source = expansion.source
            tools_state.disclosure_requested_tools = list(requested_names)
            tools_state.disclosure_rejected_requests = list(rejected)
            tools_state.second_pass_added_tools = list(added_names)
            tools_state.filtered_tools = [self._tool_name(tool) for tool in merged if self._tool_name(tool)]
            tools_state.record_step(
                "executor.tool_disclosure_second_pass",
                [self._tool_name(tool) for tool in current_tools or []],
                tools_state.filtered_tools,
                "requested_packages(" + ",".join(packages) + ");requested_tools(" + ",".join(requested_names) + ")",
            )
        for tool_name in added_names:
            record_tool_lifecycle(
                event,
                tool_name,
                "disclosed_second_pass",
                source="bot_capability_lookup",
                status="available",
                reason="requested_second_pass",
            )
        exact_required = [name for name in added_names if name in requested_names]
        if exact_required:
            required_tools = list(event.get_extra("astrmai_required_tools", []) or [])
            invocation_plans = [
                dict(item)
                for item in event.get_extra("astrmai_tool_invocation_plans", []) or []
                if isinstance(item, dict)
            ]
            planned_names = {
                str(item.get("tool_name") or "").strip()
                for item in invocation_plans
            }
            for name in exact_required:
                if name not in required_tools:
                    required_tools.append(name)
                if name in planned_names:
                    continue
                spec = TOOL_CAPABILITIES.get(name)
                invocation_plans.append(
                    {
                        "tool_name": name,
                        "family": str(getattr(spec, "family", "") or ""),
                        "source": "model_disclosure_request",
                        "required": True,
                        "deterministic_fallback": False,
                        "reason": "model_requested_exact_readonly_tool",
                        "entity_domain": "",
                        "operation": "",
                        "target": "",
                        "prepared_arguments": {},
                        "acceptable_statuses": ["success", "not_found"],
                        "acceptable_source_domains": [],
                    }
                )
            event.set_extra("astrmai_required_tools", required_tools)
            event.set_extra("astrmai_tool_invocation_plans", invocation_plans)
            if tools_state is not None:
                tools_state.required_tools = list(required_tools)
                tools_state.invocation_plans = list(invocation_plans)
        return expansion

    async def _inject_direct_vision_context(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        model_prompt: str,
        system_prompt: str,
        vision_bundle: VisionBundle,
    ) -> tuple[str, str]:
        if not vision_bundle.direct_image_urls:
            skip_reason = str(
                event.get_extra("vision_direct_skip_reason", "") or "not_direct_path"
            )
            self._mark_vision_direct_state(
                event,
                invoked=False,
                outcome="skipped",
                skip_reason=skip_reason,
            )
            observation = {
                "policy": self._vision_reply_policy(),
                "outcome": "skipped",
                "image_count": 0,
                "resolved_count": 0,
                "analyzed_count": 0,
                "failed_count": 0,
                "timeout_count": 0,
                "attempt_count": 0,
                "image_source": [],
                "image_resolve_status": "skipped",
                "vision_barrier_status": "skipped",
                "vision_wait_ms": 0,
                "vision_timeout_ms": 0,
                "vision_fallback": False,
                "visual_memory_ids": [],
                "scope": "private" if "FriendMessage" in chat_id else "group",
                "vision_path": "direct",
                "vision_call_status": "skipped",
                "visual_memory_write_status": "not_applicable",
                "prompt_injected": False,
                "fallback_reason": "",
                "skip_reason": skip_reason,
                "final_status": "skipped",
            }
            event.set_extra("astrmai_vision_observability", observation)
            record_vision_observation(event, observation)
            return model_prompt, system_prompt

        import base64
        import io
        import os
        import tempfile
        from PIL import Image

        started_at = monotonic()
        policy = self._vision_reply_policy()
        event.set_extra("astrmai_vision_failure_disposition", "")
        event.set_extra("astrmai_vision_independent_text", "")
        event.set_extra("astrmai_vision_required_failed", False)
        original_direct_image_urls = list(vision_bundle.direct_image_urls or [])
        resolver_failed_count = 0
        resolver_strategy = ""
        resolver_failure_reasons: list[str] = []
        final_target = event.get_extra("astrmai_final_vision_target", None)
        if final_target and self.image_resolver is not None:
            resolve_timeout = min(
                self._vision_side_path_timeout_override(),
                max(
                    0.1,
                    float(
                        getattr(
                            getattr(self.config, "timing", None),
                            "image_resolve_timeout_sec",
                            15.0,
                        )
                        or 15.0
                    ),
                ),
            )
            try:
                resolution = await asyncio.wait_for(
                    self.image_resolver.resolve_candidate(event, final_target),
                    timeout=resolve_timeout,
                )
            except asyncio.TimeoutError:
                resolution = None
                resolver_failed_count = 1
                resolver_failure_reasons.append("resolve_timeout")
                event.set_extra("astrmai_final_vision_resolve_status", "timeout")
            except Exception as exc:
                resolution = None
                resolver_failed_count = 1
                event.set_extra("astrmai_final_vision_resolve_status", f"failed:{type(exc).__name__}")
                logger.warning(
                    f"[{chat_id}] final reply image resolver degraded: {type(exc).__name__}"
                )
            if resolution is not None and resolution.images:
                resolved_image = resolution.images[-1]
                vision_bundle.direct_image_urls = [resolved_image.local_path]
                resolver_strategy = str(getattr(resolved_image, "strategy", "") or "direct")
                event.set_extra("astrmai_final_vision_resolve_status", "success")
                event.set_extra("astrmai_final_vision_resolver_strategy", resolver_strategy)
            else:
                if resolution is not None:
                    for item in list(getattr(resolution, "failure_details", []) or []):
                        if not isinstance(item, dict):
                            continue
                        reason = str(item.get("reason") or "").strip()
                        if reason and reason not in resolver_failure_reasons:
                            resolver_failure_reasons.append(reason)
                vision_bundle.direct_image_urls = []
                resolver_failed_count = max(1, resolver_failed_count)
                if not event.get_extra("astrmai_final_vision_resolve_status", ""):
                    event.set_extra("astrmai_final_vision_resolve_status", "failed")
        event.set_extra(
            "astrmai_vision_resolve_failure_reasons",
            list(resolver_failure_reasons),
        )
        configured_max_images = getattr(
            getattr(self.config, "vision", None),
            "max_images_per_turn",
            1,
        )
        try:
            max_images = max(1, min(int(configured_max_images or 1), 8))
        except (TypeError, ValueError):
            max_images = 1
        all_direct_image_urls = original_direct_image_urls or list(vision_bundle.direct_image_urls or [])
        final_reply_limit = 1 if final_target else max_images
        direct_image_urls = list(vision_bundle.direct_image_urls or [])[:final_reply_limit]
        dropped_image_count = max(0, len(all_direct_image_urls) - len(direct_image_urls) - resolver_failed_count)
        event.set_extra("astrmai_vision_owner", "direct")
        event.set_extra("astrmai_vision_state", "analysis_pending")
        logger.info(f"[{chat_id}] vision direct path triggered in executor")
        self._mark_vision_direct_state(event, invoked=True, outcome="skipped")
        vision_descriptions: list[str] = []
        visual_memory_ids: list[str] = []
        asset_ids: list[str] = []
        model_ids: list[str] = []
        image_sources: list[str] = []
        failed_count = resolver_failed_count
        resolved_count = 0
        timeout_count = 0
        attempt_count = 0
        cache_hit_count = 0
        cache_miss_count = 0
        singleflight_wait_count = 0
        binding_count = 0
        asset_storage_statuses: list[str] = []
        prompt_versions: list[str] = []
        failure_stages: list[str] = ["resolve"] if resolver_failed_count else []
        skip_reasons: list[str] = ["resolver_failed"] if resolver_failed_count else []
        saw_invalid_output = False
        saw_exception = False
        analysis_facts: dict[str, Any] = {}
        message_obj = getattr(event, "message_obj", None)
        message_id = str(
            (final_target or {}).get("message_id", "") if isinstance(final_target, dict) else ""
        ) or str(
            getattr(message_obj, "message_id", "")
            or getattr(message_obj, "id", "")
            or getattr(event, "message_id", "")
            or ""
        )
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        record_vision_observation(
            event,
            {
                **vision_observation_facts(event),
                "vision_state": "analysis_pending",
                "direct_vision_scheduled": True,
                "image_count": len(all_direct_image_urls),
                "dropped_image_count": dropped_image_count,
            },
        )
        for image_index, url_or_path in enumerate(direct_image_urls):
            temp_file_path = None
            created_temp_file_path = None
            item_succeeded = False
            source_text = str(url_or_path or "").strip()
            source_lower = source_text.lower()
            if source_lower.startswith("data:"):
                source_category = "data_uri"
            elif source_lower.startswith(("http://", "https://")):
                source_category = "url"
            elif source_text:
                source_category = "local_file"
            else:
                source_category = "unknown"
            if source_category not in image_sources:
                image_sources.append(source_category)
            try:
                image_bytes = None
                if source_text.startswith("data:image"):
                    _, encoded = source_text.split(",", 1)
                    image_bytes = base64.b64decode(encoded)
                elif os.path.exists(source_text):
                    temp_file_path = source_text
                elif source_text.startswith("http"):
                    if "resolve" not in failure_stages:
                        failure_stages.append("resolve")
                    if "remote_fetch_disabled" not in skip_reasons:
                        skip_reasons.append("remote_fetch_disabled")
                    logger.info(
                        f"[{chat_id}] vision direct image skipped: remote_fetch_disabled"
                    )
                elif source_text:
                    if "resolve" not in failure_stages:
                        failure_stages.append("resolve")
                    if "source_unavailable" not in skip_reasons:
                        skip_reasons.append("source_unavailable")

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
                    resolved_count += 1
                    vision_timeout = self._vision_side_path_timeout_override()
                    if vision_timeout <= 0.5:
                        saw_exception = True
                        timeout_count += 1
                        if "budget" not in failure_stages:
                            failure_stages.append("budget")
                        if "turn_budget_exhausted" not in skip_reasons:
                            skip_reasons.append("turn_budget_exhausted")
                        logger.warning(f"[{chat_id}] vision side-path skipped: turn budget exhausted")
                        continue
                    attempt_count += 1
                    record_vision_observation(
                        event,
                        {
                            "direct_vision_resolve_status": "success",
                            "direct_vision_model_called": True,
                            "direct_vision_model_status": "pending",
                        },
                    )
                    picid = hashlib.sha256(
                        f"{chat_id}:{source_text}".encode("utf-8", errors="ignore")
                    ).hexdigest()
                    if self.visual_cortex is not None and hasattr(self.visual_cortex, "analyze_image_path"):
                        try:
                            vision_call = self.visual_cortex.analyze_image_path(
                                picid,
                                temp_file_path,
                                scope_id=chat_id,
                                timeout_override=vision_timeout,
                                binding_context={
                                    "chat_id": chat_id,
                                    "message_id": message_id,
                                    "sender_id": sender_id,
                                    "image_index": image_index,
                                    "source_ref": source_text,
                                },
                            )
                        except TypeError:
                            vision_call = self.visual_cortex.analyze_image_path(
                                picid,
                                temp_file_path,
                                scope_id=chat_id,
                                timeout_override=vision_timeout,
                            )
                    else:
                        vision_call = self.gateway.call_vision_task(
                            image_data=temp_file_path,
                            prompt=VISION_USER_PROMPT,
                            system_prompt=VISION_SYSTEM_PROMPT,
                            lane_key=LaneKey(subsystem="sys1", task_family="vision", scope_id=chat_id),
                            base_origin=chat_id,
                            timeout_override=vision_timeout,
                        )
                    result_dict = await asyncio.wait_for(vision_call, timeout=vision_timeout)
                    payload, invalid_reason = normalize_vision_result(result_dict)
                    if payload is None:
                        saw_invalid_output = True
                        if "analysis" not in failure_stages:
                            failure_stages.append("analysis")
                        if "invalid_model_output" not in skip_reasons:
                            skip_reasons.append("invalid_model_output")
                        logger.warning(f"[{chat_id}] vision side-path invalid output: {invalid_reason}")
                        continue
                    item_succeeded = True
                    analysis_facts = vision_analysis_observation_facts(result_dict)
                    model_id = str(
                        (result_dict or {}).get("_model_id", "")
                        or (result_dict or {}).get("_vision_model_id", "")
                    )
                    if model_id and model_id not in model_ids:
                        model_ids.append(model_id)
                    if self.visual_cortex is not None:
                        memory_id = str(
                            (result_dict or {}).get("_visual_memory_id", "")
                            or f"{chat_id}:{picid}"
                        )
                        if memory_id not in visual_memory_ids:
                            visual_memory_ids.append(memory_id)
                        asset_id = str((result_dict or {}).get("_asset_id", "") or "")
                        if asset_id and asset_id not in asset_ids:
                            asset_ids.append(asset_id)
                        cache_kind = str(
                            (result_dict or {}).get("_cache_kind", "miss") or "miss"
                        )
                        if bool((result_dict or {}).get("_cache_hit", False)):
                            cache_hit_count += 1
                        elif cache_kind == "miss":
                            cache_miss_count += 1
                        if bool((result_dict or {}).get("_singleflight_join", False)):
                            singleflight_wait_count += 1
                        if (
                            str((result_dict or {}).get("_binding_status", "") or "")
                            == "persisted"
                        ):
                            binding_count += 1
                        storage_status = str(
                            (result_dict or {}).get("_asset_storage_status", "") or ""
                        )
                        if storage_status and storage_status not in asset_storage_statuses:
                            asset_storage_statuses.append(storage_status)
                        prompt_version = str(
                            (result_dict or {}).get("_prompt_version", "") or ""
                        )
                        if prompt_version and prompt_version not in prompt_versions:
                            prompt_versions.append(prompt_version)
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
            except asyncio.TimeoutError:
                saw_exception = True
                timeout_count += 1
                if "analysis" not in failure_stages:
                    failure_stages.append("analysis")
                if "hard_timeout" not in skip_reasons:
                    skip_reasons.append("hard_timeout")
                logger.warning(f"[{chat_id}] vision side-path hard timeout")
            except Exception as exc:
                saw_exception = True
                if "analysis" not in failure_stages:
                    failure_stages.append("analysis")
                attempted_models, failure_reason, failure_kind = self._extract_cascade_failure_meta(exc)
                normalized_failure_reason = str(failure_reason or failure_kind or "").strip()
                if (
                    normalized_failure_reason
                    and normalized_failure_reason not in skip_reasons
                ):
                    skip_reasons.append(normalized_failure_reason)
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
                if not item_succeeded:
                    failed_count += 1

        prompt_injected = bool(vision_descriptions)
        if vision_descriptions:
            heading = "[最新图片转述]\n" if final_target else ""
            vision_inject = "\n\n" + heading + "\n".join(vision_descriptions)
            model_prompt += vision_inject
            self._mark_vision_direct_state(
                event,
                invoked=True,
                outcome="success",
                details=f"descriptions={len(vision_descriptions)}",
            )
            event.set_extra(
                "astrmai_vision_state",
                "cached_result" if cache_hit_count and cache_miss_count == 0 else "analysis_ready",
            )
        elif saw_invalid_output:
            self._mark_vision_direct_state(
                event,
                invoked=True,
                outcome="invalid_output",
                details="all_descriptions_rejected",
            )
            event.set_extra("astrmai_vision_state", "analysis_failed")
        elif saw_exception:
            if not event.get_extra("vision_direct_failure_reason"):
                self._mark_vision_direct_state(
                    event,
                    invoked=True,
                    outcome="exception",
                )
            event.set_extra("astrmai_vision_state", "analysis_failed")
        else:
            self._mark_vision_direct_state(
                event,
                invoked=True,
                outcome="exception",
                details="no_usable_image_input",
            )
            event.set_extra("astrmai_vision_state", "analysis_failed")

        fallback_reason = ""
        failure_disposition = ""
        if failed_count and not vision_descriptions:
            user_text = str(getattr(event, "message_str", "") or "").strip()
            if vision_bundle.is_image_only:
                text_kind, independent_text = "image_only", ""
            else:
                text_kind, independent_text = classify_vision_failure_text(user_text)
            origin = str(getattr(event, "unified_msg_origin", "") or "")
            is_private = "FriendMessage" in origin
            is_direct = bool(
                is_private
                or event.get_extra("astrmai_at_bot_wakeup", False)
                or event.get_extra("astrmai_group_direct_wakeup", False)
                or event.get_extra("astrmai_cross_message_vision_bound", False)
            )
            image_dependent = text_kind in {"image_dependent", "ambiguous"}
            if text_kind == "independent_text" and independent_text:
                failure_disposition = "continue_text_only"
                event.set_extra("astrmai_vision_independent_text", independent_text)
                if user_text:
                    text_offset = model_prompt.rfind(user_text)
                    if text_offset >= 0:
                        before = model_prompt[:text_offset]
                        after = model_prompt[text_offset + len(user_text) :]
                        before = re.sub(
                            r"\[(?:图片|image)\](?P<spacing>[ \t]*(?:\r?\n[ \t]*)?)$",
                            lambda match: "\n" if "\n" in match.group("spacing") else "",
                            before,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                        after = re.sub(
                            r"^(?P<spacing>[ \t]*(?:\r?\n[ \t]*)?)\[(?:图片|image)\]",
                            lambda match: "\n" if "\n" in match.group("spacing") else "",
                            after,
                            count=1,
                            flags=re.IGNORECASE,
                        )
                        model_prompt = before + independent_text + after
                model_prompt += (
                    "\n\n[媒体失败处置] 图片内容不可用。只回答以下独立文字任务："
                    f"{independent_text}。禁止猜测或回答图片相关部分。"
                )
                system_prompt += (
                    "\n\n[媒体状态约束] 当前图片内容不可用；只处理已提取的独立文字任务，"
                    "不要猜测图片，也不要提及加载、转圈或看不清。"
                )
            elif is_direct or image_dependent or event.get_extra(
                "astrmai_user_asked_about_image", False
            ):
                failure_disposition = "notify_failure"
            else:
                failure_disposition = "suppress_passive_group"

            fallback_reason = failure_disposition
            event.set_extra("astrmai_vision_failure_text_kind", text_kind)
            event.set_extra("astrmai_vision_failure_disposition", failure_disposition)
            event.set_extra(
                "astrmai_vision_required_failed",
                bool(policy == "require_analysis" and failure_disposition == "notify_failure"),
            )
            media_only_failure = bool(vision_bundle.is_image_only or text_kind == "image_only")
            event.set_extra("astrmai_media_status_nonsemantic", True)
            event.set_extra("astrmai_media_only_failure", media_only_failure)
            event.set_extra(
                "astrmai_media_status",
                {
                    "status": "unavailable",
                    "owner": "direct",
                    "reason": fallback_reason,
                    "image_count": len(all_direct_image_urls),
                    "image_only": media_only_failure,
                },
            )

        elapsed_ms = int((monotonic() - started_at) * 1000)
        if failure_disposition == "notify_failure" and policy == "require_analysis":
            observation_outcome = "required_failed"
        elif failed_count and vision_descriptions:
            observation_outcome = "partial_fallback"
        elif failed_count:
            observation_outcome = "fallback"
        else:
            observation_outcome = "success"
        observation = {
            **vision_observation_facts(event),
            **analysis_facts,
            "policy": policy,
            "outcome": observation_outcome,
            "image_count": len(all_direct_image_urls),
            "raw_image_count": int(
                event.get_extra("astrmai_image_raw_component_count", 0) or 0
            ),
            "candidate_ref_count": len(
                list((final_target or {}).get("candidate_refs", []) or [])
                if isinstance(final_target, dict)
                else []
            ),
            "resolved_count": resolved_count,
            "analyzed_count": len(vision_descriptions),
            "failed_count": failed_count,
            "timeout_count": timeout_count,
            "attempt_count": attempt_count,
            "vision_model_attempt_count": attempt_count,
            "image_source": image_sources,
            "image_resolve_status": "failed" if failed_count and not vision_descriptions else "success",
            "vision_barrier_status": "failed" if failed_count else "completed",
            "vision_wait_ms": elapsed_ms,
            "vision_timeout_ms": elapsed_ms if timeout_count else 0,
            "vision_fallback": bool(failed_count and policy != "require_analysis"),
            "visual_memory_id": visual_memory_ids[0] if visual_memory_ids else "",
            "visual_memory_ids": visual_memory_ids,
            "scope": "private" if "FriendMessage" in chat_id else "group",
            "vision_path": "direct",
            "vision_call_status": "failed" if failed_count else "success",
            "visual_memory_write_status": (
                "persisted_or_cache_hit"
                if visual_memory_ids
                else "not_available"
            ),
            "prompt_injected": prompt_injected,
            "fallback_reason": fallback_reason,
            "failure_disposition": failure_disposition,
            "resolve_failure_reasons": resolver_failure_reasons,
            "selected_message_id": str(
                (final_target or {}).get("message_id", "")
                if isinstance(final_target, dict)
                else ""
            ),
            "selected_sender_id": str(
                (final_target or {}).get("sender_id", "")
                if isinstance(final_target, dict)
                else ""
            ),
            "selected_pairing_mode": str(
                (final_target or {}).get("pairing_mode", "")
                if isinstance(final_target, dict)
                else ""
            ),
            "cache_hit_count": cache_hit_count,
            "cache_miss_count": cache_miss_count,
            "singleflight_wait_count": singleflight_wait_count,
            "asset_ids": asset_ids,
            "binding_count": binding_count,
            "failure_stage": ",".join(failure_stages),
            "skip_reason": ",".join(skip_reasons),
            "model_ids": model_ids,
            "analysis_prompt_version": prompt_versions[0] if prompt_versions else "",
            "asset_storage_status": ",".join(asset_storage_statuses),
            "resolver_strategy": resolver_strategy,
            "final_status": observation_outcome,
            "direct_vision_scheduled": True,
            "direct_vision_resolve_status": "success" if resolved_count else "failed",
            "direct_vision_model_called": bool(attempt_count),
            "direct_vision_model_status": "success" if vision_descriptions else "failed",
            "direct_vision_elapsed_ms": elapsed_ms,
            "direct_vision_injected": prompt_injected,
            "dropped_image_count": dropped_image_count,
        }
        event.set_extra("astrmai_vision_observability", observation)
        record_vision_observation(event, observation)
        return model_prompt, system_prompt

    async def _check_pre_model_freshness(self, event: AstrMessageEvent, chat_id: str, label: str) -> bool:
        freshness_state, freshness_reason = await self._evaluate_execution_freshness(event, chat_id)
        if freshness_state == FreshnessState.EXPIRED:
            # 预检终止也要落 stale 观测字段，否则 trace 的 stale_category 为空无法归因
            ReplyFreshnessMixin._record_freshness_observation(event, freshness_state, freshness_reason)
            logger.info(f"[{chat_id}] stop expired {label}: {freshness_reason}")
            return False
        return True

    async def _send_required_vision_failure(
        self,
        event: AstrMessageEvent,
        chat_id: str,
    ) -> Optional[str]:
        text = "这张图片暂时没有识别成功，我现在无法确认图片内容，请稍后再发一次。"
        if bool(event.get_extra("astrmai_vision_failure_notice_sent", False)):
            return text
        sent = False
        try:
            artifact = await self.reply_engine.handle_reply(event, text, chat_id)
            sent = bool(getattr(artifact, "sent", False)) if artifact is not None else True
        except Exception as exc:
            logger.warning(f"[{chat_id}] required vision fallback send failed: {exc}")
        event.set_extra("astrmai_vision_failure_notice_sent", sent)
        event.set_extra(
            "astrmai_execution_status",
            "skipped_vision_required" if sent else "fatal_no_send",
        )
        return text if sent else None

    async def _finalize_reply(self, event: AstrMessageEvent, chat_id: str, bot_id: str, reply_text: str, *, trace_mode: str, model: str) -> Optional[str]:
        valid_image_context = has_valid_image_context(event)
        mentions_image = reply_mentions_image(reply_text)
        guard_enabled = bool(
            getattr(
                getattr(self.config, "vision", None),
                "ignore_placeholder_without_question",
                True,
            )
        )
        reply_text, image_guard_action, image_guard_reason = guard_unresolved_image_reply(
            reply_text,
            user_text=str(getattr(event, "message_str", "") or ""),
            has_valid_image_context=valid_image_context,
            enabled=guard_enabled,
        )
        event.set_extra("astrmai_image_reply_guard_action", image_guard_action)
        event.set_extra("astrmai_image_reply_guard_reason", image_guard_reason)
        event.set_extra("astrmai_reply_guard_action", image_guard_action)
        record_vision_observation(
            event,
            {
                **vision_observation_facts(event),
                "reply_mentions_image": mentions_image,
                "has_valid_image_context": valid_image_context,
                "image_reply_blocked": image_guard_action in {"repaired", "suppressed"},
                "reply_guard_action": image_guard_action,
            },
        )
        if image_guard_action == "suppressed":
            event.set_extra("astrmai_execution_status", "suppressed_reply_guard")
            logger.warning(f"[{chat_id}] suppressed reply emptied by unresolved-image guard")
            return None
        if image_guard_action == "repaired":
            logger.warning(f"[{chat_id}] removed unrequested unresolved-image claim from reply")
        actor_guard = GroupActorConsistencyGuard.inspect_and_repair(event, reply_text)
        reply_text = actor_guard.text
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_actor_guard_action", actor_guard.action)
            event.set_extra("astrmai_actor_guard_reason", actor_guard.reason)
            event.set_extra("astrmai_actor_guard_current_id", actor_guard.current_actor_id)
            event.set_extra("astrmai_actor_guard_current_name", actor_guard.current_actor_name)
            event.set_extra(
                "astrmai_actor_guard_foreign_names",
                list(actor_guard.foreign_actor_names),
            )
        if actor_guard.action == "repaired":
            logger.warning(
                f"[{chat_id}] repaired foreign group addressee "
                f"current={actor_guard.current_actor_name or actor_guard.current_actor_id} "
                f"foreign={actor_guard.foreign_actor_names}"
            )
        debug_trace(
            event,
            "execution.actor_consistency",
            action=actor_guard.action,
            reason=actor_guard.reason,
            current_actor_id=actor_guard.current_actor_id,
            foreign_actor_names=list(actor_guard.foreign_actor_names),
        )
        artifact = await self.reply_engine.handle_reply(event, reply_text, chat_id)
        sent = bool(getattr(artifact, "sent", False)) if artifact is not None else True
        if not sent:
            metadata = getattr(artifact, "metadata", {}) or {}
            send_status = str(metadata.get("send_status", "") or "")
            blocked_reason = str(getattr(artifact, "blocked_reason", "") or "")
            if is_stale_reply_reason(blocked_reason):
                event.set_extra("astrmai_execution_status", "stale_drop")
            elif send_status == "duplicate_blocked":
                event.set_extra("astrmai_execution_status", "duplicate_blocked")
            else:
                event.set_extra("astrmai_execution_status", "send_failed")
                raise RuntimeError(blocked_reason or send_status or "visible reply was not sent")
            return None
        committed_turn = event.get_extra("astrmai_committed_bot_turn", None)
        committed_text = str(
            getattr(committed_turn, "persistable_text", "")
            or getattr(artifact, "persistable_text", "")
            or reply_text
        ).strip()
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
            reply_preview=preview_text(committed_text, 120),
        )
        return committed_text

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
                if str(event.get_extra("astrmai_execution_status", "") or "") == "queue_timeout":
                    debug_trace(
                        event,
                        "execution.executor.queue_timeout_stop",
                        mode="chat",
                        stage=str(event.get_extra("astrmai_queue_timeout_stage", "") or ""),
                        model=provider_id,
                    )
                    return None
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
        # OPT-09/TL-04: 级联重试前记录真实副作用基线——工具已发过私聊/戳人/表情后
        # 换模型整轮重跑会重复真实动作（space_transition 去重键含精确文本，跨模型
        # 措辞不同必失配）
        side_effect_baseline = self._side_effect_footprint(event)
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
                missing_required = self._record_required_tool_outcomes(event)
                auto_packages = self._request_contract_correction_packages(event, missing_required)
                expansion = self._expand_tools_for_disclosure_request(event, tools)
                expansion_markers = [
                    *expansion.requested_packages,
                    *(f"tool:{name}" for name in expansion.added_tools if name in expansion.requested_tools),
                ]
                correction_packages = list(dict.fromkeys([*auto_packages, *expansion_markers]))
                needs_correction = bool(missing_required or expansion.added_tools)
                if needs_correction:
                    tools = expansion.tools if expansion.added_tools else tools
                    missing_set = set(missing_required)
                    correction_tools = list(tools)
                    if missing_set:
                        correction_tools = [
                            tool
                            for tool in tools
                            if self._tool_name(tool) in missing_set | {"bot_capability_lookup"}
                        ]
                    if not correction_tools:
                        correction_tools = list(tools)
                    for tool_name in missing_required:
                        record_tool_lifecycle(
                            event,
                            tool_name,
                            "required_tool_retry",
                            source="executor_contract_correction",
                            status="started",
                        )
                    correction_reason = (
                        "unsatisfied_tool_contracts(" + ",".join(missing_required) + ")"
                        if missing_required
                        else "model_requested_disclosure"
                    )
                    event.set_extra("astrmai_tool_correction_pass_used", True)
                    event.set_extra("astrmai_tool_correction_packages", correction_packages)
                    event.set_extra("astrmai_tool_correction_reason", correction_reason)
                    turn_context = event.get_extra("astrmai_turn_context", None)
                    tools_state = getattr(turn_context, "tools", None)
                    if tools_state is not None:
                        tools_state.correction_pass_used = True
                        tools_state.correction_packages = list(correction_packages)
                        tools_state.correction_reason = correction_reason
                    self._record_tool_second_pass_resolution(
                        event,
                        "unresolved",
                        reason="correction_started",
                        selected_tools=self._executed_tool_names(event),
                    )
                    self._sync_execution_event_trace(event, execution_event)
                    result = await self.gateway.tool_chat_in_lane_result(
                        lane_key=runtime["dialog_lane_key"],
                        base_origin=runtime["dialog_base_origin"],
                        event=execution_event,
                        prompt=self._required_tool_retry_prompt(
                            api_prompt,
                            missing_required,
                            correction_packages,
                            expansion.added_tools,
                            list(event.get_extra("astrmai_tool_invocation_plans", []) or []),
                        ),
                        system_prompt=system_prompt,
                        tools=ToolSet(correction_tools),
                        models=[provider_id],
                        max_steps=max(2, runtime["max_steps"]),
                        timeout=runtime["timeout"],
                        image_urls=image_urls,
                        prefix_hash=runtime["prefix_hash"],
                        raw_user_text=runtime["raw_user_text"],
                    )
                    self._sync_execution_event_trace(execution_event, event)
                    missing_required = self._record_required_tool_outcomes(event)
                executed_after_correction = set(self._executed_tool_names(event))
                expanded_tool_not_used = bool(
                    expansion.added_tools
                    and not (set(expansion.added_tools) & executed_after_correction)
                )
                if expanded_tool_not_used and not missing_required:
                    self._record_tool_second_pass_resolution(
                        event,
                        "unresolved",
                        reason="second_pass_added_tool_not_executed",
                        selected_tools=list(executed_after_correction),
                    )
                    if not str(event.get_extra("astrmai_tool_clarification_prompt", "") or "").strip():
                        event.set_extra(
                            "astrmai_tool_clarification_prompt",
                            "我还没能可靠完成刚才申请的查询。请补充要查的对象或具体信息，我再继续核对。",
                        )
                    return await self._handle_required_tool_missing(
                        event,
                        chat_id,
                        runtime["bot_id"],
                        ["requested_readonly_capability"],
                        model=provider_id,
                    )
                if missing_required:
                    self._record_tool_second_pass_resolution(
                        event,
                        self._missing_tool_resolution(event, missing_required),
                        reason="required_tool_contract_still_unsatisfied",
                        selected_tools=self._executed_tool_names(event),
                    )
                    return await self._handle_required_tool_missing(
                        event,
                        chat_id,
                        runtime["bot_id"],
                        sorted(missing_required),
                        model=provider_id,
                    )
                invocation_plans = list(event.get_extra("astrmai_tool_invocation_plans", []) or [])
                resolution = (
                    "degraded"
                    if expansion.rejected_requests and not needs_correction
                    else "satisfied"
                    if needs_correction or invocation_plans
                    else "not_needed"
                )
                self._record_tool_second_pass_resolution(
                    event,
                    resolution,
                    reason=(
                        "disclosure_request_rejected"
                        if expansion.rejected_requests and not needs_correction
                        else "correction_completed"
                        if needs_correction
                        else "no_second_pass_required"
                    ),
                    selected_tools=self._executed_tool_names(event),
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
                    if event.get_extra("astrmai_reread_request", None):
                        if await self._dispatch_reread_request(event):
                            debug_trace(event, "execution.executor.reread_dispatched", model=provider_id)
                            return None
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
                if str(event.get_extra("astrmai_execution_status", "") or "") == "queue_timeout":
                    debug_trace(
                        event,
                        "execution.executor.queue_timeout_stop",
                        mode="tool",
                        stage=str(event.get_extra("astrmai_queue_timeout_stage", "") or ""),
                        model=provider_id,
                    )
                    return None
                side_effects_recorded = self._side_effect_footprint(event) > side_effect_baseline
                debug_trace(
                    event,
                    "execution.executor.model_failure",
                    mode="tool",
                    pool_name="agent",
                    model=provider_id,
                    failure_kind=last_failure_kind,
                    fatal=self._is_executor_failure_fatal(last_error),
                    side_effects_recorded=side_effects_recorded,
                    will_retry_or_switch=(index < len(agent_models) - 1) and not side_effects_recorded,
                    error_preview=preview_text(last_error, 120),
                    skipped_cooldown_models=list(selection_meta.get("skipped_cooldown_models", []) or []),
                    cooldown_overridden=bool(selection_meta.get("cooldown_overridden", False)),
                )
                if side_effects_recorded:
                    # OPT-09/TL-04: 已执行真实副作用的失败轮不得换模型整轮重跑；
                    # 清空待提交动作，防止随 fatal fallback 文本一起被 commit
                    logger.error(
                        f"[{chat_id}] tool model {provider_id} failed after real side effects; "
                        "stopping cascade to avoid replaying sends/actions"
                    )
                    if hasattr(event, "set_extra"):
                        event.set_extra("astrmai_pending_actions", [])
                        event.set_extra("astrmai_side_effect_cascade_stop", True)
                    break
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
        lock_stage = begin_stage(
            event,
            "executor.chat_lock_wait",
            critical_path=True,
            metadata={
                "chat_id": str(chat_id or ""),
                "thread_id": self._turn_thread_id(event),
                "lock_scope": "thread" if self._turn_thread_id(event) else "chat_fallback",
            },
        )
        try:
            chat_lock, using_runtime_coordinator, lock_outcome = await self._acquire_chat_execution_lock(chat_id, event)
        except asyncio.CancelledError:
            finish_stage(event, lock_stage, status="cancelled", reason="acquire_cancelled")
            raise
        except Exception as exc:
            finish_stage(event, lock_stage, status="error", reason=type(exc).__name__)
            raise
        if chat_lock is None:
            reason = lock_outcome or "too_many_pending"
            finish_stage(event, lock_stage, status="timeout" if reason == "queue_timeout" else "dropped", reason=reason)
            logger.warning(f"[{chat_id}] executor ended before execution: {reason}")
            debug_trace(event, "execution.executor.dropped", reason=reason)
            if reason == "queue_timeout":
                event.set_extra("astrmai_execution_status", "queue_timeout")
                event.set_extra("astrmai_queue_timeout_stage", "executor.chat_lock_wait")
            else:
                event.set_extra("astrmai_execution_status", "pending_drop")
            return None
        finish_stage(event, lock_stage, metadata={"using_runtime_coordinator": using_runtime_coordinator})

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
                        if result is None and str(
                            event.get_extra("astrmai_execution_status", "") or ""
                        ) == "suppressed_reply_guard":
                            raise RuntimeError(
                                "provider_failure_text: unresolved native image reply"
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
                if not await self._check_pre_model_freshness(event, chat_id, "post-vision execution"):
                    event.set_extra("astrmai_execution_status", "stale_drop")
                    debug_trace(event, "execution.executor.stale_drop", reason="post_vision_freshness_failed")
                    return None
                failure_disposition = str(
                    event.get_extra("astrmai_vision_failure_disposition", "") or ""
                )
                if failure_disposition == "suppress_passive_group":
                    event.set_extra("astrmai_execution_status", "suppressed_vision_failure")
                    return None
                if failure_disposition == "notify_failure":
                    return await self._send_required_vision_failure(event, chat_id)

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
            await self._release_chat_execution_lock(chat_id, using_runtime_coordinator, chat_lock, event)
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
        if str(event.get_extra("astrmai_execution_status", "") or "") == "queue_timeout":
            debug_trace(
                event,
                "execution.executor.fatal_fallback_skipped",
                reason="queue_timeout",
                stage=str(event.get_extra("astrmai_queue_timeout_stage", "") or ""),
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
