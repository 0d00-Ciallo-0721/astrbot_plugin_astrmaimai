from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from ...infrastructure.context_economy import PromptTemplateId, WorkloadFamily
from ...infrastructure.context_economy.models import WorkloadTrace
from ...infrastructure.gateway.output_guard import looks_like_provider_failure_text
from ...infrastructure.gateway.provider_capabilities import resolve_provider_capabilities
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.turn_call_ledger import (
    begin_llm_call,
    finish_llm_call,
    record_llm_attempt,
)


class CompactionProviderMixin:
    """Provider-calling methods extracted from ContextCompactionEngine."""

    async def _run_compaction_model(self, awaitable_factory):
        budget = getattr(self, "background_task_budget", None)
        if budget is None:
            return await awaitable_factory()
        return await budget.run(awaitable_factory, task_name="compaction")

    def _compaction_provider_validated(self, context, configured: str) -> bool:
        cache = getattr(self, "_compaction_provider_validation", None)
        if cache is None:
            cache = {}
            self._compaction_provider_validation = cache
        if configured in cache:
            return cache[configured]
        valid = True
        accessor = getattr(context, "get_provider_by_id", None) if context is not None else None
        if callable(accessor):
            try:
                valid = accessor(configured) is not None
            except Exception:
                valid = True  # 校验接口异常时不拦截，保持旧行为
        if not valid:
            logger.warning(
                f"[Compaction] configured compaction_provider_id '{configured}' 不存在，"
                "已从候选剔除（仅告警一次）；请在配置页修正"
            )
        cache[configured] = valid
        return valid

    async def _resolve_provider_candidates(self, chat_id: str) -> list[str]:
        candidates: list[str] = []
        configured = str(self.provider_id or "").strip()
        context = getattr(self.gateway, "context", None) if self.gateway else None
        if configured:
            # OPT-09/RT-07: 配置残留的旧 provider（生产实证 openai/deepseek-v4-pro）
            # 会让每次压缩首试必败；做一次性存在校验，无效则剔除并 WARN 一次
            if self._compaction_provider_validated(context, configured):
                candidates.append(configured)
        if context is not None and hasattr(context, "get_current_chat_provider_id"):
            try:
                current_provider = await context.get_current_chat_provider_id(chat_id)
            except Exception as exc:
                logger.debug(f"[{chat_id}] compaction provider resolve failed: {exc}")
            else:
                current_text = str(current_provider or "").strip()
                if current_text and current_text not in candidates:
                    candidates.append(current_text)
        return candidates

    def _render_compaction_envelope(self, template_id: PromptTemplateId, *, lines_text: str):
        gateway = self.gateway
        economy = getattr(gateway, "context_economy", None) if gateway else None
        prompt_registry = getattr(economy, "templates", None)
        if prompt_registry is None:
            return None
        return prompt_registry.render_template(template_id, {"lines_text": lines_text})

    def _compaction_provider_timeout_seconds(self) -> float:
        config = getattr(self.gateway, "config", None) if self.gateway else None
        timing = getattr(config, "timing", None)
        try:
            configured = float(getattr(timing, "compaction_timeout_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            configured = 0.0
        if configured > 0.0:
            return max(0.01, configured)
        gateway_timeout = getattr(self.gateway, "_api_timeout", None) if self.gateway else None
        if callable(gateway_timeout):
            try:
                return max(0.01, float(gateway_timeout()))
            except (TypeError, ValueError):
                pass
        settings = getattr(self.gateway, "settings", None) if self.gateway else None
        try:
            configured = float(getattr(settings, "api_timeout", 60.0) or 60.0)
        except (TypeError, ValueError):
            configured = 60.0
        return max(0.01, configured)

    def _compaction_provider_capabilities(self, provider_id: str):
        resolver = getattr(self.gateway, "_provider_capabilities", None) if self.gateway else None
        if callable(resolver):
            return resolver(provider_id)
        context = getattr(self.gateway, "context", None) if self.gateway else None
        return resolve_provider_capabilities(context, provider_id)

    @staticmethod
    def _compaction_lane_key(chat_id: str) -> LaneKey:
        return LaneKey(subsystem="bg", task_family="compaction", scope_id=chat_id, scope_kind="chat")

    def _compaction_provider_kwargs(
        self,
        chat_id: str,
        provider_id: str,
        system_prompt: str,
        prompt: str,
        template_id: str,
        template_version: str,
        schema_id: str,
        stable_prefix_text: str = "",
        dynamic_payload_text: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        gateway = self.gateway
        economy = getattr(gateway, "context_economy", None)
        lane_manager = getattr(gateway, "lane_manager", None)
        if not gateway or economy is None:
            return {}, {}
        request = economy.build_request(
            family=WorkloadFamily.COMPACTION_SUMMARY,
            pool_name="memory",
            prompt=prompt,
            system_prompt=system_prompt,
            models=[provider_id],
            lane_key=self._compaction_lane_key(chat_id),
            base_origin="",
            scope_id=chat_id,
            scope_kind="chat",
            template_id=template_id,
            template_version=template_version,
            schema_id=schema_id,
            stable_prefix_text=stable_prefix_text,
            dynamic_payload_text=dynamic_payload_text,
        )
        policy = economy.resolve_policy(request)
        kwargs: dict[str, Any] = {}
        caps = self._compaction_provider_capabilities(provider_id)
        if policy is None:
            trace = WorkloadTrace(
                workload_family=WorkloadFamily.COMPACTION_SUMMARY.value,
                lane_scope_id=chat_id,
                template_id=template_id,
                template_version=template_version,
                schema_id=schema_id,
                primary_model=provider_id,
                actual_model=provider_id,
                fallback_used=True,
                provider_family=caps.provider_family,
                cache_affinity_reason="policy_missing",
                stable_prefix_length=len(stable_prefix_text or ""),
                dynamic_payload_length=len(dynamic_payload_text or ""),
                template_schema_id=schema_id,
            )
            return kwargs, trace.as_dict()
        lane_umo = ""
        if lane_manager and policy.lane_key:
            lane_umo = lane_manager.resolve_lane_umo("", policy.lane_key)
        if lane_umo and policy.use_provider_session and caps.supports_remote_session:
            kwargs["session_id"] = economy.build_provider_session_id(
                lane_manager=lane_manager,
                lane_umo=lane_umo,
                provider_family=caps.provider_family,
                policy=policy,
            )
        if policy.use_cache_hint and caps.supports_cache_control:
            kwargs["cache_control"] = {"type": "ephemeral"}
        trace = economy.build_trace(
            policy=policy,
            lane_umo=lane_umo,
            actual_model=provider_id,
            provider_family=caps.provider_family,
            provider_session_id=str(kwargs.get("session_id", "") or ""),
            provider_session_enabled=bool(kwargs.get("session_id")),
            provider_cache_hint_enabled=bool(kwargs.get("cache_control")),
        )
        return kwargs, trace.as_dict()

    async def _build_summary_with_provider(self, chat_id: str, drained_segments) -> str:
        if not drained_segments or not self.gateway:
            return ""
        context = getattr(self.gateway, "context", None)
        if context is None or not hasattr(context, "llm_generate"):
            return ""
        lines = [line for line in (self._segment_to_summary_line(segment) for segment in drained_segments) if line]
        if not lines:
            return ""
        provider_candidates = await self._resolve_provider_candidates(chat_id)
        if not provider_candidates:
            return ""
        system_prompt = (
            "你是群聊上下文压缩助手。"
            "请将给定的旧对话片段压缩成简洁、连续、可供系统内部使用的摘要。"
            "只保留人物关系变化、关键决策、未完成事项、情绪转折和话题结论。"
            "不要编造，不要输出多余解释。"
        )
        prompt = (
            "请压缩以下旧对话片段，输出 3 到 6 行摘要，每行一个要点：\n"
            + "\n".join(lines)
        )
        envelope = self._render_compaction_envelope(
            PromptTemplateId.COMPACTION_SUMMARY_V1,
            lines_text="\n".join(lines),
        )
        if envelope is not None:
            system_prompt = envelope.system_prompt
            prompt = envelope.prompt
            template_id = envelope.template_id
            template_version = envelope.template_version
            schema_id = envelope.schema_id
            stable_prefix_text = envelope.stable_prefix_text
            dynamic_payload_text = envelope.dynamic_payload_text
        else:
            template_id = PromptTemplateId.COMPACTION_SUMMARY_V1.value
            template_version = "v1"
            schema_id = "bullet_summary"
            stable_prefix_text = system_prompt
            dynamic_payload_text = prompt
        ledger_call_id = begin_llm_call(
            None,
            stage="attention.compaction.v1",
            family=WorkloadFamily.COMPACTION_SUMMARY.value,
            pool="compaction",
            prompt=prompt,
            system_prompt=system_prompt,
            contexts=lines,
            metadata={"provider_count": len(provider_candidates)},
        )
        last_error_kind = ""
        for attempt_index, provider_id in enumerate(provider_candidates):
            attempt_started = time.perf_counter()
            request_kwargs: dict[str, Any] = {}
            trace_payload: dict[str, Any] = {}
            try:
                request_kwargs, trace_payload = self._compaction_provider_kwargs(
                    chat_id,
                    provider_id,
                    system_prompt,
                    prompt,
                    template_id,
                    template_version,
                    schema_id,
                    stable_prefix_text,
                    dynamic_payload_text,
                )
                response = await self._run_compaction_model(
                    lambda: asyncio.wait_for(
                        context.llm_generate(
                            chat_provider_id=provider_id,
                            system_prompt=system_prompt,
                            prompt=prompt,
                            **request_kwargs,
                        ),
                        timeout=self._compaction_provider_timeout_seconds(),
                    )
                )
            except Exception as exc:
                last_error_kind = exc.__class__.__name__
                record_llm_attempt(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    status="timeout" if isinstance(exc, asyncio.TimeoutError) else "error",
                    elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                    error_kind=last_error_kind,
                    fallback=attempt_index > 0,
                    retry_index=attempt_index,
                )
                logger.debug(f"[{chat_id}] compaction provider {provider_id} failed: {exc}")
                request_kwargs.pop("session_id", None)
                # 失效损坏的 remote session，防止下次复用（D6）
                _lane_mgr = getattr(self.gateway, "lane_manager", None) if self.gateway else None
                if _lane_mgr is not None:
                    try:
                        _lane_key = self._compaction_lane_key(chat_id)
                        _lane_umo = _lane_mgr.resolve_lane_umo("", _lane_key)
                        _lane_mgr.expire_remote_sessions_for_lane(_lane_umo)
                    except Exception as lane_exc:
                        logger.warning(f"[Compaction] lane session expiry degraded: {lane_exc}")
                continue
            content = getattr(response, "completion_text", response)
            if looks_like_provider_failure_text(str(content or "")):
                last_error_kind = "provider_failure_text"
                record_llm_attempt(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    status="invalid_output",
                    elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                    error_kind=last_error_kind,
                    fallback=attempt_index > 0,
                    retry_index=attempt_index,
                )
                logger.warning(f"[{chat_id}] compaction provider {provider_id} returned failure text")
                continue
            clipped = self._clip_summary(str(content or "").strip())
            if clipped:
                record_llm_attempt(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    status="success",
                    elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                    fallback=attempt_index > 0,
                    retry_index=attempt_index,
                )
                finish_llm_call(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    output=clipped,
                )
                if getattr(self.gateway, "context_economy", None):
                    self.gateway.context_economy.record_trace(WorkloadTrace(**trace_payload))
                return clipped
            last_error_kind = "empty_output"
            record_llm_attempt(
                None,
                ledger_call_id,
                model=provider_id,
                status="invalid_output",
                elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                error_kind=last_error_kind,
                fallback=attempt_index > 0,
                retry_index=attempt_index,
            )
        finish_llm_call(
            None,
            ledger_call_id,
            status="error",
            error_kind=last_error_kind or "empty_output",
            error=last_error_kind or "empty_output",
        )
        return ""

    async def _build_summary_with_provider_v2(self, chat_id: str, drained_segments) -> str:
        if not drained_segments or not self.gateway:
            return ""
        context = getattr(self.gateway, "context", None)
        if context is None or not hasattr(context, "llm_generate"):
            return ""
        lines = [line for line in (self._segment_to_summary_line(segment) for segment in drained_segments) if line]
        if not lines:
            return ""
        provider_candidates = await self._resolve_provider_candidates(chat_id)
        if not provider_candidates:
            return ""
        system_prompt = (
            "你是群聊上下文压缩助手。"
            "请把旧对话片段整理成结构稳定的主题摘要。"
            "只输出这些 section，未命中的可以省略："
            "[topics] [decisions] [open_items] [relationship_changes] [emotional_turns] [visual_notes] [long_term_constraints]。"
            "每个 section 下只用 `- ` 开头的短句，不要输出额外解释。"
        )
        prompt = "请压缩以下旧对话片段，输出稳定 section 摘要：\n" + "\n".join(lines)
        envelope = self._render_compaction_envelope(
            PromptTemplateId.COMPACTION_SUMMARY_V2,
            lines_text="\n".join(lines),
        )
        if envelope is not None:
            system_prompt = envelope.system_prompt
            prompt = envelope.prompt
            template_id = envelope.template_id
            template_version = envelope.template_version
            schema_id = envelope.schema_id
            stable_prefix_text = envelope.stable_prefix_text
            dynamic_payload_text = envelope.dynamic_payload_text
        else:
            template_id = PromptTemplateId.COMPACTION_SUMMARY_V2.value
            template_version = "v2"
            schema_id = "section_summary"
            stable_prefix_text = system_prompt
            dynamic_payload_text = prompt
        ledger_call_id = begin_llm_call(
            None,
            stage="attention.compaction.v2",
            family=WorkloadFamily.COMPACTION_SUMMARY.value,
            pool="compaction",
            prompt=prompt,
            system_prompt=system_prompt,
            contexts=lines,
            metadata={"provider_count": len(provider_candidates)},
        )
        last_error_kind = ""
        for attempt_index, provider_id in enumerate(provider_candidates):
            attempt_started = time.perf_counter()
            request_kwargs: dict[str, Any] = {}
            trace_payload: dict[str, Any] = {}
            try:
                request_kwargs, trace_payload = self._compaction_provider_kwargs(
                    chat_id,
                    provider_id,
                    system_prompt,
                    prompt,
                    template_id,
                    template_version,
                    schema_id,
                    stable_prefix_text,
                    dynamic_payload_text,
                )
                response = await self._run_compaction_model(
                    lambda: asyncio.wait_for(
                        context.llm_generate(
                            chat_provider_id=provider_id,
                            system_prompt=system_prompt,
                            prompt=prompt,
                            **request_kwargs,
                        ),
                        timeout=self._compaction_provider_timeout_seconds(),
                    )
                )
            except Exception as exc:
                last_error_kind = exc.__class__.__name__
                record_llm_attempt(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    status="timeout" if isinstance(exc, asyncio.TimeoutError) else "error",
                    elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                    error_kind=last_error_kind,
                    fallback=attempt_index > 0,
                    retry_index=attempt_index,
                )
                logger.debug(f"[{chat_id}] compaction provider {provider_id} failed: {exc}")
                request_kwargs.pop("session_id", None)
                # 失效损坏的 remote session，防止下次复用（D6）
                _lane_mgr = getattr(self.gateway, "lane_manager", None) if self.gateway else None
                if _lane_mgr is not None:
                    try:
                        _lane_key = self._compaction_lane_key(chat_id)
                        _lane_umo = _lane_mgr.resolve_lane_umo("", _lane_key)
                        _lane_mgr.expire_remote_sessions_for_lane(_lane_umo)
                    except Exception as lane_exc:
                        logger.warning(f"[Compaction] lane session expiry degraded: {lane_exc}")
                continue
            content = getattr(response, "completion_text", response)
            rendered = str(content or "").strip()
            if looks_like_provider_failure_text(rendered):
                last_error_kind = "provider_failure_text"
                record_llm_attempt(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    status="invalid_output",
                    elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                    error_kind=last_error_kind,
                    fallback=attempt_index > 0,
                    retry_index=attempt_index,
                )
                logger.warning(f"[{chat_id}] compaction provider {provider_id} returned failure text")
                continue
            if rendered:
                record_llm_attempt(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    status="success",
                    elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                    fallback=attempt_index > 0,
                    retry_index=attempt_index,
                )
                finish_llm_call(
                    None,
                    ledger_call_id,
                    model=provider_id,
                    output=rendered,
                )
                if getattr(self.gateway, "context_economy", None):
                    self.gateway.context_economy.record_trace(WorkloadTrace(**trace_payload))
                return rendered
            last_error_kind = "empty_output"
            record_llm_attempt(
                None,
                ledger_call_id,
                model=provider_id,
                status="invalid_output",
                elapsed_ms=(time.perf_counter() - attempt_started) * 1000,
                error_kind=last_error_kind,
                fallback=attempt_index > 0,
                retry_index=attempt_index,
            )
        finish_llm_call(
            None,
            ledger_call_id,
            status="error",
            error_kind=last_error_kind or "empty_output",
            error=last_error_kind or "empty_output",
        )
        return ""


__all__ = ["CompactionProviderMixin"]
