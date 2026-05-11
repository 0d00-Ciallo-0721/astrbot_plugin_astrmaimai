import asyncio
from typing import Any, Callable, Dict, List, Optional, Union

from astrbot.api import logger

from ..runtime.runtime_contracts import FailureKind, LLMCallResult
from .gateway_exceptions import LLMCascadeFailureException
from .output_guard import looks_like_provider_failure_text, sanitize_visible_reply_text
from .provider_capabilities import infer_provider_capabilities


class GatewayCallMixin:
    async def _elastic_call_result(
        self,
        pool_name: str,
        prompt: str,
        system_prompt: str,
        models: List[str],
        is_json: bool = False,
        retry_penalty: float = 0.0,
        image_urls: Optional[List[str]] = None,
        use_fallback: bool = True,
        contexts: Optional[List[Any]] = None,
        debug_meta: Optional[Dict[str, Any]] = None,
        request_kwargs: Optional[Dict[str, Any]] = None,
        request_kwargs_factory: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> LLMCallResult:
        async with self._global_semaphore:
            primary_models, attempt_queue = self._build_attempt_queue(pool_name, models, use_fallback)
            if not attempt_queue:
                raise LLMCascadeFailureException(f"未配置可用模型池: {pool_name}")

            timeout_limit = self._api_timeout()
            max_retries = self._max_retries()
            backoff_factor = self._backoff_factor()
            last_result = self._build_failure_result(
                error_kind=FailureKind.UNKNOWN,
                error_message="model queue not started",
            )

            for model_id in attempt_queue:
                report_pool = pool_name if model_id in primary_models else "fallback"
                for attempt in range(max_retries + 1):
                    try:
                        response = await asyncio.wait_for(
                            self.context.llm_generate(
                                chat_provider_id=model_id,
                                prompt=prompt if prompt else None,
                                contexts=list(contexts or []),
                                **self._build_request_kwargs(
                                    model_id=model_id,
                                    system_prompt=system_prompt,
                                    image_urls=image_urls,
                                    request_kwargs=request_kwargs,
                                    request_kwargs_factory=request_kwargs_factory,
                                ),
                            ),
                            timeout=timeout_limit,
                        )
                    except asyncio.TimeoutError as exc:
                        raise TimeoutError(f"api timeout ({timeout_limit}s)") from exc
                    except Exception as exc:
                        last_error = str(exc)
                        last_result = self._build_failure_result(
                            error_kind=self._classify_failure_kind(last_error),
                            error_message=last_error,
                            model_id=model_id,
                        )
                        is_fatal = self._is_fatal_failure(last_error)
                        self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                        if is_fatal:
                            logger.error(f"[Gateway] fatal model failure {model_id}: {last_error[:120]}")
                            break
                        logger.warning(
                            f"[Gateway] model {model_id} failed ({attempt + 1}/{max_retries + 1}): {last_error}"
                        )
                        if attempt < max_retries:
                            await asyncio.sleep((backoff_factor + retry_penalty) ** attempt)
                        continue

                    try:
                        content = getattr(response, "completion_text", "") or ""
                        if not content.strip():
                            raise ValueError("empty_response")
                        if looks_like_provider_failure_text(content):
                            raise ValueError("provider_failure_text")

                        usage = self._extract_usage(response)
                        log_meta = dict(debug_meta or {})
                        log_meta["provider"] = infer_provider_capabilities(model_id).provider_family

                        if is_json:
                            parsed_json = self._parse_json_completion(content)
                            self.router.report_success(report_pool, model_id)
                            self._log_usage(report_pool, model_id, usage, log_meta)
                            return self._build_success_result(
                                text=str(content).strip(),
                                parsed_json=parsed_json,
                                model_id=model_id,
                                usage=usage,
                            )

                        safe_text = sanitize_visible_reply_text(
                            content,
                            fallback_text="",
                            speaker_names=self._bot_speaker_names(),
                        )
                        if not safe_text:
                            raise ValueError("unsafe_or_empty_text")

                        self.router.report_success(report_pool, model_id)
                        self._log_usage(report_pool, model_id, usage, log_meta)
                        return self._build_success_result(
                            text=safe_text.strip(),
                            model_id=model_id,
                            usage=usage,
                        )
                    except Exception as exc:
                        last_error = str(exc)
                        last_result = self._build_failure_result(
                            error_kind=self._classify_failure_kind(last_error),
                            error_message=last_error,
                            model_id=model_id,
                        )
                        is_fatal = self._is_fatal_failure(last_error)
                        self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                        if is_fatal:
                            logger.error(f"[Gateway] fatal model failure {model_id}: {last_error[:120]}")
                            break
                        logger.warning(
                            f"[Gateway] model {model_id} failed ({attempt + 1}/{max_retries + 1}): {last_error}"
                        )
                        if attempt < max_retries:
                            await asyncio.sleep((backoff_factor + retry_penalty) ** attempt)

            raise LLMCascadeFailureException(f"所有模型均失败: {last_result.error_message}")

    async def _elastic_call(
        self,
        pool_name: str,
        prompt: str,
        system_prompt: str,
        models: List[str],
        is_json: bool = False,
        retry_penalty: float = 0.0,
        image_urls: Optional[List[str]] = None,
        use_fallback: bool = True,
        contexts: Optional[List[Any]] = None,
        debug_meta: Optional[Dict[str, Any]] = None,
        request_kwargs: Optional[Dict[str, Any]] = None,
        request_kwargs_factory: Optional[Callable[[str], Dict[str, Any]]] = None,
    ) -> Union[str, Dict[str, Any]]:
        result = await self._elastic_call_result(
            pool_name=pool_name,
            prompt=prompt,
            system_prompt=system_prompt,
            models=models,
            is_json=is_json,
            retry_penalty=retry_penalty,
            image_urls=image_urls,
            use_fallback=use_fallback,
            contexts=contexts,
            debug_meta=debug_meta,
            request_kwargs=request_kwargs,
            request_kwargs_factory=request_kwargs_factory,
        )
        return result.parsed_json if is_json else result.text
