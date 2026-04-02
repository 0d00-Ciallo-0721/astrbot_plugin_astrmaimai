import json
import re
import asyncio
from typing import Dict, Any,List,Union
from astrbot.api import logger
from astrbot.api.star import Context
from .model_router import ModelRouter

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
        # 智能模型路由器（健康分 + 冷却隔离 + 轮询均衡）
        self.router = ModelRouter()
        # 全局并发速率限制器
        max_concurrent = getattr(config.infra, 'max_concurrent_llm_calls', 3) if hasattr(config, 'infra') else 3
        self._global_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"[Gateway] 🛡️ 全局速率限制器已启动，最大并发: {max_concurrent}")

    def get_models_for_task(self, pool_name: str, models: List[str]) -> List[str]:
        """兼容接口: 委托给 ModelRouter（保留签名以兼容外部调用）"""
        return self.router.get_ranked_models(pool_name, models)

# [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
# [修改] 位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def _elastic_call(self, pool_name: str, prompt: str, system_prompt: str, models: List[str], is_json: bool = False, retry_penalty: float = 0.0, image_urls: List[str] = None, use_fallback: bool = True) -> Union[str, Dict[str, Any]]: 
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
                        contexts = []
                        llm_kwargs = {}
                        
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
                                contexts.append(UserMessageSegment(content=user_content))
                                current_prompt = "" 
                            else:
                                llm_kwargs["image_urls"] = processed_image_urls
                                
                        # 超时熔断
                        try:
                            resp = await asyncio.wait_for(
                                self.context.llm_generate(
                                    chat_provider_id=model_id,
                                    prompt=current_prompt if current_prompt else None,
                                    contexts=contexts,
                                    **llm_kwargs
                                ),
                                timeout=timeout_limit
                            )
                        except asyncio.TimeoutError:
                            raise TimeoutError(f"网关硬中断：API 响应超时 ({timeout_limit}s)")
                        
                        content = resp.completion_text
                        if not content or not content.strip():
                            raise ValueError("响应为空")
                        
                        # ✅ 调用成功 → 上报健康
                        self.router.report_success(report_pool, model_id)
                            
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
    
    async def call_vision_task(self, image_data: str, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        """多模态视觉任务专用网关"""
        vision_models = getattr(self.config.provider, 'vision_models', [])
        if not vision_models:
            logger.error("[AstrMai-Gateway] 🚨 视觉任务失败：未配置专属视觉模型池 (vision_models)！请在 config.yaml 中配置。")
            return {}
        
        image_urls = [image_data] if image_data else None

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
    async def call_data_process_task(self, prompt: str, system_prompt: str = "", is_json: bool = False) -> Union[str, Dict[str, Any]]:
        """记忆结构化/群组黑话推断/实时对话目标分析"""
        task_models = getattr(self.config.provider, 'task_models', [])
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=is_json, retry_penalty=0.5)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_proactive_task(self, prompt: str, system_prompt: str = "") -> str:
        """主动冷场破冰开场白生成/深度用户画像侧写"""
        task_models = getattr(self.config.provider, 'task_models', [])
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=False, retry_penalty=0.5)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def call_persona_task(self, prompt: str, system_prompt: str = "", is_json: bool = False) -> Union[str, Dict[str, Any]]:
        """人设压缩与多维切片"""
        task_models = getattr(self.config.provider, 'task_models', [])
        return await self._elastic_call("task", prompt, system_prompt, task_models, is_json=is_json)

    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    def get_agent_models(self) -> List[str]:
        """提供给 Brain/Executor 的原生智能体模型备用列表 (按健康度排序)"""
        agent_models = getattr(self.config.provider, 'agent_models', [])
        fallback_models_raw = getattr(self.config.provider, 'fallback_models', [])
        
        primary = self.router.get_ranked_models("agent", agent_models)
        fallback = self.router.get_ranked_models("fallback", fallback_models_raw)
        
        return primary + [m for m in fallback if m not in primary]