from typing import Any, Dict, List, Optional, Union

from astrbot.api import logger

from ..context_economy import PromptEnvelope, WorkloadFamily
from ..runtime.lane_manager import LaneKey


class GatewayTaskMixin:
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

        image_urls = [image_data] if image_data else None
        if lane_key and self.lane_manager:
            result = await self.chat_in_lane_result(
                lane_key=lane_key,
                base_origin=base_origin,
                prompt=prompt,
                system_prompt=system_prompt,
                models=vision_models,
                is_json=True,
                retry_penalty=0.5,
                image_urls=image_urls,
                use_fallback=False,
                prefix_hash=prefix_hash,
                persona_id=persona_id,
                template_envelope=template_envelope,
            )
            return result.parsed_json or {}

        result = await self._elastic_call_result(
            pool_name="vision",
            prompt=prompt,
            system_prompt=system_prompt,
            models=vision_models,
            is_json=True,
            retry_penalty=0.5,
            image_urls=image_urls,
            use_fallback=False,
            workload_policy=self.context_economy.resolve_policy(
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
            ),
        )
        return result.parsed_json or {}

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
        )
        return result.parsed_json if is_json else result.text

    def get_agent_models(self) -> List[str]:
        primary = self.router.get_ranked_models("agent", self._agent_models())
        fallback = self.router.get_ranked_models("fallback", self._fallback_models())
        return primary + [model_id for model_id in fallback if model_id not in primary]
