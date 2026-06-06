from __future__ import annotations

from typing import Any

from astrbot.api import logger

from ...infrastructure.context_economy import PromptTemplateId, WorkloadFamily
from ...infrastructure.gateway.provider_capabilities import infer_provider_capabilities
from ...infrastructure.runtime.lane_manager import LaneKey


class CompactionProviderMixin:
    """Provider-calling methods extracted from ContextCompactionEngine."""

    async def _resolve_provider_candidates(self, chat_id: str) -> list[str]:
        candidates: list[str] = []
        configured = str(self.provider_id or "").strip()
        if configured:
            candidates.append(configured)
        context = getattr(self.gateway, "context", None) if self.gateway else None
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
        caps = infer_provider_capabilities(provider_id)
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
        ) if hasattr(self, "_render_compaction_envelope") else None
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
        for provider_id in provider_candidates:
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
            try:
                response = await context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    **request_kwargs,
                )
            except Exception as exc:
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
            clipped = self._clip_summary(str(content or "").strip())
            if clipped:
                if getattr(self.gateway, "context_economy", None):
                    self.gateway.context_economy.record_trace(
                        self.gateway.context_economy.build_trace(
                            policy=self.gateway.context_economy.resolve_policy(
                                self.gateway.context_economy.build_request(
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
                            ),
                            lane_umo=str(trace_payload.get("lane_umo", "") or ""),
                            actual_model=provider_id,
                            provider_family=str(trace_payload.get("provider_family", "") or ""),
                            provider_session_id=str(trace_payload.get("provider_session_id", "") or ""),
                            provider_session_enabled=bool(trace_payload.get("provider_session_enabled", False)),
                            provider_cache_hint_enabled=bool(trace_payload.get("provider_cache_hint_enabled", False)),
                        )
                    )
                return clipped
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
        ) if hasattr(self, "_render_compaction_envelope") else None
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
        for provider_id in provider_candidates:
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
            try:
                response = await context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                    **request_kwargs,
                )
            except Exception as exc:
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
            if rendered:
                if getattr(self.gateway, "context_economy", None):
                    self.gateway.context_economy.record_trace(
                        self.gateway.context_economy.build_trace(
                            policy=self.gateway.context_economy.resolve_policy(
                                self.gateway.context_economy.build_request(
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
                            ),
                            lane_umo=str(trace_payload.get("lane_umo", "") or ""),
                            actual_model=provider_id,
                            provider_family=str(trace_payload.get("provider_family", "") or ""),
                            provider_session_id=str(trace_payload.get("provider_session_id", "") or ""),
                            provider_session_enabled=bool(trace_payload.get("provider_session_enabled", False)),
                            provider_cache_hint_enabled=bool(trace_payload.get("provider_cache_hint_enabled", False)),
                        )
                    )
                return rendered
        return ""


__all__ = ["CompactionProviderMixin"]
