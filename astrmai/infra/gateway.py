import json
import re
import asyncio
from typing import Dict, Any,List,Union
from astrbot.api import logger
from astrbot.api.star import Context
import contextvars

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

_internal_call_flag = contextvars.ContextVar('astrmai_internal_call', default=False)


class GlobalModelGateway:
    """
    统一模型网关 (重构版：增加弹性熔断与指数退避)
    已彻底接入 Pydantic AstrMaiConfig 对象。
    """
    def __init__(self, context: Context, config: Any):
        import uuid
        self.context = context
        self.config = config
        # 🟢 [深层修复 Bug 3] 生成运行时级唯一高熵防伪凭证，免疫 Prompt 注入
        self.internal_marker = f"__ASTRMAI_INTERNAL_{uuid.uuid4().hex}__"


    # [新增] 统一列表合成器：获取用于当前任务的模型轮询列表
    def get_models_for_task(self, specific_model: str) -> List[str]:
        """优先使用传入的 specific_model，随后附加 fallback_models，去除空值与重复项"""
        models = []
        if specific_model and specific_model.strip():
            models.append(specific_model.strip())
            
        fallback_models = getattr(self.config.provider, 'fallback_models', [])
        if fallback_models:
            models.extend(fallback_models)
            
        # 去重且保持原顺序
        return list(dict.fromkeys([m for m in models if m]))
    
    # [修改] 函数位置：astrmai/infra/gateway.py -> GlobalModelGateway 类下
    async def _elastic_call(self, prompt: str, system_prompt: str, models: List[str], is_json: bool = False, retry_penalty: float = 0.0, image_urls: List[str] = None) -> Union[str, Dict[str, Any]]: 
        """统一网关底层调用引擎 (动态凭证注入防递归版 + 异常快速熔断)"""
        
        modified_system_prompt = f"{system_prompt}\n\n{self.internal_marker}" if system_prompt else self.internal_marker
        
        if not models:
            logger.warning("[AstrMai-Gateway] 🚨 任务执行失败：未配置任何可用模型且无备用池！")
            return {} if is_json else ""
            
        max_retries = self.config.infra.llm_retries
        backoff_factor = self.config.infra.backoff_factor
        last_error = ""
        
        for model_id in models:
            logger.debug(f"[AstrMai-Gateway] 🔄 尝试调用模型: {model_id} (JSON模式: {is_json})")
            for attempt in range(max_retries + 1):
                try:
                    contexts = []
                    if modified_system_prompt:
                        contexts.append(SystemMessageSegment(content=[TextPart(text=modified_system_prompt)]))
                        
                    llm_kwargs = {}
                    current_prompt = prompt
                    if image_urls and len(image_urls) > 0:
                        if ImagePart:
                            user_content = []
                            if current_prompt:
                                user_content.append(TextPart(text=current_prompt))
                            for url in image_urls:
                                if url.startswith("data:image"):
                                    import base64
                                    # 剥离 "data:image/jpeg;base64," 前缀
                                    b64_data = url.split(",", 1)[1]
                                    # 将 Base64 解码为原始 bytes 字节流
                                    img_bytes = base64.b64decode(b64_data)
                                    # 通过 file 参数传递字节流，避免触发本地路径检测
                                    user_content.append(ImagePart(file=img_bytes))
                                else:
                                    user_content.append(ImagePart(url=url))
                                user_content.append(ImagePart(url=url))
                            contexts.append(UserMessageSegment(content=user_content))
                            current_prompt = "" 
                        else:
                            llm_kwargs["image_urls"] = image_urls
                            
                    resp = await self.context.llm_generate(
                        chat_provider_id=model_id,
                        prompt=current_prompt if current_prompt else None,
                        contexts=contexts,
                        **llm_kwargs
                    )
                    
                    content = resp.completion_text
                    if not content or not content.strip():
                        raise ValueError("响应为空")
                        
                    if not is_json:
                        return content.strip() 
                        
                    raw_json_str = self._extract_json(content)
                    try:
                        return json.loads(raw_json_str)
                    except json.JSONDecodeError:
                        # 🟢 [核心修复 Bug 4] 快速熔断防线：如果文本中连 `{` 或 `[` 都没有，说明模型完全脱轨输出纯对话，立即熔断放弃死磕重试
                        if "{" not in raw_json_str and "[" not in raw_json_str:
                            logger.error(f"[AstrMai-Gateway] 🚨 快速熔断：模型 {model_id} 严重幻觉(完全丢失 JSON 结构)，拒绝重试，直接抛弃。")
                            return {}

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
                    logger.warning(f"[AstrMai-Gateway] ⚠️ 模型 {model_id} 失败 (Try {attempt+1}/{max_retries+1}): {e}")
                    if attempt < max_retries:
                        await asyncio.sleep((backoff_factor + retry_penalty) ** attempt) 
            
        logger.error(f"[AstrMai-Gateway] ❌ 所有模型池耗尽，最终异常: {last_error}")
        return {} if is_json else ""
    
    async def call_vision_task(self, image_data: str, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        """
        独立的视觉调用网关。
        不使用全局的 get_models_for_task()，坚决隔离纯语言的 fallback_models，
        确保只将图片发送给具备多模态能力的专属视觉模型，防止模型池污染报错。
        """
        # 读取专属的视觉模型配置 (需确保用户在 ProviderConfig 中配置了 vision_model)
        vision_model = getattr(self.config.provider, 'vision_model', '')
        if not vision_model:
            logger.error("[AstrMai-Gateway] 🚨 视觉任务失败：未配置专属视觉模型 (vision_model)！请在 config.yaml 中配置。")
            return {}

        # 仅投递给专属视觉模型
        models = [vision_model]
        
        # 将传入的 Base64/URL 包装为标准列表格式
        image_urls = [image_data] if image_data else None

        # 完美继承 _elastic_call：
        # 1. 强行开启 is_json=True 确保返回安全字典
        # 2. 视觉任务耗时较长，施加 0.5 的重试退避惩罚
        return await self._elastic_call(
            prompt=prompt, 
            system_prompt=system_prompt, 
            models=models, 
            is_json=True, 
            retry_penalty=0.5, 
            image_urls=image_urls
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
    #[新增] 替换原 call_judge：意图与动作快速判决
    async def call_judge_task(self, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        models = self.get_models_for_task(self.config.provider.judge_model)
        return await self._elastic_call(prompt, system_prompt, models, is_json=True)

    # [新增] 情绪值分析与好感度闭环
    async def call_mood_task(self, prompt: str, system_prompt: str = "") -> Dict[str, Any]:
        models = self.get_models_for_task(self.config.provider.mood_model)
        return await self._elastic_call(prompt, system_prompt, models, is_json=True)

    # [新增] 记忆结构化/群组黑话推断/实时对话目标分析
    async def call_data_process_task(self, prompt: str, system_prompt: str = "", is_json: bool = False) -> Union[str, Dict[str, Any]]:
        models = self.get_models_for_task(self.config.provider.data_process_model)
        # 后台数据处理较慢，带上 0.5 的退避惩罚
        return await self._elastic_call(prompt, system_prompt, models, is_json=is_json, retry_penalty=0.5)

    # [新增] 主动冷场破冰开场白生成/深度用户画像侧写
    async def call_proactive_task(self, prompt: str, system_prompt: str = "") -> str:
        models = self.get_models_for_task(self.config.provider.proactive_model)
        # 替换原 call_planner，带上 0.5 的退避惩罚
        return await self._elastic_call(prompt, system_prompt, models, is_json=False, retry_penalty=0.5)

    # [新增] 人设压缩与多维切片
    async def call_persona_task(self, prompt: str, system_prompt: str = "", is_json: bool = False) -> Union[str, Dict[str, Any]]:
        models = self.get_models_for_task(self.config.provider.persona_model)
        return await self._elastic_call(prompt, system_prompt, models, is_json=is_json)

    # [新增] 提供给 Brain/Executor 的原生智能体模型备用列表
    def get_agent_models(self) -> List[str]:
        return self.get_models_for_task(self.config.provider.agent_model)