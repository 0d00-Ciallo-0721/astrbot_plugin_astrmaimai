import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Union

from astrbot.api import logger

from ..context_economy import PromptEnvelope, WorkloadFamily, WorkloadPolicy
from ..runtime.lane_manager import LaneKey
from ..runtime.runtime_contracts import LLMCallResult
from ..runtime.trace_runtime import append_trace_stage, preview_text
from .gateway_exceptions import LLMCascadeFailureException
from .output_guard import validate_visible_output_text
from .provider_capabilities import infer_provider_capabilities


class GatewayLaneMixin:
    @staticmethod
    def _stringify_request_cache_control(value: Any) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            return str(value)

    def _record_event_request_trace(
        self,
        event: Any,
        *,
        system_prompt: str,
        prompt: str,
        post_hook_system_prompt: str = "",
        request_kwargs: Optional[Dict[str, Any]] = None,
        provider_family: str = "",
        model_id: str = "",
        usage: Optional[Dict[str, int]] = None,
    ) -> None:
        if event is None or not hasattr(event, "set_extra") or not hasattr(event, "get_extra"):
            return
        existing_payload = event.get_extra("astrmai_request_trace", {})
        if not isinstance(existing_payload, dict):
            existing_payload = {}
        def _reuse_or_hash(key: str, fallback_text: str) -> str:
            existing = existing_payload.get(key)
            return existing if existing else self._stable_hash_text(fallback_text)

        _raw_sid = (request_kwargs or {}).get("session_id")
        trace_payload = {
            **existing_payload,
            "gateway_system_hash": self._stable_hash_text(system_prompt),
            "gateway_prompt_hash": self._stable_hash_text(prompt),
            "provider_visible_system_hash": _reuse_or_hash("provider_visible_system_hash", system_prompt),
            "provider_visible_prompt_hash": _reuse_or_hash("provider_visible_prompt_hash", prompt),
            "post_hook_system_hash": _reuse_or_hash("post_hook_system_hash", post_hook_system_prompt or system_prompt),
            "request_session_id": str(_raw_sid) if _raw_sid is not None else "",
            "request_cache_control": self._stringify_request_cache_control((request_kwargs or {}).get("cache_control")),
            "request_provider_family": str(provider_family or ""),
            "request_model_id": str(model_id or ""),
            "usage_input_tokens": int((usage or {}).get("input_tokens", 0) or 0),
            "usage_input_cached": int((usage or {}).get("input_cached", 0) or 0),
            "usage_output_tokens": int((usage or {}).get("output_tokens", 0) or 0),
        }
        event.set_extra("astrmai_request_trace", trace_payload)

    def _lane_debug_meta(
        self,
        lane_key: LaneKey,
        conversation_id: str,
        prefix_hash: str,
        workload_trace: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        meta = {
            "lane_key": lane_key.as_log_key(),
            "conversation_id": conversation_id,
            "prefix_hash": prefix_hash,
        }
        if workload_trace:
            meta["workload_family"] = workload_trace.get("workload_family", "")
            meta["template_id"] = workload_trace.get("template_id", "")
            meta["template_version"] = workload_trace.get("template_version", "")
            meta["primary_model"] = workload_trace.get("primary_model", "")
            meta["lane_rotate_reason"] = workload_trace.get("lane_rotate_reason", "")
        return meta

    def _lane_request_kwargs(self, lane_umo: str, workload_policy: WorkloadPolicy) -> Callable[[str], Dict[str, Any]]:
        def _factory(actual_model: str) -> Dict[str, Any]:
            capabilities = infer_provider_capabilities(actual_model)
            kwargs: Dict[str, Any] = {}
            if self.lane_manager and workload_policy.use_provider_session and capabilities.supports_remote_session:
                kwargs["session_id"] = self.context_economy.build_provider_session_id(
                    lane_manager=self.lane_manager,
                    lane_umo=lane_umo,
                    provider_family=capabilities.provider_family,
                    policy=workload_policy,
                )
            if workload_policy.use_cache_hint and capabilities.supports_cache_control:
                kwargs["cache_control"] = {"type": "ephemeral"}
            return kwargs

        return _factory

    def _lane_workload_family(self, lane_key: LaneKey, tool_mode: bool = False) -> WorkloadFamily:
        return self.context_economy.infer_workload_family(
            lane_key=lane_key,
            pool_name=lane_key.task_family,
            tool_mode=tool_mode,
        )

    async def chat_in_lane(
        self,
        lane_key: LaneKey,
        base_origin: str,
        prompt: str,
        system_prompt: str,
        models: List[str],
        is_json: bool = False,
        retry_penalty: float = 0.0,
        image_urls: Optional[List[str]] = None,
        use_fallback: bool = True,
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> Union[str, Dict[str, Any]]:
        result = await self.chat_in_lane_result(
            lane_key=lane_key,
            base_origin=base_origin,
            prompt=prompt,
            system_prompt=system_prompt,
            models=models,
            is_json=is_json,
            retry_penalty=retry_penalty,
            image_urls=image_urls,
            use_fallback=use_fallback,
            prefix_hash=prefix_hash,
            persona_id=persona_id,
            raw_user_text=raw_user_text,
            template_envelope=template_envelope,
        )
        return result.parsed_json if is_json else result.text

    async def chat_in_lane_result(
        self,
        lane_key: LaneKey,
        base_origin: str,
        prompt: str,
        system_prompt: str,
        models: List[str],
        is_json: bool = False,
        retry_penalty: float = 0.0,
        image_urls: Optional[List[str]] = None,
        use_fallback: bool = True,
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
        template_envelope: Optional[PromptEnvelope] = None,
        event: Any = None,
    ) -> LLMCallResult:
        workload_request = self.context_economy.build_request(
            family=self._lane_workload_family(lane_key, tool_mode=False),
            pool_name=lane_key.task_family,
            prompt=prompt,
            system_prompt=system_prompt,
            models=models,
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=prefix_hash,
            persona_id=persona_id,
            is_json=is_json,
            scope_id=lane_key.scope_id,
            scope_kind=lane_key.scope_kind,
            template_version=template_envelope.template_version if template_envelope else lane_key.prompt_version,
            template_id=template_envelope.template_id if template_envelope else "",
            schema_id=template_envelope.schema_id if template_envelope else "",
            stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
            dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
            template_envelope=template_envelope,
        )
        workload_policy = self.context_economy.resolve_policy(workload_request)
        effective_lane_key = workload_policy.lane_key or lane_key

        if not self.lane_manager:
            return await self._elastic_call_result(
                pool_name=lane_key.task_family,
                prompt=prompt,
                system_prompt=system_prompt,
                models=models,
                is_json=is_json,
                retry_penalty=retry_penalty,
                image_urls=image_urls,
                use_fallback=use_fallback,
                workload_policy=workload_policy,
            )

        if not models:
            raise LLMCascadeFailureException(f"未配置可用模型池: {lane_key.task_family}")
        model_hint = models[0]
        lane_umo, conversation_id, history, _ = await self.lane_manager.ensure_lane(
            lane_key=effective_lane_key,
            base_origin=base_origin,
            prefix_hash=workload_policy.effective_prefix_hash,
            model_id=model_hint,
            persona_id=persona_id,
            template_id=workload_policy.template_id,
            schema_id=workload_policy.schema_id,
            persona_core_version=workload_policy.persona_core_version,
        )
        lane_runtime_meta = await self.lane_manager.get_runtime_meta(lane_umo)
        seed_trace = self.context_economy.build_trace(
            policy=workload_policy,
            lane_umo=lane_umo,
            lane_rotated=bool(lane_runtime_meta.get("lane_rotated", False)),
            lane_rotate_reason=str(lane_runtime_meta.get("lane_rotate_reason", "") or ""),
            provider_session_enabled=bool(workload_policy.use_provider_session),
            provider_cache_hint_enabled=bool(workload_policy.use_cache_hint),
        )
        result = await self._elastic_call_result(
            pool_name=lane_key.task_family,
            prompt=prompt,
            system_prompt=system_prompt,
            models=models,
            is_json=is_json,
                retry_penalty=retry_penalty,
                image_urls=image_urls,
                use_fallback=use_fallback,
                contexts=history,
            debug_meta=self._lane_debug_meta(
                effective_lane_key,
                conversation_id,
                workload_policy.effective_prefix_hash,
                seed_trace.as_dict(),
            ),
            request_kwargs_factory=self._lane_request_kwargs(lane_umo, workload_policy),
            workload_policy=workload_policy,
            record_economy=False,
        )
        if not result.model_id:
            # 防御性回退：_elastic_call_result 成功路径必定设置 model_id；
            # 到达此处说明 _build_success_result 未正确传递 model_id，属异常路径。
            result.model_id = model_hint
            logger.warning(
                f"[Gateway] chat_in_lane_result: result.model_id unexpectedly empty "
                f"(pool={lane_key.task_family}), falling back to models[0]={model_hint}"
            )
        provider_caps = infer_provider_capabilities(result.model_id)
        provider_session_id = ""
        if workload_policy.use_provider_session and provider_caps.supports_remote_session:
            provider_session_id = self.context_economy.build_provider_session_id(
                lane_manager=self.lane_manager,
                lane_umo=lane_umo,
                provider_family=provider_caps.provider_family,
                policy=workload_policy,
            )
        workload_trace = self.context_economy.build_trace(
            policy=workload_policy,
            lane_umo=lane_umo,
            actual_model=result.model_id or model_hint,
            fallback_used=bool((result.model_id or model_hint) and (result.model_id or model_hint) != workload_policy.primary_model),
            lane_rotated=bool(lane_runtime_meta.get("lane_rotated", False)),
            lane_rotate_reason=str(lane_runtime_meta.get("lane_rotate_reason", "") or ""),
            provider_family=result.provider_family or provider_caps.provider_family,
            provider_session_id=provider_session_id,
            provider_session_enabled=bool(provider_session_id),
            provider_cache_hint_enabled=bool(workload_policy.use_cache_hint and provider_caps.supports_cache_control),
        )
        result.economy = workload_trace.as_dict()
        self.context_economy.record_trace(workload_trace)
        if event is not None and hasattr(event, "set_extra"):
            event.set_extra("astrmai_cache_affinity_enabled", bool(workload_trace.cache_affinity_enabled))
            event.set_extra(
                "astrmai_cached_usage_supported",
                bool((result.usage or {}).get("cached_usage_supported", False) or int((result.usage or {}).get("input_cached", 0) or 0) > 0),
            )
        append_trace_stage(
            event,
            "gateway_chat",
            workload_family=workload_policy.family.value,
            lane_key=effective_lane_key.as_log_key(),
            lane_umo=lane_umo,
            prefix_hash=workload_policy.stable_prefix_hash,
            model_id=result.model_id or model_hint,
            fallback_used=bool((result.model_id or model_hint) and (result.model_id or model_hint) != workload_policy.primary_model),
            skipped_cooldown_models=list(result.skipped_cooldown_models),
            cooldown_overridden=bool(result.cooldown_overridden),
        )
        self._record_event_request_trace(
            event,
            system_prompt=system_prompt,
            prompt=prompt,
            request_kwargs={
                "session_id": provider_session_id,
                "cache_control": {"type": "ephemeral"} if (workload_policy.use_cache_hint and provider_caps.supports_cache_control) else None,
            },
            provider_family=result.provider_family or provider_caps.provider_family,
            model_id=result.model_id or model_hint,
            usage=result.usage,
        )

        assistant_content = json.dumps(result.parsed_json, ensure_ascii=False) if is_json else result.text
        artifact = self._build_lane_artifact(result, assistant_content)
        await self.lane_manager.append_visible_reply_artifact(
            lane_key=effective_lane_key,
            base_origin=base_origin,
            raw_user_text=raw_user_text or prompt,
            artifact=artifact,
            token_usage=result.usage.get("total_tokens", 0),
            prefix_hash=workload_policy.effective_prefix_hash,
            model_id=result.model_id or model_hint,
            persona_id=persona_id,
            template_id=workload_policy.template_id,
            schema_id=workload_policy.schema_id,
            persona_core_version=workload_policy.persona_core_version,
        )
        if self._debug_mode():
            logger.debug(
                f"[Gateway] lane={effective_lane_key.as_log_key()} raw_user_text={preview_text(raw_user_text or prompt, 120)!r} history_roles_tail={self._history_roles_tail(history)}"
            )
        return result

    async def tool_chat_in_lane(
        self,
        lane_key: LaneKey,
        base_origin: str,
        event: Any,
        prompt: str,
        system_prompt: str,
        tools: Any,
        models: List[str],
        max_steps: int,
        timeout: int,
        image_urls: Optional[List[str]] = None,
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> str:
        result = await self.tool_chat_in_lane_result(
            lane_key=lane_key,
            base_origin=base_origin,
            event=event,
            prompt=prompt,
            system_prompt=system_prompt,
            tools=tools,
            models=models,
            max_steps=max_steps,
            timeout=timeout,
            image_urls=image_urls,
            prefix_hash=prefix_hash,
            persona_id=persona_id,
            raw_user_text=raw_user_text,
            template_envelope=template_envelope,
        )
        return result.text

    async def tool_chat_in_lane_result(
        self,
        lane_key: LaneKey,
        base_origin: str,
        event: Any,
        prompt: str,
        system_prompt: str,
        tools: Any,
        models: List[str],
        max_steps: int,
        timeout: int,
        image_urls: Optional[List[str]] = None,
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> LLMCallResult:
        if not self.lane_manager:
            raise LLMCascadeFailureException("lane manager is required for tool_chat_in_lane")

        workload_request = self.context_economy.build_request(
            family=self._lane_workload_family(lane_key, tool_mode=True),
            pool_name=lane_key.task_family,
            prompt=prompt,
            system_prompt=system_prompt,
            models=models,
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=prefix_hash,
            persona_id=persona_id,
            is_json=False,
            scope_id=lane_key.scope_id,
            scope_kind=lane_key.scope_kind,
            tool_mode=True,
            template_version=template_envelope.template_version if template_envelope else lane_key.prompt_version,
            template_id=template_envelope.template_id if template_envelope else "",
            schema_id=template_envelope.schema_id if template_envelope else "",
            stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
            dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
            template_envelope=template_envelope,
        )
        workload_policy = self.context_economy.resolve_policy(workload_request)
        effective_lane_key = workload_policy.lane_key or lane_key
        primary_models, attempt_queue = self._build_attempt_queue(
            lane_key.task_family,
            models,
            True,
            workload_policy=workload_policy,
        )
        attempt_queue, skipped_cooldown_models, cooldown_overridden = self._filter_cooldown_attempt_queue(
            lane_key.task_family,
            primary_models,
            attempt_queue,
        )
        if not attempt_queue:
            raise LLMCascadeFailureException(f"未配置可用模型池: {lane_key.task_family}")

        lane_umo, conversation_id, history, _ = await self.lane_manager.ensure_lane(
            lane_key=effective_lane_key,
            base_origin=base_origin,
            prefix_hash=workload_policy.effective_prefix_hash,
            model_id=attempt_queue[0],
            persona_id=persona_id,
            template_id=workload_policy.template_id,
            schema_id=workload_policy.schema_id,
            persona_core_version=workload_policy.persona_core_version,
        )
        lane_runtime_meta = await self.lane_manager.get_runtime_meta(lane_umo)
        last_error = ""
        last_raw_completion = ""
        attempted_models: List[str] = []
        for model_id in attempt_queue:
            attempted_models.append(model_id)
            report_pool = lane_key.task_family if model_id in primary_models else "fallback"
            capabilities = infer_provider_capabilities(model_id)
            tool_kwargs = self._lane_request_kwargs(lane_umo, workload_policy)(model_id)
            try:
                response = await asyncio.wait_for(
                    self.context.tool_loop_agent(
                        event=event,
                        chat_provider_id=model_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=history,
                        image_urls=image_urls,
                        tools=tools,
                        max_steps=max_steps,
                        tool_call_timeout=timeout,
                        **tool_kwargs,
                    ),
                    timeout=self._api_timeout(),
                )
                reply_text = getattr(response, "completion_text", "") or ""
                last_raw_completion = reply_text
                if not reply_text.strip():
                    raise ValueError("empty_response")
                stripped_reply = reply_text.strip()
                if "[SYSTEM_WAIT_SIGNAL]" in stripped_reply or "[TERMINAL_YIELD]:" in stripped_reply:
                    self.router.report_success(report_pool, model_id)
                    usage = self._extract_usage(response)
                    _tool_log_meta = {
                        "lane_key": effective_lane_key.as_log_key(),
                        "conversation_id": conversation_id,
                        "prefix_hash": workload_policy.effective_prefix_hash,
                        "provider": capabilities.provider_family,
                        "workload_family": workload_policy.family.value,
                        "template_id": workload_policy.template_id,
                        "template_version": workload_policy.template_version,
                    }
                    if tool_kwargs.get("session_id"):
                        _tool_log_meta["request_session_id"] = str(tool_kwargs["session_id"])
                    if tool_kwargs.get("cache_control"):
                        _tool_log_meta["request_cache_control"] = "ephemeral"
                    _tool_log_meta = self._enrich_cache_debug_meta(
                        _tool_log_meta,
                        workload_policy=workload_policy,
                        usage=usage,
                    )
                    self._log_usage(report_pool, model_id, usage, _tool_log_meta)
                    provider_session_id = str(tool_kwargs.get("session_id", "") or "")
                    workload_trace = self.context_economy.build_trace(
                        policy=workload_policy,
                        lane_umo=lane_umo,
                        actual_model=model_id,
                        fallback_used=bool(model_id != workload_policy.primary_model),
                        lane_rotated=bool(lane_runtime_meta.get("lane_rotated", False)),
                        lane_rotate_reason=str(lane_runtime_meta.get("lane_rotate_reason", "") or ""),
                        provider_family=capabilities.provider_family,
                        provider_session_id=provider_session_id,
                        provider_session_enabled=bool(provider_session_id),
                        provider_cache_hint_enabled=bool("cache_control" in tool_kwargs),
                    )
                    result = self._build_success_result(
                        text=stripped_reply,
                        model_id=model_id,
                        usage=usage,
                        economy=workload_trace.as_dict(),
                    )
                    self.context_economy.record_trace(workload_trace)
                    if event is not None and hasattr(event, "set_extra"):
                        event.set_extra("astrmai_cache_affinity_enabled", bool(workload_trace.cache_affinity_enabled))
                        event.set_extra(
                            "astrmai_cached_usage_supported",
                            bool((usage or {}).get("cached_usage_supported", False) or int((usage or {}).get("input_cached", 0) or 0) > 0),
                        )
                    self._record_event_request_trace(
                        event,
                        system_prompt=system_prompt,
                        prompt=prompt,
                        request_kwargs=tool_kwargs,
                        provider_family=capabilities.provider_family,
                        model_id=model_id,
                        usage=usage,
                    )
                    if "[TERMINAL_YIELD]:" in stripped_reply:
                        terminal_content = stripped_reply.split("[TERMINAL_YIELD]:", 1)[1].strip()
                        artifact = self._build_lane_artifact(result, terminal_content)
                        await self.lane_manager.append_visible_reply_artifact(
                            lane_key=effective_lane_key,
                            base_origin=base_origin,
                            raw_user_text=raw_user_text or prompt,
                            artifact=artifact,
                            token_usage=usage.get("total_tokens", 0),
                            prefix_hash=workload_policy.effective_prefix_hash,
                            model_id=model_id,
                            persona_id=persona_id,
                            template_id=workload_policy.template_id,
                            schema_id=workload_policy.schema_id,
                            persona_core_version=workload_policy.persona_core_version,
                        )
                    append_trace_stage(
                        event,
                        "gateway_tool_call",
                        workload_family=workload_policy.family.value,
                        lane_key=effective_lane_key.as_log_key(),
                        lane_umo=lane_umo,
                        prefix_hash=workload_policy.stable_prefix_hash,
                        model_id=model_id,
                        fallback_used=bool(model_id != workload_policy.primary_model),
                        protocol_passthrough=True,
                        protocol_type="terminal_yield" if "[TERMINAL_YIELD]:" in stripped_reply else "wait_signal",
                        skipped_cooldown_models=list(skipped_cooldown_models),
                        cooldown_overridden=bool(cooldown_overridden),
                    )
                    return result
                safe_text, failure_kind = validate_visible_output_text(
                    reply_text,
                    speaker_names=self._bot_speaker_names(),
                )
                if failure_kind:
                    raise ValueError(failure_kind)

                self.router.report_success(report_pool, model_id)
                usage = self._extract_usage(response)
                _tool_log_meta = {
                    "lane_key": effective_lane_key.as_log_key(),
                    "conversation_id": conversation_id,
                    "prefix_hash": workload_policy.effective_prefix_hash,
                    "provider": capabilities.provider_family,
                    "workload_family": workload_policy.family.value,
                    "template_id": workload_policy.template_id,
                    "template_version": workload_policy.template_version,
                }
                if tool_kwargs.get("session_id"):
                    _tool_log_meta["request_session_id"] = str(tool_kwargs["session_id"])
                if tool_kwargs.get("cache_control"):
                    _tool_log_meta["request_cache_control"] = "ephemeral"
                _tool_log_meta = self._enrich_cache_debug_meta(
                    _tool_log_meta,
                    workload_policy=workload_policy,
                    usage=usage,
                )
                self._log_usage(report_pool, model_id, usage, _tool_log_meta)
                provider_session_id = str(tool_kwargs.get("session_id", "") or "")
                workload_trace = self.context_economy.build_trace(
                    policy=workload_policy,
                    lane_umo=lane_umo,
                    actual_model=model_id,
                    fallback_used=bool(model_id != workload_policy.primary_model),
                    lane_rotated=bool(lane_runtime_meta.get("lane_rotated", False)),
                    lane_rotate_reason=str(lane_runtime_meta.get("lane_rotate_reason", "") or ""),
                    provider_family=capabilities.provider_family,
                    provider_session_id=provider_session_id,
                    provider_session_enabled=bool(provider_session_id),
                    provider_cache_hint_enabled=bool("cache_control" in tool_kwargs),
                )
                result = self._build_success_result(
                    text=safe_text,
                    model_id=model_id,
                    usage=usage,
                    economy=workload_trace.as_dict(),
                )
                self.context_economy.record_trace(workload_trace)
                if event is not None and hasattr(event, "set_extra"):
                    event.set_extra("astrmai_cache_affinity_enabled", bool(workload_trace.cache_affinity_enabled))
                    event.set_extra(
                        "astrmai_cached_usage_supported",
                        bool((usage or {}).get("cached_usage_supported", False) or int((usage or {}).get("input_cached", 0) or 0) > 0),
                    )
                self._record_event_request_trace(
                    event,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    request_kwargs=tool_kwargs,
                    provider_family=capabilities.provider_family,
                    model_id=model_id,
                    usage=usage,
                )
                artifact = self._build_lane_artifact(result, result.text)
                await self.lane_manager.append_visible_reply_artifact(
                    lane_key=effective_lane_key,
                    base_origin=base_origin,
                    raw_user_text=raw_user_text or prompt,
                    artifact=artifact,
                    token_usage=usage.get("total_tokens", 0),
                    prefix_hash=workload_policy.effective_prefix_hash,
                    model_id=model_id,
                    persona_id=persona_id,
                    template_id=workload_policy.template_id,
                    schema_id=workload_policy.schema_id,
                    persona_core_version=workload_policy.persona_core_version,
                )
                append_trace_stage(
                    event,
                    "gateway_tool_call",
                    workload_family=workload_policy.family.value,
                    lane_key=effective_lane_key.as_log_key(),
                    lane_umo=lane_umo,
                    prefix_hash=workload_policy.stable_prefix_hash,
                    model_id=model_id,
                    fallback_used=bool(model_id != workload_policy.primary_model),
                    skipped_cooldown_models=list(skipped_cooldown_models),
                    cooldown_overridden=bool(cooldown_overridden),
                )
                if self._debug_mode():
                    trace_id = getattr(event, "get_extra", lambda *_args, **_kwargs: "")("astrmai_trace_id", "")
                    logger.debug(
                        f"[Gateway] trace={trace_id} tool-lane={effective_lane_key.as_log_key()} raw_user_text={preview_text(raw_user_text or prompt, 120)!r} history_roles_tail={self._history_roles_tail(history)}"
                    )
                return result
            except Exception as exc:
                last_error = str(exc)
                last_failure_kind = self._classify_failure_kind(last_error)
                is_fatal = self._is_fatal_failure(last_error)
                self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                cooldown_meta = self._open_model_cooldown(report_pool, model_id, f"{last_error} {last_raw_completion}")
                append_trace_stage(
                    event,
                    "gateway_tool_call_failure",
                    workload_family=workload_policy.family.value,
                    lane_key=effective_lane_key.as_log_key(),
                    lane_umo=lane_umo,
                    prefix_hash=workload_policy.stable_prefix_hash,
                    model_id=model_id,
                    fallback_used=bool(model_id != workload_policy.primary_model),
                    failure_kind=last_failure_kind.value,
                    failure_reason=preview_text(last_error, 160),
                    attempted_models=list(attempted_models),
                    skipped_cooldown_models=list(skipped_cooldown_models),
                    cooldown_overridden=bool(cooldown_overridden),
                    model_cooldown_until=cooldown_meta.get("until", 0.0),
                    cooldown_reason=cooldown_meta.get("reason", ""),
                    raw_completion=last_raw_completion,
                    will_retry_or_switch=bool(model_id != attempt_queue[-1]),
                )
                if is_fatal:
                    logger.error(f"[Gateway] fatal tool_loop failure {model_id}: {last_error[:120]}")
                else:
                    logger.warning(f"[Gateway] tool_loop model {model_id} failed, trying next: {last_error}")
                continue

        raise LLMCascadeFailureException(
            f"tool_loop model pool exhausted: {last_error}",
            pool_name=lane_key.task_family,
            last_failure_kind=self._classify_failure_kind(last_error).value if last_error else "unknown",
            attempted_models=attempted_models,
            model_id=attempted_models[-1] if attempted_models else "",
            raw_completion=last_raw_completion,
            failure_reason=last_error,
        )
