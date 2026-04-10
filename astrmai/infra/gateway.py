import json
import re
import asyncio
from typing import Callable
from typing import Dict, Any, List, Union, Optional
from astrbot.api import logger
from astrbot.api.star import Context
from .model_router import ModelRouter
from .lane_manager import LaneKey, LaneManager
from .output_guard import looks_like_provider_failure_text, sanitize_visible_reply_text
from .provider_capabilities import infer_provider_capabilities

# [修改] 引入 AstrBot 标准消息片段类，并添加防崩溃动态导入 (兼容 v4.12 - v4.18+)
from astrbot.core.agent.message import SystemMessageSegment, UserMessageSegment, TextPart
try:
    from astrbot.core.agent.message import ImagePart
except ImportError:
    ImagePart = None  # 降级标志：当前版本不支持 ImagePart，将退回使用 image_urls 传参

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

class LLMCascadeFailureException(Exception):
    """自定义异常：底层模型级联失效（所有模型池均耗尽或超时）"""
    pass

class GlobalModelGateway:
    """
    统一模型网关 (重构版)
    调度决策委托给独立的 ModelRouter，本类专注于 API 协议适配与消息编排。
    """
    def __init__(self, context: Context, config: Any):
        self.context = context
        self.config = config
        self.lane_manager: Optional[LaneManager] = None
        # 智能模型路由器（健康分 + 冷却隔离 + 轮询均衡）
        self.router = ModelRouter()
        # 全局并发速率限制器
        max_concurrent = getattr(config.infra, 'max_concurrent_llm_calls', 3) if hasattr(config, 'infra') else 3
        self._global_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"[Gateway] 🛡️ 全局速率限制器已启动，最大并发: {max_concurrent}")

    def get_models_for_task(self, pool_name: str, models: List[str]) -> List[str]:
        """兼容接口: 委托给 ModelRouter（保留签名以兼容外部调用）"""
        return self.router.get_ranked_models(pool_name, models)

    def set_lane_manager(self, lane_manager: LaneManager) -> None:
        self.lane_manager = lane_manager

    def _read_usage_field(self, usage: Any, *names: str) -> int:
        if usage is None:
            return 0
        for name in names:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
        return 0

    def _extract_usage(self, resp: Any) -> Dict[str, int]:
        usage = getattr(resp, "usage", None)
        input_tokens = self._read_usage_field(usage, "input", "input_tokens", "prompt_tokens")
        input_cached = self._read_usage_field(usage, "input_cached", "cached_tokens")
        output_tokens = self._read_usage_field(usage, "output", "output_tokens", "completion_tokens")
        return {
            "input_tokens": input_tokens,
            "input_cached": input_cached,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def _log_usage(self, pool_name: str, model_id: str, usage: Dict[str, int], debug_meta: Optional[Dict[str, Any]] = None) -> None:
        debug_meta = debug_meta or {}
        input_tokens = usage.get("input_tokens", 0)
        input_cached = usage.get("input_cached", 0)
        cache_rate = (input_cached / input_tokens) if input_tokens else 0.0
        logger.info(
            "[GatewayUsage] pool=%s model=%s provider=%s lane_key=%s conversation_id=%s prefix_hash=%s input_tokens=%s input_cached=%s output_tokens=%s cache_rate=%.4f",
            pool_name,
            model_id,
            debug_meta.get("provider", model_id),
            debug_meta.get("lane_key", ""),
            debug_meta.get("conversation_id", ""),
            debug_meta.get("prefix_hash", ""),
            input_tokens,
            input_cached,
            usage.get("output_tokens", 0),
            cache_rate,
        )

# [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
# [修改] 位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def _elastic_call(self, pool_name: str, prompt: str, system_prompt: str, models: List[str], is_json: bool = False, retry_penalty: float = 0.0, image_urls: List[str] = None, use_fallback: bool = True, contexts: Optional[List[Any]] = None, debug_meta: Optional[Dict[str, Any]] = None, request_kwargs: Optional[Dict[str, Any]] = None, request_kwargs_factory: Optional[Callable[[str], Dict[str, Any]]] = None) -> Union[str, Dict[str, Any]]:
        """统一网关底层调用引擎 (ModelRouter 智能调度 + 全局信号量限流 + 超时熔断)"""
        
        # 全局速率门控
        async with self._global_semaphore:
            # 1. 通过 ModelRouter 获取按健康度排序的主模型队列
            primary_models = self.router.get_ranked_models(pool_name, models)
            attempt_queue = primary_models.copy()
            
            # 2. 兜底池追加
            if use_fallback:
                fallback_models_raw = getattr(self.config.provider, 'fallback_models', [])
                fallback_models = self.router.get_ranked_models("fallback", fallback_models_raw)
                attempt_queue += [m for m in fallback_models if m not in attempt_queue]

            if not attempt_queue:
                logger.warning(f"[Gateway] 🚨 任务执行失败：未配置任何可用模型且无备用池 (池: {pool_name})！")
                raise LLMCascadeFailureException(f"未配置任何可用模型且无备用池 (池: {pool_name})")
                
            max_retries = getattr(self.config.infra, 'llm_retries', 1)
            backoff_factor = getattr(self.config.infra, 'backoff_factor', 1.5)
            timeout_limit = getattr(self.config.infra, 'api_timeout', 15.0)
            last_error = ""
            
            for model_id in attempt_queue:
                # 判断当前模型属于哪个池（用于上报）
                report_pool = pool_name if model_id in primary_models else "fallback"
                
                logger.debug(f"[Gateway] 🔄 尝试模型: {model_id} (池: {report_pool}, JSON: {is_json})")
                for attempt in range(max_retries + 1):
                    try:
                        processed_image_urls = image_urls if image_urls else []
                        request_contexts = list(contexts or [])
                        llm_kwargs = dict(request_kwargs or {})
                        if request_kwargs_factory:
                            dynamic_kwargs = request_kwargs_factory(model_id) or {}
                            llm_kwargs.update(dynamic_kwargs)
                        
                        if system_prompt:
                            llm_kwargs["system_prompt"] = system_prompt
                            
                        current_prompt = prompt
                        
                        if processed_image_urls:
                            if ImagePart:
                                user_content = []
                                if current_prompt:
                                    user_content.append(TextPart(text=current_prompt))
                                for path_or_url in processed_image_urls:
                                    user_content.append(ImagePart(url=path_or_url))
                                request_contexts.append(UserMessageSegment(content=user_content))
                                current_prompt = "" 
                            else:
                                llm_kwargs["image_urls"] = processed_image_urls
                                
                        # 超时熔断
                        try:
                            resp = await asyncio.wait_for(
                                self.context.llm_generate(
                                    chat_provider_id=model_id,
                                    prompt=current_prompt if current_prompt else None,
                                    contexts=request_contexts,
                                    **llm_kwargs
                                ),
                                timeout=timeout_limit
                            )
                        except asyncio.TimeoutError:
                            raise TimeoutError(f"网关硬中断：API 响应超时 ({timeout_limit}s)")
                        
                        content = resp.completion_text
                        if not content or not content.strip():
                            raise ValueError("响应为空")
                        if looks_like_provider_failure_text(content):
                            raise ValueError("模型返回了原始失败载荷")
                        
                        # ✅ 调用成功 → 上报健康
                        self.router.report_success(report_pool, model_id)
                        usage = self._extract_usage(resp)
                        log_meta = dict(debug_meta or {})
                        log_meta["provider"] = infer_provider_capabilities(model_id).provider_family
                        self._log_usage(report_pool, model_id, usage, log_meta)

                        if not is_json:
                            return content.strip() 
                            
                        raw_json_str = self._extract_json(content)
                        try:
                            return json.loads(raw_json_str)
                        except json.JSONDecodeError:
                            if "{" not in raw_json_str and "[" not in raw_json_str:
                                logger.error(f"[Gateway] 🚨 模型 {model_id} 严重幻觉，跳过。")
                                self.router.report_failure(report_pool, model_id, is_fatal=False)
                                break 

                            if repair_json:
                                try: 
                                    repaired = repair_json(raw_json_str, return_objects=False)
                                    if repaired and isinstance(repaired, str):
                                        return json.loads(repaired)
                                    elif repaired and isinstance(repaired, (dict, list)):
                                        return repaired
                                except json.JSONDecodeError:
                                    pass 
                            raise ValueError(f"JSON 损坏且无法修复: {raw_json_str[:50]}...")
                            
                    except Exception as e:
                        last_error = str(e)
                        error_str = last_error.lower()
                        
                        # 致命错误关键词检测
                        fatal_keywords = [
                            "429", "ratelimit", "too many requests", 
                            "invalid_request_error", "apitimeouterror",
                            "request timed out", "timeout"
                        ]
                        
                        is_fatal = any(kw in error_str for kw in fatal_keywords) or "content=none" in error_str
                        
                        # ❌ 调用失败 → 上报故障
                        self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                        
                        if is_fatal:
                            logger.error(f"[Gateway] 🚨 模型 {model_id} 致命错误，冷却隔离并跳过: {last_error[:100]}")
                            break

                        logger.warning(f"[Gateway] ⚠️ 模型 {model_id} 失败 (Try {attempt+1}/{max_retries+1}): {e}")
                        if attempt < max_retries:
                            await asyncio.sleep((backoff_factor + retry_penalty) ** attempt)

            logger.error(f"[Gateway] ❌ 所有模型池均已耗尽，最终异常: {last_error}")
            raise LLMCascadeFailureException(f"所有模型池均已耗尽，发生级联失效。最终异常: {last_error}")
    
    async def call_vision_task(self, image_data: str, prompt: str, system_prompt: str = "", lane_key: Optional[LaneKey] = None, base_origin: str = "", prefix_hash: str = "", persona_id: str = "") -> Dict[str, Any]:
        """多模态视觉任务专用网关"""
        vision_models = getattr(self.config.provider, 'vision_models', [])
        if not vision_models:
            logger.error("[AstrMai-Gateway] 🚨 视觉任务失败：未配置专属视觉模型池 (vision_models)！请在 config.yaml 中配置。")
            return {}
        
        image_urls = [image_data] if image_data else None

        if lane_key and self.lane_manager:
            return await self.chat_in_lane(
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
            )

        return await self._elastic_call(
            pool_name="vision",
            prompt=prompt, 
            system_prompt=system_prompt, 
            models=vision_models, 
            is_json=True, 
            retry_penalty=0.5, 
            image_urls=image_urls,
            use_fallback=False # 隔离总模型池，防止把图片发给不支持视觉的文本 LLM
        )

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        
        # 尝试直接解析整个文本为 JSON
        try:

            json.loads(text)
            return text
        except json.JSONDecodeError:
            pass
        
        # 尝试从 Markdown 代码块中提取 JSON
        match = re.search(r"```(?:json)?(.*?)```", text, re.DOTALL)
        if match:
            extracted = match.group(1).strip()
            try:
                json.loads(extracted)
                return extracted
            except json.JSONDecodeError:
                pass
        
        # 如果上述方法都失败，则返回原始文本
        return text

    async def chat_in_lane(
        self,
        lane_key: LaneKey,
        base_origin: str,
        prompt: str,
        system_prompt: str,
        models: List[str],
        is_json: bool = False,
        retry_penalty: float = 0.0,
        image_urls: List[str] = None,
        use_fallback: bool = True,
        prefix_hash: str = "",
        persona_id: str = "",
        raw_user_text: str = "",
    ) -> Union[str, Dict[str, Any]]:
        if not self.lane_manager:
            return await self._elastic_call(
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
        debug_meta = {
            "lane_key": lane_key.as_log_key(),
            "conversation_id": conversation_id,
            "prefix_hash": prefix_hash,
        }
        def _lane_request_kwargs(actual_model: str) -> Dict[str, Any]:
            capabilities = infer_provider_capabilities(actual_model)
            kwargs: Dict[str, Any] = {}
            if capabilities.supports_remote_session:
                kwargs["session_id"] = self.lane_manager.get_remote_session_id(
                    lane_umo,
                    capabilities.provider_family,
                )
            if capabilities.supports_cache_control:
                kwargs["cache_control"] = {"type": "ephemeral"}
            return kwargs

        result = await self._elastic_call(
            pool_name=lane_key.task_family,
            prompt=prompt,
            system_prompt=system_prompt,
            models=models,
            is_json=is_json,
            retry_penalty=retry_penalty,
            image_urls=image_urls,
            use_fallback=use_fallback,
            contexts=history,
            debug_meta=debug_meta,
            request_kwargs_factory=_lane_request_kwargs,
        )
        assistant_content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if isinstance(assistant_content, str):
            assistant_content = sanitize_visible_reply_text(assistant_content, fallback_text="")
        lane_user_content = raw_user_text or prompt
        if assistant_content:
            await self.lane_manager.append_exchange(
                lane_key=lane_key,
                base_origin=base_origin,
                user_content=lane_user_content,
                assistant_content=assistant_content,
                prefix_hash=prefix_hash,
                model_id=model_hint,
                persona_id=persona_id,
            )
        if getattr(getattr(self.config, "global_settings", None), "debug_mode", False):
            history_tail = []
            if history:
                for item in history[-4:]:
                    if isinstance(item, dict):
                        history_tail.append(str(item.get("role", "")))
            logger.debug(
                f"[Gateway] lane={lane_key.as_log_key()} "
                f"raw_user_text={lane_user_content[:120]!r} "
                f"history_roles_tail={history_tail}"
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
        if not self.lane_manager:
            raise LLMCascadeFailureException("lane manager 未初始化，无法执行 tool_chat_in_lane")

        primary_models = self.router.get_ranked_models(lane_key.task_family, models)
        attempt_queue = primary_models.copy()
        fallback_models_raw = getattr(self.config.provider, 'fallback_models', [])
        fallback_models = self.router.get_ranked_models("fallback", fallback_models_raw)
        attempt_queue += [m for m in fallback_models if m not in attempt_queue]
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
            try:
                tool_kwargs: Dict[str, Any] = {}
                if capabilities.supports_remote_session:
                    tool_kwargs["session_id"] = self.lane_manager.get_remote_session_id(lane_umo, capabilities.provider_family)
                if capabilities.supports_cache_control:
                    tool_kwargs["cache_control"] = {"type": "ephemeral"}
                llm_resp = await asyncio.wait_for(
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
                    timeout=getattr(self.config.infra, 'api_timeout', 15.0),
                )
                reply_text = getattr(llm_resp, "completion_text", "")
                if not reply_text:
                    raise ValueError("回复为空")
                self.router.report_success(report_pool, model_id)
                usage = self._extract_usage(llm_resp)
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
                clean_assistant_text = sanitize_visible_reply_text(reply_text, fallback_text="")
                if clean_assistant_text:
                    await self.lane_manager.append_exchange(
                        lane_key=lane_key,
                        base_origin=base_origin,
                        user_content=raw_user_text or prompt,
                        assistant_content=clean_assistant_text,
                        token_usage=usage.get("total_tokens", 0),
                        prefix_hash=prefix_hash,
                        model_id=model_id,
                        persona_id=persona_id,
                    )
                if getattr(getattr(self.config, "global_settings", None), "debug_mode", False):
                    history_tail = []
                    if history:
                        for item in history[-4:]:
                            if isinstance(item, dict):
                                history_tail.append(str(item.get("role", "")))
                    logger.debug(
                        f"[Gateway] tool-lane={lane_key.as_log_key()} "
                        f"raw_user_text={(raw_user_text or prompt)[:120]!r} "
                        f"history_roles_tail={history_tail}"
                    )
                return reply_text
            except Exception as e:
                last_error = str(e)
                error_str = last_error.lower()
                fatal_keywords = [
                    "429", "ratelimit", "too many requests",
                    "invalid_request_error", "apitimeouterror",
                    "request timed out", "timeout",
                ]
                is_fatal = any(kw in error_str for kw in fatal_keywords) or "content=none" in error_str
                self.router.report_failure(report_pool, model_id, is_fatal=is_fatal)
                if is_fatal:
                    logger.error(f"[Gateway] 模型 {model_id} tool_loop 致命错误，跳过: {last_error[:100]}")
                else:
                    logger.warning(f"[Gateway] tool_loop 模型 {model_id} 失败，切换后备: {e}")
                continue

        raise LLMCascadeFailureException(f"tool_loop 模型池耗尽: {last_error}")
    

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_judge_task(self, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        """意图与动作快速判决"""
        task_models = getattr(self.config.provider, 'task_models', [])
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=True)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_mood_task(self, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        """情绪值分析与好感度闭环"""
        task_models = getattr(self.config.provider, 'task_models', [])
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=True)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_data_process_task(self, prompt: str, system_prompt: str = "", is_json: bool = False, lane_key: Optional[LaneKey] = None, base_origin: str = "", prefix_hash: str = "", persona_id: str = "") -> Union[str, Dict[str, Any]]:
        """记忆结构化/群组黑话推断/实时对话目标分析"""
        task_models = getattr(self.config.provider, 'task_models', [])
        if lane_key:
            return await self.chat_in_lane(
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
            )
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=is_json, retry_penalty=0.5)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_proactive_task(self, prompt: str, system_prompt: str = "", lane_key: Optional[LaneKey] = None, base_origin: str = "", prefix_hash: str = "", persona_id: str = "") -> str:
        """主动冷场破冰开场白生成/深度用户画像侧写"""
        task_models = getattr(self.config.provider, 'task_models', [])
        if lane_key:
            return await self.chat_in_lane(
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
            )
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=False, retry_penalty=0.5)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_persona_task(self, prompt: str, system_prompt: str = "", is_json: bool = False, lane_key: Optional[LaneKey] = None, base_origin: str = "", prefix_hash: str = "", persona_id: str = "") -> Union[str, Dict[str, Any]]:
        """人设压缩与多维切片"""
        task_models = getattr(self.config.provider, 'task_models', [])
        if lane_key:
            return await self.chat_in_lane(
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
            )
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=is_json)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    def get_agent_models(self) -> List[str]:
        """提供给 Brain/Executor 的原生智能体模型备用列表 (按健康度排序)"""
        agent_models = getattr(self.config.provider, 'agent_models', [])
        fallback_models_raw = getattr(self.config.provider, 'fallback_models', [])
        
        primary = self.router.get_ranked_models("agent", agent_models)
        fallback = self.router.get_ranked_models("fallback", fallback_models_raw)
        
        return primary + [m for m in fallback if m not in primary]
