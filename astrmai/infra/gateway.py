import json
import re
import asyncio
from typing import Dict, Any,List,Union
from astrbot.api import logger
from astrbot.api.star import Context

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
    统一模型网关 (重构版：增加弹性熔断与指数退避)
    已彻底接入 Pydantic AstrMaiConfig 对象。
    """
    def __init__(self, context: Context, config: Any):
        self.context = context
        self.config = config
        # 🟢 [新增] 状态机：初始化所有模型池的轮询指针 (Cursors)
        self._cursors = {
            "fallback": 0,
            "agent": 0,
            "task": 0,
            "vision": 0,
            "embedding": 0
        }
        # 🟢 [新增 3.3] 全局并发速率限制器 (防止后台任务雪崩 429)
        max_concurrent = getattr(config.infra, 'max_concurrent_llm_calls', 3) if hasattr(config, 'infra') else 3
        self._global_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"[Gateway] 🛡️ 全局速率限制器已启动，最大并发: {max_concurrent}")

    def get_models_for_task(self, pool_name: str, models: List[str]) -> List[str]:
        """状态轮询调度算法：获取重排后的模型列表并推进游标 (严格复用原文件函数名)"""
        clean_models = [m.strip() for m in models if m and m.strip()]
        if not clean_models:
            return []
            
        # 去重且保持原配置顺序
        unique_models = list(dict.fromkeys(clean_models))
        
        # 获取当前池的游标，若超限则归零重置
        cursor = self._cursors.get(pool_name, 0)
        if cursor >= len(unique_models):
            cursor = 0
            
        # 数组切片重排，将游标指向的模型放到队列首位
        rearranged = unique_models[cursor:] + unique_models[:cursor]
        
        # 游标步进，为下一次调用做准备
        self._cursors[pool_name] = (cursor + 1) % len(unique_models)
        
        return rearranged

# [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
# [修改] 位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def _elastic_call(self, pool_name: str, prompt: str, system_prompt: str, models: List[str], is_json: bool = False, retry_penalty: float = 0.0, image_urls: List[str] = None, use_fallback: bool = True) -> Union[str, Dict[str, Any]]: 
        """统一网关底层调用引擎 (增加全局信号量限流 + asyncio 硬中断超时防卡死)"""
        
        # 🟢 [3.3] 全局速率门控：超量请求在此排队等待
        async with self._global_semaphore:
            # 1. 尝试主模型池（已根据轮询游标重排）
            primary_models = self.get_models_for_task(pool_name, models)
            attempt_queue = primary_models.copy()
            
            # 2. 如果允许兜底，主池全挂则尝试总模型池（同样根据总池自己的游标重排）
            if use_fallback:
                fallback_models_raw = getattr(self.config.provider, 'fallback_models', [])
                fallback_models = self.get_models_for_task("fallback", fallback_models_raw)
                attempt_queue += [m for m in fallback_models if m not in attempt_queue]

            if not attempt_queue:
                logger.warning(f"[AstrMai-Gateway] 🚨 任务执行失败：未配置任何可用模型且无备用池 (池: {pool_name})！")
                raise LLMCascadeFailureException(f"未配置任何可用模型且无备用池 (池: {pool_name})")
                
            # 🟢 降低重试次数的默认值，从 2 降为 1，减少网络不佳时的死等时间
            max_retries = getattr(self.config.infra, 'llm_retries', 1)
            backoff_factor = getattr(self.config.infra, 'backoff_factor', 1.5)
            
            # 🟢 [核心修复] 网关级绝对超时时间 (如果 config 没配，默认给 15 秒)
            timeout_limit = getattr(self.config.infra, 'api_timeout', 15.0)
            last_error = ""
            
            for model_id in attempt_queue:
                logger.debug(f"[AstrMai-Gateway] 🔄 尝试调用模型: {model_id} (JSON模式: {is_json})")
                for attempt in range(max_retries + 1):
                    try:
                        processed_image_urls = image_urls if image_urls else []

                        # 2. 构建上下文请求
                        contexts = []
                        llm_kwargs = {}
                        
                        # 移除 SystemMessageSegment 的封装，直接作为原生字符串键值对放入字典
                        if system_prompt:
                            llm_kwargs["system_prompt"] = system_prompt
                            
                        current_prompt = prompt
                        
                        if processed_image_urls:
                            if ImagePart:
                                user_content = []
                                if current_prompt:
                                    user_content.append(TextPart(text=current_prompt))
                                for path_or_url in processed_image_urls:
                                    # 直接注入完整的 Data URI 或 URL，底层 Adapter 会接管解析
                                    user_content.append(ImagePart(url=path_or_url))
                                contexts.append(UserMessageSegment(content=user_content))
                                current_prompt = "" 
                            else:
                                llm_kwargs["image_urls"] = processed_image_urls
                                
                        # 🟢 [核心修复] 强制包裹超时熔断器，干掉无限等待的僵尸 API
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
                            
                        if not is_json:
                            return content.strip() 
                            
                        raw_json_str = self._extract_json(content)
                        try:
                            return json.loads(raw_json_str)
                        except json.JSONDecodeError:
                            if "{" not in raw_json_str and "[" not in raw_json_str:
                                logger.error(f"[AstrMai-Gateway] 🚨 快速熔断：模型 {model_id} 严重幻觉，拒绝重试，跳过本模型。")
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
                        
                        # 🟢 [修改] 智能快速熔断 (Smart Circuit Breaker) 加上 timeout
                        fatal_keywords = [
                            "无法解析", 
                            "429", 
                            "ratelimit", 
                            "too many requests", 
                            "invalid_request_error",
                            "apitimeouterror",
                            "request timed out",
                            "timeout"
                        ]
                        
                        if any(kw in error_str for kw in fatal_keywords) or "content=none" in error_str:
                            logger.error(f"[AstrMai-Gateway] 🚨 触发快速熔断：检测到确定性异常、限流或超时，放弃本模型重试，立刻推进轮询队列！异常: {last_error[:100]}...")
                            break

                        logger.warning(f"[AstrMai-Gateway] ⚠️ 模型 {model_id} 失败 (Try {attempt+1}/{max_retries+1}): {e}")
                        if attempt < max_retries:
                            await asyncio.sleep((backoff_factor + retry_penalty) ** attempt)

            logger.error(f"[AstrMai-Gateway] ❌ 所有模型池(主池+兜底池)均已耗尽，最终异常: {last_error}")
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
        """提供给 Brain/Executor 的原生智能体模型备用列表 (带轮询顺序)"""
        agent_models = getattr(self.config.provider, 'agent_models', [])
        fallback_models_raw = getattr(self.config.provider, 'fallback_models', [])
        
        # 替换为调用修复名称后的 get_models_for_task
        primary = self.get_models_for_task("agent", agent_models)
        fallback = self.get_models_for_task("fallback", fallback_models_raw)
        
        return primary + [m for m in fallback if m not in primary]