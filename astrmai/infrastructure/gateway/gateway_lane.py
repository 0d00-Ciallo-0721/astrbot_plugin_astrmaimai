import asyncio
import json
from typing import Any, Callable, Dict, List, Optional, Union

from astrbot.api import logger

from ..runtime.lane_manager import LaneKey
from ..runtime.runtime_contracts import LLMCallResult
from ..runtime.trace_runtime import preview_text
from .gateway_exceptions import LLMCascadeFailureException
from .output_guard import sanitize_visible_reply_text
from .provider_capabilities import infer_provider_capabilities


class GatewayLaneMixin:
    def _lane_debug_meta(self, lane_key: LaneKey, conversation_id: str, prefix_hash: str) -> Dict[str, Any]:
        return {
            "lane_key": lane_key.as_log_key(),
            "conversation_id": conversation_id,
            "prefix_hash": prefix_hash,
        }

    def _lane_request_kwargs(self, lane_umo: str) -> Callable[[str], Dict[str, Any]]:
        def _factory(actual_model: str) -> Dict[str, Any]:
            capabilities = infer_provider_capabilities(actual_model)
            kwargs: Dict[str, Any] = {}
            if self.lane_manager and capabilities.supports_remote_session:
                kwargs["session_id"] = self.lane_manager.get_remote_session_id(
                    lane_umo,
                    capabilities.provider_family,
                )
            if capabilities.supports_cache_control:
                kwargs["cache_control"] = {"type": "ephemeral"}
            return kwargs

        return _factory

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
    ) -> LLMCallResult:
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
            )

        primary_models = self.router.get_ranked_models(lane_key.task_family, models)
        model_hint = primary_models[0] if primary_models else ""
        lane_umo, conversation_id, history, _ = await self.lane_manager.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=prefix_hash,
            model_id=model_hint,
            persona_id=persona_id,
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
            debug_meta=self._lane_debug_meta(lane_key, conversation_id, prefix_hash),
            request_kwargs_factory=self._lane_request_kwargs(lane_umo),
        )
        if not result.model_id:
            result.model_id = model_hint

        assistant_content = json.dumps(result.parsed_json, ensure_ascii=False) if is_json else result.text
        artifact = self._build_lane_artifact(result, assistant_content)
        await self.lane_manager.append_visible_reply_artifact(
            lane_key=lane_key,
            base_origin=base_origin,
            raw_user_text=raw_user_text or prompt,
            artifact=artifact,
            token_usage=result.usage.get("total_tokens", 0),
            prefix_hash=prefix_hash,
            model_id=result.model_id or model_hint,
            persona_id=persona_id,
        )
        if self._debug_mode():
            logger.debug(
                f"[Gateway] lane={lane_key.as_log_key()} raw_user_text={preview_text(raw_user_text or prompt, 120)!r} history_roles_tail={self._history_roles_tail(history)}"
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
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
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
            prefix_hash=prefix_hash,
            persona_id=persona_id,
            raw_user_text=raw_user_text,
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
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
    ) -> LLMCallResult:
        if not self.lane_manager:
            raise LLMCascadeFailureException("lane manager is required for tool_chat_in_lane")

        primary_models, attempt_queue = self._build_attempt_queue(lane_key.task_family, models, True)
        if not attempt_queue:
            raise LLMCascadeFailureException(f"未配置可用模型池: {lane_key.task_family}")

        lane_umo, conversation_id, history, _ = await self.lane_manager.ensure_lane(
            lane_key=lane_key,
            base_origin=base_origin,
            prefix_hash=prefix_hash,
            model_id=attempt_queue[0],
            persona_id=persona_id,
        )
        last_error = ""
        for model_id in attempt_queue:
            report_pool = lane_key.task_family if model_id in primary_models else "fallback"
            capabilities = infer_provider_capabilities(model_id)
            tool_kwargs = self._lane_request_kwargs(lane_umo)(model_id)
            try:
                response = await asyncio.wait_for(
                    self.context.tool_loop_agent(
                        event=event,
                        chat_provider_id=model_id,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        contexts=history,
                        tools=tools,
                        max_steps=max_steps,
                        tool_call_timeout=timeout,
                        **tool_kwargs,
                    ),
                    timeout=self._api_timeout(),
                )
                reply_text = getattr(response, "completion_text", "") or ""
                if not reply_text.strip():
                    raise ValueError("empty_response")
                safe_text = sanitize_visible_reply_text(
                    reply_text,
                    fallback_text="",
                    speaker_names=self._bot_speaker_names(),
                )
                if not safe_text:
                    raise ValueError("unsafe_or_empty_text")

                self.router.report_success(report_pool, model_id)
                usage = self._extract_usage(response)
                self._log_usage(
                    report_pool,
                    model_id,
                    usage,
                    {
                        "lane_key": lane_key.as_log_key(),
                        "conversation_id": conversation_id,
                        "prefix_hash": prefix_hash,
                        "provider": capabilities.provider_family,
                    },
                )
                result = self._build_success_result(text=safe_text, model_id=model_id, usage=usage)
                artifact = self._build_lane_artifact(result, result.text)
                await self.lane_manager.append_visible_reply_artifact(
                    lane_key=lane_key,
                    base_origin=base_origin,
                    raw_user_text=raw_user_text or prompt,
                    artifact=artifact,
                    token_usage=usage.get("total_tokens", 0),
                    prefix_hash=prefix_hash,
                    model_id=model_id,
                    persona_id=persona_id,
                )
                if self._debug_mode():
                    trace_id = getattr(event, "get_extra", lambda *_args, **_kwargs: "")("astrmai_trace_id", "")
                    logger.debug(
                        f"[Gateway] trace={trace_id} tool-lane={lane_key.as_log_key()} raw_user_text={preview_text(raw_user_text or prompt, 120)!r} history_roles_tail={self._history_roles_tail(history)}"
                    )
                return result
            except Exception as exc:
                last_error = str(exc)
                is_fatal = self._is_fatal_failure(last_error)
                self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                if is_fatal:
                    logger.error(f"[Gateway] fatal tool_loop failure {model_id}: {last_error[:120]}")
                else:
                    logger.warning(f"[Gateway] tool_loop model {model_id} failed, trying next: {last_error}")
                continue

        raise LLMCascadeFailureException(f"tool_loop model pool exhausted: {last_error}")
