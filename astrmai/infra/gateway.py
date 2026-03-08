import json
import re
import asyncio
from typing import Dict, Any,List,Union
from astrbot.api import logger
from astrbot.api.star import Context

try:
    from json_repair import repair_json
except ImportError:
    repair_json = None

class GlobalModelGateway:
    """
    统一模型网关 (重构版：增加弹性熔断与指数退避)
    已彻底接入 Pydantic AstrMaiConfig 对象。
    """
    def __init__(self, context: Context, config: Any):
        self.context = context
        self.config = config

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

    # [新增] 统一网关底层调用引擎 (完美继承原版逻辑 + 弹性模型池)
    async def _elastic_call(self, prompt: str, system_prompt: str, models: List[str], is_json: bool = False, retry_penalty: float = 0.0) -> Union[str, Dict[str, Any]]:
        if not models:
            logger.warning("[AstrMai-Gateway] 🚨 任务执行失败：未配置任何可用模型且无备用池！")
            return {} if is_json else ""
            
        contexts = [{"role": "system", "content": system_prompt}] if system_prompt else []
        
        # 动态读取基础配置 (完美对应原版)
        max_retries = self.config.infra.llm_retries
        backoff_factor = self.config.infra.backoff_factor
        
        last_error = ""
        
        # 二维容错：第一维 (遍历模型池)
        for model_id in models:
            logger.debug(f"[AstrMai-Gateway] 🔄 尝试调用模型: {model_id} (JSON模式: {is_json})")

            # 二维容错：第二维 (单个模型自身的指数退避重试，完美对应原版)
            for attempt in range(max_retries + 1):
                try:
                    resp = await self.context.llm_generate(
                        chat_provider_id=model_id,
                        prompt=prompt,
                        contexts=contexts
                    )
                    
                    content = resp.completion_text
                    if not content or not content.strip():
                        raise ValueError("响应为空")
                        
                    if not is_json:
                        return content.strip() # 纯文本直接返回
                        
                    # 原版 JSON 提取与 JsonRepair 自动修复逻辑 (完美对应原版)
                    raw_json_str = self._extract_json(content)
                    try:
                        return json.loads(raw_json_str)
                    except json.JSONDecodeError:
                        if repair_json:
                            repaired = repair_json(raw_json_str)
                            if repaired:
                                return json.loads(repaired)
                        raise ValueError(f"JSON 损坏且无法修复: {raw_json_str[:50]}...")
                        
                except Exception as e:
                    last_error = str(e)
                    logger.warning(f"[AstrMai-Gateway] ⚠️ 模型 {model_id} 失败 (Try {attempt+1}/{max_retries+1}): {e}")
                    if attempt < max_retries:
                        # 原版的指数退避 + 慢思考惩罚系数
                        await asyncio.sleep((backoff_factor + retry_penalty) ** attempt) 
            
            # 如果走到这里，说明当前模型重试了 max_retries 次全败，循环将继续，自动切换下一个 fallback 模型
                    
        logger.error(f"[AstrMai-Gateway] ❌ 所有模型池耗尽，最终异常: {last_error}")
        return {} if is_json else ""

    def _extract_json(self, text: str) -> str:
        text = text.strip()
        match = re.search(r"```(?:json)?(.*?)```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
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