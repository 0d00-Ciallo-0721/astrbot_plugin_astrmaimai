from typing import Any, Dict, List, Optional, Union

from astrbot.api import logger

from ..context_economy import PromptEnvelope, WorkloadFamily
from ..runtime.lane_manager import LaneKey
from .gateway_exceptions import LLMCascadeFailureException


class GatewayTaskMixin:
    def _normalize_vision_failure_reason(self, result: Any) -> tuple[bool, str]:
        if not isinstance(result, dict) or not result:
            return False, "empty_result"
        description = result.get("description", "")
        if not isinstance(description, str) or not description.strip():
            return False, "empty_description"
        normalized = description.strip().lower()
        if self._classify_failure_kind(description).value == "provider_failure_text":
            return False, "provider_failure_text"
        if normalized in {"none", "null", "unknown image", "unknown"}:
            return False, "empty_description"
        raw_tags = result.get("emotion_tags", [])
        if raw_tags is None:
            return True, ""
        if isinstance(raw_tags, list):
            cleaned_tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
            if raw_tags and not cleaned_tags:
                return False, "invalid_emotion_tags"
            if any(self._classify_failure_kind(tag).value == "provider_failure_text" for tag in cleaned_tags):
                return False, "provider_failure_text"
            return True, ""
        if isinstance(raw_tags, str):
            if raw_tags.strip() and self._classify_failure_kind(raw_tags).value == "provider_failure_text":
                return False, "provider_failure_text"
            return True, ""
        return False, "invalid_emotion_tags"

    async def call_vision_task(
        self,
        image_data: str,
        prompt: str,
        system_prompt: str = "",
        lane_key: Optional[LaneKey] = None,
        base_origin: str = "",
        prefix_hash: str = "",
        persona_id: str = "",
        workload_family: WorkloadFamily = WorkloadFamily.VISION,
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> Dict[str, Any]:
        vision_models = self._vision_models()
        if not vision_models:
            logger.error("[Gateway] vision task requested without vision models configured")
            return {}

        router = getattr(self, "router", None)
        if router is not None:
            vision_models = router.get_ranked_models(
                "vision",
                vision_models,
                cooldown_checker=self._is_model_cooldown,
            )
        image_urls = [image_data] if image_data else None
        attempted_models: List[str] = []
        if lane_key and self.lane_manager:
            last_error = ""
            last_kind = "unknown"
            last_model_id = ""
            attempt_queue, skipped_cooldown_models, cooldown_overridden = self._filter_cooldown_attempt_queue(
                "vision",
                vision_models,
                vision_models,
            )
            for model_id in attempt_queue:
                attempted_models.append(model_id)
                try:
                    result = await self.chat_in_lane_result(
                        lane_key=lane_key,
                        base_origin=base_origin,
                        prompt=prompt,
                        system_prompt=system_prompt,
                        models=[model_id],
                        is_json=True,
                        retry_penalty=0.5,
                        image_urls=image_urls,
                        use_fallback=False,
                        prefix_hash=prefix_hash,
                        persona_id=persona_id,
                        template_envelope=template_envelope,
                        result_validator=self._normalize_vision_failure_reason,
                    )
                    parsed = result.parsed_json or {}
                    is_valid, failure_reason = self._normalize_vision_failure_reason(parsed)
                    if is_valid:
                        return parsed
                    last_error = failure_reason
                    last_kind = self._classify_failure_kind(failure_reason).value
                    last_model_id = model_id
                    self._open_model_cooldown("vision", model_id, failure_reason)
                    logger.warning(f"[Gateway] vision model {model_id} returned unusable result: {failure_reason}")
                except LLMCascadeFailureException as exc:
                    last_error = exc.error_message
                    last_kind = exc.last_failure_kind
                    last_model_id = exc.model_id or model_id
                    self._open_model_cooldown("vision", model_id, f"{exc.error_message} {exc.raw_completion}")
                    logger.warning(f"[Gateway] vision model {model_id} failed, trying next: {exc.error_message}")
                except Exception as exc:
                    last_error = str(exc)
                    last_kind = self._classify_failure_kind(last_error).value
                    last_model_id = model_id
                    self._open_model_cooldown("vision", model_id, last_error)
                    logger.warning(f"[Gateway] vision model {model_id} failed, trying next: {exc}")
            raise LLMCascadeFailureException(
                f"vision model pool exhausted: {last_error}; skipped_cooldown_models={skipped_cooldown_models}; cooldown_overridden={cooldown_overridden}",
                pool_name="vision",
                last_failure_kind=last_kind,
                attempted_models=attempted_models,
                model_id=last_model_id,
                failure_reason=last_error,
            )

        workload_policy = self.context_economy.resolve_policy(
            self.context_economy.build_request(
                family=workload_family,
                pool_name="vision",
                prompt=prompt,
                system_prompt=system_prompt,
                models=vision_models,
                prefix_hash=prefix_hash,
                persona_id=persona_id,
                is_json=True,
                scope_id="global",
                scope_kind="global",
                template_id=template_envelope.template_id if template_envelope else "",
                template_version=template_envelope.template_version if template_envelope else "v1",
                schema_id=template_envelope.schema_id if template_envelope else "",
                stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
                dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
                template_envelope=template_envelope,
            )
        )
        last_error = ""
        last_kind = "unknown"
        last_model_id = ""
        attempt_queue, skipped_cooldown_models, cooldown_overridden = self._filter_cooldown_attempt_queue(
            "vision",
            vision_models,
            vision_models,
        )
        for model_id in attempt_queue:
            attempted_models.append(model_id)
            try:
                result = await self._elastic_call_result(
                    pool_name="vision",
                    prompt=prompt,
                    system_prompt=system_prompt,
                    models=[model_id],
                    is_json=True,
                    retry_penalty=0.5,
                    image_urls=image_urls,
                    use_fallback=False,
                    workload_policy=workload_policy,
                    result_validator=self._normalize_vision_failure_reason,
                    ledger_stage="multimodal.vision",
                    ledger_family=workload_family.value,
                )
                parsed = result.parsed_json or {}
                is_valid, failure_reason = self._normalize_vision_failure_reason(parsed)
                if is_valid:
                    return parsed
                last_error = failure_reason
                last_kind = self._classify_failure_kind(failure_reason).value
                last_model_id = result.model_id or model_id
                self._open_model_cooldown("vision", model_id, failure_reason)
                logger.warning(f"[Gateway] vision model {model_id} returned unusable result: {failure_reason}")
            except LLMCascadeFailureException as exc:
                last_error = exc.error_message
                last_kind = exc.last_failure_kind
                last_model_id = exc.model_id or model_id
                self._open_model_cooldown("vision", model_id, f"{exc.error_message} {exc.raw_completion}")
                logger.warning(f"[Gateway] vision model {model_id} failed, trying next: {exc.error_message}")
            except Exception as exc:
                last_error = str(exc)
                last_kind = self._classify_failure_kind(last_error).value
                last_model_id = model_id
                self._open_model_cooldown("vision", model_id, last_error)
                logger.warning(f"[Gateway] vision model {model_id} failed, trying next: {exc}")
        raise LLMCascadeFailureException(
            f"vision model pool exhausted: {last_error}; skipped_cooldown_models={skipped_cooldown_models}; cooldown_overridden={cooldown_overridden}",
            pool_name="vision",
            last_failure_kind=last_kind,
            attempted_models=attempted_models,
            model_id=last_model_id,
            failure_reason=last_error,
        )

    async def call_judge_task(self, prompt: str, system_prompt: str = "", template_envelope: Optional[PromptEnvelope] = None) -> Dict[str, Any]:
        workload_policy = self.context_economy.resolve_policy(
            self.context_economy.build_request(
                family=WorkloadFamily.JUDGE,
                pool_name="task",
                prompt=prompt,
                system_prompt=system_prompt,
                models=self._task_models(),
                is_json=True,
                scope_id="global",
                scope_kind="global",
                template_id=template_envelope.template_id if template_envelope else "",
                template_version=template_envelope.template_version if template_envelope else "v1",
                schema_id=template_envelope.schema_id if template_envelope else "",
                stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
                dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
                template_envelope=template_envelope,
            )
        )
        result = await self._elastic_call_result(
            "task",
            prompt,
            system_prompt,
            self._task_models(),
            is_json=True,
            workload_policy=workload_policy,
            ledger_stage="attention.judge",
            ledger_family=WorkloadFamily.JUDGE.value,
        )
        return result.parsed_json or {}

    async def call_mood_task(self, prompt: str, system_prompt: str = "", template_envelope: Optional[PromptEnvelope] = None) -> Dict[str, Any]:
        workload_policy = self.context_economy.resolve_policy(
            self.context_economy.build_request(
                family=WorkloadFamily.MOOD,
                pool_name="task",
                prompt=prompt,
                system_prompt=system_prompt,
                models=self._task_models(),
                is_json=True,
                scope_id="global",
                scope_kind="global",
                template_id=template_envelope.template_id if template_envelope else "",
                template_version=template_envelope.template_version if template_envelope else "v1",
                schema_id=template_envelope.schema_id if template_envelope else "",
                stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
                dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
                template_envelope=template_envelope,
            )
        )
        result = await self._elastic_call_result(
            "task",
            prompt,
            system_prompt,
            self._task_models(),
            is_json=True,
            workload_policy=workload_policy,
            ledger_stage="attention.mood",
            ledger_family=WorkloadFamily.MOOD.value,
        )
        return result.parsed_json or {}

    async def call_data_process_task(
        self,
        prompt: str,
        system_prompt: str = "",
        is_json: bool = False,
        lane_key: Optional[LaneKey] = None,
        base_origin: str = "",
        prefix_hash: str = "",
        persona_id: str = "",
        workload_family: Optional[WorkloadFamily] = None,
        template_envelope: Optional[PromptEnvelope] = None,
        allow_global_scope: bool = False,
        timeout_override: Optional[float] = None,
        max_retries_override: Optional[int] = None,
        max_models_override: Optional[int] = None,
        use_fallback: bool = True,
    ) -> Union[str, Dict[str, Any]]:
        task_models = self._task_models()
        resolved_family = workload_family or self.context_economy.infer_workload_family(
            lane_key=lane_key,
            pool_name="task",
            tool_mode=False,
        )
        if lane_key:
            result = await self.chat_in_lane_result(
                lane_key=lane_key,
                base_origin=base_origin,
                prompt=prompt,
                system_prompt=system_prompt,
                models=task_models,
                is_json=is_json,
                retry_penalty=0.5,
                use_fallback=use_fallback,
                prefix_hash=prefix_hash,
                persona_id=persona_id,
                template_envelope=template_envelope,
                timeout_override=timeout_override,
                max_retries_override=max_retries_override,
                max_models_override=max_models_override,
            )
            return result.parsed_json if is_json else result.text
        normalized_origin = str(base_origin or "").strip()
        scope_id = normalized_origin or "global"
        scope_kind = "chat" if normalized_origin and normalized_origin != "global" else "global"
        if scope_id == "global" and not allow_global_scope:
            template_id = template_envelope.template_id if template_envelope else ""
            logger.warning(
                f"[Gateway] data process task using implicit global scope "
                f"family={resolved_family.value} template={template_id or 'unknown'}"
            )
        result = await self._elastic_call_result(
            "task",
            prompt,
            system_prompt,
            task_models,
            is_json=is_json,
            retry_penalty=0.5,
            workload_policy=self.context_economy.resolve_policy(
                self.context_economy.build_request(
                    family=resolved_family,
                    pool_name="task",
                    prompt=prompt,
                    system_prompt=system_prompt,
                    models=task_models,
                    prefix_hash=prefix_hash,
                    persona_id=persona_id,
                    is_json=is_json,
                    scope_id=scope_id,
                    scope_kind=scope_kind,
                    template_id=template_envelope.template_id if template_envelope else "",
                    template_version=template_envelope.template_version if template_envelope else "v1",
                    schema_id=template_envelope.schema_id if template_envelope else "",
                    stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
                    dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
                    template_envelope=template_envelope,
                )
            ),
            ledger_stage=f"data.{resolved_family.value}",
            ledger_family=resolved_family.value,
            timeout_override=timeout_override,
            max_retries_override=max_retries_override,
            max_models_override=max_models_override,
            use_fallback=use_fallback,
        )
        return result.parsed_json if is_json else result.text

    async def call_proactive_task(
        self,
        prompt: str,
        system_prompt: str = "",
        lane_key: Optional[LaneKey] = None,
        base_origin: str = "",
        prefix_hash: str = "",
        persona_id: str = "",
        workload_family: Optional[WorkloadFamily] = None,
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> str:
        task_models = self._task_models()
        resolved_family = workload_family or self.context_economy.infer_workload_family(
            lane_key=lane_key,
            pool_name="task",
            tool_mode=False,
        )
        if lane_key:
            result = await self.chat_in_lane_result(
                lane_key=lane_key,
                base_origin=base_origin,
                prompt=prompt,
                system_prompt=system_prompt,
                models=task_models,
                is_json=False,
                retry_penalty=0.5,
                use_fallback=True,
                prefix_hash=prefix_hash,
                persona_id=persona_id,
                template_envelope=template_envelope,
            )
            return result.text
        result = await self._elastic_call_result(
            "task",
            prompt,
            system_prompt,
            task_models,
            is_json=False,
            retry_penalty=0.5,
            workload_policy=self.context_economy.resolve_policy(
                self.context_economy.build_request(
                    family=resolved_family,
                    pool_name="task",
                    prompt=prompt,
                    system_prompt=system_prompt,
                    models=task_models,
                    prefix_hash=prefix_hash,
                    persona_id=persona_id,
                    is_json=False,
                    scope_id="global",
                    scope_kind="global",
                    template_id=template_envelope.template_id if template_envelope else "",
                    template_version=template_envelope.template_version if template_envelope else "v1",
                    schema_id=template_envelope.schema_id if template_envelope else "",
                    stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
                    dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
                    template_envelope=template_envelope,
                )
            ),
            ledger_stage=f"proactive.{resolved_family.value}",
            ledger_family=resolved_family.value,
            ledger_critical_path=False,
        )
        return result.text

    async def call_persona_task(
        self,
        prompt: str,
        system_prompt: str = "",
        is_json: bool = False,
        lane_key: Optional[LaneKey] = None,
        base_origin: str = "",
        prefix_hash: str = "",
        persona_id: str = "",
        workload_family: WorkloadFamily = WorkloadFamily.PERSONA_SUMMARY,
        template_envelope: Optional[PromptEnvelope] = None,
    ) -> Union[str, Dict[str, Any]]:
        task_models = self._task_models()
        if lane_key:
            result = await self.chat_in_lane_result(
                lane_key=lane_key,
                base_origin=base_origin,
                prompt=prompt,
                system_prompt=system_prompt,
                models=task_models,
                is_json=is_json,
                retry_penalty=0.0,
                use_fallback=True,
                prefix_hash=prefix_hash,
                persona_id=persona_id,
                template_envelope=template_envelope,
            )
            return result.parsed_json if is_json else result.text
        result = await self._elastic_call_result(
            "task",
            prompt,
            system_prompt,
            task_models,
            is_json=is_json,
            workload_policy=self.context_economy.resolve_policy(
                self.context_economy.build_request(
                    family=workload_family,
                    pool_name="task",
                    prompt=prompt,
                    system_prompt=system_prompt,
                    models=task_models,
                    prefix_hash=prefix_hash,
                    persona_id=persona_id,
                    is_json=is_json,
                    scope_id="global",
                    scope_kind="global",
                    template_id=template_envelope.template_id if template_envelope else "",
                    template_version=template_envelope.template_version if template_envelope else "v1",
                    schema_id=template_envelope.schema_id if template_envelope else "",
                    stable_prefix_text=template_envelope.stable_prefix_text if template_envelope else "",
                    dynamic_payload_text=template_envelope.dynamic_payload_text if template_envelope else "",
                    template_envelope=template_envelope,
                )
            ),
            ledger_stage=f"persona.{workload_family.value}",
            ledger_family=workload_family.value,
            ledger_critical_path=False,
        )
        return result.parsed_json if is_json else result.text

    def get_agent_models(self) -> List[str]:
        primary = self.router.get_ranked_models("agent", self._agent_models())
        fallback = self.router.get_ranked_models("fallback", self._fallback_models())
        attempt_queue = primary + [model_id for model_id in fallback if model_id not in primary]
        filtered, skipped, overridden = self._filter_cooldown_attempt_queue("agent", primary, attempt_queue)
        self._last_agent_model_selection = {
            "skipped_cooldown_models": list(skipped),
            "cooldown_overridden": bool(overridden),
        }
        return filtered
