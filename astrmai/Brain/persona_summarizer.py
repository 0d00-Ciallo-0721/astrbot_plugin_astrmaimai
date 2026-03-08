# astrmai/Brain/persona_summarizer.py
import hashlib
import asyncio
import json
from typing import Dict, Any, Tuple
from astrbot.api import logger
from ..infra.persistence import PersistenceManager
from ..infra.gateway import GlobalModelGateway

class PersonaSummarizer:
    """
    人设摘要/压缩管理器 (System 2)
    职责: 将冗长的 System Prompt 压缩为高密度的核心特征与风格指南，减少 Token 消耗。
    """
    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway, config=None):
        self.persistence = persistence
        self.gateway = gateway
        self.config = config if config else gateway.config
        # 加载持久化缓存
        self.cache = self.persistence.load_persona_cache()
        # 运行时任务锁
        self.pending_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _compute_hash(self, text: str) -> str:
        """计算人设内容的 Hash 值，用于缓存 Key"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    async def get_summary(self, original_prompt: str, persona_id: str = "", session_id: str = "global") -> Dict[str, Any]:
        """
        [修改] 基于 Persona ID 获取人设切片。
        :param original_prompt: 原始人设文本（用于首次生成）。
        :param persona_id: 配置中填写的唯一 ID。
        :param session_id: 如果 ID 为空，用 session_id 做兜底（实现千人千面缓存）。
        """
        # 接入 Config 阈值
        summary_threshold = self.config.performance.summary_threshold

        # 1. 确定 Cache Key (ID 优先)
        if persona_id and persona_id.strip():
            cache_key = persona_id.strip()
        else:
            # 如果没填 ID，默认绑定到当前会话 (chat_id)
            cache_key = f"session_{session_id}"

        # 2. 查缓存 (Fast Path) - 只要 ID 命中，直接返回，忽略 Prompt 内容变化
        if cache_key in self.cache:
            return self.cache[cache_key]

        # 3. 缓存未命中（新 ID 或新会话），启动生成流程
        
        # 如果 Prompt 太短或为空，直接返回兜底结构，不进行切片
        # (注意：如果是初次配置 ID 且没填 System Prompt，这里会返回空结构，避免报错)
        if not original_prompt or len(original_prompt) < summary_threshold:
            return {
                "summary": original_prompt,
                "style": "保持原始风格",
                "shards": {},
                "is_full_ready": True,
                "raw": original_prompt,
                "timestamp": __import__("time").time()
            }

        async with self._lock:
            # 双重检查锁
            if cache_key in self.cache:
                return self.cache[cache_key]
                
            logger.info(f"[PersonaSummarizer] 🆕 未找到 ID [{cache_key}] 的缓存，开始构建新的人设切片...")
            
            # [步骤 1] 首先同步调用 Sys1 生成 Summary 和 Style（作为首屏兜底，实现秒开回复）
            summary, style = await self._summarize_remote(original_prompt)
            
            # [步骤 2] 构建并保存初始缓存结构（is_full_ready 初始为 False）
            new_cache_data = {
                "summary": summary,
                "style": style,
                "shards": {},
                "is_full_ready": False,
                "raw": original_prompt,
                "timestamp": __import__("time").time()
            }
            self.cache[cache_key] = new_cache_data
            self.persistence.save_persona_cache(self.cache)
            
            # [步骤 3] 抛出后台任务，异步顺序生成8大维度切片，防止主回复程序卡死
            task = asyncio.create_task(self._generate_all_shards_background(original_prompt, cache_key))
            self.pending_tasks[cache_key] = task
            
            return new_cache_data

# [修改] 替换 call_judge 为 call_persona_task
    async def _summarize_remote(self, original_prompt: str) -> Tuple[str, str]:
        """调用 Sys1 (Judge) 模型进行核心压缩 (作为渐进式加载的底层兜底)"""
        logger.info(f"[PersonaSummarizer] 🔨 正在生成核心人设摘要 (首屏兜底) (Len: {len(original_prompt)})...")
        
        prompt = f"""
你的任务是将以下[原始人设]压缩为极高密度的【基础特征】和【回复风格】，作为AI的临时底层人格。

[原始人设]
{original_prompt}

[压缩要求]
1. **summarized_persona**: 提取最核心的身份、基础性格。必须控制在200字以内。不要写具体经历或复杂关系，只保留最基本的人设骨架。
2. **style_guide**: 提取具体的回复格式要求（如：不加句号、傲娇语气、特殊口癖等）。

请严格按照以下 JSON 格式返回:
{{
    "summarized_persona": "string",
    "style_guide": "string"
}}
"""
        try:
            # [修改点] 使用人设压缩专项接口
            result = await self.gateway.call_persona_task(prompt, system_prompt="你是一个资深的角色扮演设定提取专家。", is_json=True)
            
            if not isinstance(result, dict):
                import re, json
                match = re.search(r'\{.*\}', str(result), re.DOTALL)
                if match:
                    result = json.loads(match.group(0))
                else:
                    result = {}
            
            # 拿到结果，做安全回退
            summary = result.get("summarized_persona", "")
            if not summary:
                summary = original_prompt[:300] + "..." # 防止原始人设过长导致兜底失败
                
            style = result.get("style_guide", "保持自然对话风格")
            return summary, style
            
        except Exception as e:
            logger.warning(f"[PersonaSummarizer] 核心摘要提取失败，触发截断降级: {e}")
            # 降级：如果 LLM 挂了，强行截断原文前 300 字作为兜底，绝对不返回完整的 5000 字
            return original_prompt[:300] + "...", "保持自然对话风格"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_logic_style(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 性格逻辑 (logic_style)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【性格逻辑】维度的切片信息。
[提取要求]
重点关注：角色的内在行为模式、战斗与日常状态的切换逻辑、思考方式。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_speech_style(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 语言风格 (speech_style)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【语言风格】维度的切片信息。
[提取要求]
重点关注：角色的口癖、特殊发声习惯、语调特点、标志性词汇。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_world_view(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 世界观 (world_view)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【世界观】维度的切片信息。
[提取要求]
重点关注：角色所处的社会现象、政治立场、地理位置、常识与阵营设定。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_timeline(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 生平经历 (timeline)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【生平经历】维度的切片信息。
[提取要求]
重点关注：角色过去的关键事件、创伤、童年回忆或重大转折。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_relations(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 人际关系 (relations)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【人际关系】维度的切片信息。
[提取要求]
重点关注：角色对特定设定的亲友、其他角色的称呼、态度和关系。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_skills(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 技能能力 (skills)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【技能能力】维度的切片信息。
[提取要求]
重点关注：角色的战斗方式、生活技能、特殊天赋或魔法能力。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_values(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 价值观 (values)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【价值观】维度的切片信息。
[提取要求]
重点关注：角色的喜好、厌恶、恐惧、面临道德抉择时的倾向。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_secrets(self, original_prompt: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 深层秘密 (secrets)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【深层秘密】维度的切片信息。
[提取要求]
重点关注：角色的黑历史、潜意识深处的恐惧、不可告人的秘密。
只返回提取后的核心设定内容，提炼成高密度的描述，不要有任何多余的开头/结尾问候或解释。
如果人设中完全没有提到相关内容，请回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self.gateway.call_persona_task(prompt, is_json=False)
        except Exception:
            return "无"