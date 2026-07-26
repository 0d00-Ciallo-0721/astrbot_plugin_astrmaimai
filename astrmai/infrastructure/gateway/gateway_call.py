import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Union

from astrbot.api import logger

from ..context_economy import WorkloadPolicy
from ..runtime.runtime_contracts import FailureKind, LLMCallResult
from ..runtime.turn_call_ledger import (
    begin_llm_call,
    clamp_timeout_to_turn_budget,
    finish_llm_call,
    record_llm_attempt,
)
from .gateway_exceptions import LLMCascadeFailureException
from .output_guard import validate_visible_output_text


class GatewayCallMixin:
    def _raise_cascade_failure(
        self,
        *,
        pool_name: str,
        error_message: str,
        last_failure_kind: str,
        attempted_models: List[str],
        model_id: str = "",
        raw_completion: str = "",
        failure_reason: str = "",
    ) -> None:
        raise LLMCascadeFailureException(
            error_message,
            pool_name=pool_name,
            last_failure_kind=last_failure_kind,
            attempted_models=attempted_models,
            model_id=model_id,
            raw_completion=raw_completion,
            failure_reason=failure_reason,
        )

    async def _record_benchmark_sample(
        self,
        *,
        usage: Dict[str, int],
        model_id: str,
        provider_family: str,
        workload_policy: Optional[WorkloadPolicy],
        economy_payload: Optional[Dict[str, Any]],
        fallback_used: bool,
        provider_session_id: str = "",
    ) -> None:
        store = getattr(self, "benchmark_sample_store", None)
        if store is None:
            return
        economy_payload = dict(economy_payload or {})
        try:
            template_id = str(
                economy_payload.get("template_id", "")
                or getattr(workload_policy, "template_id", "")
                or "unknown"
            )
            template_version = str(
                economy_payload.get("template_version", "")
                or getattr(workload_policy, "template_version", "")
                or "v1"
            )
            sample = {
                "source_run_id": str(getattr(store, "run_id", "") or ""),
                "created_at": time.time(),
                "workload_family": str(
                    economy_payload.get("workload_family", "")
                    or getattr(getattr(workload_policy, "family", None), "value", "")
                    or "unknown"
                ),
                "template_id": template_id,
                "template_version": template_version,
                "template_key": f"{template_id}@{template_version}",
                "schema_id": str(
                    economy_payload.get("schema_id", "")
                    or getattr(workload_policy, "schema_id", "")
                    or ""
                ),
                "model_id": str(model_id or ""),
                "provider_family": str(provider_family or ""),
                "input_tokens": int(usage.get("input_tokens", 0) or 0),
                "cached_input_tokens": int(usage.get("input_cached", 0) or 0),
                "output_tokens": int(usage.get("output_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
                "provider_session_id": str(
                    provider_session_id
                    or economy_payload.get("provider_session_id", "")
                    or ""
                ),
                "lane_rotated": bool(economy_payload.get("lane_rotated", False)),
                "lane_rotate_reason": str(economy_payload.get("lane_rotate_reason", "") or ""),
                "stable_prefix_length": int(economy_payload.get("stable_prefix_length", 0) or 0),
                "dynamic_payload_length": int(economy_payload.get("dynamic_payload_length", 0) or 0),
                "primary_model": str(
                    economy_payload.get("primary_model", "")
                    or getattr(workload_policy, "primary_model", "")
                    or ""
                ),
                "actual_model": str(model_id or ""),
                "primary_hit": bool(
                    str(model_id or "")
                    and str(model_id or "") == str(
                        economy_payload.get("primary_model", "")
                        or getattr(workload_policy, "primary_model", "")
                        or ""
                    )
                ),
                "fallback_used": bool(fallback_used),
            }
            await store.append(sample)
        except Exception as exc:
            logger.debug(f"[Gateway] benchmark sample skipped: {exc}")

    async def _record_success_artifacts(
        self,
        *,
        report_pool: str,
        model_id: str,
        usage: Dict[str, int],
        log_meta: Dict[str, Any],
        latency_ms: float,
        workload_policy: Optional[WorkloadPolicy],
        record_economy: bool,
        fallback_used: bool,
        provider_session_id: str,
    ) -> Dict[str, Any]:
        try:
            self._log_usage(report_pool, model_id, usage, log_meta, latency_ms=latency_ms)
        except Exception as exc:
            logger.warning(f"[Gateway] usage logging degraded after successful call: {exc}")

        economy_payload: Dict[str, Any] = {}
        if workload_policy and record_economy:
            try:
                workload_trace = self.context_economy.build_trace(
                    policy=workload_policy,
                    actual_model=model_id,
                    fallback_used=fallback_used,
                    provider_family=log_meta["provider"],
                    provider_session_id=provider_session_id,
                    provider_session_enabled=bool(provider_session_id),
                    provider_cache_hint_enabled=bool(log_meta.get("request_cache_control")),
                )
                self.context_economy.record_trace(workload_trace)
                economy_payload = workload_trace.as_dict()
            except Exception as exc:
                logger.warning(f"[Gateway] economy trace degraded after successful call: {exc}")

        try:
            await self._record_benchmark_sample(
                usage=usage,
                model_id=model_id,
                provider_family=log_meta["provider"],
                workload_policy=workload_policy,
                economy_payload=economy_payload,
                fallback_used=fallback_used,
                provider_session_id=provider_session_id,
            )
        except Exception as exc:
            logger.warning(f"[Gateway] benchmark recording degraded after successful call: {exc}")
        return economy_payload

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
        workload_policy: Optional[WorkloadPolicy] = None,
        record_economy: bool = True,
        result_validator: Optional[Callable[[Any], tuple[bool, str]]] = None,
        ledger_call_id: str = "",
        ledger_stage: str = "",
        ledger_family: str = "",
        ledger_critical_path: bool = True,
        timeout_override: Optional[float] = None,
        max_retries_override: Optional[int] = None,
        max_models_override: Optional[int] = None,
        allow_cooldown_override: bool = True,
        reserve_for_reply: bool = False,
    ) -> LLMCallResult:
        # OPT-08/RT-11: 记录全局信号量排队时长（skipped 轮 judge elapsed 51.7s vs
        # attempt 数秒的差值疑似排队）；先埋点取证，拆分信号量待数据定论
        semaphore_wait_started = time.perf_counter()
        async with self._global_semaphore:
            semaphore_wait_ms = round((time.perf_counter() - semaphore_wait_started) * 1000, 1)
            primary_models, attempt_queue = self._build_attempt_queue(
                pool_name,
                models,
                use_fallback,
                workload_policy=workload_policy,
            )
            if allow_cooldown_override:
                attempt_queue, skipped_cooldown_models, cooldown_overridden = (
                    self._filter_cooldown_attempt_queue(
                        pool_name,
                        primary_models,
                        attempt_queue,
                    )
                )
            else:
                attempt_queue, skipped_cooldown_models, cooldown_overridden = (
                    self._filter_cooldown_attempt_queue(
                        pool_name,
                        primary_models,
                        attempt_queue,
                        allow_override=False,
                    )
                )
            if max_models_override is not None:
                attempt_queue = attempt_queue[: max(1, int(max_models_override))]
            owns_ledger_call = not bool(ledger_call_id)
            if owns_ledger_call:
                ledger_call_id = begin_llm_call(
                    None,
                    stage=ledger_stage or f"gateway.{pool_name}",
                    critical_path=ledger_critical_path,
                    family=ledger_family
                    or str(getattr(getattr(workload_policy, "family", None), "value", "") or ""),
                    pool=pool_name,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    contexts=contexts,
                    metadata={
                        "lane_enabled": bool(contexts),
                        "model_count": len(models or []),
                        "semaphore_wait_ms": semaphore_wait_ms,
                    },
                )
            if not attempt_queue:
                failure_reason = "all_models_cooling" if skipped_cooldown_models else "empty_model_pool"
                if owns_ledger_call:
                    finish_llm_call(
                        None,
                        ledger_call_id,
                        status="error",
                        error_kind=FailureKind.UNKNOWN.value,
                        error=failure_reason,
                    )
                self._raise_cascade_failure(
                    pool_name=pool_name,
                    error_message=(
                        f"all_models_cooling: {pool_name}; skipped_cooldown_models={skipped_cooldown_models}"
                        if skipped_cooldown_models
                        else f"未配置可用模型池: {pool_name}"
                    ),
                    last_failure_kind=FailureKind.UNKNOWN.value,
                    attempted_models=[],
                    failure_reason=failure_reason,
                )

            timeout_limit = (
                max(0.1, float(timeout_override))
                if timeout_override is not None
                else self._api_timeout()
            )
            max_retries = (
                max(0, int(max_retries_override))
                if max_retries_override is not None
                else self._max_retries()
            )
            backoff_factor = self._backoff_factor()
            attempted_models: List[str] = []
            last_result = self._build_failure_result(
                error_kind=FailureKind.UNKNOWN,
                error_message="model queue not started",
            )

            for model_id in attempt_queue:
                attempted_models.append(model_id)
                report_pool = pool_name if model_id in primary_models else "fallback"
                for attempt in range(max_retries + 1):
                    attempt_started = time.perf_counter()
                    raw_completion_text = ""
                    try:
                        effective_timeout = clamp_timeout_to_turn_budget(
                            None,
                            timeout_limit,
                            reserve_for_reply=reserve_for_reply,
                        )
                        if effective_timeout <= 0.0:
                            raise asyncio.TimeoutError("turn_deadline_exhausted")
                        llm_kwargs = self._build_request_kwargs(
                            model_id=model_id,
                            system_prompt=system_prompt,
                            image_urls=image_urls,
                            request_kwargs=request_kwargs,
                            request_kwargs_factory=request_kwargs_factory,
                        )
                        t0 = time.perf_counter()
                        response = await asyncio.wait_for(
                            self.context.llm_generate(
                                chat_provider_id=model_id,
                                prompt=prompt if prompt else None,
                                contexts=list(contexts or []),
                                **llm_kwargs,
                            ),
                            timeout=effective_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        # ponytail: timeout is not fatal — should retry, not raise into outer except (R8)
                        last_error = str(exc)
                        last_result = self._build_failure_result(
                            error_kind=self._classify_failure_kind(last_error, error=exc),
                            error_message=last_error,
                            model_id=model_id,
                            raw_completion="",
                        )
                        is_fatal = False  # ponytail: asyncio timeout → retry, not fatal
                        record_llm_attempt(
                            None,
                            ledger_call_id,
                            model=model_id,
                            status="timeout",
                            elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            error_kind=last_result.error_kind.value,
                            fallback=model_id not in primary_models,
                            retry_index=attempt,
                        )
                        self.router.report_failure(report_pool, model_id, is_fatal=False)
                        logger.warning(f"[Gateway] model {model_id} timeout ({attempt+1}/{max_retries+1}): {last_error}")
                        if attempt < max_retries:
                            backoff = (backoff_factor + retry_penalty) ** attempt
                            remaining = clamp_timeout_to_turn_budget(
                                None,
                                backoff,
                                reserve_for_reply=reserve_for_reply,
                            )
                            if remaining <= 0.0:
                                break
                            await asyncio.sleep(remaining)
                        continue
                    except Exception as exc:
                        last_error = str(exc)
                        last_result = self._build_failure_result(
                            error_kind=self._classify_failure_kind(last_error, error=exc),
                            error_message=last_error,
                            model_id=model_id,
                            raw_completion="",
                        )
                        is_fatal = self._is_fatal_failure(last_error, error=exc)
                        record_llm_attempt(
                            None,
                            ledger_call_id,
                            model=model_id,
                            status="fatal" if is_fatal else "error",
                            elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            error_kind=last_result.error_kind.value,
                            fallback=model_id not in primary_models,
                            retry_index=attempt,
                        )
                        self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                        self._open_model_cooldown(report_pool, model_id, last_error)
                        if is_fatal:
                            logger.exception(f"[Gateway] fatal model failure {model_id}")
                            break
                        logger.warning(
                            f"[Gateway] model {model_id} failed ({attempt + 1}/{max_retries + 1}): {last_error}"
                        )
                        if attempt < max_retries:
                            backoff = (backoff_factor + retry_penalty) ** attempt
                            remaining = clamp_timeout_to_turn_budget(
                                None,
                                backoff,
                                reserve_for_reply=reserve_for_reply,
                            )
                            if remaining <= 0.0:
                                break
                            await asyncio.sleep(remaining)
                        continue

                    raw_completion_text = ""  # ponytail: init before try to avoid NameError in nested except
                    try:
                        content = getattr(response, "completion_text", "") or ""
                        latency_ms = (time.perf_counter() - t0) * 1000
                        raw_completion_text = content
                        if not content.strip():
                            raise ValueError("empty_response")

                        usage = self._extract_usage(response)
                        log_meta = dict(debug_meta or {})
                        log_meta["provider"] = self._provider_capabilities(model_id).provider_family
                        if llm_kwargs.get("session_id"):
                            log_meta["request_session_id"] = str(llm_kwargs["session_id"])
                        if llm_kwargs.get("cache_control"):
                            log_meta["request_cache_control"] = "ephemeral"
                        log_meta = self._enrich_cache_debug_meta(
                            log_meta,
                            workload_policy=workload_policy,
                            usage=usage,
                        )

                        if is_json:
                            parsed_json = self._parse_json_completion(content)
                            if result_validator is not None:
                                is_valid, validation_error = result_validator(parsed_json)
                                if not is_valid:
                                    raise ValueError(validation_error or "invalid_result")
                            self.router.report_success(report_pool, model_id)
                            economy_payload = await self._record_success_artifacts(
                                report_pool=report_pool,
                                model_id=model_id,
                                usage=usage,
                                log_meta=log_meta,
                                latency_ms=latency_ms,
                                workload_policy=workload_policy,
                                record_economy=record_economy,
                                fallback_used=bool(model_id not in primary_models),
                                provider_session_id=str(llm_kwargs.get("session_id", "") or ""),
                            )
                            record_llm_attempt(
                                None,
                                ledger_call_id,
                                model=model_id,
                                status="success",
                                elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                                fallback=model_id not in primary_models,
                                retry_index=attempt,
                            )
                            if owns_ledger_call:
                                finish_llm_call(
                                    None,
                                    ledger_call_id,
                                    model=model_id,
                                    provider=str(log_meta.get("provider", "") or ""),
                                    output=content,
                                )
                            return self._build_success_result(
                                text=str(content).strip(),
                                parsed_json=parsed_json,
                                model_id=model_id,
                                usage=usage,
                                economy=economy_payload,
                                skipped_cooldown_models=skipped_cooldown_models,
                                cooldown_overridden=cooldown_overridden,
                            )

                        safe_text, failure_kind = validate_visible_output_text(
                            content,
                            speaker_names=self._bot_speaker_names(
                                getattr(getattr(getattr(self, "config", None), "system1", None), "nicknames", [])
                            ),
                        )
                        if failure_kind:
                            raise ValueError(failure_kind)

                        self.router.report_success(report_pool, model_id)
                        economy_payload = await self._record_success_artifacts(
                            report_pool=report_pool,
                            model_id=model_id,
                            usage=usage,
                            log_meta=log_meta,
                            latency_ms=latency_ms,
                            workload_policy=workload_policy,
                            record_economy=record_economy,
                            fallback_used=bool(model_id not in primary_models),
                            provider_session_id=str(llm_kwargs.get("session_id", "") or ""),
                        )
                        record_llm_attempt(
                            None,
                            ledger_call_id,
                            model=model_id,
                            status="success",
                            elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            fallback=model_id not in primary_models,
                            retry_index=attempt,
                        )
                        if owns_ledger_call:
                            finish_llm_call(
                                None,
                                ledger_call_id,
                                model=model_id,
                                provider=str(log_meta.get("provider", "") or ""),
                                output=safe_text,
                            )
                        return self._build_success_result(
                            text=safe_text.strip(),
                            model_id=model_id,
                            usage=usage,
                            economy=economy_payload,
                            skipped_cooldown_models=skipped_cooldown_models,
                            cooldown_overridden=cooldown_overridden,
                        )
                    except Exception as exc:
                        last_error = str(exc)
                        last_result = self._build_failure_result(
                            error_kind=self._classify_failure_kind(last_error, error=exc),
                            error_message=last_error,
                            model_id=model_id,
                            raw_completion=raw_completion_text,
                        )
                        is_fatal = self._is_fatal_failure(last_error, error=exc)
                        record_llm_attempt(
                            None,
                            ledger_call_id,
                            model=model_id,
                            status="fatal" if is_fatal else "invalid_output",
                            elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                            error_kind=last_result.error_kind.value,
                            fallback=model_id not in primary_models,
                            retry_index=attempt,
                        )
                        self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                        self._open_model_cooldown(report_pool, model_id, f"{last_error} {raw_completion_text}")
                        if is_fatal:
                            logger.exception(f"[Gateway] fatal model failure {model_id}")
                            break
                        logger.warning(
                            f"[Gateway] model {model_id} failed ({attempt + 1}/{max_retries + 1}): {last_error}"
                        )
                        if attempt < max_retries:
                            await asyncio.sleep((backoff_factor + retry_penalty) ** attempt)

            if owns_ledger_call:
                finish_llm_call(
                    None,
                    ledger_call_id,
                    status="error",
                    model=last_result.model_id,
                    error_kind=last_result.error_kind.value,
                    error=last_result.error_message,
                )
            self._raise_cascade_failure(
                pool_name=pool_name,
                error_message=f"所有模型均失败: {last_result.error_message}",
                last_failure_kind=last_result.error_kind.value,
                attempted_models=attempted_models,
                model_id=last_result.model_id,
                raw_completion=last_result.raw_completion,
                failure_reason=last_result.error_message,
            )

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
